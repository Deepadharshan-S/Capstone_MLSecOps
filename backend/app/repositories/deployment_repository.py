from typing import Optional, Sequence
from sqlalchemy.orm import Session
from sqlalchemy import select, update, delete

from app.models.deployment import Deployment


class DeploymentRepository:
    """
    Encapsulates all database persistence operations for model deployments.
    Decouples deployment tracking from MLflow tags and provides clean CRUD abstraction.
    """

    def __init__(self, db: Session):
        self.db = db

    def create(self, deployment: Deployment) -> Deployment:
        """Persists a new Deployment entity into PostgreSQL."""
        self.db.add(deployment)
        self.db.commit()
        self.db.refresh(deployment)
        return deployment

    def get_by_deployment_id(self, deployment_id: str) -> Optional[Deployment]:
        """Fetches a deployment record by its unique deployment_id."""
        stmt = select(Deployment).where(Deployment.deployment_id == deployment_id)
        return self.db.execute(stmt).scalar_one_or_none()

    def get_by_rayservice_name(self, rayservice_name: str) -> Optional[Deployment]:
        """Fetches a deployment record by its Kubernetes RayService name."""
        stmt = select(Deployment).where(Deployment.rayservice_name == rayservice_name)
        return self.db.execute(stmt).scalar_one_or_none()

    def get_by_model_name(self, model_name: str) -> Sequence[Deployment]:
        """Retrieves all deployment records for a given model name."""
        stmt = (
            select(Deployment)
            .where(Deployment.model_name == model_name)
            .order_by(Deployment.created_at.desc())
        )
        return self.db.execute(stmt).scalars().all()

    def list(
        self,
        status: Optional[str] = None,
        environment: Optional[str] = None,
    ) -> Sequence[Deployment]:
        """Lists deployments with optional status and environment filtering."""
        stmt = select(Deployment).order_by(Deployment.created_at.desc())

        if status:
            stmt = stmt.where(Deployment.status.ilike(status))
        if environment:
            stmt = stmt.where(Deployment.environment.ilike(environment))

        return self.db.execute(stmt).scalars().all()

    def save(self, deployment: Deployment) -> Deployment:
        """Flushes and commits changes to an existing Deployment entity."""
        self.db.add(deployment)
        self.db.commit()
        self.db.refresh(deployment)
        return deployment

    def delete_by_deployment_id(self, deployment_id: str) -> bool:
        """Deletes a deployment record by its deployment_id."""
        stmt = delete(Deployment).where(Deployment.deployment_id == deployment_id)
        res = self.db.execute(stmt)
        self.db.commit()
        return (res.rowcount or 0) > 0
