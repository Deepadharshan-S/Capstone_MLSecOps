from __future__ import annotations
from typing import Optional
from sqlalchemy.orm import Session
from app.models.dataset import Dataset
from app.repositories.base import BaseRepository


class DatasetRepository(BaseRepository[Dataset]):
    """
    Dedicated data access layer for Dataset records in PostgreSQL.
    """

    def __init__(self, db: Session) -> None:
        super().__init__(Dataset, db)

    def get_by_name(self, name: str) -> Optional[Dataset]:
        """Find a dataset by its unique name."""
        return self.db.query(Dataset).filter(Dataset.name == name).first()
