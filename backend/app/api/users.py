import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Security, status
from sqlalchemy.orm import Session

from app.api.permissions import get_current_active_user
from app.db.session import get_db
from app.models.user import User
from app.schemas.user import UserResponse, UserUpdateRole

router = APIRouter(prefix="/users", tags=["users"])
logger = logging.getLogger("security_audit")


@router.get("/me", response_model=UserResponse)
def read_user_me(
    current_user: Annotated[User, Depends(get_current_active_user)]
):
    """
    Returns the currently authenticated user's profile details.
    """
    return current_user


@router.get("/", response_model=list[UserResponse])
def list_users(
    db: Session = Depends(get_db),
    admin_user: User = Security(get_current_active_user, scopes=["users:manage"]),
):
    """
    Lists all registered users in the system. Admin-only.
    """
    users = db.query(User).all()
    return users


@router.get("/audit-logs")
def list_audit_logs(
    db: Session = Depends(get_db),
    admin_user: User = Security(get_current_active_user, scopes=["users:manage"]),
):
    """
    Lists all security audit logs. Admin-only.
    """
    from app.models.audit_log import AuditLog
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


@router.put("/{user_id}/role", response_model=UserResponse)
def update_user_role(
    user_id: UUID,
    role_update: UserUpdateRole,
    request: Request,
    db: Session = Depends(get_db),
    admin_user: User = Security(get_current_active_user, scopes=["users:manage"]),
):
    """
    Updates the role of a user. Admin-only. Enforces audit logging of role updates.
    """
    # Prevent admin from changing their own role (optional but good security design to prevent accidental lockout)
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

    from app.api.auth import log_audit_event
    ip_addr = request.client.host if request.client else None

    old_role = user.role
    user.role = role_update.role
    db.commit()
    db.refresh(user)

    log_audit_event(
        db,
        "role_update",
        user.username,
        ip_addr,
        f"Admin '{admin_user.username}' changed role from '{old_role}' to '{user.role}'."
    )
    return user
