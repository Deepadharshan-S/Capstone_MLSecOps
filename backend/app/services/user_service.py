import logging
from typing import Optional
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.models.user import User
from app.models.audit_log import AuditLog
from app.schemas.user import UserUpdateRole
from app.services.auth_service import log_audit_event


def get_user_by_id(db: Session, user_id: UUID) -> Optional[User]:
    """Retrieve a user by their unique UUID identifier."""
    return db.query(User).filter(User.id == user_id).first()


def get_all_users(db: Session) -> list[User]:
    """Retrieve all users in the system."""
    return db.query(User).all()


def get_all_audit_logs(db: Session) -> list[dict]:
    """Retrieve all security audit logs, sorted chronologically descending."""
    logs = db.query(AuditLog).order_by(AuditLog.created_at.desc()).all()
    return [
        {
            "id": log.id,
            "action": log.action,
            "username": log.username,
            "ip_address": log.ip_address,
            "details": log.details,
            "timestamp": log.created_at,
        }
        for log in logs
    ]


def update_user_role(
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
        db,
        "role_update",
        user.username,
        ip_address,
        f"Admin '{admin_user.username}' changed role from '{old_role}' to '{user.role}'.",
    )
    return user
