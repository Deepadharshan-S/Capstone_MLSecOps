from app.services.auth.auth_service import AuthService, auth_service
from app.services.auth.user_service import UserService, user_service
from app.services.auth.exceptions import (
    AuthDomainError,
    WeakPasswordError,
    UserAlreadyExistsError,
    InvalidCredentialsError,
    AccountLockedError,
    InvalidTokenError,
    TokenReuseError,
    UserNotFoundError,
    SelfRoleModificationError,
)

__all__ = [
    "AuthService",
    "auth_service",
    "UserService",
    "user_service",
    "AuthDomainError",
    "WeakPasswordError",
    "UserAlreadyExistsError",
    "InvalidCredentialsError",
    "AccountLockedError",
    "InvalidTokenError",
    "TokenReuseError",
    "UserNotFoundError",
    "SelfRoleModificationError",
]
