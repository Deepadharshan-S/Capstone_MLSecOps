import io
import logging
from dataclasses import dataclass, field
from typing import Optional, Union

import numpy as np
import pandas as pd

from app.services.drift import stats as drift_stats

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

    @staticmethod
    def inspect_raw_csv_headers(csv_bytes: bytes) -> list[str]:
        """
        Extracts raw column headers directly from CSV bytes before pandas
        normalizes or mangles duplicate columns (e.g. '.1' suffixing).
        """
        import csv
        text_stream = io.StringIO(csv_bytes[:65536].decode("utf-8", errors="replace"))
        reader = csv.reader(text_stream)
        for row in reader:
            if row:
                return [str(cell) for cell in row]
        return []

    @staticmethod
    def inspect_raw_parquet_headers(parquet_bytes: bytes) -> list[str]:
        """
        Extracts field names directly from Parquet schema metadata.
        """
        import pyarrow.parquet as pq
        reader = pq.ParquetFile(io.BytesIO(parquet_bytes))
        return [str(name) for name in reader.schema.names]

    def validate_schemas(
        self,
        baseline_df: pd.DataFrame,
        candidate_df: pd.DataFrame,
        allow_new_columns: Optional[bool] = None,
        allowed_new_columns: Optional[Union[list[str], set[str]]] = None,
        target_column: Optional[str] = None,
        raw_candidate_headers: Optional[list[str]] = None,
    ) -> list[ValidationViolation]:
        """
        Enforces strict schema consistency and detects tabular schema injection attacks (P2):
        - Rejects malformed schemas or empty dataframes.
        - Rejects candidate if duplicate column names exist (in raw input or dataframe).
        - Rejects non-printable or illegal control characters in column headers.
        - Rejects whitespace padding or normalization collisions in column names.
        - Rejects column case-folding collisions.
        - Rejects candidate if any baseline columns are missing or renamed (reports likely renames).
        - Rejects newly added columns unless explicitly permitted by policy or allowlist.
        - Rejects column data type incompatibilities (e.g. numeric vs object/string).
        - Rejects target/label schema tampering (type changes, domain shifts).
        """
        allow_new = allow_new_columns if allow_new_columns is not None else self.allow_new_columns
        violations: list[ValidationViolation] = []

        # 0. Malformed / empty dataframe check
        if len(baseline_df.columns) == 0 or len(candidate_df.columns) == 0:
            violations.append(
                ValidationViolation(
                    feature="schema",
                    test_type="schema_validation",
                    statistic=1.0,
                    p_value=None,
                    threshold=0.0,
                    message="Malformed or empty schema: dataset contains 0 columns.",
                )
            )
            return violations

        if len(candidate_df) == 0:
            violations.append(
                ValidationViolation(
                    feature="dataset",
                    test_type="schema_validation",
                    statistic=1.0,
                    p_value=None,
                    threshold=0.0,
                    message="Candidate dataset contains 0 rows.",
                )
            )
            return violations

        base_cols = [str(c) for c in baseline_df.columns]
        cand_cols = [str(c) for c in candidate_df.columns]
        header_check_cols = [str(c) for c in raw_candidate_headers] if raw_candidate_headers is not None else cand_cols

        # 1. Suspicious column names: Duplicates, control chars, whitespace padding, and case collisions
        # 1a. Duplicate headers
        seen_cand = set()
        dup_cand = set()
        for c in header_check_cols:
            if c in seen_cand:
                dup_cand.add(c)
            seen_cand.add(c)
        for dup in sorted(dup_cand):
            violations.append(
                ValidationViolation(
                    feature=dup,
                    test_type="schema_validation",
                    statistic=1.0,
                    p_value=None,
                    threshold=0.0,
                    message=f"Candidate schema contains duplicate column name '{dup}'.",
                )
            )
        if dup_cand:
            return violations

        # Also detect if pandas mangled duplicate columns (e.g. "feat.1" when "feat" exists)
        if raw_candidate_headers is None:
            import re
            mangled_dups = set()
            for c in cand_cols:
                m = re.match(r"^(.*?)\.\d+$", c)
                if m and m.group(1) in cand_cols:
                    mangled_dups.add(m.group(1))
            for m_dup in sorted(mangled_dups):
                violations.append(
                    ValidationViolation(
                        feature=m_dup,
                        test_type="schema_validation",
                        statistic=1.0,
                        p_value=None,
                        threshold=0.0,
                        message=f"Candidate schema contains duplicate column header derived from '{m_dup}'.",
                    )
                )
            if mangled_dups:
                return violations

        # 1b. Control characters and illegal non-printable characters
        for col in header_check_cols:
            if any(ord(ch) < 32 and ch not in ("\t",) for ch in col):
                violations.append(
                    ValidationViolation(
                        feature=col,
                        test_type="schema_validation",
                        statistic=1.0,
                        p_value=None,
                        threshold=0.0,
                        message=f"Column name '{repr(col)}' contains illegal control or non-printable characters.",
                    )
                )

        # 1c. Whitespace padding & normalization collisions
        stripped_cand = [c.strip() for c in header_check_cols]
        for orig, stripped in zip(header_check_cols, stripped_cand):
            if orig != stripped or "  " in orig:
                violations.append(
                    ValidationViolation(
                        feature=orig,
                        test_type="schema_validation",
                        statistic=1.0,
                        p_value=None,
                        threshold=0.0,
                        message=f"Column name '{orig}' contains leading or trailing whitespace padding.",
                    )
                )
        if len(stripped_cand) != len(set(stripped_cand)):
            for col in sorted(set(header_check_cols)):
                matching = [c for c in header_check_cols if c.strip() == col.strip()]
                if len(matching) > 1:
                    violations.append(
                        ValidationViolation(
                            feature=col,
                            test_type="schema_validation",
                            statistic=1.0,
                            p_value=None,
                            threshold=0.0,
                            message=f"Column normalization collision: multiple columns normalize to '{col.strip()}'.",
                        )
                    )
                    break

        # 1d. Case collisions (e.g. 'feature' and 'Feature')
        lowered_cand = [c.lower() for c in header_check_cols]
        if len(lowered_cand) != len(set(lowered_cand)):
            for col in sorted(set(header_check_cols)):
                matching = [c for c in header_check_cols if c.lower() == col.lower()]
                if len(matching) > 1:
                    violations.append(
                        ValidationViolation(
                            feature=col,
                            test_type="schema_validation",
                            statistic=1.0,
                            p_value=None,
                            threshold=0.0,
                            message=f"Candidate schema contains case collision: '{col}' conflicts with multiple variants.",
                        )
                    )
                    break

        # 1e. Case mismatch against baseline
        base_lowered = {b.lower(): b for b in base_cols}
        for col in cand_cols:
            if col not in base_cols and col.lower() in base_lowered:
                base_match = base_lowered[col.lower()]
                violations.append(
                    ValidationViolation(
                        feature=col,
                        test_type="schema_validation",
                        statistic=1.0,
                        p_value=None,
                        threshold=0.0,
                        message=f"Column '{col}' collides with baseline column '{base_match}' with conflicting case.",
                    )
                )

        # 2. Missing columns and likely rename detection
        missing_cols = set(base_cols) - set(cand_cols)
        new_cols = set(cand_cols) - set(base_cols)

        for col in sorted(missing_cols):
            # If 1 missing and 1 new, report likely rename
            if len(missing_cols) == 1 and len(new_cols) == 1:
                likely_rename = list(new_cols)[0]
                msg = f"Required baseline column '{col}' is missing in candidate dataset schema (likely renamed to '{likely_rename}')."
            else:
                msg = f"Required baseline column '{col}' is missing in candidate dataset schema."
            violations.append(
                ValidationViolation(
                    feature=col,
                    test_type="schema_validation",
                    statistic=1.0,
                    p_value=None,
                    threshold=0.0,
                    message=msg,
                )
            )

        # 3. Unexpected newly added columns (respecting allowlist)
        if not allow_new:
            allowed_set = set(allowed_new_columns or [])
            for col in sorted(new_cols):
                if col not in allowed_set:
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

        # 4. Data type compatibility for common columns
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
            # Check boolean stability
            b_is_bool = pd.api.types.is_bool_dtype(baseline_df[col])
            c_is_bool = pd.api.types.is_bool_dtype(candidate_df[col])
            if b_is_bool != c_is_bool:
                violations.append(
                    ValidationViolation(
                        feature=col,
                        test_type="type_incompatibility",
                        statistic=1.0,
                        p_value=None,
                        threshold=0.0,
                        message=(
                            f"Column '{col}' boolean type mismatch: baseline is {baseline_df[col].dtype}, "
                            f"candidate is {candidate_df[col].dtype}."
                        ),
                    )
                )

        # 5. Target / Label column stability check
        target = target_column
        if not target:
            for cand in ["target", "label", "class", "churn", "is_fraud", "fraud"]:
                if cand in base_cols and cand in cand_cols:
                    target = cand
                    break

        if target and target in base_cols and target in cand_cols:
            b_target = baseline_df[target].dropna()
            c_target = candidate_df[target].dropna()
            if len(b_target) > 0 and len(c_target) > 0:
                # Type stability on target
                b_num = pd.api.types.is_numeric_dtype(b_target)
                c_num = pd.api.types.is_numeric_dtype(c_target)
                if b_num != c_num:
                    violations.append(
                        ValidationViolation(
                            feature=target,
                            test_type="target_schema_tampering",
                            statistic=1.0,
                            p_value=None,
                            threshold=0.0,
                            message=f"Target column '{target}' type mismatch: baseline is {b_target.dtype}, candidate is {c_target.dtype}.",
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

        Delegates to the shared drift statistics module so poisoning detection
        and drift detection cannot disagree about the same numbers.
        """
        return drift_stats.compute_psi(baseline, candidate, bins=bins, eps=eps)

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

            ks = drift_stats.ks_two_sample(b_vals, c_vals)
            if ks is None:
                # Sample size too small for reliable KS test
                continue
            stat_d, p_val = ks

            psi_val = drift_stats.compute_psi(b_vals, c_vals)

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

        result = drift_stats.categorical_contingency(
            baseline_labels, candidate_labels, alpha=alpha_val
        )
        if result is None:
            return violations

        if result["significant"]:
            violations.append(
                ValidationViolation(
                    feature=target_name,
                    test_type=result["test_type"],
                    statistic=result["statistic"],
                    p_value=result["p_value"],
                    threshold=alpha_val,
                    message=(
                        f"Target label '{target_name}' distribution shifted significantly "
                        f"({result['test_type']} p={result['p_value']:.2e} < {alpha_val:.2e})."
                    ),
                )
            )

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
        allowed_new_columns: Optional[Union[list[str], set[str]]] = None,
        raw_candidate_headers: Optional[list[str]] = None,
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
            baseline_df,
            candidate_df,
            allow_new_columns=allow_new_columns,
            allowed_new_columns=allowed_new_columns,
            target_column=target_column,
            raw_candidate_headers=raw_candidate_headers,
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
