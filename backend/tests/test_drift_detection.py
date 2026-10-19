"""
Drift detector regressions (D1 data drift, D2 prediction drift, D3 confidence drift).

These are deliberately DB-free: they exercise the statistics and the
finding/report logic only, so they run whether or not the Docker stack is up.
"""

import numpy as np
import pandas as pd
import pytest

from app.services.drift.detectors import (
    ConfidenceDriftDetector,
    DataDriftDetector,
    MinSampleGuard,
    PredictionDriftDetector,
)
from app.services.drift.findings import (
    CONFIDENCE_DRIFT,
    DATA_DRIFT,
    GENERAL_LIMITATIONS,
    PREDICTION_DRIFT,
    DriftFinding,
)
from app.services.drift import stats as st
from app.services.drift.telemetry import confidence_from_probabilities

THRESHOLDS = {
    "ks_alpha": 0.01,
    "ks_d_threshold": 0.15,
    "chi2_alpha": 0.01,
    "psi_threshold": 0.25,
    "jsd_threshold": 0.10,
    "missing_rate_delta": 0.10,
}


def _guard(n: int = 50) -> MinSampleGuard:
    return MinSampleGuard(n)


def _baseline_for(df: pd.DataFrame, **kwargs) -> dict:
    return st.compute_feature_stats(df, sample_size=kwargs.pop("sample_size", 5000))


# ============================================================================
# Shared statistics
# ============================================================================
def test_compute_feature_stats_reports_numeric_and_categorical():
    df = pd.DataFrame(
        {
            "age": [20, 21, 22, 23, 24, 25, 26, 27, 28, 29],
            "city": ["a", "a", "b", "b", "c", "c", "d", "d", "e", "e"],
            "score": [None, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0],
        }
    )
    stats = st.compute_feature_stats(df, sample_size=5000)

    assert stats["age"]["kind"] == "numeric"
    assert stats["age"]["mean"] == pytest.approx(24.5)
    assert stats["age"]["histogram"]["counts"]
    assert stats["age"]["sample"], "numeric baseline must retain a raw sample for KS/PSI"

    assert stats["city"]["kind"] == "categorical"
    assert sum(stats["city"]["category_counts"].values()) == 10

    # Missing rate is tracked for both kinds — a rise in nulls is itself drift.
    assert stats["score"]["missing_count"] == 1
    assert stats["score"]["missing_rate"] == pytest.approx(0.1)


def test_compute_feature_stats_respects_sample_cap():
    df = pd.DataFrame({"x": np.arange(1000, dtype=float)})
    stats = st.compute_feature_stats(df, sample_size=50)
    assert len(stats["x"]["sample"]) == 50
    assert stats["x"]["sample_count"] == 1000


def test_psi_zero_for_identical_and_large_for_shifted():
    base = pd.Series(np.random.default_rng(1).normal(0, 1, 2000))
    assert st.compute_psi(base, base.copy()) == pytest.approx(0.0, abs=1e-9)

    shifted = pd.Series(np.random.default_rng(2).normal(5, 1, 2000))
    assert st.compute_psi(base, shifted) > 0.25


def test_confidence_from_probabilities_prefers_vectors():
    confs, mean = confidence_from_probabilities([[0.2, 0.8], [0.9, 0.1], [0.55, 0.45]])
    assert confs == pytest.approx([0.8, 0.9, 0.55])
    assert mean == pytest.approx(np.mean([0.8, 0.9, 0.55]))


def test_confidence_from_probabilities_returns_none_when_absent():
    """
    When the serving contract supplied no probabilities — the case that arises
    for class labels — there is no confidence to measure.

    Applying max(p, 1-p) to a binary label makes every prediction look
    perfectly confident, which is exactly what left the legacy detector
    permanently silent. Returning None makes `has_probabilities=False`
    explicit instead.
    """
    confs, mean = confidence_from_probabilities(None)
    assert confs is None and mean is None
    assert confidence_from_probabilities([]) == (None, None)
    assert confidence_from_probabilities("not-a-vector") == (None, None)


def test_confidence_from_probabilities_treats_bare_scalar_as_p1():
    confs, _ = confidence_from_probabilities([0.3, 0.9])
    assert confs == pytest.approx([0.7, 0.9])


def test_jsd_is_zero_for_identical_distributions():
    a = np.array([0.7, 0.3])
    assert st.jensen_shannon(a, a) == pytest.approx(0.0, abs=1e-9)
    assert st.jensen_shannon(a, np.array([0.3, 0.7])) > 0.1


def test_schema_hash_ignores_column_order():
    a = st.schema_hash(["x", "y"], {"x": "float", "y": "int"})
    b = st.schema_hash(["y", "x"], {"x": "float", "y": "int"})
    assert a == b

    c = st.schema_hash(["x", "z"], {"x": "float", "z": "int"})
    assert a != c


# ============================================================================
# D1 — Data drift
# ============================================================================
def test_data_drift_detects_shifted_numeric_feature():
    rng = np.random.default_rng(7)
    ref = pd.DataFrame({"age": rng.normal(35, 8, 2000), "fare": rng.normal(30, 10, 2000)})
    obs = pd.DataFrame(
        {"age": rng.normal(75, 8, 500), "fare": rng.normal(30, 10, 500)}  # shifted
    )

    detector = DataDriftDetector(_guard(), THRESHOLDS)
    finding, notes = detector.detect(
        model_name="m",
        model_version="1",
        baseline=_baseline_for(ref),
        observation=obs,
        window_start=pd.Timestamp("2026-01-01", tz="UTC").to_pydatetime(),
        window_end=pd.Timestamp("2026-01-02", tz="UTC").to_pydatetime(),
        baseline_meta={"sample_count": 2000, "dataset_ref": "abc123"},
    )

    assert finding is not None
    assert finding.detection_type == DATA_DRIFT
    assert "age" in finding.affected_features
    assert "fare" not in finding.affected_features

    age_detail = finding.measurements["age"]
    assert any(t["test"] == "ks_2samp" for t in age_detail["tests"])
    ks = next(t for t in age_detail["tests"] if t["test"] == "ks_2samp")
    assert ks["significant"] is True
    assert ks["p_value"] < THRESHOLDS["ks_alpha"]


def test_data_drift_stays_silent_on_matching_distributions():
    rng = np.random.default_rng(11)
    ref = pd.DataFrame({"age": rng.normal(35, 8, 2000)})
    obs = pd.DataFrame({"age": rng.normal(35, 8, 500)})

    detector = DataDriftDetector(_guard(), THRESHOLDS)
    finding, _ = detector.detect(
        model_name="m",
        model_version="1",
        baseline=_baseline_for(ref),
        observation=obs,
        window_start=pd.Timestamp("2026-01-01", tz="UTC").to_pydatetime(),
        window_end=pd.Timestamp("2026-01-02", tz="UTC").to_pydatetime(),
    )
    assert finding is None


def test_data_drift_flags_removed_feature_as_schema_change():
    ref = pd.DataFrame({"age": [1.0, 2.0, 3.0], "fare": [4.0, 5.0, 6.0]})
    obs = pd.DataFrame({"age": [1.0, 2.0, 3.0]})

    detector = DataDriftDetector(_guard(3), THRESHOLDS)
    finding, _ = detector.detect(
        model_name="m",
        model_version="1",
        baseline=_baseline_for(ref),
        observation=obs,
        window_start=pd.Timestamp("2026-01-01", tz="UTC").to_pydatetime(),
        window_end=pd.Timestamp("2026-01-02", tz="UTC").to_pydatetime(),
    )
    assert finding is not None
    assert "fare" in finding.affected_features
    assert finding.measurements["fare"]["issue"] == "feature_missing_from_traffic"


def test_data_drift_flags_unseen_categorical_value():
    ref = pd.DataFrame({"city": ["a"] * 30 + ["b"] * 30})
    obs = pd.DataFrame({"city": ["a"] * 30 + ["z"] * 30})  # "z" never in reference

    detector = DataDriftDetector(_guard(10), THRESHOLDS)
    finding, _ = detector.detect(
        model_name="m",
        model_version="1",
        baseline=_baseline_for(ref),
        observation=obs,
        window_start=pd.Timestamp("2026-01-01", tz="UTC").to_pydatetime(),
        window_end=pd.Timestamp("2026-01-02", tz="UTC").to_pydatetime(),
    )
    assert finding is not None
    assert finding.measurements["city"]["unseen_categories"] == ["z"]


def test_data_drift_reports_missing_rate_jump():
    ref = pd.DataFrame({"x": np.arange(100, dtype=float)})  # 0% missing
    obs = pd.DataFrame({"x": [np.nan] * 40 + list(np.arange(60, dtype=float))})  # 40%

    detector = DataDriftDetector(_guard(10), THRESHOLDS)
    finding, _ = detector.detect(
        model_name="m",
        model_version="1",
        baseline=_baseline_for(ref),
        observation=obs,
        window_start=pd.Timestamp("2026-01-01", tz="UTC").to_pydatetime(),
        window_end=pd.Timestamp("2026-01-02", tz="UTC").to_pydatetime(),
    )
    assert finding is not None
    assert "x" in finding.affected_features
    assert finding.measurements["x"]["missing_rate_shift"]["delta"] == pytest.approx(0.4, abs=1e-6)


# ============================================================================
# D2 — Prediction drift
# ============================================================================
def test_prediction_drift_detects_proportion_shift():
    detector = PredictionDriftDetector(_guard(50), THRESHOLDS)
    finding, _ = detector.detect(
        model_name="fraud",
        model_version="3",
        baseline_dist={"legitimate": 0.92, "fraudulent": 0.08},
        baseline_samples=10000,
        predictions=["fraudulent"] * 175 + ["legitimate"] * 325,
        window_start=pd.Timestamp("2026-01-01", tz="UTC").to_pydatetime(),
        window_end=pd.Timestamp("2026-01-02", tz="UTC").to_pydatetime(),
        baseline_meta={"sample_count": 10000},
    )
    assert finding is not None
    assert finding.detection_type == PREDICTION_DRIFT
    shift = finding.measurements["__predictions__"]["class_shifts"]["fraudulent"]
    assert shift["observed"] == pytest.approx(0.35)
    assert shift["delta"] > 0.2


def test_prediction_drift_stays_silent_when_distribution_holds():
    rng = np.random.default_rng(3)
    labels = np.where(rng.random(500) < 0.08, "fraudulent", "legitimate")
    detector = PredictionDriftDetector(_guard(50), THRESHOLDS)
    finding, _ = detector.detect(
        model_name="fraud",
        model_version="3",
        baseline_dist={"legitimate": 0.92, "fraudulent": 0.08},
        baseline_samples=10000,
        predictions=list(labels),
        window_start=pd.Timestamp("2026-01-01", tz="UTC").to_pydatetime(),
        window_end=pd.Timestamp("2026-01-02", tz="UTC").to_pydatetime(),
    )
    assert finding is None


def test_prediction_drift_needs_a_baseline_and_samples():
    detector = PredictionDriftDetector(_guard(50), THRESHOLDS)

    finding, notes = detector.detect(
        model_name="m",
        model_version="1",
        baseline_dist={},
        baseline_samples=None,
        predictions=["a"] * 100,
        window_start=pd.Timestamp("2026-01-01", tz="UTC").to_pydatetime(),
        window_end=pd.Timestamp("2026-01-02", tz="UTC").to_pydatetime(),
    )
    assert finding is None and any("No reference prediction distribution" in n for n in notes)

    finding, notes = detector.detect(
        model_name="m",
        model_version="1",
        baseline_dist={"a": 1.0},
        baseline_samples=100,
        predictions=["b"] * 3,
        window_start=pd.Timestamp("2026-01-01", tz="UTC").to_pydatetime(),
        window_end=pd.Timestamp("2026-01-02", tz="UTC").to_pydatetime(),
    )
    assert finding is None and any("at least 50" in n for n in notes)


def test_prediction_drift_tracks_all_multiclass_classes():
    """Tracking only one class would miss a shift among the others."""
    baseline = {"setosa": 0.34, "versicolor": 0.33, "virginica": 0.33}
    preds = ["setosa"] * 50 + ["versicolor"] * 50 + ["virginica"] * 400
    detector = PredictionDriftDetector(_guard(50), THRESHOLDS)
    finding, _ = detector.detect(
        model_name="iris",
        model_version="1",
        baseline_dist=baseline,
        baseline_samples=3000,
        predictions=preds,
        window_start=pd.Timestamp("2026-01-01", tz="UTC").to_pydatetime(),
        window_end=pd.Timestamp("2026-01-02", tz="UTC").to_pydatetime(),
    )
    assert finding is not None
    shifts = finding.measurements["__predictions__"]["class_shifts"]
    assert set(shifts) == {"setosa", "versicolor", "virginica"}


# ============================================================================
# D3 — Confidence drift
# ============================================================================
def test_confidence_drift_detects_collapse_against_calibrated_baseline():
    """A model whose reference median is 0.91 dropping to 0.55 must fire."""
    baseline_conf = st.compute_confidence_stats(
        list(np.random.default_rng(5).normal(0.92, 0.04, 500)), sample_size=500
    )
    obs = list(np.random.default_rng(6).normal(0.55, 0.05, 300))

    detector = ConfidenceDriftDetector(_guard(50), THRESHOLDS)
    finding, _ = detector.detect(
        model_name="m",
        model_version="1",
        baseline_conf=baseline_conf,
        confidences=obs,
        window_start=pd.Timestamp("2026-01-01", tz="UTC").to_pydatetime(),
        window_end=pd.Timestamp("2026-01-02", tz="UTC").to_pydatetime(),
    )
    assert finding is not None
    assert finding.detection_type == CONFIDENCE_DRIFT
    # The finding must state what it did NOT establish.
    assert any("not checked against" in lim or "Ground-truth" in lim for lim in finding.limitations)
    assert len(finding.limitations) >= len(GENERAL_LIMITATIONS)


def test_confidence_drift_silent_when_confidence_holds():
    baseline_conf = st.compute_confidence_stats(
        list(np.random.default_rng(5).normal(0.92, 0.04, 500)), sample_size=500
    )
    obs = list(np.random.default_rng(7).normal(0.91, 0.05, 300))

    detector = ConfidenceDriftDetector(_guard(50), THRESHOLDS)
    finding, _ = detector.detect(
        model_name="m",
        model_version="1",
        baseline_conf=baseline_conf,
        confidences=obs,
        window_start=pd.Timestamp("2026-01-01", tz="UTC").to_pydatetime(),
        window_end=pd.Timestamp("2026-01-02", tz="UTC").to_pydatetime(),
    )
    assert finding is None


def test_confidence_drift_refuses_without_a_reference():
    detector = ConfidenceDriftDetector(_guard(50), THRESHOLDS)
    finding, notes = detector.detect(
        model_name="m",
        model_version="1",
        baseline_conf={},
        confidences=[0.9] * 100,
        window_start=pd.Timestamp("2026-01-01", tz="UTC").to_pydatetime(),
        window_end=pd.Timestamp("2026-01-02", tz="UTC").to_pydatetime(),
    )
    assert finding is None
    assert any("No reference confidence distribution" in n for n in notes)


def test_confidence_drift_refuses_without_probabilities():
    detector = ConfidenceDriftDetector(_guard(50), THRESHOLDS)
    finding, _ = detector.detect(
        model_name="m",
        model_version="1",
        baseline_conf=st.compute_confidence_stats([0.9] * 100, sample_size=100),
        confidences=[],
        window_start=pd.Timestamp("2026-01-01", tz="UTC").to_pydatetime(),
        window_end=pd.Timestamp("2026-01-02", tz="UTC").to_pydatetime(),
    )
    assert finding is None


# ============================================================================
# Guard + finding semantics
# ============================================================================
@pytest.mark.parametrize("detector_cls", [DataDriftDetector, PredictionDriftDetector, ConfidenceDriftDetector])
def test_min_sample_guard_blocks_every_detector(detector_cls):
    detector = detector_cls(_guard(50), THRESHOLDS)
    assert detector.insufficient(49) is True
    assert detector.insufficient(50) is False
    assert "at least 50" in detector.guard.reason(49)


def test_finding_dedup_key_is_stable_and_feature_sensitive():
    def make(features, start="2026-01-01T00:00:00+00:00"):
        return DriftFinding(
            detection_type=DATA_DRIFT,
            model_name="m",
            model_version="1",
            observation_start=start,
            observation_end="2026-01-01T01:00:00+00:00",
            observation_samples=100,
            affected_features=features,
        )

    # Same signature in a different window collapses into one alert...
    a = make(["age", "fare"])
    b = make(["fare", "age"], start="2026-01-02T00:00:00+00:00")
    assert a.dedup_key == b.dedup_key
    assert a.window_key != b.window_key

    # ...but a different affected feature is a different signature.
    assert make(["age"]).dedup_key != a.dedup_key


def test_every_finding_declares_its_limitations():
    finding = DriftFinding(
        detection_type=CONFIDENCE_DRIFT,
        model_name="m",
        model_version="1",
        observation_start="2026-01-01T00:00:00+00:00",
        observation_end="2026-01-01T01:00:00+00:00",
        observation_samples=100,
        limitations=["Confidence is not correctness."],
    )
    assert finding.limitations[0] in GENERAL_LIMITATIONS
    assert "Confidence is not correctness." in finding.limitations

    attrs = finding.to_event_attributes()
    assert attrs["detection_type"] == CONFIDENCE_DRIFT
    assert finding.evidence()["observation_samples"] == 100
