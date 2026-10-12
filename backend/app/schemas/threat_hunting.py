from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# ============================================================================
# Security Event Schemas
# ============================================================================

class SecurityEventBase(BaseModel):
    event_type: str = Field(..., min_length=2, max_length=100)
    source: str = Field(..., min_length=2, max_length=100)
    actor_id: Optional[str] = Field(None, max_length=100)
    resource_id: Optional[str] = Field(None, max_length=255)
    resource_type: Optional[str] = Field(None, max_length=100)
    severity: str = Field("LOW", pattern="^(INFO|LOW|MEDIUM|HIGH|CRITICAL)$")
    attributes: dict[str, Any] = Field(default_factory=dict)
    correlation_id: Optional[str] = Field(None, max_length=128)


class SecurityEventCreate(SecurityEventBase):
    occurrence_time: Optional[datetime] = None


class SecurityEventResponse(SecurityEventBase):
    id: UUID
    occurrence_time: datetime
    ingestion_time: datetime
    dedup_hash: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class SecurityEventFilter(BaseModel):
    event_type: Optional[str] = None
    source: Optional[str] = None
    actor_id: Optional[str] = None
    resource_id: Optional[str] = None
    severity: Optional[str] = None
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    correlation_id: Optional[str] = None
    limit: int = Field(50, ge=1, le=500)
    offset: int = Field(0, ge=0)


# ============================================================================
# Detection Rule Schemas
# ============================================================================

class RuleConfigThreshold(BaseModel):
    """
    Safe deterministic configuration schema for threshold and window-based rules.
    Never allows arbitrary expressions or eval.
    """
    event_type: str = Field(..., min_length=2, max_length=100)
    threshold: int = Field(..., ge=1, le=10000)
    window_seconds: int = Field(..., ge=10, le=86400)
    group_by: Optional[str] = Field("actor_id", pattern="^(actor_id|source|resource_id|correlation_id|none)$")
    match_attributes: dict[str, Any] = Field(default_factory=dict)


class DetectionRuleBase(BaseModel):
    name: str = Field(..., min_length=3, max_length=150)
    description: str = Field(..., min_length=5, max_length=500)
    rule_type: str = Field("threshold", pattern="^(threshold|aggregation)$")
    is_enabled: bool = True
    severity: str = Field("MEDIUM", pattern="^(INFO|LOW|MEDIUM|HIGH|CRITICAL)$")
    mitre_attack_id: Optional[str] = Field(None, max_length=50)
    mitre_atlas_id: Optional[str] = Field(None, max_length=50)


class DetectionRuleCreate(DetectionRuleBase):
    config: dict[str, Any]

    @field_validator("config")
    @classmethod
    def validate_rule_config(cls, v: dict[str, Any]) -> dict[str, Any]:
        RuleConfigThreshold(**v)
        return v


class DetectionRuleUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=3, max_length=150)
    description: Optional[str] = Field(None, min_length=5, max_length=500)
    is_enabled: Optional[bool] = None
    severity: Optional[str] = Field(None, pattern="^(INFO|LOW|MEDIUM|HIGH|CRITICAL)$")
    config: Optional[dict[str, Any]] = None
    mitre_attack_id: Optional[str] = Field(None, max_length=50)
    mitre_atlas_id: Optional[str] = Field(None, max_length=50)

    @field_validator("config")
    @classmethod
    def validate_rule_config(cls, v: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
        if v is not None:
            RuleConfigThreshold(**v)
        return v


class DetectionRuleResponse(DetectionRuleBase):
    id: UUID
    version: int
    config: dict[str, Any]
    created_by: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class DetectionRuleTestRequest(BaseModel):
    window_seconds: Optional[int] = Field(None, ge=10, le=86400)


class DetectionRuleTestResponse(BaseModel):
    rule_id: UUID
    matches_found: int
    candidate_alerts: list[dict[str, Any]]
    evaluation_window_seconds: int
    duration_ms: float


# ============================================================================
# Alert Schemas
# ============================================================================

class AlertBase(BaseModel):
    title: str = Field(..., min_length=3, max_length=200)
    description: str
    severity: str = Field(..., pattern="^(INFO|LOW|MEDIUM|HIGH|CRITICAL)$")
    status: str = Field("OPEN", pattern="^(OPEN|INVESTIGATING|RESOLVED|FALSE_POSITIVE)$")
    detection_source: str = Field("rule_engine", max_length=100)
    mitre_attack_id: Optional[str] = None
    mitre_atlas_id: Optional[str] = None


class AlertUpdate(BaseModel):
    status: Optional[str] = Field(None, pattern="^(OPEN|INVESTIGATING|RESOLVED|FALSE_POSITIVE)$")
    severity: Optional[str] = Field(None, pattern="^(INFO|LOW|MEDIUM|HIGH|CRITICAL)$")
    assignee_id: Optional[UUID] = None
    assigned_to: Optional[str] = None
    resolution_reason: Optional[str] = Field(None, max_length=500)
    resolution_notes: Optional[str] = Field(None, max_length=500)
    notes: Optional[str] = Field(None, max_length=1000)


class AlertHistoryResponse(BaseModel):
    id: UUID
    actor_id: Optional[str] = None
    previous_status: Optional[str] = None
    new_status: str
    notes: Optional[str] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class AlertResponse(AlertBase):
    id: UUID
    rule_id: Optional[UUID] = None
    rule_version: Optional[int] = None
    first_seen: datetime
    last_seen: datetime
    event_count: int
    supporting_evidence: dict[str, Any]
    dedup_key: Optional[str] = None
    assignee_id: Optional[UUID] = None
    resolution_reason: Optional[str] = None
    resolved_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class AlertDetailResponse(AlertResponse):
    history: list[AlertHistoryResponse] = Field(default_factory=list)


class AlertFilter(BaseModel):
    status: Optional[str] = None
    severity: Optional[str] = None
    rule_id: Optional[UUID] = None
    detection_source: Optional[str] = None
    assignee_id: Optional[UUID] = None
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    limit: int = Field(50, ge=1, le=200)
    offset: int = Field(0, ge=0)


# ============================================================================
# Hunt Job Schemas
# ============================================================================

class HuntDefinition(BaseModel):
    """
    Structured query definition for historical hunts — prevents arbitrary SQL injection.
    """
    event_types: Optional[list[str]] = None
    sources: Optional[list[str]] = None
    actor_id: Optional[str] = None
    resource_id: Optional[str] = None
    severities: Optional[list[str]] = None
    start_time: datetime
    end_time: datetime
    attribute_filters: dict[str, Any] = Field(default_factory=dict)
    limit: int = Field(100, ge=1, le=1000)

    @model_validator(mode="after")
    def validate_time_range(self) -> "HuntDefinition":
        st = self.start_time
        et = self.end_time
        if st.tzinfo is None:
            st = st.replace(tzinfo=timezone.utc)
        if et.tzinfo is None:
            et = et.replace(tzinfo=timezone.utc)
        if st > et:
            raise ValueError("start_time cannot be after end_time")
        return self


class HuntJobCreate(BaseModel):
    name: str = Field(..., min_length=3, max_length=150)
    hunt_definition: HuntDefinition


class HuntJobResponse(BaseModel):
    id: UUID
    name: str
    status: str
    hunt_definition: dict[str, Any]
    requested_by: Optional[UUID] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    result_count: int
    error_message: Optional[str] = None
    results_summary: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class HuntJobResultResponse(BaseModel):
    hunt_id: UUID
    status: str
    result_count: int
    events: list[SecurityEventResponse]
    summary: dict[str, Any]
