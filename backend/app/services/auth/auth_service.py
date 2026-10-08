from __future__ import annotations
from datetime import datetime, timezone, timedelta
from typing import Optional
from uuid import UUID

from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

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
from app.schemas.auth import UserCreate
from app.repositories.user_repository import UserRepository
from app.repositories.token_repository import RefreshTokenRepository, BlacklistedTokenRepository
from app.core.logging_config import log_audit_event
from app.services.auth.exceptions import (
    WeakPasswordError,
    UserAlreadyExistsError,
    InvalidCredentialsError,
    AccountLockedError,
    InvalidTokenError,
    TokenReuseError,
    AuthDomainError,
)


class AuthService:
    """
    Domain service class managing user registration, authentication session states,
    lockout verification, and token rotation workflows.
    Completely decoupled from FastAPI HTTP primitives (Request, Response, HTTPException).
    """

    def __init__(
        self,
        user_repo: Optional[UserRepository] = None,
        refresh_token_repo: Optional[RefreshTokenRepository] = None,
        blacklisted_token_repo: Optional[BlacklistedTokenRepository] = None,
    ) -> None:
        self.user_repo = user_repo
        self.refresh_token_repo = refresh_token_repo
        self.blacklisted_token_repo = blacklisted_token_repo

    def _get_user_repo(self, db: Session) -> UserRepository:
        if self.user_repo is not None and self.user_repo.db == db:
            return self.user_repo
        return UserRepository(db)

    def _get_refresh_token_repo(self, db: Session) -> RefreshTokenRepository:
        if self.refresh_token_repo is not None and self.refresh_token_repo.db == db:
            return self.refresh_token_repo
        return RefreshTokenRepository(db)

    def _get_blacklisted_token_repo(self, db: Session) -> BlacklistedTokenRepository:
        if self.blacklisted_token_repo is not None and self.blacklisted_token_repo.db == db:
            return self.blacklisted_token_repo
        return BlacklistedTokenRepository(db)

    def register_user(
        self,
        db: Session,
        username: Optional[str] = None,
        email: Optional[str] = None,
        password: Optional[str] = None,
        ip_address: Optional[str] = None,
        user_in: Optional[UserCreate] = None,
    ) -> User:
        """Enforces password complexity, checks collision, and registers new users."""
        if user_in is not None:
            username = user_in.username
            email = user_in.email
            password = user_in.password

        if not username or not email or not password:
            raise WeakPasswordError("Username, email, and password are required.")

        # 1. Enforce password complexity policy
        is_valid, error_msg = validate_password_strength(password)
        if not is_valid:
            log_audit_event(
                "register_failed_weak_password",
                username,
                ip_address,
                "Weak password policy validation failed.",
                db=db,
            )
            raise WeakPasswordError(error_msg)

        user_repo = self._get_user_repo(db)

        # 2. Check for username/email collision
        existing_user = user_repo.get_by_username_or_email(username) or user_repo.get_by_email(email)
        if existing_user:
            log_audit_event(
                "register_failed_collision",
                username,
                ip_address,
                "Username or email already exists.",
                db=db,
            )
            raise UserAlreadyExistsError("Username or email is unavailable.")

        # 3. Create user inside database transaction
        try:
            hashed_pwd = hash_password(password)
            new_user = User(
                username=username,
                email=email,
                password_hash=hashed_pwd,
                role="viewer",
                is_active=True,
            )
            new_user = user_repo.create(new_user)
        except IntegrityError:
            db.rollback()
            log_audit_event(
                "register_failed_collision",
                username,
                ip_address,
                "Integrity constraint violation (username or email collision).",
                db=db,
            )
            raise UserAlreadyExistsError("Username or email is unavailable.")
        except Exception as e:
            db.rollback()
            raise AuthDomainError(
                detail="An error occurred during user registration.",
                status_code=500,
            ) from e

        log_audit_event(
            "register_success",
            new_user.username,
            ip_address,
            f"Registered with role '{new_user.role}'.",
            db=db,
        )
        return new_user

    def authenticate_user(
        self,
        db: Session,
        username: str,
        password: str,
        ip_address: Optional[str] = None,
    ) -> dict:
        """Authenticates credentials, handles lockout thresholds, and issues access and refresh tokens."""
        user_repo = self._get_user_repo(db)
        user = user_repo.get_by_username_or_email(username)

        if not user:
            log_audit_event(
                "login_failed_nonexistent_user",
                username,
                ip_address,
                "Attempted login for non-existent user.",
                db=db,
            )
            raise InvalidCredentialsError("Incorrect username or password.")

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
                    "login_blocked_locked",
                    user.username,
                    ip_address,
                    f"Login blocked. Locked until {locked_until}.",
                    db=db,
                )
                raise AccountLockedError("Account is temporarily locked. Please try again later.")

        # 2. Verify password correctness
        if not verify_password(password, user.password_hash):
            try:
                user.failed_login_attempts += 1
                if user.failed_login_attempts >= 5:
                    user.locked_until = current_time + timedelta(minutes=15)
                    log_audit_event(
                        "user_lockout_triggered",
                        user.username,
                        ip_address,
                        "Account locked due to 5 failed attempts.",
                        db=db,
                    )
                    user_repo.save(user)
                    raise AccountLockedError("Account is temporarily locked. Please try again later.")
                user_repo.save(user)
            except AccountLockedError:
                raise
            except Exception as e:
                db.rollback()
                raise AuthDomainError(
                    detail="An error occurred during authentication.",
                    status_code=500,
                ) from e

            log_audit_event(
                "login_failed_incorrect_password",
                user.username,
                ip_address,
                f"Attempt {user.failed_login_attempts}/5.",
                db=db,
            )
            raise InvalidCredentialsError("Incorrect username or password.")

        # 3. Successful authentication: Reset attempts and issue tokens
        try:
            user.failed_login_attempts = 0
            user.locked_until = None
            user_repo.save(user)

            access_token = create_access_token(subject=user.id)
            refresh_token, token_id, expires_at = create_refresh_token(subject=user.id)

            refresh_token_repo = self._get_refresh_token_repo(db)
            db_refresh_token = RefreshToken(
                token_hash=token_id,
                user_id=user.id,
                expires_at=expires_at,
                is_revoked=False,
            )
            refresh_token_repo.create(db_refresh_token)
        except Exception as e:
            db.rollback()
            raise AuthDomainError(
                detail="An error occurred during token generation.",
                status_code=500,
            ) from e

        log_audit_event(
            "login_success",
            user.username,
            ip_address,
            "Successful login. Access token issued.",
            db=db,
        )
        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "bearer",
        }

    def rotate_refresh_token(
        self,
        db: Session,
        refresh_token: Optional[str],
        ip_address: Optional[str] = None,
    ) -> dict:
        """Enforces Refresh Token Rotation (RTR) and checks for token reuse compromise."""
        if not refresh_token:
            raise InvalidTokenError("Refresh token missing")

        payload = decode_token(refresh_token)
        if not payload or payload.get("type") != "refresh":
            raise InvalidTokenError("Invalid refresh token")

        user_id = payload.get("sub")
        jti = payload.get("jti")

        try:
            user_uuid = UUID(user_id) if isinstance(user_id, str) else user_id
        except (ValueError, TypeError):
            raise InvalidTokenError("Invalid refresh token claims")

        user_repo = self._get_user_repo(db)
        user_record = user_repo.get_by_id(user_uuid)
        username = user_record.username if user_record else "unknown"

        refresh_token_repo = self._get_refresh_token_repo(db)
        db_token = refresh_token_repo.get_by_jti(jti)

        # 1. Session compromise check: token reuse detection
        if db_token and db_token.is_revoked:
            log_audit_event(
                "token_reuse_detected",
                username,
                ip_address,
                f"REUSE DETECTION! Revoked refresh token '{jti}' was reused. Revoking all active sessions.",
                db=db,
            )
            try:
                refresh_token_repo.revoke_all_for_user(user_uuid)
            except Exception:
                db.rollback()
            raise TokenReuseError("Security warning: Session compromised. Please log in again.")

        # 2. General token checks
        current_time = datetime.now(timezone.utc)
        if not db_token:
            raise InvalidTokenError("Invalid or expired refresh token")

        db_token_expires = (
            db_token.expires_at.replace(tzinfo=timezone.utc)
            if db_token.expires_at.tzinfo is None
            else db_token.expires_at
        )
        if db_token_expires < current_time:
            raise InvalidTokenError("Invalid or expired refresh token")

        # 3. Rotate tokens: revoke current and generate new ones inside transaction
        try:
            db_token.is_revoked = True
            refresh_token_repo.save(db_token)

            new_access_token = create_access_token(subject=user_uuid)
            new_refresh_token, new_jti, new_expires_at = create_refresh_token(
                subject=user_uuid
            )

            db_new_token = RefreshToken(
                token_hash=new_jti,
                user_id=user_uuid,
                expires_at=new_expires_at,
                is_revoked=False,
            )
            refresh_token_repo.create(db_new_token)
        except Exception as e:
            db.rollback()
            raise AuthDomainError(
                detail="An error occurred during token rotation.",
                status_code=500,
            ) from e

        log_audit_event(
            "token_refresh_success",
            username,
            ip_address,
            f"Rotated refresh token. New JTI: '{new_jti}'.",
            db=db,
        )
        return {
            "access_token": new_access_token,
            "refresh_token": new_refresh_token,
            "token_type": "bearer",
        }

    def logout_user(
        self,
        db: Session,
        access_token: Optional[str] = None,
        refresh_token: Optional[str] = None,
        ip_address: Optional[str] = None,
    ) -> dict:
        """Blacklists the access token and revokes the active refresh token."""
        username = "unknown"
        user_repo = self._get_user_repo(db)
        blacklisted_token_repo = self._get_blacklisted_token_repo(db)
        refresh_token_repo = self._get_refresh_token_repo(db)

        # 1. Blacklist access token
        if access_token:
            payload = decode_token(access_token)
            if payload:
                jti = payload.get("jti")
                exp = payload.get("exp")
                user_id = payload.get("sub")
                if user_id:
                    try:
                        user_uuid = (
                            UUID(user_id) if isinstance(user_id, str) else user_id
                        )
                        user_record = user_repo.get_by_id(user_uuid)
                        if user_record:
                            username = user_record.username
                    except (ValueError, TypeError):
                        pass
                if jti and exp:
                    expires_at = datetime.fromtimestamp(exp, tz=timezone.utc)
                    try:
                        existing_blacklist = blacklisted_token_repo.get_by_jti(jti)
                        if not existing_blacklist:
                            db_blacklist = BlacklistedToken(
                                jti=jti, expires_at=expires_at
                            )
                            blacklisted_token_repo.create(db_blacklist)
                    except IntegrityError:
                        db.rollback()
                    except Exception:
                        db.rollback()

        # 2. Revoke refresh token
        if refresh_token:
            payload = decode_token(refresh_token)
            if payload:
                jti = payload.get("jti")
                try:
                    db_token = refresh_token_repo.get_by_jti(jti)
                    if db_token:
                        db_token.is_revoked = True
                        refresh_token_repo.save(db_token)
                        log_audit_event(
                            "logout_success",
                            username,
                            ip_address,
                            f"Logged out. Revoked refresh token '{jti}' and blacklisted access token.",
                            db=db,
                        )
                        return {"message": "Successfully logged out."}
                except Exception:
                    db.rollback()

        log_audit_event("logout_success", username, ip_address, "Logged out.", db=db)
        return {"message": "Successfully logged out."}

    def cleanup_expired_tokens(self, db: Session) -> dict[str, int]:
        """
        Prunes expired refresh tokens and expired blacklisted tokens from the database.
        Safe, idempotent maintenance operation.
        """
        now = datetime.now(timezone.utc)
        refresh_repo = RefreshTokenRepository(db)
        blacklist_repo = BlacklistedTokenRepository(db)

        deleted_refresh = refresh_repo.delete_expired(now=now)
        deleted_blacklist = blacklist_repo.delete_expired(now=now)

        log_audit_event(
            "token_cleanup",
            "system",
            None,
            f"Pruned {deleted_refresh} expired refresh tokens and {deleted_blacklist} expired blacklisted tokens.",
            db=db,
        )

        return {
            "deleted_refresh_tokens": deleted_refresh,
            "deleted_blacklisted_tokens": deleted_blacklist,
            "total_deleted": deleted_refresh + deleted_blacklist,
        }


auth_service = AuthService()

