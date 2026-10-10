import logging
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
    # Authentication & Authorization
    "login_failed": {
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
    "account_locked": {
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
    "mfa_failed": {
        "event_type": "auth.login_failed",
        "severity": "HIGH",
        "source": "auth_service",
        "resource_type": "user",
    },
    # ML Infrastructure
    "rayjob_submitted": {
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

        dedup_hash = compute_event_dedup_hash(
            source=event_in.source,
            event_type=event_in.event_type,
            actor_id=event_in.actor_id,
            resource_id=event_in.resource_id,
            occurrence_time=occurrence,
            attributes=redacted_attrs,
        )

        # Check for duplicate
        existing = db.execute(
            select(SecurityEvent).where(SecurityEvent.dedup_hash == dedup_hash)
        ).scalar_one_or_none()
        if existing:
            return existing

        event = SecurityEvent(
            event_type=event_in.event_type,
            source=event_in.source,
            actor_id=event_in.actor_id,
            resource_id=event_in.resource_id,
            resource_type=event_in.resource_type,
            occurrence_time=occurrence,
            ingestion_time=datetime.now(timezone.utc),
            severity=event_in.severity.upper(),
            attributes=redacted_attrs,
            correlation_id=event_in.correlation_id,
            dedup_hash=dedup_hash,
        )
        try:
            db.add(event)
            db.commit()
            db.refresh(event)
            return event
        except IntegrityError:
            db.rollback()
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
        """
        mapping = AUDIT_ACTION_MAP.get(action.lower())
        if not mapping:
            is_suspicious = any(
                k in action.lower()
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
                "event_type": f"audit.{action.lower()}",
                "severity": "MEDIUM",
                "source": "audit_log",
                "resource_type": "audit_record",
            }

        # Clean actor_id
        actor = username if username and username.lower() not in ("anonymous", "none") else None

        event_in = SecurityEventCreate(
            event_type=mapping["event_type"],
            source=mapping["source"],
            actor_id=actor,
            resource_type=mapping["resource_type"],
            resource_id=None,
            severity=mapping["severity"],
            attributes={
                "audit_action": action,
                "ip_address": ip_address,
                "details": details,
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
