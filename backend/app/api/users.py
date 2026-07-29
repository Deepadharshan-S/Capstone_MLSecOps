from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Security
from sqlalchemy.orm import Session

from app.api.permissions import get_current_active_user
from app.db.session import get_db
from app.models.user import User
from app.schemas.user import UserResponse, UserUpdateRole
from app.services import user_service

router = APIRouter(prefix="/users", tags=["users"])


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
    return user_service.get_all_users(db)


@router.get("/audit-logs")
def list_audit_logs(
    db: Session = Depends(get_db),
    admin_user: User = Security(get_current_active_user, scopes=["users:manage"]),
):
    """
    Lists all security audit logs. Admin-only.
    """
    return user_service.get_all_audit_logs(db)


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
    ip_addr = request.client.host if request.client else None
    return user_service.update_user_role(db, user_id, role_update, admin_user, ip_addr)
