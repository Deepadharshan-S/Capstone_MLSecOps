import logging
from dataclasses import dataclass, field
from typing import Optional, Union

import numpy as np
import pandas as pd
from scipy.stats import chi2_contingency, fisher_exact, ks_2samp

logger = logging.getLogger("dataset.poisoning_detector")


@dataclass
class ValidationViolation:
    feature: str
    test_type: str  # "ks_2samp", "chi2_contingency", "fisher_exact", "psi"
    statistic: float
    p_value: Optional[float]
    threshold: float
    message: str


@dataclass
class ValidationResult:
    passed: bool
    drift_detected: bool
    summary: dict = field(default_factory=dict)
    violations: list[ValidationViolation] = field(default_factory=list)


class StatisticalPoisoningDetector:
    """
    Evaluates dataset pairs for statistical data poisoning and distribution shift
    using non-parametric Kolmogorov-Smirnov tests, Chi-Square contingency tests,
    and Population Stability Index (PSI).

    Scientific Note:
    Statistical divergence flags distribution shift; it does not constitute conclusive
    forensic proof of adversarial intent, nor does a failure to reject H0 prove that
    data is free from stealthy poisoning.
    """

    def __init__(
        self,
        ks_alpha: Optional[float] = None,
        ks_d_threshold: Optional[float] = None,
        chi2_alpha: Optional[float] = None,
        psi_threshold: Optional[float] = None,
        max_sample_rows: Optional[int] = None,
        allow_new_columns: Optional[bool] = None,
    ):
        try:
            from app.core.config import settings

            self.ks_alpha = ks_alpha if ks_alpha is not None else getattr(settings, "POISONING_KS_ALPHA", 0.01)
            self.ks_d_threshold = (
                ks_d_threshold if ks_d_threshold is not None else getattr(settings, "POISONING_KS_D_THRESHOLD", 0.15)
            )
            self.chi2_alpha = (
                chi2_alpha if chi2_alpha is not None else getattr(settings, "POISONING_CHI2_ALPHA", 0.01)
            )
            self.psi_threshold = (
                psi_threshold if psi_threshold is not None else getattr(settings, "POISONING_PSI_THRESHOLD", 0.25)
            )
            self.max_sample_rows = (
                max_sample_rows if max_sample_rows is not None else getattr(settings, "POISONING_MAX_SAMPLE_ROWS", 50000)
            )
            self.allow_new_columns = (
                allow_new_columns
                if allow_new_columns is not None
                else getattr(settings, "POISONING_ALLOW_NEW_COLUMNS", False)
            )
        except Exception:
            self.ks_alpha = ks_alpha if ks_alpha is not None else 0.01
            self.ks_d_threshold = ks_d_threshold if ks_d_threshold is not None else 0.15
            self.chi2_alpha = chi2_alpha if chi2_alpha is not None else 0.01
            self.psi_threshold = psi_threshold if psi_threshold is not None else 0.25
            self.max_sample_rows = max_sample_rows if max_sample_rows is not None else 50000
            self.allow_new_columns = allow_new_columns if allow_new_columns is not None else False

    def validate_schemas(
        self,
        baseline_df: pd.DataFrame,
        candidate_df: pd.DataFrame,
        allow_new_columns: Optional[bool] = None,
    ) -> list[ValidationViolation]:
        """
        Enforces strict schema consistency:
        - Rejects candidate if any baseline columns are missing or renamed.
        - Rejects newly added columns unless explicitly permitted by policy.
        - Rejects column data type incompatibilities (e.g. numeric vs object/string).
        """
        allow_new = allow_new_columns if allow_new_columns is not None else self.allow_new_columns
        violations: list[ValidationViolation] = []

        base_cols = list(baseline_df.columns)
        cand_cols = list(candidate_df.columns)

        # 1. Missing columns
        missing_cols = set(base_cols) - set(cand_cols)
        for col in sorted(missing_cols):
            violations.append(
                ValidationViolation(
                    feature=col,
                    test_type="schema_validation",
                    statistic=1.0,
                    p_value=None,
                    threshold=0.0,
                    message=f"Required baseline column '{col}' is missing in candidate dataset schema.",
                )
            )

        # 2. Unexpected newly added columns
        if not allow_new:
            new_cols = set(cand_cols) - set(base_cols)
            for col in sorted(new_cols):
                violations.append(
                    ValidationViolation(
                        feature=col,
                        test_type="schema_validation",
                        statistic=1.0,
                        p_value=None,
                        threshold=0.0,
                        message=f"Candidate dataset contains unexpected new column '{col}' not present in baseline schema.",
                    )
                )

        # 3. Data type compatibility for common columns
        common_cols = [c for c in base_cols if c in cand_cols]
        for col in common_cols:
            b_is_num = pd.api.types.is_numeric_dtype(baseline_df[col])
            c_is_num = pd.api.types.is_numeric_dtype(candidate_df[col])
            if b_is_num != c_is_num:
                violations.append(
                    ValidationViolation(
                        feature=col,
                        test_type="type_incompatibility",
                        statistic=1.0,
                        p_value=None,
                        threshold=0.0,
                        message=(
                            f"Column '{col}' data type mismatch: baseline is {baseline_df[col].dtype}, "
                            f"candidate is {candidate_df[col].dtype}."
                        ),
                    )
                )

        return violations

    def compute_psi(
        self,
        baseline: pd.Series,
        candidate: pd.Series,
        bins: int = 10,
        eps: float = 1e-4,
    ) -> float:
        """
        Computes Population Stability Index (PSI) between baseline and candidate feature distributions.
        PSI >= 0.25 indicates significant distribution drift.
        """
        clean_base = baseline.dropna()
        clean_cand = candidate.dropna()
        if len(clean_base) == 0 or len(clean_cand) == 0:
            return 0.0

        # Create quantiles on baseline
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

    def evaluate_continuous_features(
        self,
        baseline_df: pd.DataFrame,
        candidate_df: pd.DataFrame,
        feature_cols: list[str],
        alpha: Optional[float] = None,
        d_threshold: Optional[float] = None,
        psi_threshold: Optional[float] = None,
    ) -> list[ValidationViolation]:
        """
        Evaluates continuous numerical features using two-sample KS-tests with
        Bonferroni multiple-comparison correction, supplemented by PSI.
        """
        alpha_base = alpha if alpha is not None else self.ks_alpha
        d_thresh = d_threshold if d_threshold is not None else self.ks_d_threshold
        psi_thresh = psi_threshold if psi_threshold is not None else self.psi_threshold

        violations: list[ValidationViolation] = []
        if not feature_cols:
            return violations

        # Bonferroni correction for multiple hypothesis testing
        k = len(feature_cols)
        alpha_adj = alpha_base / k

        for col in feature_cols:
            if col not in baseline_df.columns or col not in candidate_df.columns:
                continue

            b_vals = pd.to_numeric(baseline_df[col], errors="coerce").dropna()
            c_vals = pd.to_numeric(candidate_df[col], errors="coerce").dropna()

            if len(b_vals) < 5 or len(c_vals) < 5:
                # Sample size too small for reliable KS test
                continue

            ks_res = ks_2samp(b_vals, c_vals)
            stat_d = float(ks_res.statistic)
            p_val = float(ks_res.pvalue)

            psi_val = self.compute_psi(b_vals, c_vals)

            # Flag if p-value passes Bonferroni threshold AND effect size D exceeds threshold
            if p_val < alpha_adj and stat_d >= d_thresh:
                violations.append(
                    ValidationViolation(
                        feature=col,
                        test_type="ks_2samp",
                        statistic=stat_d,
                        p_value=p_val,
                        threshold=d_thresh,
                        message=(
                            f"Continuous feature '{col}' exhibits significant shift "
                            f"(KS D={stat_d:.4f} >= {d_thresh:.4f}, p={p_val:.2e} < {alpha_adj:.2e})."
                        ),
                    )
                )

            # Also flag if PSI indicates critical divergence
            if psi_val >= psi_thresh:
                violations.append(
                    ValidationViolation(
                        feature=col,
                        test_type="psi",
                        statistic=psi_val,
                        p_value=None,
                        threshold=psi_thresh,
                        message=(
                            f"Continuous feature '{col}' has high population instability "
                            f"(PSI={psi_val:.4f} >= {psi_thresh:.4f})."
                        ),
                    )
                )

        return violations

    def evaluate_target_label_distribution(
        self,
        baseline_labels: pd.Series,
        candidate_labels: pd.Series,
        target_name: str = "target",
        alpha: Optional[float] = None,
    ) -> list[ValidationViolation]:
        """
        Evaluates categorical target labels across baseline and candidate samples using a
        2xC contingency table Chi-Square test of homogeneity (independence).
        Falls back to Fisher's exact test for 2x2 binary tables with sparse expected counts (< 5).
        For sparse multiclass tables (C > 2 with expected counts < 5), executes a Monte Carlo
        permutation test to guarantee non-asymptotic exact probability assessment.
        """
        alpha_val = alpha if alpha is not None else self.chi2_alpha
        violations: list[ValidationViolation] = []

        b_clean = baseline_labels.dropna()
        c_clean = candidate_labels.dropna()

        if len(b_clean) < 5 or len(c_clean) < 5:
            return violations

        all_classes = np.union1d(b_clean.unique(), c_clean.unique())
        if len(all_classes) < 2:
            return violations

        b_counts = b_clean.value_counts().reindex(all_classes, fill_value=0).values
        c_counts = c_clean.value_counts().reindex(all_classes, fill_value=0).values

        contingency = np.array([b_counts, c_counts])

        try:
            stat, p_val, dof, expected = chi2_contingency(contingency)
            # Check Cochran's rule: sparse counts on binary vs multiclass
            if len(all_classes) == 2 and np.any(expected < 5):
                _, p_val = fisher_exact(contingency)
                stat = float("nan")
                test_type = "fisher_exact"
            elif len(all_classes) > 2 and np.any(expected < 5):
                # STAT-01: Monte Carlo permutation test for sparse multiclass contingency tables
                combined_labels = np.concatenate([b_clean.values, c_clean.values])
                n_b = len(b_clean)
                observed_stat = float(stat)
                rng = np.random.default_rng(seed=42)
                num_perms = 1000
                count_extreme = 0
                for _ in range(num_perms):
                    perm = rng.permutation(combined_labels)
                    p_b_counts = pd.Series(perm[:n_b]).value_counts().reindex(all_classes, fill_value=0).values
                    p_c_counts = pd.Series(perm[n_b:]).value_counts().reindex(all_classes, fill_value=0).values
                    p_contingency = np.array([p_b_counts, p_c_counts])
                    p_stat, _, _, _ = chi2_contingency(p_contingency)
                    if p_stat >= observed_stat:
                        count_extreme += 1
                p_val = float((count_extreme + 1) / (num_perms + 1))
                test_type = "chi2_contingency_monte_carlo"
            else:
                test_type = "chi2_contingency"

            if p_val < alpha_val:
                violations.append(
                    ValidationViolation(
                        feature=target_name,
                        test_type=test_type,
                        statistic=float(stat) if not np.isnan(stat) else 0.0,
                        p_value=float(p_val),
                        threshold=alpha_val,
                        message=(
                            f"Target label '{target_name}' distribution shifted significantly "
                            f"({test_type} p={p_val:.2e} < {alpha_val:.2e})."
                        ),
                    )
                )
        except Exception as e:
            logger.warning(f"Error computing chi-square test on target '{target_name}': {e}")

        return violations

    def validate_datasets(
        self,
        baseline_df: pd.DataFrame,
        candidate_df: pd.DataFrame,
        target_column: Optional[str] = None,
        feature_columns: Optional[list[str]] = None,
        ks_alpha: Optional[float] = None,
        ks_d_threshold: Optional[float] = None,
        chi2_alpha: Optional[float] = None,
        psi_threshold: Optional[float] = None,
        allow_new_columns: Optional[bool] = None,
    ) -> ValidationResult:
        """
        Comprehensive statistical and schema gating entry point comparing candidate dataset to baseline.
        Enforces:
        1. Schema consistency (no removed/renamed columns, no unvetted new columns, compatible dtypes).
        2. Non-empty comparable feature verification.
        3. Bounded-sample statistical distribution tests (KS + Bonferroni, PSI, Chi2 / Fisher / Monte Carlo).
        """
        violations: list[ValidationViolation] = []

        # 1. Strict schema validation
        schema_violations = self.validate_schemas(
            baseline_df, candidate_df, allow_new_columns=allow_new_columns
        )
        if schema_violations:
            violations.extend(schema_violations)
            return ValidationResult(
                passed=False,
                drift_detected=True,
                summary={
                    "baseline_rows": len(baseline_df),
                    "candidate_rows": len(candidate_df),
                    "schema_violations_count": len(schema_violations),
                    "violations_count": len(violations),
                    "passed": False,
                },
                violations=violations,
            )

        # Determine target column
        detected_target = target_column
        if not detected_target:
            candidates = ["target", "label", "class", "churn", "is_fraud", "fraud"]
            for cand in candidates:
                if cand in baseline_df.columns and cand in candidate_df.columns:
                    detected_target = cand
                    break

        # Determine numerical feature columns
        if feature_columns:
            features_to_check = [c for c in feature_columns if c in baseline_df.columns and c in candidate_df.columns]
        else:
            common_cols = [c for c in baseline_df.columns if c in candidate_df.columns and c != detected_target]
            # Keep numeric columns
            features_to_check = [
                c for c in common_cols
                if pd.api.types.is_numeric_dtype(baseline_df[c]) and pd.api.types.is_numeric_dtype(candidate_df[c])
            ]

        # Ensure we have at least some comparable features or target
        has_target = bool(detected_target and detected_target in baseline_df.columns and detected_target in candidate_df.columns)
        if len(features_to_check) == 0 and not has_target:
            violations.append(
                ValidationViolation(
                    feature="schema",
                    test_type="empty_feature_set",
                    statistic=1.0,
                    p_value=None,
                    threshold=0.0,
                    message="No comparable continuous numeric features or target labels found between candidate and baseline schemas.",
                )
            )
            return ValidationResult(
                passed=False,
                drift_detected=True,
                summary={
                    "baseline_rows": len(baseline_df),
                    "candidate_rows": len(candidate_df),
                    "violations_count": len(violations),
                    "passed": False,
                },
                violations=violations,
            )

        # 2. Resource protection & bounded memory sampling
        max_rows = self.max_sample_rows
        if len(baseline_df) > max_rows:
            baseline_eval_df = baseline_df.sample(n=max_rows, random_state=42)
        else:
            baseline_eval_df = baseline_df

        if len(candidate_df) > max_rows:
            candidate_eval_df = candidate_df.sample(n=max_rows, random_state=42)
        else:
            candidate_eval_df = candidate_df

        # 3. Continuous feature evaluations (KS + PSI)
        feat_violations = self.evaluate_continuous_features(
            baseline_eval_df,
            candidate_eval_df,
            feature_cols=features_to_check,
            alpha=ks_alpha,
            d_threshold=ks_d_threshold,
            psi_threshold=psi_threshold,
        )
        violations.extend(feat_violations)

        # 4. Target label distribution evaluation (Chi2 / Fisher / Monte Carlo)
        if has_target:
            target_violations = self.evaluate_target_label_distribution(
                baseline_eval_df[detected_target],
                candidate_eval_df[detected_target],
                target_name=detected_target,
                alpha=chi2_alpha,
            )
            violations.extend(target_violations)

        passed = len(violations) == 0
        summary = {
            "baseline_rows": len(baseline_df),
            "candidate_rows": len(candidate_df),
            "features_evaluated": len(features_to_check),
            "target_evaluated": detected_target,
            "violations_count": len(violations),
            "passed": passed,
        }

        return ValidationResult(
            passed=passed,
            drift_detected=not passed,
            summary=summary,
            violations=violations,
        )


poisoning_detector = StatisticalPoisoningDetector()
