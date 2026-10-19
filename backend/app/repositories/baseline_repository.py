from typing import Optional, Sequence

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models.drift import ModelBaseline


class BaselineRepository:
    """
    Persistence for drift baselines.

    A baseline is uniquely identified by (model_name, model_version): a model
    change must never be mistaken for a data change, so creating one for a
    version that already has a baseline supersedes the old row instead of
    adding a second active reference.
    """

    def __init__(self, db: Session):
        self.db = db

    def get_active(
        self, model_name: str, model_version: Optional[str] = None
    ) -> Optional[ModelBaseline]:
        """The reference the drift detectors should compare against."""
        stmt = select(ModelBaseline).where(
            ModelBaseline.model_name == model_name,
            ModelBaseline.status == "active",
        )
        if model_version:
            stmt = stmt.where(ModelBaseline.model_version == model_version)
        # Without a version, ordering newest-first means callers still get
        # *a* reference instead of nothing.
        stmt = stmt.order_by(ModelBaseline.created_at.desc())
        return self.db.execute(stmt).scalars().first()

    def get_by_key(
        self, model_name: str, model_version: str
    ) -> Optional[ModelBaseline]:
        stmt = select(ModelBaseline).where(
            ModelBaseline.model_name == model_name,
            ModelBaseline.model_version == model_version,
        )
        return self.db.execute(stmt).scalar_one_or_none()

    def create(self, baseline: ModelBaseline) -> ModelBaseline:
        self.db.add(baseline)
        self.db.commit()
        self.db.refresh(baseline)
        return baseline

    def upsert(self, baseline: ModelBaseline) -> ModelBaseline:
        """
        Store a baseline, superseding any prior active row for the same model
        version. Keeps at most one active reference per version, which is what
        the unique (model_name, model_version) index enforces at the schema
        level for the whole table.
        """
        existing = self.get_by_key(baseline.model_name, baseline.model_version)
        if existing is None:
            return self.create(baseline)

        for field in (
            "dataset_id",
            "dataset_ref",
            "feature_stats",
            "prediction_dist",
            "confidence_stats",
            "reference_metrics",
            "sample_count",
            "schema_hash",
            "status",
            "created_by",
        ):
            setattr(existing, field, getattr(baseline, field))

        self.db.add(existing)
        self.db.commit()
        self.db.refresh(existing)
        return existing

    # Note: the unique (model_name, model_version) index means a version holds
    # exactly one baseline row — `upsert` overwrites it rather than stacking a
    # superseded row next to a new active one.

    def list(
        self,
        model_name: Optional[str] = None,
        model_version: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[ModelBaseline]:
        stmt = select(ModelBaseline).order_by(ModelBaseline.created_at.desc())
        if model_name:
            stmt = stmt.where(ModelBaseline.model_name == model_name)
        if model_version:
            stmt = stmt.where(ModelBaseline.model_version == model_version)
        if status:
            stmt = stmt.where(ModelBaseline.status == status)
        stmt = stmt.offset(offset).limit(limit)
        return self.db.execute(stmt).scalars().all()

    def count(
        self, model_name: Optional[str] = None, status: Optional[str] = None
    ) -> int:
        from sqlalchemy import func

        stmt = select(func.count()).select_from(ModelBaseline)
        if model_name:
            stmt = stmt.where(ModelBaseline.model_name == model_name)
        if status:
            stmt = stmt.where(ModelBaseline.status == status)
        return int(self.db.execute(stmt).scalar_one())

    def delete(self, baseline: ModelBaseline) -> None:
        self.db.delete(baseline)
        self.db.commit()
