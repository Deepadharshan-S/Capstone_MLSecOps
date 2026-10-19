"""
Drift Detection API (D1 data drift, D2 prediction drift, D3 confidence drift).

Reads reuse the existing alert/event machinery: findings are stored as alerts
with `detection_source="drift_engine"`, so there is no separate findings table
to keep in sync.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Security, status
from sqlalchemy.orm import Session

from app.api.permissions import get_current_user
from app.db.session import get_db
from app.models.user import User
from app.schemas.drift import (
    BaselineCreate,
    BaselineResponse,
    DriftCheckRequest,
    DriftCheckResponse,
    DriftConfigResponse,
    DriftFindingItem,
)
from app.schemas.threat_hunting import AlertFilter
from app.services.threat_hunting.alert_service import alert_service

logger = logging.getLogger("drift.api")

router = APIRouter(tags=["drift"])

# Safety bound when findings must be filtered in Python rather than in SQL.
MAX_FINDINGS_SCAN = 2000


def _service(db: Session):
    from app.services.drift.service import DriftService, DETECTION_SOURCE

    return DriftService(db), DETECTION_SOURCE


# ============================================================================
# Configuration
# ============================================================================
@router.get(
    "/drift/config",
    response_model=DriftConfigResponse,
    summary="Drift detector configuration",
)
def get_drift_config(
    db: Session = Depends(get_db),
    current_user: User = Security(get_current_user, scopes=["alerts:view"]),
):
    """Thresholds and window sizes, so the UI can explain a skipped check."""
    service, detection_source = _service(db)
    s = service.settings
    from app.services.drift.findings import DETECTION_TYPES

    return DriftConfigResponse(
        enabled=bool(getattr(s, "DRIFT_DETECTOR_ENABLED", True)),
        sweep_interval_seconds=int(getattr(s, "DRIFT_SWEEP_INTERVAL_SECONDS", 300)),
        observation_window_seconds=int(getattr(s, "DRIFT_OBSERVATION_WINDOW_SECONDS", 3600)),
        min_observations=int(getattr(s, "DRIFT_MIN_OBSERVATIONS", 50)),
        thresholds=service.thresholds,
        telemetry_retention_days=int(getattr(s, "DRIFT_TELEMETRY_RETENTION_DAYS", 30)),
        detection_source=detection_source,
        available_detections=list(DETECTION_TYPES),
    )


# ============================================================================
# Findings
# ============================================================================
def _enrich(alert) -> dict[str, Any]:
    """Flatten the finding evidence stored on the alert."""
    evidence = alert.supporting_evidence or {}
    if isinstance(evidence, str):
        try:
            evidence = json.loads(evidence)
        except Exception:
            evidence = {}
    return {
        "alert_id": alert.id,
        "title": alert.title,
        "summary": alert.description,
        "severity": alert.severity,
        "status": alert.status,
        "detection_type": evidence.get("detection_type", "unknown"),
        "model_name": evidence.get("model_name"),
        "model_version": evidence.get("model_version"),
        "affected_features": evidence.get("affected_features") or [],
        "observation_start": evidence.get("observation_start"),
        "observation_end": evidence.get("observation_end"),
        "limitations": evidence.get("limitations") or [],
        # Sample counts and thresholds are what make a finding auditable —
        # without them a reader cannot tell how much evidence stands behind it.
        "observation_samples": evidence.get("observation_samples"),
        "baseline_samples": evidence.get("baseline_samples"),
        "baseline_dataset_ref": evidence.get("baseline_dataset_ref"),
        "measurements": evidence.get("measurements") or {},
        "thresholds": evidence.get("thresholds") or {},
        "event_count": alert.event_count,
        "first_seen": alert.first_seen,
        "last_seen": alert.last_seen,
    }


@router.get(
    "/drift/findings",
    summary="List drift findings with filtering and pagination",
)
def list_findings(
    detection_type: Optional[str] = Query(None, description="data_drift | prediction_drift | confidence_drift"),
    model_name: Optional[str] = None,
    severity: Optional[str] = None,
    status_filter: Optional[str] = Query(None, alias="status"),
    limit: int = Query(25, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Security(get_current_user, scopes=["alerts:view"]),
):
    """
    Drift findings surfaced through their correlated alerts.

    Severity and status filter in SQL; model and detection type are matched
    against the stored evidence, so they are applied after retrieval. The set
    is small because each drift signature dedups into a single open alert.
    """
    from app.services.drift.service import DETECTION_SOURCE

    needs_python_filter = bool(model_name or detection_type)

    if needs_python_filter:
        filters = AlertFilter(
            severity=severity,
            status=status_filter,
            detection_source=DETECTION_SOURCE,
            limit=MAX_FINDINGS_SCAN,
            offset=0,
        )
        rows, _ = alert_service.list_alerts(db, filters)
        items = [_enrich(a) for a in rows]
        if model_name:
            items = [i for i in items if i["model_name"] == model_name]
        if detection_type:
            items = [i for i in items if i["detection_type"] == detection_type]
        total = len(items)
        items = items[offset : offset + limit]
    else:
        filters = AlertFilter(
            severity=severity,
            status=status_filter,
            detection_source=DETECTION_SOURCE,
            limit=limit,
            offset=offset,
        )
        rows, total = alert_service.list_alerts(db, filters)
        items = [_enrich(a) for a in rows]

    return {"items": items, "total": total, "limit": limit, "offset": offset}


# ============================================================================
# Baselines
# ============================================================================
def _baseline_to_response(baseline) -> BaselineResponse:
    feature_stats = baseline.feature_stats or {}
    return BaselineResponse(
        id=baseline.id,
        model_name=baseline.model_name,
        model_version=baseline.model_version,
        dataset_id=baseline.dataset_id,
        dataset_ref=baseline.dataset_ref,
        sample_count=baseline.sample_count,
        schema_hash=baseline.schema_hash,
        status=baseline.status,
        reference_metrics=baseline.reference_metrics,
        created_by=baseline.created_by,
        created_at=baseline.created_at,
        feature_names=sorted(feature_stats.keys()),
        has_prediction_dist=bool(baseline.prediction_dist),
        has_confidence_stats=bool(baseline.confidence_stats),
    )


@router.get("/drift/baselines", summary="List drift baselines")
def list_baselines(
    model_name: Optional[str] = None,
    model_version: Optional[str] = None,
    status_filter: Optional[str] = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Security(get_current_user, scopes=["alerts:view"]),
):
    from app.repositories.baseline_repository import BaselineRepository

    repo = BaselineRepository(db)
    rows = repo.list(
        model_name=model_name,
        model_version=model_version,
        status=status_filter,
        limit=limit,
        offset=offset,
    )
    total = repo.count(model_name=model_name, status=status_filter)
    return {
        "items": [_baseline_to_response(b) for b in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get(
    "/drift/baselines/detail",
    summary="Full baseline statistics for one model version",
)
def get_baseline_detail(
    model_name: str,
    model_version: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Security(get_current_user, scopes=["alerts:view"]),
):
    """Per-feature reference statistics — the numbers a finding was compared to."""
    from app.repositories.baseline_repository import BaselineRepository

    baseline = BaselineRepository(db).get_active(model_name, model_version)
    if baseline is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No active baseline for '{model_name}'.",
        )
    return {
        "id": baseline.id,
        "model_name": baseline.model_name,
        "model_version": baseline.model_version,
        "dataset_id": baseline.dataset_id,
        "dataset_ref": baseline.dataset_ref,
        "sample_count": baseline.sample_count,
        "schema_hash": baseline.schema_hash,
        "status": baseline.status,
        "reference_metrics": baseline.reference_metrics,
        "feature_stats": baseline.feature_stats or {},
        "prediction_dist": baseline.prediction_dist,
        "confidence_stats": baseline.confidence_stats,
        "created_at": baseline.created_at,
    }


@router.post(
    "/drift/baselines",
    response_model=BaselineResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Compute and store a baseline from the training dataset",
)
def create_baseline(
    body: BaselineCreate,
    db: Session = Depends(get_db),
    current_user: User = Security(get_current_user, scopes=["rules:manage"]),
):
    """
    Builds reference statistics from the training dataset in lakeFS and stores
    them for this exact model version.

    A baseline must be tied to one version — otherwise a deployment change
    would be attributed to a data change.
    """
    from app.services.drift.service import DriftService

    try:
        baseline = DriftService(db).build_baseline_for_model(
            body.model_name,
            body.model_version,
            dataset_id=body.dataset_id,
            data_path=body.data_path,
            created_by=getattr(current_user, "username", None),
        )
    except ValueError as err:
        # Typically an unreachable lakeFS or an unresolvable dataset — report
        # the reason rather than a stack trace.
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(err))
    except Exception as err:
        logger.error(f"Baseline build failed for {body.model_name}: {err}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Baseline computation failed: {err}",
        )

    return _baseline_to_response(baseline)


# ============================================================================
# Manual check
# ============================================================================
@router.post(
    "/drift/check",
    response_model=DriftCheckResponse,
    summary="Run a drift check now for one model",
)
def run_drift_check(
    body: DriftCheckRequest,
    db: Session = Depends(get_db),
    current_user: User = Security(get_current_user, scopes=["rules:manage"]),
):
    """
    Executes D1/D2/D3 over the observation window immediately.

    Detectors that declined to fire report why — no baseline, no telemetry, no
    probabilities, or too few observations — so an operator can tell the
    difference between "clean" and "not measured".
    """
    from app.services.drift.service import DriftService

    try:
        report = DriftService(db).run_drift_check(
            body.model_name,
            body.model_version,
            body.window_seconds,
            publish=body.publish,
        )
    except Exception as err:
        logger.error(f"Manual drift check failed for {body.model_name}: {err}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Drift check failed: {err}",
        )
    return DriftCheckResponse(**report)


# ============================================================================
# Telemetry inspection
# ============================================================================
@router.get(
    "/drift/telemetry",
    summary="Inspect captured inference telemetry",
)
def list_telemetry(
    model_name: Optional[str] = None,
    model_version: Optional[str] = None,
    limit: int = Query(25, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Security(get_current_user, scopes=["alerts:view"]),
):
    """Raw per-request rows the detectors measure against — for verifying that
    telemetry is actually being captured for a model."""
    from app.repositories.inference_repository import InferenceRepository

    repo = InferenceRepository(db)
    rows = repo.list(limit=limit, offset=offset, model_name=model_name, model_version=model_version)
    total = repo.count(model_name=model_name)

    items = []
    for r in rows:
        preds = r.prediction
        if isinstance(preds, list) and len(preds) > 5:
            preview = preds[:5]
        else:
            preview = preds
        items.append(
            {
                "id": r.id,
                "model_name": r.model_name,
                "model_version": r.model_version,
                "deployment_id": r.deployment_id,
                "prediction": preview,
                "has_probabilities": r.has_probabilities,
                "confidence": r.confidence,
                "latency_ms": r.latency_ms,
                "feature_count": r.feature_count,
                "features_truncated": r.features_truncated,
                "recorded_at": r.recorded_at,
            }
        )

    return {"items": items, "total": total, "limit": limit, "offset": offset}
