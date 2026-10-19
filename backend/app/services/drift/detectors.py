"""
Label-free drift detectors: D1 data drift, D2 prediction drift, D3 confidence drift.

Each detector returns a DriftFinding or None. None means "not enough evidence"
— including the case where there were too few observations to test at all,
which is reported to the caller rather than silently treated as "no drift".

None of these detectors takes ground-truth labels, which is why they can run
against live traffic today. D4 (performance degradation) and D5 (concept drift)
are deliberately absent: without outcomes there is nothing to measure them on.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import numpy as np
import pandas as pd
from scipy.stats import chisquare

from app.services.drift.findings import (
    CONFIDENCE_DRIFT,
    DATA_DRIFT,
    PREDICTION_DRIFT,
    DriftFinding,
    iso,
)
from app.services.drift import stats as st

logger = logging.getLogger("drift.detectors")


class MinSampleGuard:
    """Shared gate: a KS test on a handful of rows returns meaningless p-values,
    so detectors stay silent until a window carries enough observations."""

    def __init__(self, min_observations: int):
        self.min_observations = min_observations

    def ok(self, n: int) -> bool:
        return n >= self.min_observations

    def reason(self, n: int) -> str:
        return (
            f"Only {n} observations in the window; at least "
            f"{self.min_observations} are required before distribution tests are meaningful."
        )


class DataDriftDetector:
    """
    D1 — compares incoming feature distributions against the reference dataset.

    Uses the same statistics as the poisoning detector (KS with Bonferroni,
    PSI, category-share comparison), so the two subsystems cannot disagree
    about the same numbers.
    """

    detection_type = DATA_DRIFT

    def __init__(self, guard: MinSampleGuard, thresholds: dict[str, Any]):
        self.guard = guard
        self.thresholds = thresholds

    def detect(
        self,
        *,
        model_name: str,
        model_version: Optional[str],
        baseline: dict[str, Any],
        observation: pd.DataFrame,
        window_start: Any,
        window_end: Any,
        baseline_meta: Optional[dict[str, Any]] = None,
    ) -> tuple[Optional[DriftFinding], list[str]]:
        """
        Returns (finding, notes). A finding is produced only when at least one
        feature crossed a threshold; notes explain anything that prevented a
        clean comparison (schema change, insufficient samples).
        """
        notes: list[str] = []
        baseline = baseline or {}
        if observation is None or len(observation) == 0 or not baseline:
            return None, notes

        n = len(observation)
        measurements: dict[str, Any] = {}
        affected: list[str] = []

        alpha = float(self.thresholds.get("ks_alpha", 0.01))
        d_thresh = float(self.thresholds.get("ks_d_threshold", 0.15))
        psi_thresh = float(self.thresholds.get("psi_threshold", 0.25))
        missing_delta = float(self.thresholds.get("missing_rate_delta", 0.10))

        # ── Schema first: a changed schema makes per-feature comparisons
        # meaningless, and is itself worth reporting.
        obs_cols = set(str(c) for c in observation.columns)
        base_cols = set(baseline)
        removed = sorted(base_cols - obs_cols)
        added = sorted(obs_cols - base_cols)
        kind_changed = [
            c
            for c in sorted(base_cols & obs_cols)
            if _kind_of(observation[c]) != (baseline[c] or {}).get("kind")
        ]

        for c in removed:
            measurements[c] = {
                "kind": (baseline[c] or {}).get("kind", "unknown"),
                "flagged": True,
                "tests": [],
                "issue": "feature_missing_from_traffic",
            }
            affected.append(c)
        for c in kind_changed:
            measurements[c] = {
                "kind": "schema",
                "flagged": True,
                "tests": [],
                "issue": (
                    f"feature type changed from {baseline[c].get('kind')} to "
                    f"{_kind_of(observation[c])}"
                ),
            }
            affected.append(c)
        if added:
            notes.append(
                "Traffic contained features absent from the reference: "
                + ", ".join(added)
            )

        shared = sorted(base_cols & obs_cols - set(kind_changed))
        if not shared:
            notes.append("No comparable features between baseline and observation.")
            return None, notes

        # ── Numeric features: KS (Bonferroni) + PSI, both against the stored
        # reference sample.
        numeric = [c for c in shared if _kind_of(observation[c]) == "numeric"]
        # Bonferroni across every feature tested, so more features means a
        # stricter per-feature bar rather than more false positives.
        k = len(shared)
        alpha_adj = alpha / k if k > 0 else alpha

        for c in numeric:
            entry = baseline[c] or {}
            ref_sample = entry.get("sample")
            obs_vals = pd.to_numeric(observation[c], errors="coerce").dropna()
            detail: dict[str, Any] = {
                "kind": "numeric",
                "flagged": False,
                "tests": [],
                "baseline": {
                    "mean": entry.get("mean"),
                    "std": entry.get("std"),
                    "missing_rate": entry.get("missing_rate"),
                },
                "observed": {
                    "mean": float(obs_vals.mean()) if len(obs_vals) else None,
                    "std": float(obs_vals.std(ddof=0)) if len(obs_vals) else None,
                    "missing_rate": float(observation[c].isna().mean()),
                },
                "alpha_adjusted": alpha_adj,
            }

            if ref_sample and len(ref_sample) >= 5 and len(obs_vals) >= 5:
                ks = st.ks_two_sample(pd.Series(ref_sample), obs_vals)
                if ks is not None:
                    d_val, p_val = ks
                    # Gate on significance AND effect size: a large sample will
                    # report tiny differences as significant, and a large
                    # difference on a small sample may be noise.
                    sig = (p_val < alpha_adj) and (d_val >= d_thresh)
                    detail["tests"].append(
                        {
                            "test": "ks_2samp",
                            "statistic": round(d_val, 6),
                            "p_value": p_val,
                            "threshold": d_thresh,
                            "significant": bool(sig),
                        }
                    )
                    if sig:
                        detail["flagged"] = True

                psi = st.compute_psi(pd.Series(ref_sample), obs_vals)
                psi_hit = psi >= psi_thresh
                detail["tests"].append(
                    {
                        "test": "psi",
                        "statistic": round(psi, 6),
                        "threshold": psi_thresh,
                        "significant": bool(psi_hit),
                    }
                )
                if psi_hit:
                    detail["flagged"] = True
            else:
                detail["insufficient_reference"] = True
                notes.append(
                    f"Feature '{c}' has too few reference or observed values for a "
                    f"two-sample test; compared on summary statistics only."
                )

            # Missing-rate shift is meaningful even without raw reference values.
            b_miss = entry.get("missing_rate")
            o_miss = detail["observed"]["missing_rate"]
            if b_miss is not None and (o_miss - b_miss) >= missing_delta:
                detail["missing_rate_shift"] = {
                    "baseline": b_miss,
                    "observed": o_miss,
                    "delta": round(o_miss - b_miss, 6),
                }
                detail["flagged"] = True

            measurements[c] = detail
            if detail["flagged"]:
                affected.append(c)

        # ── Categorical features: compare category shares against the stored
        # reference counts, and treat a category that never occurred in the
        # reference as evidence in its own right.
        categorical = [c for c in shared if _kind_of(observation[c]) != "numeric"]
        for c in categorical:
            entry = baseline[c] or {}
            ref_counts = {str(k): float(v) for k, v in (entry.get("category_counts") or {}).items()}
            obs_series = observation[c].dropna().astype(str)
            obs_counts = obs_series.value_counts().to_dict()
            obs_counts = {str(k): float(v) for k, v in obs_counts.items()}

            detail = {
                "kind": "categorical",
                "flagged": False,
                "tests": [],
                "baseline": {
                    "distinct_count": entry.get("distinct_count"),
                    "missing_rate": entry.get("missing_rate"),
                },
                "observed": {
                    "distinct_count": len(obs_counts),
                    "missing_rate": float(observation[c].isna().mean()),
                },
            }

            if ref_counts and obs_counts:
                psi = st.proportion_psi(ref_counts, obs_counts)
                hit = psi >= psi_thresh
                detail["tests"].append(
                    {
                        "test": "proportion_psi",
                        "statistic": round(psi, 6),
                        "threshold": psi_thresh,
                        "significant": bool(hit),
                    }
                )
                if hit:
                    detail["flagged"] = True

                novel = sorted(set(obs_counts) - set(ref_counts))
                if novel:
                    detail["unseen_categories"] = novel[:20]
                    detail["flagged"] = True

            b_miss = entry.get("missing_rate")
            o_miss = detail["observed"]["missing_rate"]
            if b_miss is not None and (o_miss - b_miss) >= missing_delta:
                detail["missing_rate_shift"] = {
                    "baseline": b_miss,
                    "observed": o_miss,
                    "delta": round(o_miss - b_miss, 6),
                }
                detail["flagged"] = True

            measurements[c] = detail
            if detail["flagged"]:
                affected.append(c)

        if not affected:
            return None, notes

        max_d = _max_statistic(measurements, "ks_2samp")
        if len(affected) >= 3 or (max_d is not None and max_d >= 0.5):
            severity = "HIGH"
        elif len(affected) == 1 and measurements[affected[0]].get("issue"):
            severity = "LOW"
        else:
            severity = "MEDIUM"

        meta = baseline_meta or {}
        finding = DriftFinding(
            detection_type=DATA_DRIFT,
            model_name=model_name,
            model_version=model_version,
            observation_start=iso(window_start),
            observation_end=iso(window_end),
            observation_samples=n,
            baseline_samples=meta.get("sample_count"),
            baseline_id=meta.get("id"),
            baseline_dataset_id=meta.get("dataset_id"),
            baseline_dataset_ref=meta.get("dataset_ref"),
            severity=severity,
            title=f"Data drift on '{model_name}'",
            summary=(
                f"{len(affected)} feature(s) shifted against the reference dataset over "
                f"{n} observations: {', '.join(affected[:8])}"
                + ("…" if len(affected) > 8 else "")
            ),
            affected_features=affected,
            measurements=measurements,
            thresholds=self.thresholds,
            limitations=notes,
        )
        return finding, notes

    def insufficient(self, n: int) -> bool:
        return not self.guard.ok(n)


class PredictionDriftDetector:
    """
    D2 — compares the distribution of predicted classes against the reference.

    Uses only what the model emits, so it needs no labels. A rise in one class's
    share is a behavioural change worth investigating, not a defect on its own:
    genuine fraud prevalence may simply have increased.
    """

    detection_type = PREDICTION_DRIFT

    def __init__(self, guard: MinSampleGuard, thresholds: dict[str, Any]):
        self.guard = guard
        self.thresholds = thresholds

    def detect(
        self,
        *,
        model_name: str,
        model_version: Optional[str],
        baseline_dist: dict[str, float],
        baseline_samples: Optional[int],
        predictions: list[Any],
        window_start: Any,
        window_end: Any,
        baseline_meta: Optional[dict[str, Any]] = None,
    ) -> tuple[Optional[DriftFinding], list[str]]:
        notes: list[str] = []
        predictions = [p for p in predictions if p is not None]
        n = len(predictions)

        if not baseline_dist:
            notes.append("No reference prediction distribution is recorded for this model.")
            return None, notes
        if not self.guard.ok(n):
            notes.append(self.guard.reason(n))
            return None, notes

        obs_dist = st.compute_prediction_dist(predictions)
        psi_thresh = float(self.thresholds.get("psi_threshold", 0.25))
        jsd_thresh = float(self.thresholds.get("jsd_threshold", 0.10))
        alpha = float(self.thresholds.get("chi2_alpha", 0.01))

        psi = st.proportion_psi(baseline_dist, obs_dist)

        # Significance via a goodness-of-fit test: observed counts against the
        # counts the reference proportions predict for this window size.
        chi2_p = None
        chi2_stat = None
        classes = sorted(set(baseline_dist) | set(obs_dist))
        observed_counts = np.array([obs_dist.get(c, 0.0) for c in classes], dtype=float)
        expected = np.array([baseline_dist.get(c, 0.0) * n for c in classes], dtype=float)

        # scipy requires the expected counts to sum to the observed totals; the
        # reference proportions may not sum to exactly 1 after JSON round-trips.
        nonzero = expected > 0
        if nonzero.sum() >= 2 and observed_counts.sum() > 0:
            exp_nz = expected[nonzero]
            obs_nz = observed_counts[nonzero]
            exp_nz = exp_nz * (obs_nz.sum() / exp_nz.sum())
            try:
                chi2_stat, chi2_p = chisquare(obs_nz, exp_nz)
                chi2_stat = float(chi2_stat)
                chi2_p = float(chi2_p)
            except Exception as err:
                logger.debug(f"Prediction drift chi-square failed: {err}")
                chi2_stat = None
                chi2_p = None

        jsd = st.jensen_shannon(
            np.array([baseline_dist.get(c, 0.0) for c in classes], dtype=float),
            np.array([obs_dist.get(c, 0.0) for c in classes], dtype=float),
        )

        significant = bool(psi >= psi_thresh)
        reasons = []
        if significant:
            reasons.append(f"PSI={psi:.4f} >= {psi_thresh}")
        if jsd is not None and jsd >= jsd_thresh:
            significant = True
            reasons.append(f"JSD={jsd:.4f} >= {jsd_thresh}")
        if chi2_p is not None and chi2_p < alpha:
            # Significance alone is not enough — a large window makes almost
            # everything significant, so the effect size still has to matter.
            if psi >= psi_thresh / 2:
                significant = True
                reasons.append(f"chi2 p={chi2_p:.2e} < {alpha} with non-trivial effect")

        if not significant:
            return None, notes

        shifts = {
            c: {
                "baseline": baseline_dist.get(c, 0.0),
                "observed": obs_dist.get(c, 0.0),
                "delta": round(obs_dist.get(c, 0.0) - baseline_dist.get(c, 0.0), 6),
            }
            for c in classes
        }
        meta = baseline_meta or {}
        finding = DriftFinding(
            detection_type=PREDICTION_DRIFT,
            model_name=model_name,
            model_version=model_version,
            observation_start=iso(window_start),
            observation_end=iso(window_end),
            observation_samples=n,
            baseline_samples=baseline_samples,
            baseline_id=meta.get("id"),
            baseline_dataset_ref=meta.get("dataset_ref"),
            severity="HIGH" if jsd is not None and jsd >= jsd_thresh * 2 else "MEDIUM",
            title=f"Prediction drift on '{model_name}'",
            summary=(
                f"Predicted class proportions shifted over {n} observations "
                f"({'; '.join(reasons)})."
            ),
            affected_features=["__predictions__"],
            measurements={
                "__predictions__": {
                    "kind": "distribution",
                    "flagged": True,
                    "baseline_distribution": baseline_dist,
                    "observed_distribution": obs_dist,
                    "class_shifts": shifts,
                    "tests": [
                        {"test": "proportion_psi", "statistic": round(psi, 6), "threshold": psi_thresh},
                        {"test": "jensen_shannon", "statistic": round(jsd, 6), "threshold": jsd_thresh}
                        if jsd is not None
                        else {"test": "jensen_shannon", "statistic": None},
                        {"test": "chi2_goodness_of_fit", "statistic": chi2_stat, "p_value": chi2_p, "threshold": alpha},
                    ],
                }
            },
            thresholds=self.thresholds,
            limitations=notes
            + ["Class shares may have shifted because the underlying population changed, not because the model did."],
        )
        return finding, notes

    def insufficient(self, n: int) -> bool:
        return not self.guard.ok(n)


class ConfidenceDriftDetector:
    """
    D3 — compares the distribution of model confidence against the reference.

    Thresholds are calibrated from the model's own reference distribution
    rather than applying one universal cut-off to every model: a model that is
    normally 0.95-confident and one that is normally 0.65-confident should not
    be judged by the same number.

    Confidence is not correctness — a model can be confident and wrong — so the
    finding states explicitly that performance was not verified.
    """

    detection_type = CONFIDENCE_DRIFT

    def __init__(self, guard: MinSampleGuard, thresholds: dict[str, Any]):
        self.guard = guard
        self.thresholds = thresholds

    def detect(
        self,
        *,
        model_name: str,
        model_version: Optional[str],
        baseline_conf: dict[str, Any],
        confidences: list[float],
        window_start: Any,
        window_end: Any,
        baseline_meta: Optional[dict[str, Any]] = None,
    ) -> tuple[Optional[DriftFinding], list[str]]:
        notes: list[str] = []
        clean = [float(c) for c in confidences if c is not None]
        n = len(clean)

        if not baseline_conf:
            notes.append(
                "No reference confidence distribution is recorded — the baseline was "
                "built before probabilities were available, so it cannot be compared."
            )
            return None, notes
        if not self.guard.ok(n):
            notes.append(self.guard.reason(n))
            return None, notes

        baseline_samples = baseline_conf.get("sample") or []
        base_quant = baseline_conf.get("quantiles") or {}
        base_p50 = base_quant.get("p50")
        base_p10 = base_quant.get("p10")
        base_p90 = base_quant.get("p90")

        obs = np.asarray(clean, dtype=float)
        obs_quant = np.percentile(obs, [10, 50, 90])
        obs_p10, obs_p50, obs_p90 = float(obs_quant[0]), float(obs_quant[1]), float(obs_quant[2])

        jsd_thresh = float(self.thresholds.get("jsd_threshold", 0.10))
        # Reference-relative gates: the observation's median falls below the
        # reference's 10th percentile, i.e. today's typical confidence would
        # have been an unusually bad day for this model.
        median_collapse = base_p10 is not None and obs_p50 <= float(base_p10)
        # Or the share of low-confidence predictions rose sharply versus the
        # model's own reference rate.
        base_low = float(baseline_conf.get("low_confidence_rate") or 0.0)
        obs_low = float(np.mean(obs < 0.60))
        low_rate_jump = (obs_low - base_low) >= 0.25

        # Two-sample KS against the stored reference sample, when present.
        # The KS gate alone is deliberately not enough: confidence
        # distributions are usually tight, so with a few hundred observations
        # KS reports a statistically significant result for a shift of a few
        # hundredths — which would raise an alert on essentially every window.
        # The effect must therefore also exceed what the model's own reference
        # spread would produce, making the yardstick per-model rather than
        # universal.
        ks_d = ks_p = None
        base_std = float(baseline_conf.get("std") or 0.0)
        base_p50_val = float(base_p50) if base_p50 is not None else None
        median_drop = (
            (base_p50_val - obs_p50)
            if base_p50_val is not None
            else 0.0
        )
        calibrated_drop = max(0.05, base_std)

        if baseline_samples and len(baseline_samples) >= 5:
            ks = st.ks_two_sample(pd.Series(baseline_samples), obs)
            if ks is not None:
                ks_d, ks_p = ks
        ks_hit = (
            ks_d is not None
            and ks_p is not None
            and ks_p < float(self.thresholds.get("ks_alpha", 0.01))
            and ks_d >= float(self.thresholds.get("ks_d_threshold", 0.15))
            and median_drop >= calibrated_drop
        )

        jsd = st.histogram_jsd(baseline_conf.get("histogram"), st.compute_confidence_stats(clean).get("histogram"))
        jsd_hit = jsd is not None and jsd >= jsd_thresh

        if not (median_collapse or low_rate_jump or ks_hit or jsd_hit):
            return None, notes

        reasons = []
        if median_collapse:
            reasons.append(f"median {obs_p50:.3f} <= reference p10 {float(base_p10):.3f}")
        if low_rate_jump:
            reasons.append(f"low-confidence rate {obs_low:.3f} vs reference {base_low:.3f}")
        if ks_hit:
            reasons.append(f"KS D={ks_d:.4f} (p={ks_p:.2e})")
        if jsd_hit:
            reasons.append(f"histogram JSD={jsd:.4f}")

        meta = baseline_meta or {}
        finding = DriftFinding(
            detection_type=CONFIDENCE_DRIFT,
            model_name=model_name,
            model_version=model_version,
            observation_start=iso(window_start),
            observation_end=iso(window_end),
            observation_samples=n,
            baseline_samples=baseline_conf.get("sample_count"),
            baseline_id=meta.get("id"),
            baseline_dataset_ref=meta.get("dataset_ref"),
            severity="HIGH" if median_collapse and low_rate_jump else "MEDIUM",
            title=f"Confidence drift on '{model_name}'",
            summary=(
                f"Confidence distribution changed relative to the reference window "
                f"over {n} observations ({'; '.join(reasons)})."
            ),
            affected_features=["__confidence__"],
            measurements={
                "__confidence__": {
                    "kind": "confidence",
                    "flagged": True,
                    "baseline": {
                        "mean": baseline_conf.get("mean"),
                        "quantiles": base_quant,
                        "low_confidence_rate": base_low,
                    },
                    "observed": {
                        "mean": float(obs.mean()),
                        "p10": obs_p10,
                        "p50": obs_p50,
                        "p90": obs_p90,
                        "low_confidence_rate": obs_low,
                    },
                    "tests": [
                        {"test": "median_below_reference_p10", "significant": median_collapse},
                        {"test": "low_confidence_rate_jump", "significant": low_rate_jump,
                         "baseline": base_low, "observed": obs_low},
                        {"test": "ks_2samp", "statistic": ks_d, "p_value": ks_p,
                         "significant": ks_hit,
                         "median_drop": round(median_drop, 6),
                         "required_drop": round(calibrated_drop, 6)},
                        {"test": "histogram_jsd", "statistic": jsd, "threshold": jsd_thresh,
                         "significant": jsd_hit},
                    ],
                }
            },
            thresholds=self.thresholds,
            limitations=notes
            + [
                "Confidence is not correctness: predictions were not checked against "
                "ground truth, so accuracy on this window is unknown."
            ],
        )
        return finding, notes

    def insufficient(self, n: int) -> bool:
        return not self.guard.ok(n)


# ── helpers ────────────────────────────────────────────────────────────────
def _kind_of(series: pd.Series) -> str:
    return "numeric" if pd.api.types.is_numeric_dtype(series) else "categorical"


def _max_statistic(measurements: dict[str, Any], test_name: str) -> Optional[float]:
    best: Optional[float] = None
    for detail in measurements.values():
        for test in detail.get("tests") or []:
            if test.get("test") == test_name and test.get("statistic") is not None:
                val = float(test["statistic"])
                best = val if best is None else max(best, val)
    return best
