from __future__ import annotations
from typing import Generic, TypeVar, Type, Optional, List, Any
from sqlalchemy.orm import Session
from app.db.base import Base

ModelType = TypeVar("ModelType", bound=Base)


class BaseRepository(Generic[ModelType]):
    """
    Generic Base Repository providing foundational CRUD operations.
    Pure repository layer: no FastAPI, HTTP, or transport dependencies.
    """

    def __init__(self, model: Type[ModelType], db: Session) -> None:
        self.model = model
        self.db = db

    def get_by_id(self, id: Any) -> Optional[ModelType]:
        """Retrieve a single entity by primary key."""
        return self.db.query(self.model).filter(self.model.id == id).first()

    def list(self, limit: int = 100, offset: int = 0) -> List[ModelType]:
        """List entities with pagination."""
        return self.db.query(self.model).offset(offset).limit(limit).all()

    def list_all(self) -> List[ModelType]:
        """Retrieve all entities."""
        return self.db.query(self.model).all()

    def create(self, obj: ModelType) -> ModelType:
        """Persist a new model instance."""
        self.db.add(obj)
        self.db.commit()
        self.db.refresh(obj)
        return obj

    def save(self, obj: ModelType) -> ModelType:
        """Commit updates on an existing model instance."""
        self.db.add(obj)
        self.db.commit()
        self.db.refresh(obj)
        return obj

    def delete(self, obj: ModelType) -> None:
        """Delete a model instance."""
        self.db.delete(obj)
        self.db.commit()

    def count(self) -> int:
        """Count total entities in table."""
        return self.db.query(self.model).count()
