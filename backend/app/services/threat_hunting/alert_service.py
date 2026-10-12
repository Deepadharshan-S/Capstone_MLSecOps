import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import and_, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.logging_config import log_audit_event
from app.models.threat_hunting import Alert, AlertEvent, AlertHistory, SecurityEvent
from app.schemas.threat_hunting import AlertFilter, AlertUpdate
from app.services.threat_hunting.detection_engine import DetectionResult

logger = logging.getLogger("threat_hunting.alerts")

VALID_STATUS_TRANSITIONS = {
    "OPEN": {"INVESTIGATING", "RESOLVED", "FALSE_POSITIVE"},
    "INVESTIGATING": {"RESOLVED", "FALSE_POSITIVE"},
    "RESOLVED": {"INVESTIGATING"},
    "FALSE_POSITIVE": set(),
}


def _to_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class AlertService:
    """
    Manages security alert creation, correlation, deduplication, and investigation lifecycle.
    """

    def process_detection(
        self,
        db: Session,
        detection: DetectionResult,
    ) -> Alert:
        """
        Processes a detection result: correlates with existing active alerts or creates a new one.
        Handles concurrent execution safely within a database transaction and out-of-order events.
        """
        # Serialize concurrent alert creation per correlation key on PostgreSQL
        if detection.dedup_key:
            bind = db.get_bind()
            if bind and bind.dialect.name == "postgresql":
                db.execute(
                    text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                    {"key": f"alert_dedup:{detection.dedup_key}"},
                )

        # Look for existing active alert with matching dedup_key
        existing_alert = db.execute(
            select(Alert).where(
                and_(
                    Alert.dedup_key == detection.dedup_key,
                    Alert.status.in_(["OPEN", "INVESTIGATING"]),
                )
            ).with_for_update()
        ).scalar_one_or_none()

        if existing_alert:
            # Correlate & update existing alert (support out-of-order events with timezone safety)
            t_first_existing = _to_utc(existing_alert.first_seen)
            t_first_detection = _to_utc(detection.first_seen)
            if t_first_existing and t_first_detection:
                existing_alert.first_seen = min(t_first_existing, t_first_detection)

            t_last_existing = _to_utc(existing_alert.last_seen)
            t_last_detection = _to_utc(detection.last_seen)
            if t_last_existing and t_last_detection:
                existing_alert.last_seen = max(t_last_existing, t_last_detection)

            existing_alert.supporting_evidence = detection.supporting_evidence
            existing_alert.rule_version = detection.rule_version

            # Associate new events not already linked
            existing_event_ids = set(
                db.execute(
                    select(AlertEvent.event_id).where(AlertEvent.alert_id == existing_alert.id)
                ).scalars().all()
            )

            new_links = 0
            for eid in detection.event_ids:
                if eid not in existing_event_ids:
                    db.add(AlertEvent(alert_id=existing_alert.id, event_id=eid))
                    new_links += 1

            existing_alert.event_count += new_links
            db.commit()
            db.refresh(existing_alert)
            return existing_alert

        # Create brand new alert inside nested subtransaction
        alert = Alert(
            title=detection.title,
            description=detection.description,
            severity=detection.severity,
            status="OPEN",
            detection_source="rule_engine",
            rule_id=detection.rule_id,
            rule_version=detection.rule_version,
            mitre_attack_id=detection.mitre_attack_id,
            mitre_atlas_id=detection.mitre_atlas_id,
            first_seen=detection.first_seen,
            last_seen=detection.last_seen,
            event_count=len(detection.event_ids),
            supporting_evidence=detection.supporting_evidence,
            dedup_key=detection.dedup_key,
        )
        try:
            with db.begin_nested():
                db.add(alert)
                db.flush()  # populate alert.id

                # Associate triggering events
                for eid in detection.event_ids:
                    db.add(AlertEvent(alert_id=alert.id, event_id=eid))

                # Initial history record
                db.add(
                    AlertHistory(
                        alert_id=alert.id,
                        actor_id="detection_engine",
                        previous_status=None,
                        new_status="OPEN",
                        notes=f"Alert triggered with {len(detection.event_ids)} initial events.",
                    )
                )

            db.commit()
            db.refresh(alert)
        except IntegrityError:
            # Fallback if a concurrent sweep created the alert first
            existing = db.execute(
                select(Alert).where(
                    and_(
                        Alert.dedup_key == detection.dedup_key,
                        Alert.status.in_(["OPEN", "INVESTIGATING"]),
                    )
                )
            ).scalar_one_or_none()
            if existing:
                t_first_existing = _to_utc(existing.first_seen)
                t_first_detection = _to_utc(detection.first_seen)
                if t_first_existing and t_first_detection:
                    existing.first_seen = min(t_first_existing, t_first_detection)

                t_last_existing = _to_utc(existing.last_seen)
                t_last_detection = _to_utc(detection.last_seen)
                if t_last_existing and t_last_detection:
                    existing.last_seen = max(t_last_existing, t_last_detection)
                db.commit()
                db.refresh(existing)
                return existing
            raise

        # Audit log creation
        log_audit_event(
            action="alert_created",
            username="detection_engine",
            details=f"Created {alert.severity} alert: {alert.title} (ID: {alert.id})",
            db=db,
        )

        return alert

    def process_detection_results(
        self,
        db: Session,
        detections: list[DetectionResult],
    ) -> list[Alert]:
        """
        Processes a collection of detection results into alerts.
        """
        alerts = []
        for d in detections:
            alert = self.process_detection(db, d)
            alerts.append(alert)
        return alerts

    def update_alert(
        self,
        db: Session,
        alert_id: UUID,
        update_in: AlertUpdate,
        actor_username: Optional[str] = None,
        user: Optional[str] = None,
    ) -> Alert:
        """
        Updates alert status, assignment, severity, or resolution with strict lifecycle validation.
        """
        actor = actor_username or user or "system"
        alert = db.get(Alert, alert_id)
        if not alert:
            raise ValueError(f"Alert with id {alert_id} not found.")

        old_status = alert.status
        status_changed = False

        if update_in.status:
            new_status = update_in.status.upper()
            if new_status not in VALID_STATUS_TRANSITIONS:
                raise ValueError(f"Invalid alert status: {new_status}")

            if new_status != old_status:
                if new_status not in VALID_STATUS_TRANSITIONS.get(old_status, set()):
                    raise ValueError(f"Invalid status transition from {old_status} to {new_status}")

                res_reason = (
                    getattr(update_in, "resolution_reason", None)
                    or getattr(update_in, "resolution_notes", None)
                    or f"Marked as {new_status} by {actor}"
                )

                alert.status = new_status
                status_changed = True
                if new_status in ("RESOLVED", "FALSE_POSITIVE"):
                    alert.resolved_at = datetime.now(timezone.utc)
                    alert.resolution_reason = res_reason
                else:
                    alert.resolved_at = None

        if update_in.severity:
            alert.severity = update_in.severity.upper()
        if update_in.assignee_id is not None:
            alert.assignee_id = update_in.assignee_id

        # Record history if status changed or notes provided
        if status_changed or update_in.notes:
            note_text = update_in.notes or (f"Status changed from {old_status} to {alert.status}")
            db.add(
                AlertHistory(
                    alert_id=alert.id,
                    actor_id=actor,
                    previous_status=old_status if status_changed else alert.status,
                    new_status=alert.status,
                    notes=note_text,
                )
            )

        db.commit()
        db.refresh(alert)

        if status_changed:
            log_audit_event(
                action="alert_status_changed",
                username=actor,
                details=f"Alert {alert.id} transitioned from {old_status} to {alert.status}",
                db=db,
            )

        return alert

    def get_alert_by_id(self, db: Session, alert_id: UUID) -> Optional[Alert]:
        return db.get(Alert, alert_id)

    def list_alerts(
        self,
        db: Session,
        filters: AlertFilter,
    ) -> tuple[list[Alert], int]:
        """
        Retrieves alerts with filtering and pagination.
        """
        query = select(Alert)
        conditions = []

        if filters.status:
            conditions.append(Alert.status == filters.status.upper())
        if filters.severity:
            conditions.append(Alert.severity == filters.severity.upper())
        if filters.rule_id:
            conditions.append(Alert.rule_id == filters.rule_id)
        if filters.detection_source:
            conditions.append(Alert.detection_source == filters.detection_source)
        if filters.assignee_id:
            conditions.append(Alert.assignee_id == filters.assignee_id)
        if filters.start_time:
            st = filters.start_time if filters.start_time.tzinfo else filters.start_time.replace(tzinfo=timezone.utc)
            conditions.append(Alert.last_seen >= st)
        if filters.end_time:
            et = filters.end_time if filters.end_time.tzinfo else filters.end_time.replace(tzinfo=timezone.utc)
            conditions.append(Alert.last_seen <= et)

        if conditions:
            query = query.where(and_(*conditions))

        query = query.order_by(Alert.last_seen.desc())

        count_query = select(func.count(Alert.id))
        if conditions:
            count_query = count_query.where(and_(*conditions))
        total = db.execute(count_query).scalar_one()

        alerts = list(db.execute(query.offset(filters.offset).limit(filters.limit)).scalars().all())
        return alerts, total

    def get_alert_events(
        self,
        db: Session,
        alert_id: UUID,
        limit: int = 100,
        offset: int = 0,
    ) -> list[SecurityEvent]:
        """
        Retrieves all triggering security events associated with an alert.
        """
        query = (
            select(SecurityEvent)
            .join(AlertEvent, AlertEvent.event_id == SecurityEvent.id)
            .where(AlertEvent.alert_id == alert_id)
            .order_by(SecurityEvent.occurrence_time.desc())
            .offset(offset)
            .limit(limit)
        )
        return list(db.execute(query).scalars().all())


alert_service = AlertService()
