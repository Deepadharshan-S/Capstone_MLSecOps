import os
import sys
import uuid
import json
import tempfile
from typing import Optional
from fastapi import HTTPException, status

from app.models.user import User
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
    and executing background local Ray training fallbacks.
    """

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
    ) -> dict:
        """
        Submits a custom training job to Ray by generating RayJobs CRD manifests
        and running a local Ray runner background process fallback.
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

        # 1. Render RayJob manifest using centralized utility
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

        # 2. Submit RayJob CRD to Kubernetes cluster
        k8s_submitted = submit_rayjob_to_k8s(
            rendered_yaml=rendered_yaml,
            job_id=job_id,
            username=user.username,
            mode_description="custom code",
        )

        # 3. Trigger training run locally if Kubernetes submission failed
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
    ) -> dict:
        """
        Submits an automated pipeline training job either via Kubernetes RayJob CRD
        or local fallback process.
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

        # 1. Render RayJob manifest using centralized utility
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

        # 2. Submit RayJob CRD to Kubernetes cluster
        k8s_submitted = submit_rayjob_to_k8s(
            rendered_yaml=rendered_yaml,
            job_id=job_id,
            username=user.username,
            mode_description=f"automated pipeline ({model_type})",
        )

        # 3. Trigger training run locally if Kubernetes submission failed
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
