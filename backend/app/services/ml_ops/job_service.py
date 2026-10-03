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
    """Fallback: queries MLflow directly for logged run metrics if results file is missing."""
    if not experiment_name:
        return {}
    try:
        import mlflow
        from app.core.config import settings

        client = mlflow.tracking.MlflowClient(tracking_uri=settings.MLFLOW_TRACKING_URI)
        exp = client.get_experiment_by_name(experiment_name)
        if not exp:
            return {}
        runs = client.search_runs(
            experiment_ids=[exp.experiment_id],
            filter_string=f"tags.sentinelml.job_id = '{job_id}'",
            max_results=1,
        )
        if not runs:
            return {}
        run = runs[0]
        m = run.data.metrics or {}
        return {
            "accuracy": m.get("accuracy") if "accuracy" in m else m.get("champion_accuracy"),
            "precision": m.get("precision") if "precision" in m else m.get("champion_precision"),
            "recall": m.get("recall") if "recall" in m else m.get("champion_recall"),
            "f1_score": m.get("f1_score") if "f1_score" in m else m.get("champion_f1_score"),
            "training_duration_seconds": m.get("training_duration_seconds"),
        }
    except Exception as e:
        logger.debug(f"Failed to fetch MLflow metrics for job {job_id}: {e}")
        return {}


def _fetch_job_logs(job_id: str) -> Optional[list[dict]]:
    """Retrieves execution logs from MinIO storage for the given job."""
    try:
        from app.services.dataset.s3_storage_service import S3StorageService

        s3 = S3StorageService()
        content = s3.get_log_content("mlflow", f"logs/{job_id}/training.log")
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
    Interfaces cleanly with the canonical PostgreSQL TrainingJob model.
    """

    def _build_job_response(
        self,
        db: Session,
        job: TrainingJob,
        include_logs: bool = False,
    ) -> JobStatusResponse:
        clean_id = job.job_id.removeprefix("rayjob-")

        # 1. Normalize status and map progress
        raw_status = (job.status or "PENDING").upper()
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

        # 2. Extract model_type from entrypoint if present
        model_type = None
        if job.entrypoint:
            m = re.search(r"--model_type\s+['\"]?([a-zA-Z0-9_\-]+)['\"]?", job.entrypoint)
            if m:
                model_type = m.group(1)
        if not model_type:
            model_type = job.model_name or "Custom Model"

        # 3. Read results file or fallback to MLflow
        res_data = _load_results_file(clean_id)
        if not res_data and status_str == "completed" and job.experiment_name:
            res_data = _fetch_mlflow_metrics(job.experiment_name, clean_id)

        # 4. Reconcile DB if local background process finished
        if status_str in ("pending", "training") and res_data.get("status") in ("completed", "failed"):
            new_status = "SUCCEEDED" if res_data.get("status") == "completed" else "FAILED"
            job.status = new_status
            job.completed_at = datetime.now(timezone.utc)
            if job.started_at:
                job.duration_seconds = round((job.completed_at - job.started_at).total_seconds(), 2)
            try:
                db.commit()
                db.refresh(job)
            except Exception:
                db.rollback()
            status_str = "completed" if new_status == "SUCCEEDED" else "failed"
            progress = 100

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
            logs = _fetch_job_logs(clean_id)

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
