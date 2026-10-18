import hmac
import io
import logging
from typing import Any, Optional

import pandas as pd
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.logging_config import log_audit_event
from app.db.session import get_db
from app.models.dataset import Dataset
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

        dataset_rec = db.query(Dataset).filter(Dataset.name == repo_name).first()
        baseline_branch = (dataset_rec.default_branch if dataset_rec and dataset_rec.default_branch else None) or getattr(settings, "LAKEFS_DEFAULT_BRANCH", "main")

        # Only enforce on merges targeting the configured baseline branch
        if target_branch != baseline_branch:
            return JSONResponse(status_code=200, content={"status": "skipped", "reason": f"not_baseline_branch_{target_branch}"})

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

        if not dataset_rec:
            dataset_rec = db.query(Dataset).filter(Dataset.name == repo_name).first()
        meta_info = dataset_rec.metadata_info if dataset_rec and isinstance(dataset_rec.metadata_info, dict) else {}
        target_col = meta_info.get("target_column")

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

            # Inspect raw file headers prior to DataFrame parsing to detect header tampering
            # (e.g. duplicate columns, whitespace padding, control characters) before parser normalization
            raw_cand_headers = None
            try:
                if data_path.endswith(".csv"):
                    raw_cand_headers = poisoning_detector.inspect_raw_csv_headers(candidate_bytes)
                else:
                    raw_cand_headers = poisoning_detector.inspect_raw_parquet_headers(candidate_bytes)
            except Exception as header_err:
                logger.warning(f"Failed to inspect raw headers for '{data_path}': {header_err}")

            # Parse DataFrames safely (fail-closed if corrupt/malformed)
            try:
                if data_path.endswith(".csv"):
                    baseline_df = pd.read_csv(io.BytesIO(baseline_bytes))
                    candidate_df = pd.read_csv(io.BytesIO(candidate_bytes))
                else:
                    baseline_df = pd.read_parquet(io.BytesIO(baseline_bytes))
                    candidate_df = pd.read_parquet(io.BytesIO(candidate_bytes))
            except Exception as parse_err:
                all_violations.append(
                    {
                        "feature": data_path,
                        "test": "malformed_schema",
                        "statistic": 1.0,
                        "p_value": None,
                        "threshold": 0.0,
                        "message": f"Candidate tabular file '{data_path}' is malformed or unparseable: {str(parse_err)}",
                    }
                )
                continue

            # ===================================================================
            # 4. CORR-03 & STAT-01: Schema & Statistical Validation
            # ===================================================================
            val_res = poisoning_detector.validate_datasets(
                baseline_df,
                candidate_df,
                target_column=target_col,
                raw_candidate_headers=raw_cand_headers,
            )
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
        schema_test_types = {
            "schema_validation",
            "type_incompatibility",
            "target_schema_tampering",
            "unbaselined_new_file",
            "empty_feature_set",
            "malformed_schema",
        }
        schema_violations = [
            v for v in all_violations
            if v.get("test") in schema_test_types or v.get("test_type") in schema_test_types
        ]
        statistical_violations = [
            v for v in all_violations
            if v.get("test") in ("ks_2samp", "psi", "chi2_contingency", "fisher_exact", "chi2_contingency_monte_carlo", "file_size_check")
        ]

        from app.services.threat_hunting.alert_service import alert_service
        from app.services.threat_hunting.detection_engine import detection_engine

        # 1. If schema violations exist, log audit & ingest dataset.schema_injection SecurityEvent (P2)
        if schema_violations:
            try:
                log_audit_event(
                    action="dataset_schema_injection_detected",
                    username=payload.committer or "lakefs_hook",
                    details=(
                        f"Tabular schema injection / structural anomaly detected on repository '{repo_name}' "
                        f"merging '{source_ref}' into '{target_branch}'. Schema violations: {len(schema_violations)}."
                    ),
                    db=db,
                )
            except Exception as audit_err:
                logger.error(f"Failed to record audit event for schema injection: {audit_err}")

            try:
                sec_event_schema = event_service.ingest_event(
                    db,
                    SecurityEventCreate(
                        event_type="dataset.schema_injection",
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
                            "violations": schema_violations,
                            "schema_diff": schema_violations,
                            "detection_reason": "Structural schema violation or column injection detected during pre-merge.",
                            "files_evaluated": files_evaluated,
                        },
                        correlation_id=payload.commit_id,
                    ),
                )
                if sec_event_schema:
                    detections = detection_engine.evaluate_all_rules(db)
                    for d in detections:
                        if d.resource_id == repo_name or any(ev_id == sec_event_schema.id for ev_id in d.event_ids):
                            alert_service.process_detection(db, d)
            except Exception as ev_err:
                logger.error(f"Failed to record SecurityEvent for schema injection: {ev_err}")

        # 2. If statistical drift or general poisoning violations exist, log audit & ingest dataset.poisoning_suspected (Phase 1)
        if statistical_violations or not schema_violations:
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

            try:
                sec_event_poison = event_service.ingest_event(
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
                if sec_event_poison:
                    detections = detection_engine.evaluate_all_rules(db)
                    for d in detections:
                        if d.resource_id == repo_name or any(ev_id == sec_event_poison.id for ev_id in d.event_ids):
                            alert_service.process_detection(db, d)
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


# ===============================================================================
# MinIO S3 Bucket Notification Schemas & Webhook Endpoint (P1)
# ===============================================================================

class S3ObjectMetadata(BaseModel):
    key: str
    size: Optional[int] = None
    eTag: Optional[str] = None
    versionId: Optional[str] = None
    sequencer: Optional[str] = None
    userMetadata: Optional[dict[str, Any]] = None


class S3BucketMetadata(BaseModel):
    name: str
    ownerIdentity: Optional[dict[str, Any]] = None
    arn: Optional[str] = None


class S3EventData(BaseModel):
    s3SchemaVersion: Optional[str] = None
    configurationId: Optional[str] = None
    bucket: S3BucketMetadata
    object: S3ObjectMetadata


class MinIOIdentity(BaseModel):
    principalId: Optional[str] = None


class MinIORequestParameters(BaseModel):
    principalId: Optional[str] = None
    region: Optional[str] = None
    sourceIPAddress: Optional[str] = None


class MinIOEventRecord(BaseModel):
    eventVersion: Optional[str] = None
    eventSource: Optional[str] = None
    awsRegion: Optional[str] = None
    eventTime: Optional[str] = None
    eventName: str
    userIdentity: Optional[MinIOIdentity] = None
    requestParameters: Optional[MinIORequestParameters] = None
    responseElements: Optional[dict[str, Any]] = None
    s3: S3EventData
    source: Optional[dict[str, Any]] = None


class MinIONotificationPayload(BaseModel):
    EventName: Optional[str] = None
    Key: Optional[str] = None
    Records: Optional[list[MinIOEventRecord]] = None


@router.post(
    "/minio/storage-events",
    summary="MinIO S3 storage bucket notification webhook for unversioned mutation / bypass detection",
)
def minio_storage_events_webhook(
    payload: MinIONotificationPayload,
    request: Request,
    db: Session = Depends(get_db),
    x_minio_secret: Optional[str] = Header(None, alias="X-MinIO-Webhook-Secret"),
    authorization: Optional[str] = Header(None, alias="Authorization"),
):
    """
    MinIO S3 bucket notification webhook (P1):
    1. Authenticates webhook request using shared secret header or Bearer token.
    2. Rejects query-parameter authentication (SEC-01).
    3. Handles single or multi-record S3 notifications safely without crashing.
    4. Evaluates evidence-based classification via StorageMutationDetector:
       - Authorized: expected lakeFS service user performing normal immutable blockstore operations.
       - Suspicious: unauthorized identities modifying dataset storage or direct metadata tampering.
       - Unverified: inconclusive storage activity on monitored buckets.
    5. Ingests normalized SecurityEvents (idempotent dedup_hash).
    6. Triggers immediate alert correlation for suspicious mutations (HIGH severity).
    """
    try:
        from app.core.config import settings
        from app.services.threat_hunting.storage_detector import storage_mutation_detector

        # 1. SEC-01: Authentication
        if getattr(settings, "MINIO_STORAGE_MUTATION_DETECTION_ENABLED", True):
            expected_secret = getattr(settings, "MINIO_WEBHOOK_SECRET", "")
            if not expected_secret:
                logger.error("Security failure: MINIO_STORAGE_MUTATION_DETECTION_ENABLED is True but MINIO_WEBHOOK_SECRET is empty.")
                return JSONResponse(
                    status_code=500,
                    content={"status": "error", "reason": "misconfigured_security_secret"},
                )

            provided_secret = x_minio_secret
            if not provided_secret and authorization:
                if authorization.lower().startswith("bearer "):
                    provided_secret = authorization[7:].strip()
                else:
                    provided_secret = authorization.strip()

            if not provided_secret or not hmac.compare_digest(str(provided_secret), str(expected_secret)):
                logger.warning("Unauthorized access attempt to MinIO storage webhook (invalid or missing secret).")
                return JSONResponse(
                    status_code=401,
                    content={"status": "unauthorized", "reason": "invalid_or_missing_webhook_secret"},
                )

        records = payload.Records or []
        # If payload only has top-level Key / EventName (some MinIO notification formats)
        if not records and payload.Key and payload.EventName:
            configured_bucket = getattr(settings, "LAKEFS_BLOCKSTORE_BUCKET", "lakefs")
            raw_key = payload.Key.strip()
            if "/" in raw_key:
                key_parts = raw_key.split("/", 1)
                b_name = key_parts[0]
                o_key = key_parts[1]
            else:
                b_name = configured_bucket
                o_key = raw_key

            if not o_key or not b_name:
                logger.warning(f"Malformed MinIO notification key rejected: {payload.Key}")
                return JSONResponse(
                    status_code=400,
                    content={"status": "error", "reason": "malformed_notification_key"},
                )

            records = [
                MinIOEventRecord(
                    eventName=payload.EventName,
                    s3=S3EventData(
                        bucket=S3BucketMetadata(name=b_name),
                        object=S3ObjectMetadata(key=o_key),
                    ),
                )
            ]

        if not records:
            return JSONResponse(status_code=200, content={"status": "skipped", "reason": "no_records"})

        results = []
        for rec in records:
            rec_dict = rec.model_dump()
            res = storage_mutation_detector.process_minio_record(db, rec_dict)
            if res:
                results.append(res)

        return JSONResponse(
            status_code=200,
            content={
                "status": "processed",
                "records_evaluated": len(records),
                "results": results,
            },
        )
    except Exception as e:
        logger.error(f"Critical error executing MinIO storage events webhook: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"status": "error", "detail": f"Storage webhook execution failure: {str(e)}"},
        )


# ===============================================================================
# Falco Container Runtime eBPF Intrusion Detection Webhook
# ===============================================================================

class FalcoWebhookPayload(BaseModel):
    output: str
    priority: Optional[str] = "Notice"
    rule: str
    time: Optional[str] = None
    output_fields: Optional[dict] = None


@router.post(
    "/falco",
    summary="Falco container runtime eBPF intrusion detection webhook",
)
def falco_runtime_alert_webhook(
    payload: FalcoWebhookPayload,
    db: Session = Depends(get_db),
):
    """
    Ingests runtime security alerts from Sysdig/Falco kernel eBPF engine.
    Detects unauthorized interactive shells (/bin/sh, bash) or network exfiltration
    utilities (curl, wget) spawned within Ray worker pods or serving containers.
    (MITRE ATT&CK T1059.004 / MITRE ATLAS AML.T0038)
    """
    fields = payload.output_fields or {}
    proc_name = fields.get("proc.name", "")
    proc_cmdline = fields.get("proc.cmdline", "")
    pod_name = fields.get("k8s.pod.name") or fields.get("container.name") or "ml-workload-container"
    user_name = fields.get("user.name") or "unknown"
    priority = (payload.priority or "HIGH").upper()

    severity = (
        "CRITICAL"
        if priority in ("CRITICAL", "EMERGENCY", "ALERT", "ERROR") or proc_name in ("sh", "bash", "zsh")
        else "HIGH"
    )

    log_audit_event(
        action="container_unauthorized_execution",
        username=user_name,
        ip_address=None,
        details=f"Falco runtime rule '{payload.rule}' triggered on pod '{pod_name}': {proc_cmdline or payload.output}",
        db=db,
    )

    sec_event = event_service.ingest_event(
        db,
        SecurityEventCreate(
            event_type="runtime.container_unauthorized_execution",
            source="falco_ebpf",
            actor_id=user_name,
            resource_type="pod",
            resource_id=pod_name,
            severity=severity,
            description=f"Falco runtime intrusion detected: {payload.output}",
            attributes={
                "rule": payload.rule,
                "priority": payload.priority,
                "proc_name": proc_name,
                "proc_cmdline": proc_cmdline,
                "pod_name": pod_name,
                "output_fields": fields,
                "mitre_attack_id": "T1059.004",
                "mitre_atlas_id": "AML.T0038",
            },
        ),
    )

    try:
        from app.services.threat_hunting.alert_service import alert_service
        from app.services.threat_hunting.detection_engine import detection_engine

        detections = detection_engine.evaluate_all_rules(db)
        for d in detections:
            if d.resource_id == pod_name or (
                sec_event and any(ev_id == sec_event.id for ev_id in d.event_ids)
            ):
                alert_service.process_detection(db, d)
    except Exception as alert_err:
        logger.warning(f"Failed to correlate Falco detection immediately: {alert_err}")

    return {"status": "ingested", "event_id": str(sec_event.id) if sec_event else None}
