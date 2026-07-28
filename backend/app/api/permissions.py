from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer, SecurityScopes
from sqlalchemy.orm import Session

from app.core.roles import Role, ROLE_PERMISSIONS
from app.core.security import decode_token
from app.db.session import get_db
from app.models.user import User

# Define the OAuth2 security scheme with all available permissions/scopes
oauth2_scheme = OAuth2PasswordBearer(
    tokenUrl="/api/auth/login",
    scopes={
        "datasets:upload": "Upload new datasets to the MLSecOps platform",
        "models:train": "Start and monitor training jobs",
        "models:view": "View details of trained ML models",
        "models:deploy": "Deploy trained models to target environments",
        "deployments:manage": "Manage running model deployments (restart, rollback, stop)",
        "users:manage": "Manage user accounts, roles, and view security audit logs"
    }
)


def get_current_user(
    security_scopes: SecurityScopes,
    token: Annotated[str, Depends(oauth2_scheme)],
    db: Annotated[Session, Depends(get_db)],
) -> User:
    """
    Decodes the JWT token, verifies the signature/expiry, checks blacklist,
    and enforces role-based scope access matching the requested security scopes.
    """
    if security_scopes.scopes:
        authenticate_value = f'Bearer scope="{security_scopes.scope_str}"'
    else:
        authenticate_value = "Bearer"

    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": authenticate_value},
    )
    
    payload = decode_token(token)
    if payload is None:
        raise credentials_exception
        
    user_id_str: str = payload.get("sub")
    token_type: str = payload.get("type")
    jti = payload.get("jti")
    
    if user_id_str is None or token_type != "access":
        raise credentials_exception
        
    # Check if access token is blacklisted
    if jti:
        from app.models.blacklisted_token import BlacklistedToken
        is_blacklisted = db.query(BlacklistedToken).filter(BlacklistedToken.jti == jti).first()
        if is_blacklisted:
            raise credentials_exception
        
    try:
        user_id = UUID(user_id_str)
    except ValueError:
        raise credentials_exception
        
    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
            headers={"WWW-Authenticate": authenticate_value},
        )
        
    # Enforce Role-Based Access Control (RBAC) scopes
    try:
        user_role = Role(user.role)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User has an invalid or unrecognized role",
            headers={"WWW-Authenticate": authenticate_value},
        )
        
    allowed_permissions = ROLE_PERMISSIONS.get(user_role, set())
    allowed_scopes = {p.value for p in allowed_permissions}
    
    for scope in security_scopes.scopes:
        if scope not in allowed_scopes:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Permission denied. Required scope: '{scope}'",
                headers={"WWW-Authenticate": authenticate_value},
            )
            
    return user


def get_current_active_user(
    current_user: Annotated[User, Depends(get_current_user)]
) -> User:
    """
    Ensures that the authenticated user is currently active.
    """
    if not current_user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Inactive user"
        )
    return current_user
