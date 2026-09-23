from typing import Sequence
from sqlalchemy.orm import Session
from sqlalchemy import select, func

from app.models.audit_log import AuditLog


class AuditLogRepository:
    """
    Encapsulates all database persistence and retrieval operations for security audit logs.
    """

    def __init__(self, db: Session):
        self.db = db

    def create(self, audit_log: AuditLog) -> AuditLog:
        """Persists a new security audit log into PostgreSQL."""
        self.db.add(audit_log)
        self.db.commit()
        self.db.refresh(audit_log)
        return audit_log

    def list(self, limit: int = 100, offset: int = 0) -> Sequence[AuditLog]:
        """Fetches paginated audit logs ordered chronologically descending."""
        stmt = (
            select(AuditLog)
            .order_by(AuditLog.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        return self.db.execute(stmt).scalars().all()

    def count(self) -> int:
        """Returns the total count of security audit logs in the database."""
        stmt = select(func.count(AuditLog.id))
        return self.db.execute(stmt).scalar() or 0
