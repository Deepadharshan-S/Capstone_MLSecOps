from __future__ import annotations
from typing import Optional, List
from uuid import UUID

from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app.models.user import User
from app.repositories.user_repository import UserRepository
from app.schemas.auth import UserUpdateRole
from app.core.logging_config import log_audit_event, get_all_audit_logs
from app.services.auth.exceptions import (
    UserNotFoundError,
    SelfRoleModificationError,
    AuthDomainError,
)


class UserService:
    """
    Service class managing user retrieval, role assignment transactions,
    and security audit log ingestion. Uses UserRepository for data access.
    """

    def __init__(self, repository: Optional[UserRepository] = None) -> None:
        self.repository = repository

    def _get_repo(self, db: Session) -> UserRepository:
        if self.repository is not None and self.repository.db == db:
            return self.repository
        return UserRepository(db)

    def get_user_by_id(self, db: Session, user_id: UUID) -> Optional[User]:
        """Retrieve a user by their unique UUID identifier."""
        return self._get_repo(db).get_by_id(user_id)

    def get_all_users(self, db: Session, limit: int = 100, offset: int = 0) -> List[User]:
        """Retrieve paginated users in the system."""
        return self._get_repo(db).list(limit=limit, offset=offset)

    def get_all_audit_logs(self, db: Session) -> List[dict]:
        """Retrieve all security audit logs with authoritative PostgreSQL persistence."""
        return get_all_audit_logs(db=db, limit=100, offset=0)

    def get_audit_logs(self, db: Session, limit: int = 100, offset: int = 0) -> List[dict]:
        """Retrieve paginated security audit logs."""
        return get_all_audit_logs(db=db, limit=limit, offset=offset)

    def update_user_role(
        self,
        db: Session,
        user_id: UUID,
        role_update: UserUpdateRole,
        admin_user: User,
        ip_address: Optional[str] = None,
    ) -> User:
        """Updates the role of a user and logs the security audit event inside transaction scope."""
        # Prevent self-privilege modification to guard against accidental lockout/privilege changes
        if admin_user.id == user_id:
            raise SelfRoleModificationError("Admins cannot change their own roles.")

        repo = self._get_repo(db)
        user = repo.get_by_id(user_id)
        if not user:
            raise UserNotFoundError("User not found.")

        old_role = user.role
        try:
            user.role = role_update.role
            repo.save(user)
        except IntegrityError:
            db.rollback()
            raise AuthDomainError(
                detail="Database integrity constraint violation during role update.",
                status_code=400,
            )
        except Exception:
            db.rollback()
            raise AuthDomainError(
                detail="An error occurred while updating the user role.",
                status_code=500,
            )

        log_audit_event(
            "role_update",
            user.username,
            ip_address,
            f"Admin '{admin_user.username}' changed role from '{old_role}' to '{user.role}'.",
            db=db,
        )
        return user


user_service = UserService()
