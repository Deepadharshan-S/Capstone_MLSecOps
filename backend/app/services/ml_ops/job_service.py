import os
import json
import tempfile
from uuid import UUID
from datetime import datetime, timezone
from typing import Optional
from fastapi import HTTPException, status

from sqlalchemy.orm import Session

from app.models.training_job import TrainingJob


class JobService:
    """Tracks training job lifecycle and results in the application database."""

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
    ) -> TrainingJob:
        job = TrainingJob(
            job_id=job_id,
            user_id=user_id,
            dataset_id=dataset_id,
            model_type=model_type,
            target_column=target_column,
            status="training",
            progress=0,
            model_name=model_name,
            experiment_name=experiment_name,
            started_at=datetime.now(timezone.utc),
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
        job = self.get_job(db, job_id)
        job.progress = min(max(progress, 0), 100)
        if status_val:
            job.status = status_val
        if error_message:
            job.error_message = error_message
        if status_val in ("completed", "failed"):
            job.completed_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(job)
        return job

    def complete_job(
        self,
        db: Session,
        job_id: str,
        metrics: dict,
    ) -> TrainingJob:
        job = self.get_job(db, job_id)
        job.status = "completed"
        job.progress = 100
        job.completed_at = datetime.now(timezone.utc)
        job.accuracy = metrics.get("accuracy")
        job.precision_score = metrics.get("precision")
        job.recall_score = metrics.get("recall")
        job.f1_score = metrics.get("f1_score")
        job.training_duration = metrics.get("training_duration_seconds")
        job.confusion_matrix = metrics.get("confusion_matrix")
        job.feature_importance = metrics.get("feature_importance")
        job.roc_curve = metrics.get("roc_curve")
        job.history = metrics.get("history")
        db.commit()
        db.refresh(job)
        return job

    def fail_job(
        self,
        db: Session,
        job_id: str,
        error_message: str,
    ) -> TrainingJob:
        job = self.get_job(db, job_id)
        job.status = "failed"
        job.error_message = error_message
        job.completed_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(job)
        return job

    def get_job(self, db: Session, job_id: str) -> TrainingJob:
        job = db.query(TrainingJob).filter(TrainingJob.job_id == job_id).first()
        if not job:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Job '{job_id}' not found.",
            )

        # If job is still "training", check for results file from local subprocess
        if job.status == "training":
            results_file = os.path.join(
                os.path.join(tempfile.gettempdir(), "mlsecops-training-results"),
                f"{job_id}.json",
            )
            if os.path.exists(results_file):
                try:
                    with open(results_file, "r") as f:
                        results = json.load(f)
                    if results.get("status") == "completed":
                        self.complete_job(db, job_id, results)
                        # Refresh from DB
                        job = db.query(TrainingJob).filter(TrainingJob.job_id == job_id).first()
                    elif results.get("status") == "failed":
                        self.fail_job(db, job_id, results.get("error", "Training failed"))
                        job = db.query(TrainingJob).filter(TrainingJob.job_id == job_id).first()
                except Exception:
                    pass  # Ignore read errors, return current job state

        return job

    def get_user_jobs(
        self,
        db: Session,
        user_id: UUID,
        limit: int = 20,
    ) -> list[TrainingJob]:
        return (
            db.query(TrainingJob)
            .filter(TrainingJob.user_id == user_id)
            .order_by(TrainingJob.created_at.desc())
            .limit(limit)
            .all()
        )


job_service = JobService()
