import logging
from datetime import datetime, timezone, timedelta
from typing import Annotated, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session

from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    validate_password_strength,
    verify_password,
)
from app.db.session import get_db
from app.models.refresh_token import RefreshToken
from app.models.user import User
from app.models.audit_log import AuditLog
from app.schemas.token import Token
from app.schemas.user import UserCreate, UserResponse

router = APIRouter(prefix="/auth", tags=["authentication"])
logger = logging.getLogger("security_audit")

# Configure a basic format for audit logging if not already configured
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter("[%(asctime)s] %(levelname)s - %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def log_audit_event(
    db: Session,
    action: str,
    username: str,
    ip_address: Optional[str] = None,
    details: Optional[str] = None,
):
    """
    Logs security audit events to both the Python logger and the PostgreSQL database.
    """
    logger.info(f"AUDIT: [{action}] user='{username}' ip='{ip_address}' details='{details}'")
    try:
        audit_log = AuditLog(
            action=action,
            username=username,
            ip_address=ip_address,
            details=details,
        )
        db.add(audit_log)
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to write to DB audit log: {e}")


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def register(user_in: UserCreate, request: Request, db: Session = Depends(get_db)):
    """
    Registers a new user after enforcing the password complexity policy.
    """
    ip_addr = request.client.host if request.client else None
    
    # 1. Enforce password policy
    is_valid, error_msg = validate_password_strength(user_in.password)
    if not is_valid:
        log_audit_event(db, "register_failed_weak_password", user_in.username, ip_addr, "Weak password policy validation failed.")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error_msg,
        )

    # 2. Check if username or email already exists
    existing_user = db.query(User).filter(
        (User.username == user_in.username) | (User.email == user_in.email)
    ).first()
    if existing_user:
        log_audit_event(db, "register_failed_collision", user_in.username, ip_addr, "Username or email already exists.")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username or email already registered.",
        )

    # 3. Create new user
    hashed_pwd = hash_password(user_in.password)
    new_user = User(
        username=user_in.username,
        email=user_in.email,
        password_hash=hashed_pwd,
        role=user_in.role,
        is_active=True,
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)

    log_audit_event(db, "register_success", new_user.username, ip_addr, f"Registered with role '{new_user.role}'.")
    return new_user


@router.post("/login", response_model=Token)
def login(
    response: Response,
    request: Request,
    form_data: Annotated[OAuth2PasswordRequestForm, Depends()],
    db: Session = Depends(get_db),
):
    """
    Authenticates user, handles brute-force lockout, and issues access/refresh tokens.
    """
    ip_addr = request.client.host if request.client else None

    # Find user by username or email
    user = db.query(User).filter(
        (User.username == form_data.username) | (User.email == form_data.username)
    ).first()

    if not user:
        log_audit_event(db, "login_failed_nonexistent_user", form_data.username, ip_addr, "Attempted login for non-existent user.")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Incorrect username or password.",
        )

    # 1. Check account lockout status
    current_time = datetime.now(timezone.utc)
    if user.locked_until:
        # Normalize naive datetime from SQLite to offset-aware UTC
        locked_until = user.locked_until.replace(tzinfo=timezone.utc) if user.locked_until.tzinfo is None else user.locked_until
        if locked_until > current_time:
            log_audit_event(db, "login_blocked_locked", user.username, ip_addr, f"Login blocked. Locked until {locked_until}.")
            locked_minutes = int((locked_until - current_time).total_seconds() / 60) + 1
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Account is temporarily locked due to too many failed attempts. Try again in {locked_minutes} minutes.",
            )

    # 2. Verify password
    if not verify_password(form_data.password, user.password_hash):
        # Incorrect password: increment attempts and handle lockout
        user.failed_login_attempts += 1
        if user.failed_login_attempts >= 5:
            user.locked_until = current_time + timedelta(minutes=15)
            log_audit_event(db, "user_lockout_triggered", user.username, ip_addr, "Account locked for 15 minutes due to 5 failed attempts.")
            db.commit()
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Incorrect username or password. Account is now locked for 15 minutes.",
            )
        db.commit()
        log_audit_event(db, "login_failed_incorrect_password", user.username, ip_addr, f"Attempt {user.failed_login_attempts}/5.")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Incorrect username or password.",
        )

    # 3. Successful authentication: reset failed attempts
    user.failed_login_attempts = 0
    user.locked_until = None
    
    # Generate tokens
    access_token = create_access_token(subject=user.id)
    refresh_token, token_id, expires_at = create_refresh_token(subject=user.id)

    # Store refresh token jti in DB
    db_refresh_token = RefreshToken(
        token_hash=token_id,  # store the jti
        user_id=user.id,
        expires_at=expires_at,
        is_revoked=False,
    )
    db.add(db_refresh_token)
    db.commit()

    # Set secure HttpOnly cookie for the refresh token
    response.set_cookie(
        key="refresh_token",
        value=refresh_token,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=7 * 24 * 3600,  # 7 days in seconds
        path="/api/auth",  # scope cookie to auth endpoints for security
    )

    log_audit_event(db, "login_success", user.username, ip_addr, "Successful login. Access token issued.")
    return Token(access_token=access_token)


@router.post("/refresh", response_model=Token)
def refresh(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    """
    Refreshes access and refresh tokens using Refresh Token Rotation (RTR).
    Includes automatic reuse detection to mitigate token theft.
    """
    ip_addr = request.client.host if request.client else None
    refresh_token = request.cookies.get("refresh_token")
    if not refresh_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token missing",
        )

    payload = decode_token(refresh_token)
    if not payload or payload.get("type") != "refresh":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token",
        )

    user_id = payload.get("sub")
    jti = payload.get("jti")

    # Convert user_id to UUID object for database query compatibility (especially on SQLite)
    try:
        user_uuid = UUID(user_id) if isinstance(user_id, str) else user_id
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token claims",
        )

    # Look up user to get username
    user_record = db.query(User).filter(User.id == user_uuid).first()
    username = user_record.username if user_record else "unknown"

    # Look up token in database
    db_token = db.query(RefreshToken).filter(RefreshToken.token_hash == jti).first()

    # 1. Detection of Refresh Token Reuse (Token Theft Detection)
    if db_token and db_token.is_revoked:
        log_audit_event(
            db,
            "token_reuse_detected",
            username,
            ip_addr,
            f"REUSE DETECTION! Revoked refresh token '{jti}' was reused. Revoking all active sessions."
        )
        # Revoke all tokens for this user immediately for security breach containment
        db.query(RefreshToken).filter(RefreshToken.user_id == user_uuid).update(
            {RefreshToken.is_revoked: True}
        )
        db.commit()
        response.delete_cookie(key="refresh_token", path="/api/auth")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Security warning: Session compromised. Please log in again.",
        )

    # 2. General validation (existence, expiry)
    current_time = datetime.now(timezone.utc)
    if not db_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token",
        )
        
    db_token_expires = db_token.expires_at.replace(tzinfo=timezone.utc) if db_token.expires_at.tzinfo is None else db_token.expires_at
    if db_token_expires < current_time:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token",
        )

    # 3. Successful rotation: Revoke current token and generate new ones
    db_token.is_revoked = True
    
    new_access_token = create_access_token(subject=user_uuid)
    new_refresh_token, new_jti, new_expires_at = create_refresh_token(subject=user_uuid)

    db_new_token = RefreshToken(
        token_hash=new_jti,
        user_id=user_uuid,
        expires_at=new_expires_at,
        is_revoked=False,
    )
    db.add(db_new_token)
    db.commit()

    # Set rotated refresh token cookie
    response.set_cookie(
        key="refresh_token",
        value=new_refresh_token,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=7 * 24 * 3600,
        path="/api/auth",
    )

    log_audit_event(db, "token_refresh_success", username, ip_addr, f"Rotated refresh token. New JTI: '{new_jti}'.")
    return Token(access_token=new_access_token)


@router.post("/logout", status_code=status.HTTP_200_OK)
def logout(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    """
    Logs out the user by revoking the refresh token, blacklisting the access token, and clearing the cookie.
    """
    ip_addr = request.client.host if request.client else None
    username = "unknown"
    
    # 1. Blacklist the access token
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        access_token = auth_header.split(" ")[1]
        payload = decode_token(access_token)
        if payload:
            jti = payload.get("jti")
            exp = payload.get("exp")
            user_id = payload.get("sub")
            if user_id:
                try:
                    user_uuid = UUID(user_id) if isinstance(user_id, str) else user_id
                    user_record = db.query(User).filter(User.id == user_uuid).first()
                    if user_record:
                        username = user_record.username
                except (ValueError, TypeError):
                    pass
            if jti and exp:
                expires_at = datetime.fromtimestamp(exp, tz=timezone.utc)
                from app.models.blacklisted_token import BlacklistedToken
                # Check if already blacklisted to prevent integrity errors
                existing_blacklist = db.query(BlacklistedToken).filter(BlacklistedToken.jti == jti).first()
                if not existing_blacklist:
                    db_blacklist = BlacklistedToken(jti=jti, expires_at=expires_at)
                    db.add(db_blacklist)
                    db.commit()

    # 2. Revoke the refresh token in DB
    refresh_token = request.cookies.get("refresh_token")
    if refresh_token:
        payload = decode_token(refresh_token)
        if payload:
            jti = payload.get("jti")
            # Revoke token in DB
            db_token = db.query(RefreshToken).filter(RefreshToken.token_hash == jti).first()
            if db_token:
                db_token.is_revoked = True
                db.commit()
                log_audit_event(db, "logout_success", username, ip_addr, f"Logged out. Revoked refresh token '{jti}' and blacklisted access token.")
                response.delete_cookie(key="refresh_token", path="/api/auth")
                return {"message": "Successfully logged out."}

    log_audit_event(db, "logout_success", username, ip_addr, "Logged out.")
    response.delete_cookie(key="refresh_token", path="/api/auth")
    return {"message": "Successfully logged out."}
