from __future__ import annotations
from typing import Optional
from uuid import UUID
from sqlalchemy.orm import Session
from app.models.training_job import TrainingJob


class TrainingJobRepository:
    """
    Dedicated data access layer for TrainingJob records in PostgreSQL.
    Pure repository: no FastAPI, HTTP, Kubernetes, or MinIO dependencies.
    """

    def __init__(self, db: Session) -> None:
        self.db = db

    def create(self, job: TrainingJob) -> TrainingJob:
        """Persist a new TrainingJob record."""
        self.db.add(job)
        self.db.commit()
        self.db.refresh(job)
        return job

    def get_by_job_id(self, job_id: str) -> Optional[TrainingJob]:
        """
        Find a training job by job_id or rayjob_name.
        Handles both stripped UUIDs and rayjob- prefixed identifiers.
        """
        clean_id = job_id.removeprefix("rayjob-")
        return (
            self.db.query(TrainingJob)
            .filter((TrainingJob.job_id == clean_id) | (TrainingJob.rayjob_name == job_id))
            .first()
        )

    def list(self, user_id: Optional[UUID] = None, limit: int = 100) -> list[TrainingJob]:
        """
        Retrieve training jobs ordered by creation time descending.
        Optionally filtered by user_id for owner-isolation.
        """
        query = self.db.query(TrainingJob)
        if user_id is not None:
            query = query.filter(TrainingJob.created_by_id == user_id)
        return query.order_by(TrainingJob.created_at.desc()).limit(limit).all()

    def list_active(self) -> list[TrainingJob]:
        """Retrieve all currently active jobs (PENDING, RUNNING) for reconciliation."""
        return (
            self.db.query(TrainingJob)
            .filter(TrainingJob.status.in_(["PENDING", "RUNNING"]))
            .all()
        )

    def save(self, job: TrainingJob) -> TrainingJob:
        """Commit updates on an existing training job."""
        self.db.add(job)
        self.db.commit()
        self.db.refresh(job)
        return job

    def save_all(self, jobs: list[TrainingJob]) -> None:
        """Commit updates on multiple training jobs in a single transaction."""
        for job in jobs:
            self.db.add(job)
        self.db.commit()

    def delete_by_job_id(self, job_id: str) -> bool:
        """Delete a training job record by ID (primarily for test cleanup)."""
        clean_id = job_id.removeprefix("rayjob-")
        deleted = (
            self.db.query(TrainingJob)
            .filter((TrainingJob.job_id == clean_id) | (TrainingJob.rayjob_name == job_id))
            .delete(synchronize_session=False)
        )
        self.db.commit()
        return deleted > 0
