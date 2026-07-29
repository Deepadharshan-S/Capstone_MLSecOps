from typing import Optional
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.models.user import User
from app.schemas.user import UserUpdateRole
from app.core.logging_config import log_audit_event, get_all_audit_logs


class UserService:
    """
    Service class managing user retrieval, role assignment transactions,
    and security audit log ingestion from rotating file streams.
    """

    def get_user_by_id(self, db: Session, user_id: UUID) -> Optional[User]:
        """Retrieve a user by their unique UUID identifier."""
        return db.query(User).filter(User.id == user_id).first()

    def get_all_users(self, db: Session) -> list[User]:
        """Retrieve all users in the system."""
        return db.query(User).all()

    def get_all_audit_logs(self, db: Session) -> list[dict]:
        """Retrieve all security audit logs parsed from the rotated log file."""
        return get_all_audit_logs()

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
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Admins cannot change their own roles.",
            )

        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found.",
            )

        old_role = user.role
        try:
            user.role = role_update.role
            db.commit()
            db.refresh(user)
        except Exception:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="An error occurred while updating the user role.",
            )

        log_audit_event(
            "role_update",
            user.username,
            ip_address,
            f"Admin '{admin_user.username}' changed role from '{old_role}' to '{user.role}'.",
        )
        return user


user_service = UserService()
