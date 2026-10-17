import logging
import os
import threading
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import and_, delete, func, select, text
from sqlalchemy.orm import Session

from app.core.logging_config import log_audit_event
from app.models.threat_hunting import InferenceRateRecord, SecurityEvent

logger = logging.getLogger("threat_hunting.inference_detector")


class InferenceRateDetector:
    """
    Inference Protection Engine:
    1. Shared sliding-window rate burst detector (MITRE ATLAS AML.T0024).
    2. Algorithmic Exhaustion & Tabular Matrix Flooding DoS guard (AML.T0029).
    3. Decision-Boundary Scraping margin tracker (0.48 <= P <= 0.52).
    4. Systematic Feature Perturbation analyzer (L0=1 gradient estimation).
    """

    def __init__(self):
        self._lock = threading.Lock()
        # Track recent prediction probabilities per actor: actor_key -> deque of floats
        self._boundary_history: Dict[str, deque] = {}
        # Track last feature vector and consecutive perturbation count: actor_key -> (last_vector, count)
        self._perturbation_history: Dict[str, Tuple[Dict[str, float], int]] = {}

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

            # Safely log audit event outside savepoint
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

    def validate_payload_shape(
        self,
        num_rows: int,
        num_cols: int,
        actor_id: str,
        resource_id: str,
        client_ip: Optional[str] = None,
        db: Optional[Session] = None,
    ) -> Tuple[bool, Optional[str]]:
        """
        Algorithmic Exhaustion / Tabular Memory Flooding Guard (MITRE ATLAS AML.T0029).
        Rejects extreme dimension tabular matrices exceeding preset limits (10^5 cells)
        to prevent Out-Of-Memory (OOM) crashes on RayServe replicas.
        """
        max_cells = int(os.getenv("MAX_TABULAR_INFERENCE_CELLS", "100000"))
        max_rows = int(os.getenv("MAX_TABULAR_INFERENCE_ROWS", "10000"))
        max_cols = int(os.getenv("MAX_TABULAR_INFERENCE_COLS", "1000"))

        total_cells = num_rows * num_cols

        if total_cells > max_cells or num_rows > max_rows or num_cols > max_cols:
            error_msg = (
                f"Input matrix dimension ({num_rows}x{num_cols} = {total_cells} cells) "
                f"exceeds maximum allowed threshold of {max_cells} cells."
            )
            log_audit_event(
                action="inference_matrix_flooding_blocked",
                username=actor_id,
                ip_address=client_ip,
                details=(
                    f"Algorithmic exhaustion DoS blocked on model '{resource_id}': "
                    f"payload shape ({num_rows} rows x {num_cols} cols = {total_cells} cells) "
                    f"exceeds limit ({max_cells} cells)."
                ),
                db=db,
            )
            return False, error_msg

        return True, None

    def record_and_check_decision_boundary(
        self,
        actor_id: str,
        resource_id: str,
        predictions: Any,
        client_ip: Optional[str] = None,
        db: Optional[Session] = None,
    ) -> bool:
        """
        Decision-Boundary Scraping Detector (MITRE ATLAS AML.T0024).
        Tracks prediction probability confidence margins. If a client's queries heavily cluster
        near the classification decision threshold (0.48 <= P(y=1) <= 0.52), triggers an alert.
        """
        probs: List[float] = []

        if isinstance(predictions, list):
            for item in predictions:
                if isinstance(item, (int, float)) and 0.0 <= item <= 1.0:
                    probs.append(float(item))
                elif isinstance(item, list) and len(item) >= 2:
                    p1 = float(item[1])
                    if 0.0 <= p1 <= 1.0:
                        probs.append(p1)
                elif isinstance(item, dict):
                    p = item.get("probability") or item.get("score") or item.get("p1")
                    if p is not None and isinstance(p, (int, float)) and 0.0 <= p <= 1.0:
                        probs.append(float(p))

        if not probs:
            return False

        key = f"{actor_id}:{resource_id}"
        alert_triggered = False

        with self._lock:
            if key not in self._boundary_history:
                self._boundary_history[key] = deque(maxlen=20)
            history = self._boundary_history[key]
            history.extend(probs)

            # Evaluate when at least 5 prediction samples are recorded
            if len(history) >= 5:
                boundary_count = sum(1 for p in history if 0.48 <= p <= 0.52)
                ratio = boundary_count / len(history)
                # If >= 60% of predictions fall inside [0.48, 0.52], flag decision boundary scraping
                if ratio >= 0.60:
                    alert_triggered = True
                    history.clear()  # Reset after trigger to prevent duplicate flooding

        if alert_triggered:
            log_audit_event(
                action="inference_decision_boundary_scraping",
                username=actor_id,
                ip_address=client_ip,
                details=(
                    f"Decision-boundary scraping detected on model '{resource_id}': "
                    f"Traffic heavily clusters at decision boundaries (0.48 <= P <= 0.52), "
                    f"indicating systematic reverse-engineering of model weights."
                ),
                db=db,
            )
            return True

        return False

    def record_and_check_feature_perturbation(
        self,
        actor_id: str,
        resource_id: str,
        feature_record: Dict[str, Any],
        client_ip: Optional[str] = None,
        db: Optional[Session] = None,
    ) -> bool:
        """
        Systematic Feature Perturbation Detector (MITRE ATLAS AML.T0040).
        Detects sequential requests where only one continuous feature is nudged incrementally
        while all other features remain strictly identical (L0 norm = 1, indicating gradient/boundary estimation).
        """
        # Extract continuous numeric features only
        current_numeric = {}
        for k, v in feature_record.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                current_numeric[k] = float(v)

        if len(current_numeric) < 2:
            return False

        key = f"{actor_id}:{resource_id}"
        alert_triggered = False

        with self._lock:
            if key in self._perturbation_history:
                last_vec, consecutive_count = self._perturbation_history[key]
                # Compare current_numeric with last_vec
                shared_keys = set(current_numeric.keys()) & set(last_vec.keys())
                if len(shared_keys) >= 2:
                    diff_keys = [k for k in shared_keys if abs(current_numeric[k] - last_vec[k]) > 1e-6]
                    # Exactly one feature changed (L0 = 1)
                    if len(diff_keys) == 1:
                        consecutive_count += 1
                    else:
                        consecutive_count = 0

                    if consecutive_count >= 3:
                        alert_triggered = True
                        consecutive_count = 0

                    self._perturbation_history[key] = (current_numeric, consecutive_count)
            else:
                self._perturbation_history[key] = (current_numeric, 0)

        if alert_triggered:
            log_audit_event(
                action="inference_feature_perturbation_detected",
                username=actor_id,
                ip_address=client_ip,
                details=(
                    f"Systematic feature perturbation detected on model '{resource_id}': "
                    f"Consecutive payloads nudging exactly one continuous feature (L0=1) "
                    f"indicating black-box gradient estimation or decision boundary mapping."
                ),
                db=db,
            )
            return True

        return False


inference_rate_detector = InferenceRateDetector()
