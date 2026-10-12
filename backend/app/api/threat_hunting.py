from datetime import datetime
from typing import Annotated, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Security, status
from sqlalchemy.orm import Session

from app.api.permissions import get_current_user
from app.db.session import get_db
from app.models.user import User
from app.schemas.threat_hunting import (
    AlertDetailResponse,
    AlertFilter,
    AlertResponse,
    AlertUpdate,
    DetectionRuleCreate,
    DetectionRuleResponse,
    DetectionRuleTestRequest,
    DetectionRuleTestResponse,
    DetectionRuleUpdate,
    HuntJobCreate,
    HuntJobResponse,
    HuntJobResultResponse,
    SecurityEventCreate,
    SecurityEventFilter,
    SecurityEventResponse,
)
from app.services.threat_hunting.alert_service import alert_service
from app.services.threat_hunting.event_service import event_service
from app.services.threat_hunting.hunt_service import hunt_service
from app.services.threat_hunting.rule_service import rule_service

router = APIRouter(tags=["threat_hunting"])


# ============================================================================
# Alerts API
# ============================================================================

@router.get(
    "/alerts",
    response_model=dict,
    summary="List security alerts with filtering and pagination",
)
def list_alerts(
    status_filter: Optional[str] = Query(None, alias="status"),
    severity: Optional[str] = None,
    rule_id: Optional[UUID] = None,
    detection_source: Optional[str] = None,
    assignee_id: Optional[UUID] = None,
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Security(get_current_user, scopes=["alerts:view"]),
):
    filters = AlertFilter(
        status=status_filter,
        severity=severity,
        rule_id=rule_id,
        detection_source=detection_source,
        assignee_id=assignee_id,
        start_time=start_time,
        end_time=end_time,
        limit=limit,
        offset=offset,
    )
    alerts, total = alert_service.list_alerts(db, filters)
    return {
        "items": [AlertResponse.model_validate(a) for a in alerts],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get(
    "/alerts/{alert_id}",
    response_model=AlertDetailResponse,
    summary="Get alert details with investigation history",
)
def get_alert(
    alert_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Security(get_current_user, scopes=["alerts:view"]),
):
    alert = alert_service.get_alert_by_id(db, alert_id)
    if not alert:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Alert {alert_id} not found",
        )
    return AlertDetailResponse.model_validate(alert)


@router.patch(
    "/alerts/{alert_id}",
    response_model=AlertDetailResponse,
    summary="Update alert status, assignment, severity, or add investigation notes",
)
def update_alert(
    alert_id: UUID,
    update_in: AlertUpdate,
    db: Session = Depends(get_db),
    current_user: User = Security(get_current_user, scopes=["alerts:manage"]),
):
    try:
        updated = alert_service.update_alert(
            db=db,
            alert_id=alert_id,
            update_in=update_in,
            actor_username=current_user.username,
        )
        return AlertDetailResponse.model_validate(updated)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.get(
    "/alerts/{alert_id}/events",
    response_model=dict,
    summary="Get security events associated with an alert",
)
def get_alert_events(
    alert_id: UUID,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Security(get_current_user, scopes=["alerts:view"]),
):
    alert = alert_service.get_alert_by_id(db, alert_id)
    if not alert:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Alert {alert_id} not found",
        )
    events = alert_service.get_alert_events(db, alert_id, limit=limit, offset=offset)
    return {
        "alert_id": alert_id,
        "items": [SecurityEventResponse.model_validate(e) for e in events],
        "limit": limit,
        "offset": offset,
    }


# ============================================================================
# Detection Rules API
# ============================================================================

@router.get(
    "/detection-rules",
    response_model=list[DetectionRuleResponse],
    summary="List all detection rules",
)
def list_detection_rules(
    is_enabled: Optional[bool] = None,
    db: Session = Depends(get_db),
    current_user: User = Security(get_current_user, scopes=["alerts:view"]),
):
    rules = rule_service.list_rules(db, is_enabled=is_enabled)
    return [DetectionRuleResponse.model_validate(r) for r in rules]


@router.post(
    "/detection-rules",
    response_model=DetectionRuleResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new detection rule",
)
def create_detection_rule(
    rule_in: DetectionRuleCreate,
    db: Session = Depends(get_db),
    current_user: User = Security(get_current_user, scopes=["rules:manage"]),
):
    try:
        rule = rule_service.create_rule(db, rule_in, creator=current_user.username)
        return DetectionRuleResponse.model_validate(rule)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.get(
    "/detection-rules/{rule_id}",
    response_model=DetectionRuleResponse,
    summary="Get detection rule details",
)
def get_detection_rule(
    rule_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Security(get_current_user, scopes=["alerts:view"]),
):
    rule = rule_service.get_rule(db, rule_id)
    if not rule:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Detection rule {rule_id} not found",
        )
    return DetectionRuleResponse.model_validate(rule)


@router.patch(
    "/detection-rules/{rule_id}",
    response_model=DetectionRuleResponse,
    summary="Update detection rule configuration or state",
)
def update_detection_rule(
    rule_id: UUID,
    rule_update: DetectionRuleUpdate,
    db: Session = Depends(get_db),
    current_user: User = Security(get_current_user, scopes=["rules:manage"]),
):
    try:
        updated = rule_service.update_rule(db, rule_id, rule_update)
        return DetectionRuleResponse.model_validate(updated)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.post(
    "/detection-rules/{rule_id}/test",
    response_model=DetectionRuleTestResponse,
    summary="Test rule against recent events without creating alerts",
)
def test_detection_rule(
    rule_id: UUID,
    test_req: Optional[DetectionRuleTestRequest] = None,
    db: Session = Depends(get_db),
    current_user: User = Security(get_current_user, scopes=["rules:manage"]),
):
    win = test_req.window_seconds if test_req else None
    try:
        return rule_service.test_rule(db, rule_id, window_seconds=win)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))


# ============================================================================
# Security Events API
# ============================================================================

@router.get(
    "/security-events",
    response_model=dict,
    summary="List normalized security events with filters and pagination",
)
def list_security_events(
    event_type: Optional[str] = None,
    source: Optional[str] = None,
    actor_id: Optional[str] = None,
    resource_id: Optional[str] = None,
    severity: Optional[str] = None,
    correlation_id: Optional[str] = None,
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Security(get_current_user, scopes=["events:view"]),
):
    filters = SecurityEventFilter(
        event_type=event_type,
        source=source,
        actor_id=actor_id,
        resource_id=resource_id,
        severity=severity,
        correlation_id=correlation_id,
        start_time=start_time,
        end_time=end_time,
        limit=limit,
        offset=offset,
    )
    events, total = event_service.list_events(db, filters)
    return {
        "items": [SecurityEventResponse.model_validate(e) for e in events],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.post(
    "/security-events",
    response_model=SecurityEventResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Ingest a security event directly with automatic normalization and redaction",
)
def ingest_security_event(
    event_in: SecurityEventCreate,
    db: Session = Depends(get_db),
    current_user: User = Security(get_current_user, scopes=["alerts:manage"]),
):
    event = event_service.ingest_event(db, event_in)
    return SecurityEventResponse.model_validate(event)


# ============================================================================
# Threat Hunts API
# ============================================================================

@router.post(
    "/hunts",
    response_model=HuntJobResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create and execute a historical threat hunting job",
)
def create_threat_hunt(
    hunt_in: HuntJobCreate,
    db: Session = Depends(get_db),
    current_user: User = Security(get_current_user, scopes=["hunts:execute"]),
):
    job = hunt_service.create_hunt_job(db, hunt_in, user_id=current_user.id)
    # Execute immediately
    executed_job = hunt_service.execute_hunt_job(db, job.id)
    return HuntJobResponse.model_validate(executed_job)


@router.get(
    "/hunts",
    response_model=dict,
    summary="List historical threat hunts",
)
def list_threat_hunts(
    status_filter: Optional[str] = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Security(get_current_user, scopes=["alerts:view"]),
):
    jobs, total = hunt_service.list_hunt_jobs(db, status=status_filter, limit=limit, offset=offset)
    return {
        "items": [HuntJobResponse.model_validate(j) for j in jobs],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get(
    "/hunts/{hunt_id}",
    response_model=HuntJobResponse,
    summary="Get threat hunt job status and summary",
)
def get_threat_hunt(
    hunt_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Security(get_current_user, scopes=["alerts:view"]),
):
    job = hunt_service.get_hunt_job(db, hunt_id)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Hunt job {hunt_id} not found",
        )
    return HuntJobResponse.model_validate(job)


@router.get(
    "/hunts/{hunt_id}/results",
    response_model=HuntJobResultResponse,
    summary="Get matched security events and summary for a threat hunt",
)
def get_threat_hunt_results(
    hunt_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Security(get_current_user, scopes=["alerts:view"]),
):
    try:
        job, events = hunt_service.get_hunt_results(db, hunt_id)
        return HuntJobResultResponse(
            hunt_id=job.id,
            status=job.status,
            result_count=len(events),
            events=[SecurityEventResponse.model_validate(e) for e in events],
            summary=job.results_summary or {},
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
