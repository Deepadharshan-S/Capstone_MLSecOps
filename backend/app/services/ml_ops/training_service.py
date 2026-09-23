import os
import sys
import uuid
import json
import tempfile
from datetime import datetime
from typing import Optional
from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.models.user import User
from app.models.training_job import TrainingJob
from app.core.logging_config import log_audit_event
from app.core.config import settings
from app.services.ml_ops.utils import (
    get_scoped_training_credentials,
    render_rayjob_manifest,
    submit_rayjob_to_k8s,
    spawn_local_ray_subprocess,
)


class ModelTrainingService:
    """
    Handles model training jobs, generating Kubernetes RayJob CRD manifests,
    executing background local Ray training fallbacks, and managing permanent
    training job lifecycle tracking in PostgreSQL and MinIO.
    """

    def _get_k8s_apis(self):
        """Returns initialized Kubernetes CustomObjectsApi and CoreV1Api instances."""
        from kubernetes import client, config
        try:
            config.load_incluster_config()
        except Exception:
            config.load_kube_config()
        return client.CustomObjectsApi(), client.CoreV1Api()

    @staticmethod
    def normalize_status(job_status: Optional[str], dep_status: Optional[str]) -> str:
        """
        Normalizes KubeRay status fields into one of the 4 API statuses:
        PENDING, RUNNING, SUCCEEDED, FAILED.
        """
        from app.services.ml_ops.rayjob_service import RayJobService
        return RayJobService.normalize_status(job_status, dep_status)

    def _record_job(
        self,
        db: Optional[Session],
        job_id: str,
        rayjob_name: str,
        dataset_id: str,
        ref: str,
        model_name: Optional[str],
        experiment_name: Optional[str],
        epochs: int,
        hyperparameters: dict,
        entrypoint: str,
        user: User,
    ) -> None:
        """Persists the initial TrainingJob record via TrainingJobRepository."""
        from app.repositories import TrainingJobRepository
        should_close_db = False
        if db is None:
            from app.db.session import SessionLocal
            db_conn = SessionLocal()
            should_close_db = True
        else:
            db_conn = db

        try:
            repo = TrainingJobRepository(db_conn)
            repo.create(TrainingJob(
                job_id=job_id,
                rayjob_name=rayjob_name,
                status="PENDING",
                dataset_id=dataset_id,
                ref=ref,
                model_name=model_name,
                experiment_name=experiment_name,
                epochs=epochs,
                hyperparameters=hyperparameters,
                entrypoint=entrypoint,
                created_by_id=getattr(user, "id", None) or uuid.uuid4(),
                created_by_username=getattr(user, "username", "unknown"),
                log_path=f"logs/{job_id}/training.log",
            ))
        except Exception as db_err:
            print(f"Notice: Could not persist TrainingJob record to DB ({db_err}).")
        finally:
            if should_close_db:
                db_conn.close()

    @staticmethod
    def _parse_iso(val: Optional[str]) -> Optional[datetime]:
        """Parses an ISO format timestamp string into a timezone-aware datetime."""
        if not val:
            return None
        try:
            return datetime.fromisoformat(val.replace("Z", "+00:00"))
        except Exception:
            return None

    def _archive_pod_logs_if_needed(self, core_api, storage_service, job: TrainingJob) -> None:
        """Archives head pod logs to MinIO if not already archived."""
        key = f"logs/{job.job_id}/training.log"
        existing = storage_service.get_log_content("mlflow", key)
        if existing is not None and len(existing.strip()) > 0:
            return

        try:
            pods = core_api.list_namespaced_pod(
                namespace="default",
                label_selector=f"ray.io/job-id={job.job_id}",
            )
            if pods.items:
                pod_name = pods.items[0].metadata.name
                logs = core_api.read_namespaced_pod_log(
                    name=pod_name,
                    namespace="default",
                    container="ray-head",
                )
                if logs:
                    storage_service.put_log_content("mlflow", key, logs)
        except Exception as e:
            print(f"Notice: Could not archive pod logs for {job.job_id} ({e}).")

    def _check_model_in_mlflow(self, model_name: Optional[str], job_id: str) -> bool:
        """Checks if a model was successfully registered or logged in MLflow for the given job_id."""
        if not model_name:
            return False
        try:
            from mlflow.tracking import MlflowClient
            client = MlflowClient(tracking_uri=settings.MLFLOW_TRACKING_URI)
            model_versions = client.search_model_versions(f"name = '{model_name}'")
            for mv in model_versions:
                tags = mv.tags or {}
                if tags.get("mlsecops.job_id") == job_id or tags.get("job_id") == job_id:
                    return True
        except Exception:
            pass
        return False

    def perform_model_training(
        self,
        dataset_id: str,
        ref: str,
        epochs: int,
        hyperparameters: dict,
        code: str,
        user: User,
        experiment_name: Optional[str] = None,
        model_name: Optional[str] = None,
        db: Optional[Session] = None,
    ) -> dict:
        """
        Submits a custom training job to Ray by generating RayJobs CRD manifests
        and running a local Ray runner background process fallback.
        Persists the initial PENDING job record in PostgreSQL.
        """
        job_id = uuid.uuid4().hex[:12]
        experiment_name = experiment_name or f"dataset-{dataset_id}-experiment"
        output_model_name = model_name.strip() if (model_name and model_name.strip()) else f"{dataset_id}-model"

        if not code or not code.strip():
            log_audit_event(
                "model_training_error",
                user.username,
                None,
                f"Attempted training on dataset '{dataset_id}' with empty code.",
            )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Custom training code cannot be empty. Please provide Python training code containing your trainer class.",
            )

        entrypoint_cmd = (
            f"python /app/user_code/ray_wrapper.py --dataset_id {dataset_id} --ref {ref} "
            f"--epochs {epochs} --hyperparameters '{json.dumps(hyperparameters)}' "
            f"--code_file /app/user_code/user_code.py --output_model_name '{output_model_name}' "
            f"--job_dir /tmp/rayjob-{job_id} --experiment_name '{experiment_name}' "
            f"--user '{user.username}' --job_id '{job_id}'"
        )

        scoped_creds = get_scoped_training_credentials(job_id)
        if scoped_creds["scoped_sts"]:
            log_audit_event(
                "training_credentials_scoped",
                user.username,
                None,
                f"Generated temporary MinIO STS session token for RayJob {job_id} scoped to s3://mlflow/*",
            )

        # 1. Create persistent PostgreSQL record via repository
        self._record_job(
            db=db,
            job_id=job_id,
            rayjob_name=f"rayjob-{job_id}",
            dataset_id=dataset_id,
            ref=ref,
            model_name=output_model_name,
            experiment_name=experiment_name,
            epochs=epochs,
            hyperparameters=hyperparameters,
            entrypoint=entrypoint_cmd,
            user=user,
        )

        # 2. Render RayJob manifest using centralized utility
        rendered_yaml = render_rayjob_manifest(
            job_id=job_id,
            entrypoint_cmd=entrypoint_cmd,
            user_code=code,
            dataset_id=dataset_id,
            ref=ref,
            hyperparameters=hyperparameters,
            experiment_name=experiment_name,
            scoped_creds=scoped_creds,
            epochs=epochs,
        )

        # 3. Submit RayJob CRD to Kubernetes cluster
        k8s_submitted = submit_rayjob_to_k8s(
            rendered_yaml=rendered_yaml,
            job_id=job_id,
            username=user.username,
            mode_description="custom code",
        )

        # 4. Trigger training run locally if Kubernetes submission failed
        if not k8s_submitted:
            if not settings.ALLOW_LOCAL_RAY_FALLBACK:
                log_audit_event(
                    "model_training_rejected",
                    user.username,
                    None,
                    f"Kubernetes cluster offline and local fallback execution is disabled for security (Job: {job_id}).",
                )
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Training cluster is unavailable and local fallback execution is disabled for security.",
                )

            temp_job_dir = tempfile.mkdtemp(prefix=f"rayjob-{job_id}-")
            code_file = os.path.join(temp_job_dir, "user_code.py")
            with open(code_file, "w") as f:
                f.write(code)

            ray_wrapper_script = os.path.abspath(os.path.join(os.path.dirname(__file__), "ray_wrapper.py"))
            cmd = [
                sys.executable,
                ray_wrapper_script,
                "--dataset_id",
                dataset_id,
                "--ref",
                ref,
                "--epochs",
                str(epochs),
                "--hyperparameters",
                json.dumps(hyperparameters),
                "--code_file",
                code_file,
                "--output_model_name",
                output_model_name,
                "--job_dir",
                temp_job_dir,
                "--experiment_name",
                experiment_name,
                "--user",
                user.username,
                "--job_id",
                job_id,
            ]

            spawn_local_ray_subprocess(
                cmd=cmd,
                temp_job_dir=temp_job_dir,
                scoped_creds=scoped_creds,
                username=user.username,
                job_id=job_id,
                success_description=f"Successfully trained custom model for dataset '{dataset_id}' (Job: {job_id}).",
                error_description="Error in Ray training background process",
            )

        log_audit_event(
            "model_training_start",
            user.username,
            None,
            f"Started training on dataset '{dataset_id}' for {epochs} epochs. Job ID: {job_id}",
        )

        return {
            "message": "Training job started successfully.",
            "job_id": job_id,
            "dataset_id": dataset_id,
            "epochs": epochs,
            "started_by": user.username,
            "status": "training",
            "model_name": output_model_name,
        }

    def perform_pipeline_training(
        self,
        dataset_id: str,
        ref: str,
        target_column: str,
        model_type: str,
        hyperparameters: dict,
        user: User,
        experiment_name: Optional[str] = None,
        model_name: Optional[str] = None,
        db: Optional[Session] = None,
    ) -> dict:
        """
        Submits an automated pipeline training job either via Kubernetes RayJob CRD
        or local fallback process.
        Persists the initial PENDING job record in PostgreSQL.
        """
        if user.role == "viewer":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Role 'viewer' is not authorized to train models.",
            )

        job_id = uuid.uuid4().hex[:12]
        experiment_name = experiment_name or f"dataset-{dataset_id}-experiment"
        output_model_name = model_name.strip() if (model_name and model_name.strip()) else f"{dataset_id}-model"
        log_audit_event(
            "model_training_initiated",
            user.username,
            None,
            f"Initiated automated pipeline training ({model_type}) on dataset '{dataset_id}' at ref '{ref}' (Job ID: {job_id}).",
        )

        entrypoint_cmd = (
            f"python /app/user_code/ray_wrapper.py --dataset_id {dataset_id} --ref {ref} "
            f"--pipeline_mode --target_column '{target_column}' --model_type '{model_type}' "
            f"--hyperparameters '{json.dumps(hyperparameters)}' --output_model_name '{output_model_name}' "
            f"--job_dir /tmp/rayjob-{job_id} --experiment_name '{experiment_name}' "
            f"--user '{user.username}' --job_id '{job_id}'"
        )

        scoped_creds = get_scoped_training_credentials(job_id)
        if scoped_creds["scoped_sts"]:
            log_audit_event(
                "training_credentials_scoped",
                user.username,
                None,
                f"Generated temporary MinIO STS session token for automated pipeline RayJob {job_id} scoped to s3://mlflow/*",
            )

        # 1. Create persistent PostgreSQL record via repository
        self._record_job(
            db=db,
            job_id=job_id,
            rayjob_name=f"rayjob-{job_id}",
            dataset_id=dataset_id,
            ref=ref,
            model_name=output_model_name,
            experiment_name=experiment_name,
            epochs=0,
            hyperparameters=hyperparameters,
            entrypoint=entrypoint_cmd,
            user=user,
        )

        # 2. Render RayJob manifest using centralized utility
        rendered_yaml = render_rayjob_manifest(
            job_id=job_id,
            entrypoint_cmd=entrypoint_cmd,
            user_code="",
            dataset_id=dataset_id,
            ref=ref,
            hyperparameters=hyperparameters,
            experiment_name=experiment_name,
            scoped_creds=scoped_creds,
            target_column=target_column,
            model_type=model_type,
        )

        # 3. Submit RayJob CRD to Kubernetes cluster
        k8s_submitted = submit_rayjob_to_k8s(
            rendered_yaml=rendered_yaml,
            job_id=job_id,
            username=user.username,
            mode_description=f"automated pipeline ({model_type})",
        )

        # 4. Trigger training run locally if Kubernetes submission failed
        if not k8s_submitted:
            if not settings.ALLOW_LOCAL_RAY_FALLBACK:
                log_audit_event(
                    "model_training_rejected",
                    user.username,
                    None,
                    f"Kubernetes cluster offline and local fallback execution is disabled for security (Job: {job_id}).",
                )
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Training cluster is unavailable and local fallback execution is disabled for security.",
                )

            temp_job_dir = tempfile.mkdtemp(prefix=f"rayjob-{job_id}-")
            ray_wrapper_script = os.path.abspath(os.path.join(os.path.dirname(__file__), "ray_wrapper.py"))
            cmd = [
                sys.executable,
                ray_wrapper_script,
                "--dataset_id",
                dataset_id,
                "--ref",
                ref,
                "--pipeline_mode",
                "--target_column",
                target_column,
                "--model_type",
                model_type,
                "--hyperparameters",
                json.dumps(hyperparameters),
                "--output_model_name",
                output_model_name,
                "--job_dir",
                temp_job_dir,
                "--experiment_name",
                experiment_name,
                "--user",
                user.username,
                "--job_id",
                job_id,
            ]

            spawn_local_ray_subprocess(
                cmd=cmd,
                temp_job_dir=temp_job_dir,
                scoped_creds=scoped_creds,
                username=user.username,
                job_id=job_id,
                success_description=f"Successfully trained automated pipeline model ({model_type}) for dataset '{dataset_id}' (Job: {job_id}).",
                error_description="Error in automated pipeline Ray training background process",
            )

        log_audit_event(
            "model_training_start",
            user.username,
            None,
            f"Started automated pipeline training ({model_type}) on dataset '{dataset_id}'. Job ID: {job_id}",
        )

        return {
            "message": f"Automated pipeline training job ({model_type}) started successfully.",
            "job_id": job_id,
            "dataset_id": dataset_id,
            "epochs": 0,
            "started_by": user.username,
            "status": "training",
            "model_name": output_model_name,
        }

    def _get_job_service(self, db: Session):
        """Builds a TrainingJobService instance with standard dependencies."""
        from app.repositories import TrainingJobRepository
        from app.services.ml_ops.rayjob_service import RayJobService
        from app.services.ml_ops.training_log_service import TrainingLogService
        from app.services.ml_ops.training_job_service import TrainingJobService
        from app.services.dataset.s3_storage_service import S3StorageService

        repo = TrainingJobRepository(db)
        ray_svc = RayJobService()
        log_svc = TrainingLogService(S3StorageService(), ray_svc)
        return TrainingJobService(
            repository=repo,
            rayjob_service=ray_svc,
            log_service=log_svc,
        )

    def reconcile_active_jobs(self, db: Session) -> None:
        """Delegates reconciliation to TrainingJobService."""
        self._get_job_service(db).reconcile_active_jobs()

    def retrieve_training_jobs(self, db: Session, user: User, limit: int = 100) -> dict:
        """Delegates job listing to TrainingJobService."""
        return self._get_job_service(db).list_jobs(user=user, limit=limit)

    def retrieve_training_job_detail(self, db: Session, job_id: str, user: User) -> dict:
        """Delegates job detail retrieval to TrainingJobService."""
        from app.services.ml_ops.exceptions import JobNotFoundError, JobAccessDeniedError
        try:
            return self._get_job_service(db).get_job(job_id=job_id, user=user)
        except JobNotFoundError as e:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
        except JobAccessDeniedError as e:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))

    def retrieve_training_job_logs(
        self, db: Session, job_id: str, user: User, tail_lines: Optional[int] = 1000
    ) -> dict:
        """Delegates log retrieval to TrainingJobService."""
        from app.services.ml_ops.exceptions import JobNotFoundError, JobAccessDeniedError
        try:
            return self._get_job_service(db).get_job_logs(
                job_id=job_id, user=user, tail_lines=tail_lines
            )
        except JobNotFoundError as e:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
        except JobAccessDeniedError as e:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))

