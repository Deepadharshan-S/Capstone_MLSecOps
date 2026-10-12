import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import and_, delete, func, select, text
from sqlalchemy.orm import Session

from app.core.logging_config import log_audit_event
from app.models.threat_hunting import InferenceRateRecord, SecurityEvent

logger = logging.getLogger("threat_hunting.inference_detector")


class InferenceRateDetector:
    """
    Shared, bounded sliding-window rate detector for inference prediction traffic.
    Operates across multiple backend workers / uvicorn replicas using PostgreSQL.
    Detects model extraction, inversion, and high-rate scraping (MITRE ATLAS AML.T0024)
    without logging sensitive inputs, feature payloads, tokens, or credentials.
    """

    def record_and_check_burst(
        self,
        db: Session,
        actor_id: str,
        resource_id: str,
        client_ip: Optional[str] = None,
        threshold: Optional[int] = None,
        window_seconds: Optional[int] = None,
    ) -> dict:
        """
        Records an inference request in the shared PostgreSQL sliding window
        and evaluates whether the request rate exceeds the burst threshold.

        Returns a dictionary summarizing detection status and current count.
        Failures in telemetry are safely captured and never break normal inference.
        """
        win_sec = int(
            window_seconds
            if window_seconds is not None
            else os.getenv("INFERENCE_BURST_WINDOW_SECONDS", "60")
        )
        thresh = int(
            threshold
            if threshold is not None
            else os.getenv("INFERENCE_BURST_THRESHOLD", "50")
        )

        clean_actor = str(actor_id or "anonymous").strip()
        clean_resource = str(resource_id or "unknown_model").strip()
        now = datetime.now(timezone.utc)
        window_start = now - timedelta(seconds=win_sec)

        try:
            # Serialize concurrent counting per resource across multiple workers if on PostgreSQL
            bind = db.get_bind()
            if bind and bind.dialect.name == "postgresql":
                db.execute(
                    text("SELECT pg_advisory_xact_lock(hashtext(:res_key))"),
                    {"res_key": f"inference_rate:{clean_resource}"},
                )

            burst_detected = False
            with db.begin_nested():
                # 1. Record the current request with authenticated identity and model resource
                rec = InferenceRateRecord(
                    actor_id=clean_actor,
                    resource_id=clean_resource,
                    occurrence_time=now,
                )
                db.add(rec)
                db.flush()

                # 2. Count requests for this model within the active sliding window
                count_query = select(func.count(InferenceRateRecord.id)).where(
                    and_(
                        InferenceRateRecord.resource_id == clean_resource,
                        InferenceRateRecord.occurrence_time >= window_start,
                        InferenceRateRecord.occurrence_time <= now,
                    )
                )
                current_count = db.scalar(count_query) or 0

                # 3. Periodically prune expired records beyond 3x window to keep table bounded
                db.execute(
                    delete(InferenceRateRecord).where(
                        InferenceRateRecord.occurrence_time
                        < now - timedelta(seconds=win_sec * 3)
                    )
                )

                # 4. Evaluate threshold crossing
                if current_count >= thresh:
                    # Prevent repeated evaluations from generating duplicate burst events
                    # Check if a burst event was already emitted for this model in the current window
                    existing_burst = db.scalar(
                        select(SecurityEvent.id)
                        .where(
                            and_(
                                SecurityEvent.event_type == "inference.request_burst",
                                SecurityEvent.resource_id == clean_resource,
                                SecurityEvent.occurrence_time >= window_start,
                            )
                        )
                        .limit(1)
                    )

                    if not existing_burst:
                        burst_detected = True

            # Savepoint released. Safely log audit event and ingest SecurityEvent outside savepoint
            if burst_detected:
                log_audit_event(
                    action="model_inference_spike",
                    username=clean_actor,
                    ip_address=client_ip,
                    details=(
                        f"Inference burst detected on model '{clean_resource}': "
                        f"{current_count} requests within {win_sec}s "
                        f"(threshold: {thresh})."
                    ),
                    db=db,
                )

            result = {
                "burst_detected": burst_detected,
                "current_count": current_count,
                "threshold": thresh,
                "window_seconds": win_sec,
                "resource_id": clean_resource,
                "actor_id": clean_actor,
            }
            db.commit()
            return result
        except Exception as e:
            logger.warning(
                f"Inference burst telemetry error for model '{clean_resource}': {e}",
                exc_info=False,
            )
            return {
                "burst_detected": False,
                "current_count": 0,
                "threshold": thresh,
                "window_seconds": win_sec,
                "error": str(e),
            }


inference_rate_detector = InferenceRateDetector()
