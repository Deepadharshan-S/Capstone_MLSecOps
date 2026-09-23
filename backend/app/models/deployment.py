from typing import Optional
from uuid import UUID
from sqlalchemy import String, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel


class Deployment(BaseModel):
    """
    Tracks model deployments in PostgreSQL, decoupling deployment lifecycle
    from MLflow tags and providing persistent storage for RayService deployments.
    """
    __tablename__ = "deployments"

    deployment_id: Mapped[str] = mapped_column(
        String(100),
        unique=True,
        nullable=False,
        index=True,
    )
    model_name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        index=True,
    )
    version: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )
    environment: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="staging",
    )
    rayservice_name: Mapped[Optional[str]] = mapped_column(
        String(100),
        nullable=True,
    )
    endpoint_url: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="deploying",
        index=True,
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

    created_by = relationship("User", backref="deployments")
