"""
Shared statistical primitives for drift detection.

Both the dataset poisoning detector and the drift detectors must agree on what
"significant" means, so the distribution math lives here once instead of being
written twice and drifting apart. `poisoning_detector.py` delegates to these
functions; the drift detectors call them directly.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

import numpy as np
import pandas as pd
from scipy.stats import chi2_contingency, fisher_exact, ks_2samp

# A category appearing in the candidate but absent from the baseline is evidence
# on its own, so it is counted rather than dropped.
DEFAULT_TOP_CATEGORIES = 32


# ── Distribution comparison ────────────────────────────────────────────────
def compute_psi(
    baseline: pd.Series,
    candidate: pd.Series,
    bins: int = 10,
    eps: float = 1e-4,
) -> float:
    """
    Population Stability Index between two distributions.

    Bins are cut on the baseline so the comparison is anchored to the reference
    rather than to whichever window happens to be wider. PSI >= 0.25 indicates
    significant drift.
    """
    clean_base = pd.Series(baseline).dropna()
    clean_cand = pd.Series(candidate).dropna()
    if len(clean_base) == 0 or len(clean_cand) == 0:
        return 0.0

    quantiles = np.linspace(0, 1, bins + 1)
    bin_edges = np.percentile(clean_base, quantiles * 100)
    bin_edges = np.unique(bin_edges)
    if len(bin_edges) < 2:
        return 0.0

    bin_edges[0] = -np.inf
    bin_edges[-1] = np.inf

    base_counts = pd.cut(clean_base, bins=bin_edges).value_counts(sort=False)
    cand_counts = pd.cut(clean_cand, bins=bin_edges).value_counts(sort=False)

    base_pct = (base_counts / len(clean_base)).replace(0, eps)
    cand_pct = (cand_counts / len(clean_cand)).replace(0, eps)

    psi_val = np.sum((cand_pct - base_pct) * np.log(cand_pct / base_pct))
    return float(psi_val)


def ks_two_sample(baseline: pd.Series, candidate: pd.Series) -> Optional[tuple[float, float]]:
    """Two-sample KS test. Returns (D, p_value) or None when either side is too
    small for the test to mean anything."""
    b_vals = pd.to_numeric(pd.Series(baseline), errors="coerce").dropna()
    c_vals = pd.to_numeric(pd.Series(candidate), errors="coerce").dropna()
    if len(b_vals) < 5 or len(c_vals) < 5:
        return None
    res = ks_2samp(b_vals, c_vals)
    return float(res.statistic), float(res.pvalue)


def categorical_contingency(
    baseline_labels: pd.Series,
    candidate_labels: pd.Series,
    alpha: float,
    seed: int = 42,
) -> Optional[dict[str, Any]]:
    """
    Compares class proportions across two windows.

    Uses a 2xC chi-square test of homogeneity, falling back to Fisher's exact
    test for sparse 2x2 tables and to a Monte Carlo permutation test for sparse
    multiclass tables, where the chi-square approximation is unreliable.

    Returns None when the comparison is not meaningful (fewer than 5 samples a
    side, or fewer than 2 classes).
    """
    b_clean = pd.Series(baseline_labels).dropna()
    c_clean = pd.Series(candidate_labels).dropna()

    if len(b_clean) < 5 or len(c_clean) < 5:
        return None

    all_classes = np.union1d(b_clean.unique(), c_clean.unique())
    if len(all_classes) < 2:
        return None

    b_counts = b_clean.value_counts().reindex(all_classes, fill_value=0).values
    c_counts = c_clean.value_counts().reindex(all_classes, fill_value=0).values
    contingency = np.array([b_counts, c_counts])

    try:
        stat, p_val, dof, expected = chi2_contingency(contingency)
        if len(all_classes) == 2 and np.any(expected < 5):
            _, p_val = fisher_exact(contingency)
            stat = float("nan")
            test_type = "fisher_exact"
        elif len(all_classes) > 2 and np.any(expected < 5):
            combined = np.concatenate([b_clean.values, c_clean.values])
            n_b = len(b_clean)
            observed = float(stat)
            rng = np.random.default_rng(seed=seed)
            num_perms = 1000
            count_extreme = 0
            for _ in range(num_perms):
                perm = rng.permutation(combined)
                p_b = (
                    pd.Series(perm[:n_b])
                    .value_counts()
                    .reindex(all_classes, fill_value=0)
                    .values
                )
                p_c = (
                    pd.Series(perm[n_b:])
                    .value_counts()
                    .reindex(all_classes, fill_value=0)
                    .values
                )
                p_stat, _, _, _ = chi2_contingency(np.array([p_b, p_c]))
                if p_stat >= observed:
                    count_extreme += 1
            p_val = float((count_extreme + 1) / (num_perms + 1))
            test_type = "chi2_contingency_monte_carlo"
        else:
            test_type = "chi2_contingency"

        return {
            "test_type": test_type,
            "statistic": float(stat) if not np.isnan(stat) else 0.0,
            "p_value": float(p_val),
            "threshold": alpha,
            "significant": bool(p_val < alpha),
            "classes": [str(c) for c in all_classes],
            "baseline_counts": [int(x) for x in b_counts],
            "candidate_counts": [int(x) for x in c_counts],
        }
    except Exception:
        return None


def jensen_shannon(p: np.ndarray, q: np.ndarray, eps: float = 1e-12) -> float:
    """Jensen-Shannon divergence between two normalised distributions, in
    bits (base 2). Bounded to [0, 1]."""
    p = np.asarray(p, dtype=float)
    q = np.asarray(q, dtype=float)
    p = p / p.sum() if p.sum() > 0 else p
    q = q / q.sum() if q.sum() > 0 else q
    m = 0.5 * (p + q)

    def _kl(a: np.ndarray, b: np.ndarray) -> float:
        a = np.clip(a, eps, None)
        b = np.clip(b, eps, None)
        return float(np.sum(a * np.log2(a / b)))

    return float(min(1.0, 0.5 * _kl(p, m) + 0.5 * _kl(q, m)))


def proportion_psi(
    baseline_counts: dict[str, float],
    candidate_counts: dict[str, float],
    eps: float = 1e-4,
) -> float:
    """PSI over arbitrary category proportions (used for prediction drift, where
    the categories are predicted classes rather than histogram bins)."""
    base_total = sum(baseline_counts.values())
    cand_total = sum(candidate_counts.values())
    if base_total <= 0 or cand_total <= 0:
        return 0.0

    categories = set(baseline_counts) | set(candidate_counts)
    psi = 0.0
    for cat in categories:
        b_pct = baseline_counts.get(cat, 0.0) / base_total
        c_pct = candidate_counts.get(cat, 0.0) / cand_total
        b_pct = b_pct if b_pct > 0 else eps
        c_pct = c_pct if c_pct > 0 else eps
        psi += (c_pct - b_pct) * np.log(c_pct / b_pct)
    return float(psi)


# ── Baseline profiles ──────────────────────────────────────────────────────
def compute_feature_stats(
    df: pd.DataFrame,
    top_categories: int = DEFAULT_TOP_CATEGORIES,
    sample_size: int = 0,
) -> dict[str, Any]:
    """
    Per-feature reference statistics for a baseline.

    Numeric features record moments, quantiles and a fixed histogram so a later
    window can be compared bin-by-bin; categorical features record category
    counts so frequency shifts can be measured. Missing-rate is tracked for both
    because an unexpected rise in nulls is itself a drift signal.

    When `sample_size > 0`, numeric features additionally retain a bounded
    reservoir of raw reference values. Two-sample tests (KS) and PSI both need
    an actual sample; summary statistics alone cannot support them.
    """
    stats: dict[str, Any] = {}
    if df is None or len(df.columns) == 0:
        return stats

    rng = np.random.default_rng(seed=42)

    for col in df.columns:
        series = df[col]
        name = str(col)
        missing = int(series.isna().sum())
        entry: dict[str, Any] = {
            "dtype": str(series.dtype),
            "kind": "numeric" if pd.api.types.is_numeric_dtype(series) else "categorical",
            "count": int(len(series)),
            "missing_count": missing,
            "missing_rate": float(missing / len(series)) if len(series) else 0.0,
        }

        if entry["kind"] == "numeric":
            values = pd.to_numeric(series, errors="coerce").dropna()
            entry["sample_count"] = int(len(values))
            if len(values) > 0:
                entry["mean"] = float(values.mean())
                entry["std"] = float(values.std(ddof=0))
                entry["min"] = float(values.min())
                entry["max"] = float(values.max())
                quantiles = values.quantile([0.25, 0.5, 0.75])
                entry["quantiles"] = {
                    "p25": float(quantiles.loc[0.25]),
                    "p50": float(quantiles.loc[0.5]),
                    "p75": float(quantiles.loc[0.75]),
                }
                entry["histogram"] = _histogram(values)
                if sample_size > 0:
                    entry["sample"] = _reservoir_sample(values, sample_size, rng)
        else:
            counts = series.dropna().astype(str).value_counts()
            entry["sample_count"] = int(counts.sum())
            entry["category_counts"] = {
                str(k): int(v) for k, v in counts.head(top_categories).items()
            }
            entry["distinct_count"] = int(counts.shape[0])
            if counts.shape[0] > top_categories:
                entry["categories_truncated"] = True

        stats[name] = entry

    return stats


def _histogram(values: pd.Series, bins: int = 10) -> dict[str, Any]:
    """Fixed-bin histogram whose edges are stored so a later window can be
    re-binned against the reference rather than its own quantiles."""
    if len(values) == 0:
        return {}
    try:
        counts, edges = np.histogram(values.to_numpy(dtype=float), bins=bins)
        return {
            "bin_edges": [float(e) for e in edges],
            "counts": [int(c) for c in counts],
        }
    except Exception:
        return {}


def _reservoir_sample(values: pd.Series, size: int, rng: np.random.Generator) -> list[float]:
    """
    Deterministic bounded sample of the reference values.

    Capped so a baseline on a large training set does not store the whole
    dataset, and seeded so recomputing a baseline for the same data yields the
    same sample — otherwise the reference would not be reproducible.
    """
    try:
        arr = values.to_numpy(dtype=float)
    except Exception:
        return []
    if arr.size == 0:
        return []
    if arr.size <= size:
        return [float(v) for v in arr]
    idx = rng.choice(arr.size, size=size, replace=False)
    return [float(v) for v in arr[idx]]


def compute_confidence_stats(
    confidences: list[float], sample_size: int = 0
) -> dict[str, Any]:
    """Reference confidence distribution: quantiles, mean, and the share of
    low-confidence predictions. Calibrated per model rather than assuming one
    universal threshold fits every model."""
    clean = [float(c) for c in confidences if c is not None and not np.isnan(float(c))]
    if not clean:
        return {}

    arr = np.asarray(clean, dtype=float)
    quantiles = np.percentile(arr, [10, 25, 50, 75, 90])
    counts, edges = np.histogram(arr, bins=np.linspace(0.0, 1.0, 11))

    return {
        "sample_count": int(arr.size),
        "mean": float(arr.mean()),
        "std": float(arr.std(ddof=0)),
        "quantiles": {
            "p10": float(quantiles[0]),
            "p25": float(quantiles[1]),
            "p50": float(quantiles[2]),
            "p75": float(quantiles[3]),
            "p90": float(quantiles[4]),
        },
        "histogram": {
            "bin_edges": [float(e) for e in edges],
            "counts": [int(c) for c in counts],
        },
        # A generous reference-relative trigger: fire when the current share of
        # low-confidence predictions exceeds the reference by a wide margin.
        "low_confidence_rate": float(np.mean(arr < 0.60)),
        # Raw reference values, bounded, so the observation window can be
        # compared with a two-sample test rather than only against summary
        # statistics. Sampled across the window rather than taking the first N,
        # which would bias the reference toward earlier predictions.
        "sample": _reservoir_sample(
            pd.Series(arr, dtype=float), sample_size, np.random.default_rng(seed=42)
        )
        if sample_size > 0
        else [],
    }


def compute_prediction_dist(predictions: list[Any]) -> dict[str, float]:
    """Class -> proportion. Keys are strings so JSON round-trips cleanly and
    numeric class labels still match between baseline and observation."""
    clean = [p for p in predictions if p is not None]
    if not clean:
        return {}
    series = pd.Series([str(p) for p in clean])
    counts = series.value_counts()
    total = int(counts.sum())
    return {str(k): float(v) / total for k, v in counts.items()}


def schema_hash(columns: list[str], dtypes: dict[str, str]) -> str:
    """Stable hash of the feature schema. A baseline computed against one
    schema is not comparable to traffic on another, so the detectors check this
    first and say so instead of comparing incompatible columns."""
    payload = json.dumps(
        {"columns": sorted(columns), "dtypes": {k: dtypes.get(k, "") for k in sorted(columns)}},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def histogram_jsd(
    baseline_hist: Optional[dict[str, Any]],
    candidate_hist: Optional[dict[str, Any]],
) -> Optional[float]:
    """JSD between two stored histograms that share bin edges. Returns None if
    either histogram is missing or the edges disagree."""
    if not baseline_hist or not candidate_hist:
        return None
    b_edges = baseline_hist.get("bin_edges") or []
    c_edges = candidate_hist.get("bin_edges") or []
    if not b_edges or len(b_edges) != len(c_edges):
        return None
    if not np.allclose(np.asarray(b_edges, dtype=float), np.asarray(c_edges, dtype=float)):
        return None
    return jensen_shannon(
        np.asarray(baseline_hist.get("counts", []), dtype=float),
        np.asarray(candidate_hist.get("counts", []), dtype=float),
    )
