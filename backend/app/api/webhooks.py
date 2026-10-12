import hmac
import io
import logging
from typing import Optional

import pandas as pd
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.logging_config import log_audit_event
from app.db.session import get_db
from app.schemas.threat_hunting import SecurityEventCreate
from app.services.dataset.lakefs_service import lakefs_service
from app.services.dataset.poisoning_detector import poisoning_detector
from app.services.threat_hunting.event_service import event_service

logger = logging.getLogger("webhooks.lakefs")

router = APIRouter(prefix="/webhooks", tags=["threat_hunting"])


class LakeFSWebhookPayload(BaseModel):
    event_type: str
    event_time: Optional[str] = None
    action_name: Optional[str] = None
    hook_id: Optional[str] = None
    repository_id: str
    branch_id: str
    source_ref: Optional[str] = None
    commit_message: Optional[str] = None
    commit_id: Optional[str] = None
    committer: Optional[str] = None
    commit_metadata: Optional[dict] = None
    merge_source: Optional[str] = None


@router.post(
    "/lakefs/data-validation",
    summary="lakeFS pre-merge statistical data poisoning gate webhook",
)
def lakefs_data_validation_webhook(
    payload: LakeFSWebhookPayload,
    request: Request,
    db: Session = Depends(get_db),
    x_webhook_secret: Optional[str] = Header(None, alias="X-Webhook-Secret"),
    authorization: Optional[str] = Header(None, alias="Authorization"),
    secret: Optional[str] = Query(None),
):
    """
    Synchronous lakeFS pre-merge action gate:
    1. Authenticates lakeFS webhook request using shared secret header, bearer token, or query parameter.
    2. Enforces resource protection bounds (max file size and sampling row caps).
    3. Validates that the merge targets the protected baseline branch ('main').
    4. Evaluates every CSV/Parquet tabular file in the candidate commit against baseline.
    5. Performs strict schema consistency validation (missing columns, unvetted new columns, incompatible dtypes).
    6. Runs SciPy KS-tests (with Bonferroni correction and D-threshold), Chi-Square / Fisher / Monte Carlo
       label homogeneity contingency tests, and PSI checks.
    7. On pass: returns HTTP 200, allowing lakeFS to commit the merge into main.
    8. On fail: emits a High-Severity SecurityEvent ('dataset.poisoning_suspected') to the Threat Hunting
       pipeline and returns HTTP 422, causing lakeFS to synchronously abort the merge (fail-closed).
    9. On infrastructure failure (DB down / lakeFS error): returns HTTP 500 to ensure fail-closed enforcement.
    """
    try:
        from app.core.config import settings

        # =======================================================================
        # 1. SEC-01: Webhook Authentication
        # =======================================================================
        if getattr(settings, "POISONING_DETECTION_ENABLED", True):
            expected_secret = getattr(settings, "LAKEFS_WEBHOOK_SECRET", "")
            if not expected_secret:
                logger.error("Security failure: POISONING_DETECTION_ENABLED is True but LAKEFS_WEBHOOK_SECRET is empty.")
                return JSONResponse(
                    status_code=500,
                    content={"status": "error", "reason": "misconfigured_security_secret"},
                )

            allow_query_auth = getattr(settings, "POISONING_ALLOW_QUERY_PARAM_AUTH", False)

            provided_secret = x_webhook_secret
            if not provided_secret and authorization:
                if authorization.lower().startswith("bearer "):
                    provided_secret = authorization[7:].strip()
                else:
                    provided_secret = authorization.strip()

            # Reject query-string secret unless explicitly permitted by configuration
            if not provided_secret and secret:
                if allow_query_auth:
                    provided_secret = secret
                else:
                    logger.warning("Rejected query-parameter secret because POISONING_ALLOW_QUERY_PARAM_AUTH is false.")
                    return JSONResponse(
                        status_code=401,
                        content={
                            "status": "unauthorized",
                            "reason": "query_param_auth_disabled",
                            "detail": "Query parameter authentication is disabled; use X-Webhook-Secret header.",
                        },
                    )

            if not provided_secret or not hmac.compare_digest(str(provided_secret), str(expected_secret)):
                logger.warning("Unauthorized access attempt to lakeFS data-validation webhook (invalid or missing secret).")
                return JSONResponse(
                    status_code=401,
                    content={"status": "unauthorized", "reason": "invalid_or_missing_webhook_secret"},
                )

        repo_name = payload.repository_id
        target_branch = payload.branch_id
        source_ref = payload.source_ref or payload.merge_source

        # Only enforce on merges targeting main
        if target_branch != "main":
            return JSONResponse(status_code=200, content={"status": "skipped", "reason": "not_main_branch"})

        if not source_ref:
            return JSONResponse(status_code=400, content={"status": "error", "reason": "missing_source_ref"})

        if not getattr(settings, "POISONING_DETECTION_ENABLED", True):
            logger.info("Statistical poisoning detection gate is disabled via configuration.")
            return JSONResponse(status_code=200, content={"status": "approved", "reason": "poisoning_gate_disabled"})

        # =======================================================================
        # 2. CORR-02: Complete Multi-File Dataset Discovery
        # =======================================================================
        candidate_files = [
            f
            for f in lakefs_service.list_files(repo_name, ref_id=source_ref)
            if f["path"].endswith((".csv", ".parquet")) and not f["path"].startswith(("_", "."))
        ]

        if not candidate_files:
            # No data file changes (e.g. metadata only or empty merge)
            return JSONResponse(status_code=200, content={"status": "approved", "reason": "no_data_files"})

        max_file_bytes = getattr(settings, "POISONING_MAX_FILE_SIZE_BYTES", 50 * 1024 * 1024)
        allow_new_dataset_files = getattr(settings, "POISONING_ALLOW_NEW_DATASET_FILES", False)

        all_violations = []
        files_evaluated = []

        # Check existing baseline files on target_branch (main)
        try:
            baseline_existing_files = [
                f["path"]
                for f in lakefs_service.list_files(repo_name, ref_id=target_branch)
                if f["path"].endswith((".csv", ".parquet")) and not f["path"].startswith(("_", "."))
            ]
        except Exception:
            baseline_existing_files = []

        for data_file_entry in candidate_files:
            data_path = data_file_entry["path"]
            file_size = data_file_entry.get("size_bytes")

            # ===================================================================
            # 3. CORR-01: Resource Protection / File Size Bounds
            # ===================================================================
            if file_size is not None and file_size > max_file_bytes:
                all_violations.append(
                    {
                        "feature": data_path,
                        "test": "file_size_check",
                        "statistic": float(file_size),
                        "p_value": None,
                        "threshold": float(max_file_bytes),
                        "message": (
                            f"File '{data_path}' size ({file_size} bytes) exceeds maximum "
                            f"permitted limit ({max_file_bytes} bytes)."
                        ),
                    }
                )
                continue

            # Download candidate data
            candidate_bytes = lakefs_service.download_file(repo_name, ref_id=source_ref, file_path=data_path)
            if len(candidate_bytes) > max_file_bytes:
                all_violations.append(
                    {
                        "feature": data_path,
                        "test": "file_size_check",
                        "statistic": float(len(candidate_bytes)),
                        "p_value": None,
                        "threshold": float(max_file_bytes),
                        "message": (
                            f"File '{data_path}' downloaded size ({len(candidate_bytes)} bytes) exceeds "
                            f"maximum permitted limit ({max_file_bytes} bytes)."
                        ),
                    }
                )
                continue

            # Download baseline data from target branch (main)
            baseline_bytes = None
            try:
                baseline_bytes = lakefs_service.download_file(repo_name, ref_id=target_branch, file_path=data_path)
            except Exception as base_err:
                logger.info(f"Baseline file '{data_path}' not found on '{target_branch}' ({base_err}).")

            if not baseline_bytes:
                if not baseline_existing_files:
                    # First version of this repository on main -> approve initial baseline
                    files_evaluated.append({"path": data_path, "status": "initial_baseline"})
                    continue
                else:
                    # Repository has baseline files, but this specific file is newly added
                    if not allow_new_dataset_files:
                        all_violations.append(
                            {
                                "feature": data_path,
                                "test": "unbaselined_new_file",
                                "statistic": 1.0,
                                "p_value": None,
                                "threshold": 0.0,
                                "message": (
                                    f"Candidate commit introduces new unbaselined dataset file '{data_path}' "
                                    f"into initialized repository with existing baseline."
                                ),
                            }
                        )
                        continue
                    else:
                        files_evaluated.append({"path": data_path, "status": "new_file_allowed"})
                        continue

            # Parse DataFrames
            if data_path.endswith(".csv"):
                baseline_df = pd.read_csv(io.BytesIO(baseline_bytes))
                candidate_df = pd.read_csv(io.BytesIO(candidate_bytes))
            else:
                baseline_df = pd.read_parquet(io.BytesIO(baseline_bytes))
                candidate_df = pd.read_parquet(io.BytesIO(candidate_bytes))

            # ===================================================================
            # 4. CORR-03 & STAT-01: Schema & Statistical Validation
            # ===================================================================
            val_res = poisoning_detector.validate_datasets(baseline_df, candidate_df)
            files_evaluated.append({"path": data_path, "summary": val_res.summary, "passed": val_res.passed})

            if not val_res.passed:
                for v in val_res.violations:
                    all_violations.append(
                        {
                            "feature": f"{data_path}:{v.feature}",
                            "test": v.test_type,
                            "statistic": v.statistic,
                            "p_value": v.p_value,
                            "threshold": v.threshold,
                            "message": v.message,
                        }
                    )

        # Gating Decision
        if not all_violations:
            try:
                log_audit_event(
                    action="dataset_validation_passed",
                    username=payload.committer or "lakefs_hook",
                    details=f"Statistical data validation passed for merge into '{target_branch}' on repo '{repo_name}'. Files evaluated: {len(files_evaluated)}.",
                    db=db,
                )
            except Exception as audit_err:
                logger.warning(f"Could not log validation passed audit: {audit_err}")

            return JSONResponse(
                status_code=200,
                content={"status": "approved", "files_evaluated": files_evaluated},
            )

        # Gating failed: statistical distribution shift / suspected data poisoning / schema violation
        # 1. Log security audit event (wrapped to ensure failure does not approve merge)
        try:
            log_audit_event(
                action="dataset_poisoning_detected",
                username=payload.committer or "lakefs_hook",
                details=(
                    f"Statistical data poisoning / distribution shift detected on repository '{repo_name}' "
                    f"merging '{source_ref}' into '{target_branch}'. Violations count: {len(all_violations)}."
                ),
                db=db,
            )
        except Exception as audit_err:
            logger.error(f"Failed to record audit event for poisoning: {audit_err}")

        # 2. Ingest structured SecurityEvent into Threat Hunting pipeline (INT-01 Idempotent)
        try:
            sec_event = event_service.ingest_event(
                db,
                SecurityEventCreate(
                    event_type="dataset.poisoning_suspected",
                    source="lakefs_validator",
                    actor_id=payload.committer or "lakefs_hook",
                    resource_type="dataset",
                    resource_id=repo_name,
                    severity="HIGH",
                    attributes={
                        "repository": repo_name,
                        "target_branch": target_branch,
                        "source_ref": source_ref,
                        "commit_id": payload.commit_id,
                        "violations": all_violations,
                        "files_evaluated": files_evaluated,
                    },
                    correlation_id=payload.commit_id,
                ),
            )
            # 2b. Correlate detection immediately to generate active alert
            try:
                from app.services.threat_hunting.alert_service import alert_service
                from app.services.threat_hunting.detection_engine import detection_engine

                detections = detection_engine.evaluate_all_rules(db)
                for d in detections:
                    if d.resource_id == repo_name or (
                        sec_event and any(ev_id == sec_event.id for ev_id in d.event_ids)
                    ):
                        alert_service.process_detection(db, d)
            except Exception as alert_err:
                logger.error(f"Failed to generate immediate alert for poisoning detection: {alert_err}")
        except Exception as ev_err:
            logger.error(f"Failed to record SecurityEvent for poisoning detection: {ev_err}")

        # 3. Synchronously return HTTP 422 to cause lakeFS to abort the merge
        return JSONResponse(
            status_code=422,
            content={
                "status": "rejected",
                "reason": "statistical_or_schema_violation_detected",
                "violations_count": len(all_violations),
                "violations": all_violations,
            },
        )

    except Exception as e:
        logger.error(f"Critical error executing lakeFS data validation webhook: {e}", exc_info=True)
        # Fail-closed: return 500 so lakeFS blocks the merge if validation fails unexpectedly
        return JSONResponse(
            status_code=500,
            content={"status": "error", "detail": f"Validator execution failure: {str(e)}"},
        )
