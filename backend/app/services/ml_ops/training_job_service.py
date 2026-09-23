from datetime import datetime, timezone
from typing import Optional
from app.models.training_job import TrainingJob
from app.models.user import User
from app.repositories.training_job_repository import TrainingJobRepository
from app.services.ml_ops.rayjob_service import RayJobService
from app.services.ml_ops.training_log_service import TrainingLogService
from app.services.ml_ops.exceptions import JobNotFoundError, JobAccessDeniedError
from app.core.logging_config import log_audit_event


class TrainingJobService:
    """
    Coordinates training job business logic, batch status reconciliation,
    ownership enforcement, and observability.
    Stateless and free of raw SQL or raw Kubernetes API calls.
    """

    def __init__(
        self,
        repository: TrainingJobRepository,
        rayjob_service: RayJobService,
        log_service: TrainingLogService,
    ) -> None:
        self.repository = repository
        self.rayjob_service = rayjob_service
        self.log_service = log_service

    def list_jobs(self, user: User, limit: int = 100) -> dict:
        """
        Lists all training jobs, reconciling active jobs against live Kubernetes state.
        Enforces role-based ownership: admins, ML engineers, and viewers see all jobs;
        data scientists see only their own.
        """
        self.reconcile_active_jobs()

        if user.role in ("admin", "ml_engineer", "viewer"):
            jobs = self.repository.list(limit=limit)
        else:
            jobs = self.repository.list(user_id=user.id, limit=limit)

        log_audit_event(
            "training_jobs_view",
            user.username,
            None,
            f"Retrieved {len(jobs)} training jobs list.",
        )

        return {
            "jobs": [
                {
                    "job_id": j.job_id,
                    "rayjob_name": j.rayjob_name,
                    "status": j.status,
                    "dataset_id": j.dataset_id,
                    "ref": j.ref,
                    "model_name": j.model_name,
                    "experiment_name": j.experiment_name,
                    "epochs": j.epochs,
                    "started_by": j.created_by_username,
                    "created_at": j.created_at.isoformat() if j.created_at else "",
                    "started_at": j.started_at.isoformat() if j.started_at else None,
                    "completed_at": j.completed_at.isoformat() if j.completed_at else None,
                    "duration_seconds": j.duration_seconds,
                    "error_message": j.error_message,
                }
                for j in jobs
            ],
            "total": len(jobs),
        }

    def get_job(self, job_id: str, user: User) -> dict:
        """
        Retrieves detailed information for a specific training job.
        For active jobs, blends PostgreSQL metadata with live Kubernetes execution state.
        For completed jobs, retrieves the permanent record from PostgreSQL.
        """
        clean_id = job_id.removeprefix("rayjob-")
        job = self.repository.get_by_job_id(clean_id)
        if not job:
            raise JobNotFoundError(job_id)

        if job.created_by_id != user.id and user.role not in ("admin", "ml_engineer", "viewer"):
            raise JobAccessDeniedError()

        head_pod_name = None
        head_pod_status = None
        k8s_status = None

        if job.status in ("PENDING", "RUNNING"):
            k8s_job = self.rayjob_service.get_rayjob(job.rayjob_name)
            if k8s_job:
                st = k8s_job.get("status", {})
                norm = self.rayjob_service.normalize_status(
                    st.get("jobStatus"), st.get("jobDeploymentStatus")
                )
                k8s_status = norm

                if norm != job.status:
                    job.status = norm
                    if norm in ("SUCCEEDED", "FAILED"):
                        job.completed_at = self.rayjob_service.parse_k8s_time(
                            st.get("endTime")
                        ) or datetime.now(timezone.utc)
                        if job.started_at and job.completed_at:
                            job.duration_seconds = round(
                                (job.completed_at - job.started_at).total_seconds(), 2
                            )
                        job.error_message = st.get("message") or st.get("reason")
                        self.log_service.archive_pod_logs(job.job_id)
                    self.repository.save(job)

            head_pod = self.rayjob_service.get_head_pod(job.job_id)
            if head_pod:
                head_pod_name, head_pod_status = head_pod

        log_audit_event(
            "training_job_detail_view",
            user.username,
            None,
            f"Viewed details for training job {job.job_id}.",
        )

        return {
            "job_id": job.job_id,
            "rayjob_name": job.rayjob_name,
            "status": job.status,
            "dataset_id": job.dataset_id,
            "ref": job.ref,
            "model_name": job.model_name,
            "experiment_name": job.experiment_name,
            "epochs": job.epochs,
            "hyperparameters": job.hyperparameters or {},
            "entrypoint": job.entrypoint,
            "started_by": job.created_by_username,
            "created_at": job.created_at.isoformat() if job.created_at else "",
            "started_at": job.started_at.isoformat() if job.started_at else None,
            "completed_at": job.completed_at.isoformat() if job.completed_at else None,
            "duration_seconds": job.duration_seconds,
            "error_message": job.error_message,
            "log_path": job.log_path,
            "head_pod_name": head_pod_name,
            "head_pod_status": head_pod_status,
            "k8s_status": k8s_status or job.status,
        }

    def get_job_logs(
        self,
        job_id: str,
        user: User,
        tail_lines: Optional[int] = 1000,
    ) -> dict:
        """
        Retrieves logs for a specific training job via TrainingLogService.
        Enforces role ownership before accessing logs.
        """
        clean_id = job_id.removeprefix("rayjob-")
        job = self.repository.get_by_job_id(clean_id)
        if not job:
            raise JobNotFoundError(job_id)

        if job.created_by_id != user.id and user.role not in ("admin", "ml_engineer", "viewer"):
            raise JobAccessDeniedError()

        return self.log_service.get_logs(
            job_id=job.job_id,
            rayjob_name=job.rayjob_name,
            status=job.status,
            tail_lines=tail_lines,
            error_message=job.error_message,
        )

    def reconcile_active_jobs(self) -> None:
        """
        Reconciles active jobs (PENDING, RUNNING) in PostgreSQL against live Kubernetes
        RayJobs in a single batch query to avoid N+1 queries.
        Stateless and updates permanent database records upon job completion.
        """
        active_jobs = self.repository.list_active()
        if not active_jobs:
            return

        k8s_items_list = self.rayjob_service.list_rayjobs()
        k8s_items = {
            item.get("metadata", {}).get("name"): item for item in k8s_items_list
        }
        now = datetime.now(timezone.utc)

        for job in active_jobs:
            k8s_job = k8s_items.get(job.rayjob_name)
            if k8s_job:
                status_obj = k8s_job.get("status", {})
                job_status = status_obj.get("jobStatus")
                dep_status = status_obj.get("jobDeploymentStatus")
                norm_status = self.rayjob_service.normalize_status(job_status, dep_status)

                start_str = status_obj.get("startTime")
                if start_str and not job.started_at:
                    job.started_at = self.rayjob_service.parse_k8s_time(start_str)

                if norm_status == "RUNNING" and job.status != "RUNNING":
                    job.status = "RUNNING"
                    if not job.started_at:
                        job.started_at = now

                if norm_status in ("SUCCEEDED", "FAILED"):
                    job.status = norm_status
                    end_str = status_obj.get("endTime")
                    job.completed_at = self.rayjob_service.parse_k8s_time(end_str) or now
                    if job.started_at and job.completed_at:
                        job.duration_seconds = round(
                            (job.completed_at - job.started_at).total_seconds(), 2
                        )
                    job.error_message = status_obj.get("message") or status_obj.get("reason")
                    self.log_service.archive_pod_logs(job.job_id)
                    self.rayjob_service.cleanup_job_resources(job.job_id)
            else:
                # The RayJob is no longer in Kubernetes (cleaned up by TTL or deleted)
                created_at_val = job.created_at
                if created_at_val:
                    if created_at_val.tzinfo is None:
                        created_at_val = created_at_val.replace(tzinfo=timezone.utc)
                    age_seconds = (now - created_at_val).total_seconds()
                else:
                    age_seconds = 999

                # 120s grace period for newly created PENDING jobs
                if job.status == "PENDING" and age_seconds < 120:
                    continue

                if self.log_service.has_archived_logs(job.job_id):
                    job.status = "SUCCEEDED"
                else:
                    if self._check_model_in_mlflow(job.model_name, job.job_id):
                        job.status = "SUCCEEDED"
                    else:
                        job.status = "FAILED"
                        job.error_message = "RayJob terminated and was cleaned up from Kubernetes cluster."

                job.completed_at = job.completed_at or now
                if job.started_at and job.completed_at:
                    job.duration_seconds = round(
                        (job.completed_at - job.started_at).total_seconds(), 2
                    )
                self.rayjob_service.cleanup_job_resources(job.job_id)

        self.repository.save_all(active_jobs)

    def record_training_job(
        self,
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
    ) -> TrainingJob:
        """Helper to create and persist a new TrainingJob record."""
        job = TrainingJob(
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
            created_by_id=user.id,
            created_by_username=user.username,
            log_path=f"logs/{job_id}/training.log",
        )
        return self.repository.create(job)

    def _check_model_in_mlflow(self, model_name: Optional[str], job_id: str) -> bool:
        """Fallback check against MLflow runs if K8s object is gone."""
        if not model_name:
            return False
        try:
            import mlflow
            from app.core.config import settings
            mlflow.set_tracking_uri(settings.MLFLOW_TRACKING_URI)
            runs = mlflow.search_runs(
                filter_string=f"tags.job_id = '{job_id}'",
                max_results=1,
            )
            return not runs.empty
        except Exception:
            return False
