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
    """Tracks training job lifecycle and results in the application database.

    NOTE (schema migration): ``training_jobs`` now uses the RayJob schema
    (``rayjob_name``, ``ref``, ``created_by_id`` … — see
    ``app/models/training_job.py``). There are no ``user_id``/``progress``/
    ``accuracy``/… columns any more. All queries and writes below must use
    the new columns; the legacy ``/jobs`` API shape is reconstructed in
    ``app/api/ml_ops.py`` from these rows.
    """

    def create_job(
        self,
        db: Session,
        job_id: str,
        created_by_id: UUID,
        created_by_username: str,
        dataset_id: str,
        ref: str = "main",
        model_name: Optional[str] = None,
        experiment_name: Optional[str] = None,
        epochs: int = 0,
        hyperparameters: Optional[dict] = None,
        entrypoint: Optional[str] = None,
        rayjob_name: Optional[str] = None,
        status: str = "PENDING",
    ) -> TrainingJob:
        job = TrainingJob(
            job_id=job_id,
            rayjob_name=rayjob_name or f"rayjob-{job_id}",
            status=status,
            dataset_id=dataset_id,
            ref=ref,
            model_name=model_name,
            experiment_name=experiment_name,
            epochs=epochs,
            hyperparameters=hyperparameters or {},
            entrypoint=entrypoint,
            created_by_id=created_by_id,
            created_by_username=created_by_username,
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
        # The RayJob schema has no numeric progress column — progress is
        # derived from status at read time. Only status transitions persist.
        job = self.get_job(db, job_id)
        if status_val:
            job.status = status_val
        if error_message:
            job.error_message = error_message
        if status_val in ("SUCCEEDED", "completed", "FAILED", "failed"):
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
        job.status = "SUCCEEDED"
        job.completed_at = datetime.now(timezone.utc)
        # The RayJob schema stores no per-metric columns; persist what is
        # storable and ignore the rest (metrics live in MLflow/experiment
        # tracking, not in this row).
        metrics = metrics or {}
        duration = metrics.get("training_duration_seconds")
        if duration is not None:
            try:
                job.duration_seconds = float(duration)
            except (TypeError, ValueError):
                pass
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
        job.status = "FAILED"
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

        # If job is still active, check for a results file from a local
        # subprocess run before returning the current state.
        if job.status in ("training", "RUNNING", "PENDING"):
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
        # Column is `created_by_id` in the RayJob schema (not `user_id`).
        return (
            db.query(TrainingJob)
            .filter(TrainingJob.created_by_id == user_id)
            .order_by(TrainingJob.created_at.desc())
            .limit(limit)
            .all()
        )


job_service = JobService()
