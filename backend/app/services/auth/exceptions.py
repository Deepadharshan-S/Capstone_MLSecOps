from __future__ import annotations


class AuthDomainError(Exception):
    """Base exception for authentication and authorization domain errors."""

    def __init__(self, detail: str = "Authentication error occurred.", status_code: int = 500) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


class WeakPasswordError(AuthDomainError):
    """Raised when password does not satisfy complexity requirements."""

    def __init__(self, detail: str = "Password does not meet complexity requirements.") -> None:
        super().__init__(detail=detail, status_code=400)


class UserAlreadyExistsError(AuthDomainError):
    """Raised when username or email is already taken."""

    def __init__(self, detail: str = "Username or email is unavailable.") -> None:
        super().__init__(detail=detail, status_code=400)


class InvalidCredentialsError(AuthDomainError):
    """Raised when credentials do not match or user is not found."""

    def __init__(self, detail: str = "Incorrect username or password.") -> None:
        super().__init__(detail=detail, status_code=400)


class AccountLockedError(AuthDomainError):
    """Raised when account is temporarily locked due to repeated failed login attempts."""

    def __init__(self, detail: str = "Account is temporarily locked. Please try again later.") -> None:
        super().__init__(detail=detail, status_code=403)


class InvalidTokenError(AuthDomainError):
    """Raised when refresh or access token is missing, invalid, or expired."""

    def __init__(self, detail: str = "Invalid or expired token.") -> None:
        super().__init__(detail=detail, status_code=401)


class TokenReuseError(AuthDomainError):
    """Raised when an already revoked refresh token is re-submitted (session compromise)."""

    def __init__(self, detail: str = "Security warning: Session compromised. Please log in again.") -> None:
        super().__init__(detail=detail, status_code=401)


class UserNotFoundError(AuthDomainError):
    """Raised when target user is not found."""

    def __init__(self, detail: str = "User not found.") -> None:
        super().__init__(detail=detail, status_code=404)


class SelfRoleModificationError(AuthDomainError):
    """Raised when an administrator attempts to modify their own role."""

    def __init__(self, detail: str = "Admins cannot change their own roles.") -> None:
        super().__init__(detail=detail, status_code=400)
