import logging
import re
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import select, and_
from sqlalchemy.orm import Session

from sqlalchemy.exc import IntegrityError
from app.models.threat_hunting import SecurityEvent
from app.schemas.threat_hunting import SecurityEventCreate, SecurityEventFilter
from app.services.threat_hunting.normalization import (
    compute_event_dedup_hash,
    redact_sensitive_data,
)

logger = logging.getLogger("threat_hunting.events")

AUDIT_ACTION_MAP = {
    # Authentication & Authorization - Failures & Lockouts
    "login_failed": {
        "event_type": "auth.login_failed",
        "severity": "HIGH",
        "source": "auth_service",
        "resource_type": "user",
    },
    "login_failed_incorrect_password": {
        "event_type": "auth.login_failed",
        "severity": "HIGH",
        "source": "auth_service",
        "resource_type": "user",
    },
    "login_failed_nonexistent_user": {
        "event_type": "auth.login_failed",
        "severity": "HIGH",
        "source": "auth_service",
        "resource_type": "user",
    },
    "mfa_failed": {
        "event_type": "auth.login_failed",
        "severity": "HIGH",
        "source": "auth_service",
        "resource_type": "user",
    },
    "login_locked": {
        "event_type": "auth.account_locked",
        "severity": "HIGH",
        "source": "auth_service",
        "resource_type": "user",
    },
    "login_blocked_locked": {
        "event_type": "auth.account_locked",
        "severity": "HIGH",
        "source": "auth_service",
        "resource_type": "user",
    },
    "account_locked": {
        "event_type": "auth.account_locked",
        "severity": "HIGH",
        "source": "auth_service",
        "resource_type": "user",
    },
    "user_lockout_triggered": {
        "event_type": "auth.account_locked",
        "severity": "HIGH",
        "source": "auth_service",
        "resource_type": "user",
    },
    "unauthorized_access": {
        "event_type": "auth.unauthorized_access",
        "severity": "MEDIUM",
        "source": "auth_service",
        "resource_type": "endpoint",
    },
    "token_revoked": {
        "event_type": "auth.token_revoked",
        "severity": "LOW",
        "source": "auth_service",
        "resource_type": "token",
    },
    "token_blacklisted": {
        "event_type": "auth.token_blacklisted_used",
        "severity": "HIGH",
        "source": "auth_service",
        "resource_type": "token",
    },
    "token_blacklisted_used": {
        "event_type": "auth.token_blacklisted_used",
        "severity": "HIGH",
        "source": "auth_service",
        "resource_type": "token",
    },
    "token_reuse_detected": {
        "event_type": "auth.token_blacklisted_used",
        "severity": "HIGH",
        "source": "auth_service",
        "resource_type": "token",
    },
    # ML Infrastructure & Training
    "rayjob_submitted": {
        "event_type": "ml.rayjob_submitted",
        "severity": "LOW",
        "source": "ray_operator",
        "resource_type": "rayjob",
    },
    "model_training_start": {
        "event_type": "ml.rayjob_submitted",
        "severity": "LOW",
        "source": "ray_operator",
        "resource_type": "rayjob",
    },
    "model_training_initiated": {
        "event_type": "ml.rayjob_submitted",
        "severity": "LOW",
        "source": "ray_operator",
        "resource_type": "rayjob",
    },
    "rayjob_submission_failed": {
        "event_type": "ml.rayjob_failed",
        "severity": "MEDIUM",
        "source": "ray_operator",
        "resource_type": "rayjob",
    },
    "model_training_rejected": {
        "event_type": "ml.rayjob_failed",
        "severity": "MEDIUM",
        "source": "ray_operator",
        "resource_type": "rayjob",
    },
    "model_deployment_deleted": {
        "event_type": "ml.model_deleted",
        "severity": "MEDIUM",
        "source": "ml_ops",
        "resource_type": "model",
    },
    # Dataset Integrity
    "dataset_branch_deleted": {
        "event_type": "dataset.branch_deleted",
        "severity": "HIGH",
        "source": "lakefs",
        "resource_type": "branch",
    },
    "dataset_branch_delete": {
        "event_type": "dataset.branch_deleted",
        "severity": "HIGH",
        "source": "lakefs",
        "resource_type": "branch",
    },
    "lakefs_branch_deleted": {
        "event_type": "dataset.branch_deleted",
        "severity": "HIGH",
        "source": "lakefs",
        "resource_type": "branch",
    },
    "dataset_deleted": {
        "event_type": "dataset.deleted",
        "severity": "HIGH",
        "source": "lakefs",
        "resource_type": "dataset",
    },
    "dataset_delete": {
        "event_type": "dataset.deleted",
        "severity": "HIGH",
        "source": "lakefs",
        "resource_type": "dataset",
    },
    "dataset_poisoning_detected": {
        "event_type": "dataset.poisoning_suspected",
        "severity": "HIGH",
        "source": "lakefs_validator",
        "resource_type": "dataset",
    },
    # Inference Security
    "model_inference_spike": {
        "event_type": "inference.request_burst",
        "severity": "MEDIUM",
        "source": "model_inference",
        "resource_type": "model",
    },
    "inference_spike": {
        "event_type": "inference.request_burst",
        "severity": "MEDIUM",
        "source": "model_inference",
        "resource_type": "model",
    },
    # Model Artifact Security & RCE Gatekeeper
    "model_rce_payload_detected": {
        "event_type": "model.rce_payload_detected",
        "severity": "CRITICAL",
        "source": "pickle_scanner",
        "resource_type": "model",
    },
    "model_pickle_unsafe": {
        "event_type": "model.rce_payload_detected",
        "severity": "CRITICAL",
        "source": "pickle_scanner",
        "resource_type": "model",
    },
    # Inference Attacks & Algorithmic DoS
    "inference_matrix_flooding_blocked": {
        "event_type": "inference.payload_shape_excessive",
        "severity": "HIGH",
        "source": "model_serving",
        "resource_type": "model",
    },
    "inference_decision_boundary_scraping": {
        "event_type": "inference.decision_boundary_scraping",
        "severity": "HIGH",
        "source": "inference_detector",
        "resource_type": "model",
    },
    "inference_feature_perturbation_detected": {
        "event_type": "inference.systematic_perturbation",
        "severity": "HIGH",
        "source": "inference_detector",
        "resource_type": "model",
    },
    # Model Governance, Safe Format & Attestation
    "model_unsafe_format_promoted": {
        "event_type": "model.unsafe_format_promoted",
        "severity": "HIGH",
        "source": "model_governance",
        "resource_type": "model",
    },
    "model_unattested_promotion_detected": {
        "event_type": "model.unattested_promotion",
        "severity": "HIGH",
        "source": "model_governance",
        "resource_type": "model",
    },
    "model_attestation_tampering_detected": {
        "event_type": "model.tampering_detected",
        "severity": "CRITICAL",
        "source": "model_governance",
        "resource_type": "model",
    },
    # Falco Container Runtime eBPF Security
    "container_unauthorized_execution": {
        "event_type": "runtime.container_unauthorized_execution",
        "severity": "CRITICAL",
        "source": "falco_ebpf",
        "resource_type": "pod",
    },
    "container_shell_spawned": {
        "event_type": "runtime.container_unauthorized_execution",
        "severity": "CRITICAL",
        "source": "falco_ebpf",
        "resource_type": "pod",
    },
    # S3 Artifact Integrity & Shadow Model Detection
    "orphan_artifact_detected": {
        "event_type": "artifact.orphan_detected",
        "severity": "HIGH",
        "source": "artifact_auditor",
        "resource_type": "artifact",
    },
}


class SecurityEventService:
    """
    Handles normalization, deduplication, and persistence of security events.
    """

    def ingest_event(
        self,
        db: Session,
        event_in: SecurityEventCreate,
    ) -> SecurityEvent:
        """
        Normalizes and safely persists a single security event with idempotent deduplication.
        """
        occurrence = event_in.occurrence_time or datetime.now(timezone.utc)
        if occurrence.tzinfo is None:
            occurrence = occurrence.replace(tzinfo=timezone.utc)
        else:
            occurrence = occurrence.astimezone(timezone.utc)

        redacted_attrs = redact_sensitive_data(event_in.attributes or {})
        cleaned_actor = (
            redact_sensitive_data(event_in.actor_id)
            if isinstance(event_in.actor_id, str)
            else event_in.actor_id
        )
        cleaned_resource = (
            redact_sensitive_data(event_in.resource_id)
            if isinstance(event_in.resource_id, str)
            else event_in.resource_id
        )

        dedup_hash = compute_event_dedup_hash(
            source=event_in.source,
            event_type=event_in.event_type,
            actor_id=cleaned_actor,
            resource_id=cleaned_resource,
            occurrence_time=occurrence,
            attributes=redacted_attrs,
        )

        # Check for duplicate by dedup_hash
        existing = db.execute(
            select(SecurityEvent).where(SecurityEvent.dedup_hash == dedup_hash)
        ).scalar_one_or_none()
        if existing:
            return existing

        # INT-01: Check for duplicate by correlation_id if provided
        if event_in.correlation_id:
            existing_corr = db.execute(
                select(SecurityEvent).where(
                    SecurityEvent.correlation_id == event_in.correlation_id,
                    SecurityEvent.event_type == event_in.event_type,
                    SecurityEvent.resource_id == cleaned_resource,
                )
            ).scalars().first()
            if existing_corr:
                return existing_corr

        event = SecurityEvent(
            event_type=event_in.event_type,
            source=event_in.source,
            actor_id=cleaned_actor,
            resource_id=cleaned_resource,
            resource_type=event_in.resource_type,
            occurrence_time=occurrence,
            ingestion_time=datetime.now(timezone.utc),
            severity=event_in.severity.upper(),
            attributes=redacted_attrs,
            correlation_id=event_in.correlation_id,
            dedup_hash=dedup_hash,
        )
        try:
            with db.begin_nested():
                db.add(event)
                db.flush()
            db.commit()
            db.refresh(event)
            return event
        except IntegrityError:
            existing = db.execute(
                select(SecurityEvent).where(SecurityEvent.dedup_hash == dedup_hash)
            ).scalar_one_or_none()
            if existing:
                return existing
            raise

    def ingest_from_audit(
        self,
        action: str,
        username: str,
        ip_address: Optional[str] = None,
        details: Optional[str] = None,
        db: Optional[Session] = None,
        request_id: Optional[str] = None,
    ) -> Optional[SecurityEvent]:
        """
        Translates a security audit log event into a normalized SecurityEvent.
        Guards against recursive audit ingestion loops.
        """
        action_lower = action.lower() if action else ""

        # Loop protection: Ignore audit events generated by internal detection/alerting operations
        if action_lower.startswith(("alert_", "hunt_", "rule_", "security_event_")):
            return None
        if username and username.lower() == "detection_engine":
            return None

        # INT-01: Prevent duplicate ingestion for dataset poisoning
        # Direct webhook ingestion already records the authoritative SecurityEvent with complete violation payload
        if action_lower == "dataset_poisoning_detected" and db is not None:
            m_repo = re.search(r"repository '([^']+)'", details or "", re.IGNORECASE)
            repo_id = m_repo.group(1) if m_repo else None
            if repo_id:
                recent_existing = db.execute(
                    select(SecurityEvent).where(
                        SecurityEvent.resource_id == repo_id,
                        SecurityEvent.event_type == "dataset.poisoning_suspected",
                    ).order_by(SecurityEvent.created_at.desc())
                ).scalars().first()
                if recent_existing:
                    logger.debug(f"Audit log '{action}' skipped: SecurityEvent already directly ingested for '{repo_id}'.")
                    return recent_existing

        mapping = AUDIT_ACTION_MAP.get(action_lower)
        if not mapping:
            is_suspicious = any(
                k in action_lower
                for k in (
                    "fail",
                    "unauthoriz",
                    "lock",
                    "delet",
                    "attack",
                    "tamper",
                    "burst",
                    "spike",
                    "denied",
                )
            )
            if not is_suspicious:
                return None
            mapping = {
                "event_type": f"audit.{action_lower}",
                "severity": "MEDIUM",
                "source": "audit_log",
                "resource_type": "audit_record",
            }

        # Clean actor_id
        actor = username if username and username.lower() not in ("anonymous", "none") else None

        # Extract resource_id from details string if applicable
        extracted_resource: Optional[str] = None
        if details:
            m_branch = re.search(r"branch '([^']+)'", details, re.IGNORECASE)
            m_dataset = re.search(r"dataset '([^']+)'", details, re.IGNORECASE)
            m_model = re.search(r"model '([^']+)'", details, re.IGNORECASE)
            m_job = re.search(r"(?:Job ID:\s*|RayJob\s*)([a-zA-Z0-9_\-]+)", details, re.IGNORECASE)

            res_type = mapping.get("resource_type")
            if res_type == "branch" and m_branch:
                extracted_resource = m_branch.group(1)
            elif res_type == "dataset" and m_dataset:
                extracted_resource = m_dataset.group(1)
            elif res_type == "model" and m_model:
                extracted_resource = m_model.group(1)
            elif res_type == "rayjob" and m_job:
                extracted_resource = m_job.group(1)
            elif m_branch:
                extracted_resource = m_branch.group(1)
            elif m_dataset:
                extracted_resource = m_dataset.group(1)
            elif m_model:
                extracted_resource = m_model.group(1)
            elif m_job:
                extracted_resource = m_job.group(1)

        event_in = SecurityEventCreate(
            event_type=mapping["event_type"],
            source=mapping["source"],
            actor_id=actor,
            resource_type=mapping["resource_type"],
            resource_id=extracted_resource,
            severity=mapping["severity"],
            attributes={
                "audit_action": action,
                "ip_address": ip_address,
                "details": redact_sensitive_data(details) if isinstance(details, str) else details,
                "request_id": request_id,
            },
            correlation_id=request_id,
        )

        try:
            if db is not None:
                return self.ingest_event(db, event_in)
            else:
                from app.db.session import SessionLocal

                with SessionLocal() as session:
                    return self.ingest_event(session, event_in)
        except Exception as e:
            logger.debug(f"Failed to ingest security event from audit '{action}': {e}")
            return None

    def list_events(
        self,
        db: Session,
        filters: SecurityEventFilter,
    ) -> tuple[list[SecurityEvent], int]:
        """
        Retrieves security events matching filter criteria with pagination.
        """
        query = select(SecurityEvent)
        conditions = []

        if filters.event_type:
            conditions.append(SecurityEvent.event_type == filters.event_type)
        if filters.source:
            conditions.append(SecurityEvent.source == filters.source)
        if filters.actor_id:
            conditions.append(SecurityEvent.actor_id == filters.actor_id)
        if filters.resource_id:
            conditions.append(SecurityEvent.resource_id == filters.resource_id)
        if filters.severity:
            conditions.append(SecurityEvent.severity == filters.severity.upper())
        if filters.correlation_id:
            conditions.append(SecurityEvent.correlation_id == filters.correlation_id)
        if filters.start_time:
            st = filters.start_time if filters.start_time.tzinfo else filters.start_time.replace(tzinfo=timezone.utc)
            conditions.append(SecurityEvent.occurrence_time >= st)
        if filters.end_time:
            et = filters.end_time if filters.end_time.tzinfo else filters.end_time.replace(tzinfo=timezone.utc)
            conditions.append(SecurityEvent.occurrence_time <= et)

        if conditions:
            query = query.where(and_(*conditions))

        # Order by occurrence_time descending
        query = query.order_by(SecurityEvent.occurrence_time.desc())

        # Total count
        from sqlalchemy import func
        count_query = select(func.count(SecurityEvent.id))
        if conditions:
            count_query = count_query.where(and_(*conditions))
        total_count = db.execute(count_query).scalar_one()

        events = db.execute(query.offset(filters.offset).limit(filters.limit)).scalars().all()
        return list(events), total_count

    def get_event_by_id(self, db: Session, event_id: UUID) -> Optional[SecurityEvent]:
        return db.get(SecurityEvent, event_id)


event_service = SecurityEventService()
