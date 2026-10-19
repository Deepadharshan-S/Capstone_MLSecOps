from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.models.base import BaseModel

JSON_TYPE = JSON().with_variant(JSONB, "postgresql")


class InferenceRecord(BaseModel):
    """
    Per-request inference telemetry.

    This is the substrate every label-free drift detector measures against. The
    serving path used to hand predictions to in-memory detectors and discard
    them, so nothing could be compared across time; persisting one row per
    request is what makes D1/D2/D3 possible at all.

    Feature payloads are bounded (`DRIFT_MAX_FEATURE_COLUMNS` /
    `DRIFT_MAX_FEATURE_ROWS`) and flagged with `features_truncated` rather than
    silently dropped, so a detector never mistakes a truncated capture for a
    clean one.
    """

    __tablename__ = "inference_records"

    model_name: Mapped[str] = mapped_column(
        String(100), nullable=False, index=True
    )

    model_version: Mapped[Optional[str]] = mapped_column(String(50), index=True)

    deployment_id: Mapped[Optional[str]] = mapped_column(String(100), index=True)

    rayservice_name: Mapped[Optional[str]] = mapped_column(String(100))

    # Correlates a telemetry row back to the audit_logs entry for the same call.
    request_id: Mapped[Optional[str]] = mapped_column(String(64), index=True)

    client_ip: Mapped[Optional[str]] = mapped_column(String(64))

    prediction: Mapped[Optional[dict]] = mapped_column(JSON_TYPE)

    # Probabilities are absent for models whose serving contract cannot supply
    # them; `has_probabilities` makes that explicit so D3 stays silent instead
    # of treating missing scores as low confidence.
    probabilities: Mapped[Optional[dict]] = mapped_column(JSON_TYPE)

    has_probabilities: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    # Highest class probability for the row — the D3 signal. Null when
    # probabilities were unavailable.
    confidence: Mapped[Optional[float]] = mapped_column(Float)

    latency_ms: Mapped[Optional[float]] = mapped_column(Float)

    features: Mapped[Optional[dict]] = mapped_column(JSON_TYPE)

    features_truncated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    feature_count: Mapped[Optional[int]] = mapped_column(Integer)

    # Event time, not insert time — drift windows index on this.
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        index=True,
    )

    __table_args__ = (
        Index(
            "ix_inference_records_model_version_time",
            "model_name",
            "model_version",
            "recorded_at",
        ),
    )


class ModelBaseline(BaseModel):
    """
    The reference state a drift detector compares against.

    Bound to one model version — comparing traffic for a new deployment against
    a previous model's baseline would attribute a model change to a data
    change. `dataset_ref` should be pinned to a commit SHA rather than a
    branch, otherwise the baseline stops being reproducible.
    """

    __tablename__ = "model_baselines"

    model_name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)

    model_version: Mapped[str] = mapped_column(String(50), nullable=False, index=True)

    dataset_id: Mapped[Optional[str]] = mapped_column(String(255))

    # lakeFS ref the baseline statistics were computed from (prefer a commit SHA).
    dataset_ref: Mapped[Optional[str]] = mapped_column(String(255))

    # Per-feature reference statistics: dtype, count, missing_rate, and for
    # numeric features mean/std/min/max/quantiles/histogram, for categorical
    # features category counts.
    feature_stats: Mapped[Optional[dict]] = mapped_column(JSON_TYPE)

    # Class -> proportion observed in the reference predictions (D2).
    prediction_dist: Mapped[Optional[dict]] = mapped_column(JSON_TYPE)

    # Confidence quantiles/histogram from the reference window (D3).
    confidence_stats: Mapped[Optional[dict]] = mapped_column(JSON_TYPE)

    # Training-time metrics from the MLflow run. Display-only context in the
    # UI — never a detection input, since comparing these against production
    # would require ground-truth outcomes (D4), which do not exist.
    reference_metrics: Mapped[Optional[dict]] = mapped_column(JSON_TYPE)

    sample_count: Mapped[Optional[int]] = mapped_column(Integer)

    # Guards against reusing a baseline after the feature schema changes.
    schema_hash: Mapped[Optional[str]] = mapped_column(String(64), index=True)

    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="active", server_default="active", index=True
    )

    created_by: Mapped[Optional[str]] = mapped_column(String(100))

    __table_args__ = (
        Index(
            "ix_model_baselines_name_version",
            "model_name",
            "model_version",
            unique=True,
        ),
    )
