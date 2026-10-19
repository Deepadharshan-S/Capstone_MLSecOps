from datetime import datetime
from typing import List, Optional, Sequence

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models.drift import InferenceRecord


class InferenceRepository:
    """
    Persistence for per-request inference telemetry.

    These rows are the substrate every label-free drift detector measures
    against, so the read side is organised around time windows per model
    version rather than around individual records.
    """

    def __init__(self, db: Session):
        self.db = db

    def create(self, record: InferenceRecord) -> InferenceRecord:
        """Persist one telemetry row. Commits so a later request rollback
        cannot silently discard an already-served prediction's evidence."""
        self.db.add(record)
        self.db.commit()
        self.db.refresh(record)
        return record

    def create_uncommitted(self, record: InferenceRecord) -> InferenceRecord:
        """Stage a row without committing, for batched writers."""
        self.db.add(record)
        return record

    def window(
        self,
        model_name: str,
        since: datetime,
        until: Optional[datetime] = None,
        model_version: Optional[str] = None,
    ) -> Sequence[InferenceRecord]:
        """
        Rows for one model inside an observation window, oldest first.

        `model_version` is optional on purpose: baseline comparisons can be run
        against all traffic for a model name when a deployment did not record a
        version, rather than silently returning nothing.
        """
        stmt = (
            select(InferenceRecord)
            .where(
                InferenceRecord.model_name == model_name,
                InferenceRecord.recorded_at >= since,
            )
            .order_by(InferenceRecord.recorded_at.asc())
        )
        if until is not None:
            stmt = stmt.where(InferenceRecord.recorded_at < until)
        if model_version:
            stmt = stmt.where(InferenceRecord.model_version == model_version)

        return self.db.execute(stmt).scalars().all()

    def count_window(
        self,
        model_name: str,
        since: datetime,
        until: Optional[datetime] = None,
        model_version: Optional[str] = None,
    ) -> int:
        """Observation count for a window — the input to the min-sample guard."""
        stmt = select(func.count()).select_from(InferenceRecord).where(
            InferenceRecord.model_name == model_name,
            InferenceRecord.recorded_at >= since,
        )
        if until is not None:
            stmt = stmt.where(InferenceRecord.recorded_at < until)
        if model_version:
            stmt = stmt.where(InferenceRecord.model_version == model_version)
        return int(self.db.execute(stmt).scalar_one())

    def distinct_models(self) -> Sequence[tuple]:
        """(model_name, model_version) pairs with telemetry — the drift sweep's
        work list, so it only compares models that actually received traffic."""
        stmt = (
            select(InferenceRecord.model_name, InferenceRecord.model_version)
            .group_by(InferenceRecord.model_name, InferenceRecord.model_version)
            .order_by(InferenceRecord.model_name.asc())
        )
        return list(self.db.execute(stmt).all())

    def list(
        self,
        limit: int = 50,
        offset: int = 0,
        model_name: Optional[str] = None,
        model_version: Optional[str] = None,
    ) -> Sequence[InferenceRecord]:
        """Paginated telemetry inspection, newest first."""
        stmt = select(InferenceRecord).order_by(InferenceRecord.recorded_at.desc())
        if model_name:
            stmt = stmt.where(InferenceRecord.model_name == model_name)
        if model_version:
            stmt = stmt.where(InferenceRecord.model_version == model_version)
        stmt = stmt.offset(offset).limit(limit)
        return self.db.execute(stmt).scalars().all()

    def count(self, model_name: Optional[str] = None) -> int:
        stmt = select(func.count()).select_from(InferenceRecord)
        if model_name:
            stmt = stmt.where(InferenceRecord.model_name == model_name)
        return int(self.db.execute(stmt).scalar_one())

    def prune_older_than(self, cutoff: datetime) -> int:
        """
        Drop telemetry past the retention horizon so a raw-per-request table
        cannot grow without bound. Returns the number of rows removed.
        """
        res = self.db.execute(
            delete(InferenceRecord).where(InferenceRecord.recorded_at < cutoff)
        )
        self.db.commit()
        return int(res.rowcount or 0)

    def delete_for_model(
        self, model_name: str, model_version: Optional[str] = None
    ) -> int:
        """Clear telemetry for one model (used when a baseline is recomputed)."""
        stmt = delete(InferenceRecord).where(InferenceRecord.model_name == model_name)
        if model_version:
            stmt = stmt.where(InferenceRecord.model_version == model_version)
        res = self.db.execute(stmt)
        self.db.commit()
        return int(res.rowcount or 0)
