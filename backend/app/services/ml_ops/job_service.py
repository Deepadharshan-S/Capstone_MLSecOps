import os
import re
import json
import logging
import tempfile
from uuid import UUID
from datetime import datetime, timezone
from typing import Optional, Union, List, Any
from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.models.training_job import TrainingJob
from app.models.user import User
from app.schemas.ml_ops.training import JobStatusResponse

logger = logging.getLogger("job_service")


def _load_results_file(job_id: str) -> dict:
    """Checks temporary result directories for runtime job metrics."""
    for dir_name in ("sentinelml_results", "mlsecops-training-results"):
        path = os.path.join(tempfile.gettempdir(), dir_name, f"{job_id}.json")
        if os.path.exists(path):
            try:
                with open(path, "r") as f:
                    return json.load(f)
            except Exception as e:
                logger.debug(f"Failed to load results file {path}: {e}")
    return {}


def _save_results_file(job_id: str, metrics: dict) -> None:
    """Persists metrics to disk for subsequent UI queries."""
    for dir_name in ("sentinelml_results", "mlsecops-training-results"):
        results_dir = os.path.join(tempfile.gettempdir(), dir_name)
        try:
            os.makedirs(results_dir, exist_ok=True)
            dst = os.path.join(results_dir, f"{job_id}.json")
            with open(dst, "w") as f:
                json.dump(metrics, f, indent=2, default=str)
        except Exception as e:
            logger.debug(f"Failed to save results file to {dir_name}: {e}")


def _fetch_mlflow_metrics(experiment_name: Optional[str], job_id: str) -> dict:
    """Queries MLflow directly for logged run metrics and evaluation artifacts."""
    try:
        import mlflow
        from app.core.config import settings

        os.environ["AWS_ACCESS_KEY_ID"] = settings.MINIO_ROOT_USER
        os.environ["AWS_SECRET_ACCESS_KEY"] = settings.MINIO_ROOT_PASSWORD
        os.environ["MLFLOW_S3_ENDPOINT_URL"] = settings.MINIO_ENDPOINT
        os.environ["MLFLOW_S3_IGNORE_TLS"] = "true"

        client = mlflow.tracking.MlflowClient(tracking_uri=settings.MLFLOW_TRACKING_URI)
        exp = client.get_experiment_by_name(experiment_name) if experiment_name else None
        exp_ids = [exp.experiment_id] if exp else [e.experiment_id for e in client.search_experiments()]
        runs = client.search_runs(
            experiment_ids=exp_ids,
            filter_string=f"tags.sentinelml.job_id = '{job_id}'",
            max_results=1,
        )
        if not runs:
            return {}
        run = runs[0]
        m = run.data.metrics or {}
        res = {
            "status": "completed",
            "accuracy": m.get("accuracy") if "accuracy" in m else m.get("champion_accuracy"),
            "precision": m.get("precision") if "precision" in m else m.get("champion_precision"),
            "recall": m.get("recall") if "recall" in m else m.get("champion_recall"),
            "f1_score": m.get("f1_score") if "f1_score" in m else m.get("champion_f1_score"),
            "training_duration_seconds": m.get("training_duration_seconds"),
        }

        # Download evaluation artifacts if available
        for artifact_file, key in [
            ("confusion_matrix.json", "confusion_matrix"),
            ("feature_importance.json", "feature_importance"),
            ("roc_curve.json", "roc_curve"),
            ("history.json", "history"),
        ]:
            try:
                p = client.download_artifacts(run.info.run_id, f"evaluation/{artifact_file}")
                with open(p, "r") as af:
                    res[key] = json.load(af)
            except Exception:
                pass

        return res
    except Exception as e:
        logger.debug(f"Failed to fetch MLflow metrics for job {job_id}: {e}")
        return {}


def _fetch_job_logs(job_id: str, rayjob_name: Optional[str] = None, status_val: str = "PENDING") -> Optional[list[dict]]:
    """Retrieves execution logs from Kubernetes head pod or MinIO storage for the given job."""
    try:
        from app.services.dataset.s3_storage_service import S3StorageService
        from app.services.ml_ops.rayjob_service import RayJobService
        from app.services.ml_ops.training_log_service import TrainingLogService

        storage = S3StorageService()
        ray_svc = RayJobService()
        log_svc = TrainingLogService(storage_service=storage, rayjob_service=ray_svc)
        r_name = rayjob_name or f"rayjob-{job_id}"
        logs_res = log_svc.get_logs(job_id=job_id, rayjob_name=r_name, status=status_val)
        content = logs_res.get("logs", "")
        if not content:
            return None
        lines = content.strip().splitlines()
        logs_list = []
        for line in lines[-200:]:
            line_str = line.strip()
            if not line_str:
                continue
            lvl = "error" if "error" in line_str.lower() or "exception" in line_str.lower() else "info"
            logs_list.append({
                "time": datetime.now(timezone.utc).strftime("%H:%M:%S"),
                "level": lvl,
                "msg": line_str,
            })
        return logs_list
    except Exception as e:
        logger.debug(f"Could not fetch logs for job {job_id}: {e}")
        return None


class JobService:
    """
    Tracks and hydrates training job lifecycle, results, metrics, and logs.
    Reconciles with Kubernetes KubeRay CRD state and MLflow metrics.
    """

    def _build_job_response(
        self,
        db: Session,
        job: TrainingJob,
        include_logs: bool = False,
    ) -> JobStatusResponse:
        clean_id = job.job_id.removeprefix("rayjob-")
        raw_status = (job.status or "PENDING").upper()

        # 1. Reconcile with Kubernetes RayJob state if active
        if raw_status in ("PENDING", "RUNNING"):
            try:
                from app.services.ml_ops.rayjob_service import RayJobService

                ray_svc = RayJobService()
                k8s_job = ray_svc.get_rayjob(job.rayjob_name)
                if k8s_job:
                    st = k8s_job.get("status", {})
                    norm = ray_svc.normalize_status(st.get("jobStatus"), st.get("jobDeploymentStatus"))
                    if norm != job.status:
                        job.status = norm
                        if norm in ("SUCCEEDED", "FAILED"):
                            job.completed_at = ray_svc.parse_k8s_time(st.get("endTime")) or datetime.now(timezone.utc)
                            if job.started_at and job.completed_at:
                                job.duration_seconds = round((job.completed_at - job.started_at).total_seconds(), 2)
                            job.error_message = st.get("message") or st.get("reason")
                            try:
                                from app.services.dataset.s3_storage_service import S3StorageService
                                from app.services.ml_ops.training_log_service import TrainingLogService

                                log_svc = TrainingLogService(storage_service=S3StorageService(), rayjob_service=ray_svc)
                                log_svc.archive_pod_logs(clean_id)
                            except Exception as log_arch_err:
                                logger.debug(f"Failed to archive pod logs for {clean_id}: {log_arch_err}")
                        try:
                            db.commit()
                            db.refresh(job)
                            raw_status = (job.status or "PENDING").upper()
                        except Exception:
                            db.rollback()
            except Exception as k8s_err:
                logger.debug(f"Kubernetes rayjob check skipped: {k8s_err}")

        # 2. Check local results file or MLflow if still PENDING/RUNNING
        res_data = _load_results_file(clean_id)
        if not res_data:
            res_data = _fetch_mlflow_metrics(job.experiment_name, clean_id)

        if raw_status in ("PENDING", "RUNNING") and (
            res_data.get("status") in ("completed", "failed")
            or res_data.get("accuracy") is not None
        ):
            new_status = "FAILED" if res_data.get("status") == "failed" else "SUCCEEDED"
            job.status = new_status
            job.completed_at = datetime.now(timezone.utc)
            if job.started_at:
                job.duration_seconds = round((job.completed_at - job.started_at).total_seconds(), 2)
            elif res_data.get("training_duration_seconds"):
                job.duration_seconds = float(res_data["training_duration_seconds"])
            try:
                db.commit()
                db.refresh(job)
                raw_status = new_status
            except Exception:
                db.rollback()

        # 3. Map status and progress for frontend
        if raw_status in ("SUCCEEDED", "SUCCESS"):
            status_str = "completed"
            progress = 100
        elif raw_status in ("FAILED", "ERROR"):
            status_str = "failed"
            progress = 100
        elif raw_status in ("RUNNING", "TRAINING"):
            status_str = "training"
            progress = 50
        elif raw_status in ("PENDING", "QUEUED"):
            status_str = "pending"
            progress = 10
        else:
            status_str = raw_status.lower()
            progress = 0

        # 4. Extract model_type from entrypoint if present
        model_type = None
        if job.entrypoint:
            m = re.search(r"--model_type\s+['\"]?([a-zA-Z0-9_\-]+)['\"]?", job.entrypoint)
            if m:
                model_type = m.group(1)
        if not model_type:
            model_type = job.model_name or "Custom Model"

        # 5. Extract metrics from res_data
        accuracy = res_data.get("accuracy")
        precision_score = res_data.get("precision") or res_data.get("precision_score")
        recall_score = res_data.get("recall") or res_data.get("recall_score")
        f1_score = res_data.get("f1_score")
        training_duration = (
            res_data.get("training_duration")
            or res_data.get("training_duration_seconds")
            or job.duration_seconds
        )
        confusion_matrix = res_data.get("confusion_matrix")
        feature_importance = res_data.get("feature_importance")
        roc_curve = res_data.get("roc_curve")
        history = res_data.get("history")

        logs = None
        if include_logs:
            logs = _fetch_job_logs(clean_id, rayjob_name=job.rayjob_name, status_val=raw_status)

        return JobStatusResponse(
            job_id=clean_id,
            status=status_str,
            progress=progress,
            model_name=job.model_name,
            model_type=model_type,
            dataset_id=job.dataset_id,
            accuracy=float(accuracy) if accuracy is not None else None,
            precision_score=float(precision_score) if precision_score is not None else None,
            recall_score=float(recall_score) if recall_score is not None else None,
            f1_score=float(f1_score) if f1_score is not None else None,
            training_duration=float(training_duration) if training_duration is not None else None,
            confusion_matrix=confusion_matrix if isinstance(confusion_matrix, dict) else None,
            feature_importance=feature_importance if isinstance(feature_importance, list) else None,
            roc_curve=roc_curve if isinstance(roc_curve, list) else None,
            history=history if isinstance(history, list) else None,
            error_message=job.error_message or res_data.get("error"),
            started_at=job.started_at,
            completed_at=job.completed_at,
            logs=logs,
        )

    def get_job(
        self,
        db: Session,
        job_id: str,
        user: Optional[User] = None,
    ) -> JobStatusResponse:
        clean_id = job_id.removeprefix("rayjob-")
        job = (
            db.query(TrainingJob)
            .filter((TrainingJob.job_id == clean_id) | (TrainingJob.rayjob_name == job_id))
            .first()
        )
        if not job:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Job '{job_id}' not found.",
            )

        if user and user.role not in ("admin", "ml_engineer", "viewer") and job.created_by_id != user.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied to job.",
            )

        return self._build_job_response(db, job, include_logs=True)

    def get_user_jobs(
        self,
        db: Session,
        user: Union[User, UUID, str],
        limit: int = 20,
    ) -> list[JobStatusResponse]:
        query = db.query(TrainingJob)

        if isinstance(user, User):
            if user.role not in ("admin", "ml_engineer", "viewer"):
                query = query.filter(TrainingJob.created_by_id == user.id)
        elif isinstance(user, (UUID, str)):
            db_user = db.query(User).filter(User.id == user).first()
            if db_user and db_user.role not in ("admin", "ml_engineer", "viewer"):
                query = query.filter(TrainingJob.created_by_id == db_user.id)
            elif not db_user:
                query = query.filter(TrainingJob.created_by_id == user)

        jobs = query.order_by(TrainingJob.created_at.desc()).limit(limit).all()
        return [self._build_job_response(db, j, include_logs=False) for j in jobs]

    def create_job(
        self,
        db: Session,
        job_id: str,
        user_id: UUID,
        dataset_id: str,
        model_type: str,
        target_column: str,
        model_name: str,
        experiment_name: str,
        user_username: Optional[str] = None,
    ) -> TrainingJob:
        clean_id = job_id.removeprefix("rayjob-")
        if not user_username:
            u = db.query(User).filter(User.id == user_id).first()
            user_username = u.username if u else "unknown"

        job = TrainingJob(
            job_id=clean_id,
            rayjob_name=f"rayjob-{clean_id}",
            dataset_id=dataset_id,
            model_name=model_name,
            experiment_name=experiment_name,
            status="RUNNING",
            created_by_id=user_id,
            created_by_username=user_username,
            started_at=datetime.now(timezone.utc),
            entrypoint=f"--model_type '{model_type}' --target_column '{target_column}'",
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        return job

    def update_progress(
        self,
        db: Session,
        job_id: str,
        progress: int,
        status_val: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> TrainingJob:
        clean_id = job_id.removeprefix("rayjob-")
        job = db.query(TrainingJob).filter(
            (TrainingJob.job_id == clean_id) | (TrainingJob.rayjob_name == job_id)
        ).first()
        if not job:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Job '{job_id}' not found.")

        if status_val:
            s = status_val.lower()
            if s == "completed":
                job.status = "SUCCEEDED"
                job.completed_at = datetime.now(timezone.utc)
            elif s == "failed":
                job.status = "FAILED"
                job.completed_at = datetime.now(timezone.utc)
            else:
                job.status = status_val.upper()

        if error_message:
            job.error_message = error_message

        db.commit()
        db.refresh(job)
        return job

    def complete_job(
        self,
        db: Session,
        job_id: str,
        metrics: dict,
    ) -> TrainingJob:
        clean_id = job_id.removeprefix("rayjob-")
        job = db.query(TrainingJob).filter(
            (TrainingJob.job_id == clean_id) | (TrainingJob.rayjob_name == job_id)
        ).first()
        if not job:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Job '{job_id}' not found.")

        job.status = "SUCCEEDED"
        job.completed_at = datetime.now(timezone.utc)
        if job.started_at:
            job.duration_seconds = round((job.completed_at - job.started_at).total_seconds(), 2)
        elif metrics.get("training_duration_seconds"):
            job.duration_seconds = float(metrics["training_duration_seconds"])

        db.commit()
        db.refresh(job)
        _save_results_file(clean_id, metrics)
        return job

    def fail_job(
        self,
        db: Session,
        job_id: str,
        error_message: str,
    ) -> TrainingJob:
        clean_id = job_id.removeprefix("rayjob-")
        job = db.query(TrainingJob).filter(
            (TrainingJob.job_id == clean_id) | (TrainingJob.rayjob_name == job_id)
        ).first()
        if not job:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Job '{job_id}' not found.")

        job.status = "FAILED"
        job.error_message = error_message
        job.completed_at = datetime.now(timezone.utc)
        if job.started_at:
            job.duration_seconds = round((job.completed_at - job.started_at).total_seconds(), 2)

        db.commit()
        db.refresh(job)
        return job


job_service = JobService()
