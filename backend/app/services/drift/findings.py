"""
Structured drift findings.

A finding is an observation, not an incident. It records what was measured,
against which reference, using which thresholds, and — explicitly — what it
does *not* establish. That last part is what keeps a statistical shift from
being presented as a confirmed attack.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

# Detection types — kept distinct rather than folded into one generic score,
# because each answers a different question.
DATA_DRIFT = "data_drift"
PREDICTION_DRIFT = "prediction_drift"
CONFIDENCE_DRIFT = "confidence_drift"

DETECTION_TYPES = (DATA_DRIFT, PREDICTION_DRIFT, CONFIDENCE_DRIFT)

# Limits of every label-free detector. D4/D5 (performance degradation, concept
# drift) need ground-truth outcomes, which this system does not collect, so
# these findings can never claim correctness was assessed.
GENERAL_LIMITATIONS = [
    "Ground-truth outcomes are not collected, so predictive performance has not been verified.",
    "A statistical distribution change is a signal, not proof of poisoning, evasion, or any other attack.",
    "Alternative explanations — legitimate population change, seasonality, a deployment change — remain open.",
]


@dataclass
class DriftFinding:
    """One detector's result for one model over one observation window."""

    detection_type: str
    model_name: str
    observation_start: str
    observation_end: str
    observation_samples: int
    baseline_samples: Optional[int] = None
    model_version: Optional[str] = None
    baseline_id: Optional[str] = None
    baseline_dataset_id: Optional[str] = None
    baseline_dataset_ref: Optional[str] = None
    severity: str = "MEDIUM"
    title: str = ""
    summary: str = ""
    affected_features: list[str] = field(default_factory=list)
    measurements: dict[str, Any] = field(default_factory=dict)
    thresholds: dict[str, Any] = field(default_factory=dict)
    limitations: list[str] = field(default_factory=list)
    detected_at: str = ""

    def __post_init__(self) -> None:
        if not self.detected_at:
            self.detected_at = datetime.now(timezone.utc).isoformat()
        # Any limitation beyond the general one is appended, never replaces it.
        merged = list(GENERAL_LIMITATIONS)
        for lim in self.limitations:
            if lim not in merged:
                merged.append(lim)
        self.limitations = merged

    @property
    def dedup_key(self) -> str:
        """Stable identity for alert dedup: same model, version, detection and
        affected features should collapse into one open alert."""
        features = ",".join(sorted(self.affected_features)) or "-"
        version = self.model_version or "unknown"
        return f"drift:{self.detection_type}:{self.model_name}:{version}:{features}"

    @property
    def window_key(self) -> str:
        return f"{self.observation_start}|{self.observation_end}"

    def evidence(self) -> dict[str, Any]:
        """Everything needed to reproduce and audit the finding later."""
        return asdict(self)

    def to_event_attributes(self) -> dict[str, Any]:
        """Attributes persisted on the correlated SecurityEvent."""
        return {
            "detection_type": self.detection_type,
            "model_name": self.model_name,
            "model_version": self.model_version,
            "observation_start": self.observation_start,
            "observation_end": self.observation_end,
            "observation_samples": self.observation_samples,
            "baseline_samples": self.baseline_samples,
            "baseline_dataset_ref": self.baseline_dataset_ref,
            "affected_features": self.affected_features,
            "thresholds": self.thresholds,
            "severity": self.severity,
        }


def iso(dt: datetime) -> str:
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()
