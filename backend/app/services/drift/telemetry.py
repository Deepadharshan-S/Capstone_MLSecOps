"""
Write path for per-request inference telemetry.

Called from the serving path, so every failure mode here is treated as
non-fatal: losing a telemetry row is acceptable, failing a prediction that
already succeeded is not.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

import pandas as pd

logger = logging.getLogger("drift.telemetry")


def _bounded_payload(
    df: pd.DataFrame, max_columns: int, max_rows: int
) -> tuple[Optional[list[dict]], bool, int]:
    """
    Feature payload for one request, clipped to the configured bounds.

    Returns (records, truncated, captured_column_count). Truncation is reported
    rather than silent — a detector that mistakes a clipped capture for a
    complete one would misread the distribution it just measured.

    The count is of *feature columns actually captured*, not rows: every
    telemetry row is a single request (one row), so a row count would read as
    "1 feature" regardless of how wide the model really is. The count also
    matches the payload, so a column-clipped capture is not overstated —
    `truncated` already flags that it is incomplete.
    """
    if df is None or len(df.columns) == 0 or len(df) == 0:
        return None, False, 0

    original_rows = int(len(df))
    truncated = original_rows > max_rows or len(df.columns) > max_columns

    clipped = df.iloc[:max_rows, :max_columns]
    try:
        # JSON round-trip converts numpy/pandas scalars to plain Python types.
        records = json_safe(clipped.to_dict(orient="records"))
    except Exception as conv_err:
        logger.debug(f"Could not serialise inference features: {conv_err}")
        return None, truncated, int(len(clipped.columns))

    return records, truncated, int(len(clipped.columns))


def json_safe(value: Any) -> Any:
    """Coerce numpy/pandas scalars and non-finite floats into JSON-safe values.

    NaN and Infinity are not valid JSON, and Postgres rejects them inside
    JSONB — so they become null rather than blowing up the insert."""
    import math

    import numpy as np

    if value is None:
        return None
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        value = float(value)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "item"):
        try:
            return json_safe(value.item())
        except Exception:
            return str(value)
    return str(value)


def confidence_from_probabilities(probabilities: Any) -> tuple[Optional[list[float]], Optional[float]]:
    """
    Per-sample confidence = highest class probability.

    Returns (per_sample_confidences, mean_confidence). Returns (None, None)
    when the serving contract could not supply probabilities, which the caller
    records as `has_probabilities=False` so D3 stays silent instead of
    interpreting missing scores as low confidence.
    """
    if not isinstance(probabilities, (list, tuple)) or not probabilities:
        return None, None

    per_sample: list[float] = []
    for row in probabilities:
        if isinstance(row, (list, tuple)) and row:
            try:
                numeric = [float(x) for x in row]
            except (TypeError, ValueError):
                continue
            if numeric:
                per_sample.append(max(numeric))
        elif isinstance(row, (int, float)) and not isinstance(row, bool):
            # A bare scalar is interpreted as P(y=1), so confidence in the
            # predicted class is max(p, 1-p) — matching the legacy detector.
            # This is never applied to class labels: they are not probabilities,
            # and a binary label of 0 or 1 would read as perfect confidence.
            val = float(row)
            if 0.0 <= val <= 1.0:
                per_sample.append(max(val, 1.0 - val))

    if not per_sample:
        return None, None
    return per_sample, float(sum(per_sample) / len(per_sample))


def record_inference(
    *,
    db,
    model_name: str,
    model_version: Optional[str],
    deployment_id: Optional[str],
    rayservice_name: Optional[str],
    request_id: Optional[str],
    client_ip: Optional[str],
    predictions: Any,
    probabilities: Any,
    df: pd.DataFrame,
    latency_ms: float,
) -> bool:
    """
    Persist one telemetry row for a served prediction.

    Returns True when the row was written. Every exception is swallowed and
    logged at debug: telemetry must never turn a successful inference into a
    failed one.
    """
    try:
        from app.core.config import settings
        from app.models.drift import InferenceRecord
    except Exception as import_err:
        logger.debug(f"Drift telemetry unavailable: {import_err}")
        return False

    try:
        max_cols = int(getattr(settings, "DRIFT_MAX_FEATURE_COLUMNS", 60))
        max_rows = int(getattr(settings, "DRIFT_MAX_FEATURE_ROWS", 20))

        features, truncated, captured_feature_count = _bounded_payload(df, max_cols, max_rows)

        per_sample_conf, mean_conf = confidence_from_probabilities(probabilities)

        prediction_payload = predictions
        if isinstance(prediction_payload, (list, tuple)):
            prediction_payload = json_safe(list(prediction_payload)[:max_rows])
        else:
            prediction_payload = json_safe(prediction_payload)

        record = InferenceRecord(
            model_name=str(model_name)[:100],
            model_version=(str(model_version)[:50] if model_version is not None else None),
            deployment_id=(str(deployment_id)[:100] if deployment_id else None),
            rayservice_name=(str(rayservice_name)[:100] if rayservice_name else None),
            request_id=(str(request_id)[:64] if request_id else None),
            client_ip=(str(client_ip)[:64] if client_ip else None),
            prediction=prediction_payload,
            probabilities=(
                json_safe(list(probabilities)[:max_rows])
                if isinstance(probabilities, (list, tuple))
                else json_safe(probabilities)
            ),
            has_probabilities=per_sample_conf is not None,
            confidence=mean_conf,
            latency_ms=float(latency_ms) if latency_ms is not None else None,
            features=features,
            features_truncated=bool(truncated),
            feature_count=captured_feature_count,
            recorded_at=datetime.now(timezone.utc),
        )
        db.add(record)
        db.commit()
        return True
    except Exception as rec_err:
        try:
            db.rollback()
        except Exception:
            pass
        logger.debug(f"Non-fatal inference telemetry write failed: {rec_err}")
        return False
