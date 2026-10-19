"""Schemas for the drift detection API (D1/D2/D3 findings, baselines, telemetry)."""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class BaselineCreate(BaseModel):
    """Request to compute and store a baseline for one model version."""

    model_name: str = Field(..., min_length=1, max_length=100)
    model_version: str = Field(..., min_length=1, max_length=50)
    # When omitted the dataset is resolved from the model's MLflow run tags.
    dataset_id: Optional[str] = Field(None, max_length=255)
    # Optional explicit file inside the lakeFS repository.
    data_path: Optional[str] = Field(None, max_length=500)


class BaselineResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: Any
    model_name: str
    model_version: str
    dataset_id: Optional[str] = None
    dataset_ref: Optional[str] = None
    sample_count: Optional[int] = None
    schema_hash: Optional[str] = None
    status: str
    reference_metrics: Optional[dict] = None
    created_by: Optional[str] = None
    created_at: Any = None
    # Feature keys only — the full statistics payload can be large and the
    # detail view is fetched per model when it is actually needed.
    feature_names: list[str] = []
    has_prediction_dist: bool = False
    has_confidence_stats: bool = False


class DriftCheckRequest(BaseModel):
    model_name: str = Field(..., min_length=1, max_length=100)
    model_version: Optional[str] = Field(None, max_length=50)
    # Defaults to DRIFT_OBSERVATION_WINDOW_SECONDS.
    window_seconds: Optional[int] = Field(None, ge=60, le=86400)
    # Publish findings as security events + alerts. Disabled lets an operator
    # preview what a sweep would report without raising alerts.
    publish: bool = True


class DriftCheckResponse(BaseModel):
    model_name: str
    model_version: Optional[str] = None
    window_start: str
    window_end: str
    window_seconds: int
    records: int
    observations: int
    baseline_found: bool
    baseline_id: Optional[str] = None
    findings: list[dict] = []
    skipped: list[dict] = []


class DriftFindingItem(BaseModel):
    """A drift finding as surfaced through its correlated alert."""

    alert_id: Any
    title: str
    summary: str
    severity: str
    status: str
    detection_type: str
    model_name: Optional[str] = None
    model_version: Optional[str] = None
    affected_features: list[str] = []
    observation_start: Optional[str] = None
    observation_end: Optional[str] = None
    limitations: list[str] = []
    event_count: int = 0
    first_seen: Any = None
    last_seen: Any = None


class DriftConfigResponse(BaseModel):
    """Detector configuration, so the UI can explain why a check was skipped."""

    enabled: bool
    sweep_interval_seconds: int
    observation_window_seconds: int
    min_observations: int
    thresholds: dict[str, Any]
    telemetry_retention_days: int
    detection_source: str
    available_detections: list[str]
