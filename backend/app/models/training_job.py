from datetime import datetime
from typing import Optional
from uuid import UUID
from sqlalchemy import String, Integer, Float, Text, ForeignKey, DateTime, JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel


class TrainingJob(BaseModel):
    __tablename__ = "training_jobs"

    job_id: Mapped[str] = mapped_column(
        String(50),
        unique=True,
        nullable=False,
        index=True,
    )
    rayjob_name: Mapped[str] = mapped_column(
        String(100),
        unique=True,
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="PENDING",
        index=True,
    )
    dataset_id: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )
    ref: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        default="main",
    )
    model_name: Mapped[Optional[str]] = mapped_column(
        String(100),
        nullable=True,
    )
    experiment_name: Mapped[Optional[str]] = mapped_column(
        String(100),
        nullable=True,
    )
    epochs: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )
    hyperparameters: Mapped[Optional[dict]] = mapped_column(
        JSON,
        nullable=True,
        default=dict,
    )
    entrypoint: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    duration_seconds: Mapped[Optional[float]] = mapped_column(
        Float,
        nullable=True,
    )
    error_message: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
    )
    log_path: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
    )
    created_by_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_by_username: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )

    created_by: Mapped["User"] = relationship("User", backref="training_jobs")  # noqa: F821
