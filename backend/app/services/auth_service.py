import logging
from datetime import datetime, timezone, timedelta
from typing import Optional
from uuid import UUID

from fastapi import HTTPException, status, Response, Request
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
from app.models.refresh_token import RefreshToken
from app.models.user import User
from app.models.blacklisted_token import BlacklistedToken
from app.models.audit_log import AuditLog
from app.schemas.user import UserCreate

logger = logging.getLogger("security_audit")


# Configure basic formatting if not already set
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
    """Logs security audit events to the database and standard logging system in an isolated transaction."""
    logger.info(
        f"AUDIT: [{action}] user='{username}' ip='{ip_address}' details='{details}'"
    )
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


def register_user(db: Session, user_in: UserCreate, ip_address: Optional[str] = None) -> User:
    """Enforces password complexity, checks collision, and registers new users in a managed transaction."""
    # 1. Enforce password complexity policy
    is_valid, error_msg = validate_password_strength(user_in.password)
    if not is_valid:
        log_audit_event(
            db,
            "register_failed_weak_password",
            user_in.username,
            ip_address,
            "Weak password policy validation failed.",
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error_msg,
        )

    # 2. Check for username/email collision
    existing_user = (
        db.query(User)
        .filter((User.username == user_in.username) | (User.email == user_in.email))
        .first()
    )
    if existing_user:
        log_audit_event(
            db,
            "register_failed_collision",
            user_in.username,
            ip_address,
            "Username or email already exists.",
        )
        # Use generic message to prevent username/email enumeration
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username or email is unavailable.",
        )

    # 3. Create user inside database transaction
    try:
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
    except Exception:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An error occurred during user registration.",
        )

    log_audit_event(
        db,
        "register_success",
        new_user.username,
        ip_address,
        f"Registered with role '{new_user.role}'.",
    )
    return new_user


def authenticate_user(
    db: Session,
    response: Response,
    form_data: OAuth2PasswordRequestForm,
    ip_address: Optional[str] = None,
) -> dict:
    """Authenticates credentials, handles lockout thresholds, and rotates sessions in a transaction."""
    user = (
        db.query(User)
        .filter(
            (User.username == form_data.username) | (User.email == form_data.username)
        )
        .first()
    )

    if not user:
        log_audit_event(
            db,
            "login_failed_nonexistent_user",
            form_data.username,
            ip_address,
            "Attempted login for non-existent user.",
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Incorrect username or password.",
        )

    # 1. Lockout verification
    current_time = datetime.now(timezone.utc)
    if user.locked_until:
        locked_until = (
            user.locked_until.replace(tzinfo=timezone.utc)
            if user.locked_until.tzinfo is None
            else user.locked_until
        )
        if locked_until > current_time:
            log_audit_event(
                db,
                "login_blocked_locked",
                user.username,
                ip_address,
                f"Login blocked. Locked until {locked_until}.",
            )
            # Use generic message to hide exact lockout settings and remaining time
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Account is temporarily locked. Please try again later.",
            )

    # 2. Verify password correctness
    if not verify_password(form_data.password, user.password_hash):
        try:
            # Increment failed attempts inside transaction
            user.failed_login_attempts += 1
            if user.failed_login_attempts >= 5:
                user.locked_until = current_time + timedelta(minutes=15)
                log_audit_event(
                    db,
                    "user_lockout_triggered",
                    user.username,
                    ip_address,
                    "Account locked due to 5 failed attempts.",
                )
                db.commit()
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Account is temporarily locked. Please try again later.",
                )
            db.commit()
        except HTTPException:
            raise
        except Exception:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="An error occurred during authentication.",
            )

        log_audit_event(
            db,
            "login_failed_incorrect_password",
            user.username,
            ip_address,
            f"Attempt {user.failed_login_attempts}/5.",
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Incorrect username or password.",
        )

    # 3. Successful authentication: Reset attempts and issue tokens in a single transaction
    try:
        user.failed_login_attempts = 0
        user.locked_until = None

        access_token = create_access_token(subject=user.id)
        refresh_token, token_id, expires_at = create_refresh_token(subject=user.id)

        # Record token JTI
        db_refresh_token = RefreshToken(
            token_hash=token_id,
            user_id=user.id,
            expires_at=expires_at,
            is_revoked=False,
        )
        db.add(db_refresh_token)
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An error occurred during token generation.",
        )

    # Set secure HttpOnly cookie for the refresh token
    response.set_cookie(
        key="refresh_token",
        value=refresh_token,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=7 * 24 * 3600,
        path="/api/auth",
    )

    log_audit_event(
        db,
        "login_success",
        user.username,
        ip_address,
        "Successful login. Access token issued.",
    )
    return {"access_token": access_token, "token_type": "bearer"}


def rotate_refresh_token(
    db: Session,
    request: Request,
    response: Response,
    ip_address: Optional[str] = None,
) -> dict:
    """Enforces Refresh Token Rotation (RTR), checks for reuse abuse, and re-issues session cookies."""
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

    try:
        user_uuid = UUID(user_id) if isinstance(user_id, str) else user_id
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token claims",
        )

    user_record = db.query(User).filter(User.id == user_uuid).first()
    username = user_record.username if user_record else "unknown"

    db_token = db.query(RefreshToken).filter(RefreshToken.token_hash == jti).first()

    # 1. Session compromise check: token reuse detection
    if db_token and db_token.is_revoked:
        log_audit_event(
            db,
            "token_reuse_detected",
            username,
            ip_address,
            f"REUSE DETECTION! Revoked refresh token '{jti}' was reused. Revoking all active sessions.",
        )
        try:
            # Revoke all tokens for this user immediately
            db.query(RefreshToken).filter(RefreshToken.user_id == user_uuid).update(
                {RefreshToken.is_revoked: True}
            )
            db.commit()
        except Exception:
            db.rollback()
        response.delete_cookie(key="refresh_token", path="/api/auth")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Security warning: Session compromised. Please log in again.",
        )

    # 2. General token checks
    current_time = datetime.now(timezone.utc)
    if not db_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token",
        )

    db_token_expires = (
        db_token.expires_at.replace(tzinfo=timezone.utc)
        if db_token.expires_at.tzinfo is None
        else db_token.expires_at
    )
    if db_token_expires < current_time:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token",
        )

    # 3. Rotate tokens: revoke current and generate new ones inside transaction
    try:
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
    except Exception:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An error occurred during token rotation.",
        )

    response.set_cookie(
        key="refresh_token",
        value=new_refresh_token,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=7 * 24 * 3600,
        path="/api/auth",
    )

    log_audit_event(
        db,
        "token_refresh_success",
        username,
        ip_address,
        f"Rotated refresh token. New JTI: '{new_jti}'.",
    )
    return {"access_token": new_access_token, "token_type": "bearer"}


def logout_user(
    db: Session,
    request: Request,
    response: Response,
    ip_address: Optional[str] = None,
) -> dict:
    """Blacklists the access token and revokes the active refresh token inside transaction scope."""
    username = "unknown"

    # 1. Blacklist access token
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
                try:
                    # Verify duplicates to protect unique index constraints
                    existing_blacklist = (
                        db.query(BlacklistedToken)
                        .filter(BlacklistedToken.jti == jti)
                        .first()
                    )
                    if not existing_blacklist:
                        db_blacklist = BlacklistedToken(jti=jti, expires_at=expires_at)
                        db.add(db_blacklist)
                        db.commit()
                except Exception:
                    db.rollback()

    # 2. Revoke refresh token
    refresh_token = request.cookies.get("refresh_token")
    if refresh_token:
        payload = decode_token(refresh_token)
        if payload:
            jti = payload.get("jti")
            try:
                db_token = (
                    db.query(RefreshToken).filter(RefreshToken.token_hash == jti).first()
                )
                if db_token:
                    db_token.is_revoked = True
                    db.commit()
                    log_audit_event(
                        db,
                        "logout_success",
                        username,
                        ip_address,
                        f"Logged out. Revoked refresh token '{jti}' and blacklisted access token.",
                    )
                    response.delete_cookie(key="refresh_token", path="/api/auth")
                    return {"message": "Successfully logged out."}
            except Exception:
                db.rollback()

    log_audit_event(db, "logout_success", username, ip_address, "Logged out.")
    response.delete_cookie(key="refresh_token", path="/api/auth")
    return {"message": "Successfully logged out."}
