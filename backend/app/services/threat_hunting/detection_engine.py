import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.models.threat_hunting import DetectionRule, SecurityEvent

logger = logging.getLogger("threat_hunting.engine")


@dataclass
class DetectionResult:
    """
    Structured outcome of a successful rule match ready for alert correlation.
    """
    rule_id: UUID
    rule_version: int
    title: str
    description: str
    severity: str
    mitre_attack_id: Optional[str]
    mitre_atlas_id: Optional[str]
    dedup_key: str
    event_ids: list[UUID]
    first_seen: datetime
    last_seen: datetime
    actor_id: Optional[str]
    resource_id: Optional[str]
    supporting_evidence: dict[str, Any]
    # Which subsystem produced this. The rule engine is the default; drift
    # findings set "drift_engine" so they stay filterable apart from rule hits
    # even though they share the same alert lifecycle and dedup machinery.
    detection_source: str = "rule_engine"


class DetectionEngine:
    """
    Modular, deterministic detection evaluation engine.
    Separates rule logic from API handlers and persistence.
    """

    def evaluate_all_rules(
        self,
        db: Session,
        now: Optional[datetime] = None,
    ) -> list[DetectionResult]:
        """
        Retrieves all enabled rules and evaluates them against their respective time windows.
        """
        eval_time = now or datetime.now(timezone.utc)
        if eval_time.tzinfo is None:
            eval_time = eval_time.replace(tzinfo=timezone.utc)

        rules = db.execute(
            select(DetectionRule).where(DetectionRule.is_enabled == True)  # noqa: E712
        ).scalars().all()

        results: list[DetectionResult] = []
        for rule in rules:
            try:
                rule_matches = self.evaluate_single_rule(db, rule, now=eval_time)
                results.extend(rule_matches)
            except Exception as e:
                logger.error(
                    f"Error evaluating detection rule '{rule.name}' ({rule.id}): {e}",
                    exc_info=True,
                )
        return results

    def evaluate_single_rule(
        self,
        db: Session,
        rule: DetectionRule,
        window_seconds_override: Optional[int] = None,
        now: Optional[datetime] = None,
    ) -> list[DetectionResult]:
        """
        Evaluates a single threshold-based detection rule against recent security events.
        """
        cfg = rule.config or {}
        event_type = cfg.get("event_type")
        if not event_type:
            return []

        threshold = int(cfg.get("threshold", 1))
        win_seconds = window_seconds_override or int(cfg.get("window_seconds", 300))
        group_by_field = cfg.get("group_by", "actor_id")
        match_attrs = cfg.get("match_attributes", {})

        eval_time = now or datetime.now(timezone.utc)
        if eval_time.tzinfo is None:
            eval_time = eval_time.replace(tzinfo=timezone.utc)

        start_time = eval_time - timedelta(seconds=win_seconds)

        # Query events within window matching event_type
        query = select(SecurityEvent).where(
            and_(
                SecurityEvent.event_type == event_type,
                SecurityEvent.occurrence_time >= start_time,
                SecurityEvent.occurrence_time <= eval_time,
            )
        ).order_by(SecurityEvent.occurrence_time.asc())

        events = list(db.execute(query).scalars().all())
        if not events:
            return []

        # Filter by match_attributes if provided
        if match_attrs:
            filtered_events = []
            for ev in events:
                attrs = ev.attributes or {}
                matches = all(attrs.get(k) == v for k, v in match_attrs.items())
                if matches:
                    filtered_events.append(ev)
            events = filtered_events

        if not events:
            return []

        # Group events by group_by_field
        grouped: dict[str, list[SecurityEvent]] = {}
        for ev in events:
            if group_by_field == "actor_id":
                key = ev.actor_id or "unknown_actor"
            elif group_by_field == "resource_id":
                key = ev.resource_id or "unknown_resource"
            elif group_by_field == "source":
                key = ev.source or "unknown_source"
            elif group_by_field == "correlation_id":
                key = ev.correlation_id or "unknown_correlation"
            else:
                key = "global"

            grouped.setdefault(key, []).append(ev)

        detections: list[DetectionResult] = []
        for group_val, ev_list in grouped.items():
            if len(ev_list) >= threshold:
                first_seen = ev_list[0].occurrence_time
                last_seen = ev_list[-1].occurrence_time
                event_ids = [ev.id for ev in ev_list]

                actor_id = ev_list[0].actor_id if group_by_field == "actor_id" else None
                resource_id = ev_list[0].resource_id if group_by_field == "resource_id" else None

                dedup_key = f"{rule.id}:{group_by_field}:{group_val}"

                title = f"{rule.name} on {group_val}" if group_val != "global" else rule.name
                description = (
                    f"{rule.description} "
                    f"Threshold of {threshold} reached ({len(ev_list)} events observed within {win_seconds}s)."
                )

                supporting_evidence = {
                    "matched_count": len(ev_list),
                    "threshold": threshold,
                    "window_seconds": win_seconds,
                    "grouped_by": group_by_field,
                    "group_value": group_val,
                    "sample_sources": list({ev.source for ev in ev_list}),
                    "sample_correlation_ids": list({ev.correlation_id for ev in ev_list if ev.correlation_id}),
                }

                detections.append(
                    DetectionResult(
                        rule_id=rule.id,
                        rule_version=rule.version,
                        title=title,
                        description=description,
                        severity=rule.severity,
                        mitre_attack_id=rule.mitre_attack_id,
                        mitre_atlas_id=rule.mitre_atlas_id,
                        dedup_key=dedup_key,
                        event_ids=event_ids,
                        first_seen=first_seen,
                        last_seen=last_seen,
                        actor_id=actor_id,
                        resource_id=resource_id,
                        supporting_evidence=supporting_evidence,
                    )
                )

        return detections


detection_engine = DetectionEngine()
