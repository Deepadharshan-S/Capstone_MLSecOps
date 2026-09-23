from __future__ import annotations
from typing import Optional
from uuid import UUID
from sqlalchemy.orm import Session
from app.models.refresh_token import RefreshToken
from app.models.blacklisted_token import BlacklistedToken
from app.repositories.base import BaseRepository


class RefreshTokenRepository(BaseRepository[RefreshToken]):
    """
    Dedicated data access layer for RefreshToken records in PostgreSQL.
    """

    def __init__(self, db: Session) -> None:
        super().__init__(RefreshToken, db)

    def get_by_jti(self, jti: str) -> Optional[RefreshToken]:
        """Find a refresh token by its JTI / token_hash."""
        return (
            self.db.query(RefreshToken)
            .filter(RefreshToken.token_hash == jti)
            .first()
        )

    def revoke_all_for_user(self, user_id: UUID) -> None:
        """Revoke all active refresh tokens for a user upon token reuse detection."""
        self.db.query(RefreshToken).filter(RefreshToken.user_id == user_id).update(
            {RefreshToken.is_revoked: True}
        )
        self.db.commit()


class BlacklistedTokenRepository(BaseRepository[BlacklistedToken]):
    """
    Dedicated data access layer for BlacklistedToken records in PostgreSQL.
    """

    def __init__(self, db: Session) -> None:
        super().__init__(BlacklistedToken, db)

    def get_by_jti(self, jti: str) -> Optional[BlacklistedToken]:
        """Find a blacklisted token by its JTI."""
        return (
            self.db.query(BlacklistedToken)
            .filter(BlacklistedToken.jti == jti)
            .first()
        )
