from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Security, Query
from sqlalchemy.orm import Session
from app.core.rate_limiter import RateLimiter

from app.api.permissions import get_current_active_user
from app.db.session import get_db
from app.models.user import User
from app.schemas.auth import UserResponse, UserUpdateRole, AuditLogResponse
from app.services.dependencies import get_user_service
from app.services.auth import UserService

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/me", response_model=UserResponse)
def read_user_me(current_user: Annotated[User, Depends(get_current_active_user)]):
    """
    Returns the currently authenticated user's profile details.
    """
    return current_user


@router.get("/", response_model=list[UserResponse])
def list_users(
    db: Session = Depends(get_db),
    admin_user: User = Security(get_current_active_user, scopes=["users:manage"]),
    user_service: UserService = Depends(get_user_service),
):
    """
    Lists all registered users in the system. Admin-only.
    """
    return user_service.get_all_users(db)


@router.get("/audit-logs", response_model=list[AuditLogResponse])
def list_audit_logs(
    limit: int = Query(default=100, ge=1, le=1000, description="Max number of audit logs to retrieve"),
    offset: int = Query(default=0, ge=0, description="Number of audit logs to skip"),
    db: Session = Depends(get_db),
    admin_user: User = Security(get_current_active_user, scopes=["users:manage"]),
    user_service: UserService = Depends(get_user_service),
):
    """
    Lists security audit logs with pagination support. Admin-only.
    """
    return user_service.get_audit_logs(db, limit=limit, offset=offset)


@router.put(
    "/{user_id}/role",
    response_model=UserResponse,
    dependencies=[Depends(RateLimiter(times=5, seconds=60))],
)
def update_user_role(
    user_id: UUID,
    role_update: UserUpdateRole,
    request: Request,
    db: Session = Depends(get_db),
    admin_user: User = Security(get_current_active_user, scopes=["users:manage"]),
    user_service: UserService = Depends(get_user_service),
):
    """
    Updates the role of a user. Admin-only. Enforces audit logging of role updates.
    """
    ip_addr = request.client.host if request.client else None
    return user_service.update_user_role(db, user_id, role_update, admin_user, ip_addr)
