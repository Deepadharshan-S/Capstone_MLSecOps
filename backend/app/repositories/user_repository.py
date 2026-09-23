from __future__ import annotations
from typing import Optional
from sqlalchemy.orm import Session
from app.models.user import User
from app.repositories.base import BaseRepository


class UserRepository(BaseRepository[User]):
    """
    Dedicated data access layer for User records in PostgreSQL.
    """

    def __init__(self, db: Session) -> None:
        super().__init__(User, db)

    def get_by_username(self, username: str) -> Optional[User]:
        """Find a user by unique username."""
        return self.db.query(User).filter(User.username == username).first()

    def get_by_email(self, email: str) -> Optional[User]:
        """Find a user by unique email."""
        return self.db.query(User).filter(User.email == email).first()

    def get_by_username_or_email(self, identifier: str) -> Optional[User]:
        """Find a user matching either username or email."""
        return (
            self.db.query(User)
            .filter((User.username == identifier) | (User.email == identifier))
            .first()
        )
