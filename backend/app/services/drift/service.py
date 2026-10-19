"""
Drift orchestration: build baselines, run detection windows, publish findings.

Findings are not stored in a new table. They are emitted as SecurityEvents and
correlated into Alerts with `detection_source="drift_engine"`, which reuses the
existing dedup, status lifecycle, investigation history and event correlation
instead of duplicating them.
"""

from __future__ import annotations

import io
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import pandas as pd
from sqlalchemy.orm import Session

from app.services.drift.detectors import (
    ConfidenceDriftDetector,
    DataDriftDetector,
    MinSampleGuard,
    PredictionDriftDetector,
)
from app.services.drift.findings import (
    CONFIDENCE_DRIFT,
    DATA_DRIFT,
    PREDICTION_DRIFT,
    DriftFinding,
)
from app.services.drift import stats as st
from app.services.drift.telemetry import confidence_from_probabilities

logger = logging.getLogger("drift.service")

DETECTION_SOURCE = "drift_engine"

EVENT_TYPES = {
    DATA_DRIFT: "drift.data_drift_detected",
    PREDICTION_DRIFT: "drift.prediction_drift_detected",
    CONFIDENCE_DRIFT: "drift.confidence_drift_detected",
}


class DriftService:
    """Stateless service bound to one DB session."""

    def __init__(self, db: Session):
        self.db = db
        from app.core.config import settings

        self.settings = settings
        self.thresholds = {
            "ks_alpha": getattr(settings, "DRIFT_KS_ALPHA", 0.01),
            "ks_d_threshold": getattr(settings, "DRIFT_KS_D_THRESHOLD", 0.15),
            "chi2_alpha": getattr(settings, "DRIFT_CHI2_ALPHA", 0.01),
            "psi_threshold": getattr(settings, "DRIFT_PSI_THRESHOLD", 0.25),
            "jsd_threshold": getattr(settings, "DRIFT_JSD_THRESHOLD", 0.10),
            "missing_rate_delta": getattr(settings, "DRIFT_MISSING_RATE_DELTA", 0.10),
        }
        guard = MinSampleGuard(getattr(settings, "DRIFT_MIN_OBSERVATIONS", 50))
        self.data_detector = DataDriftDetector(guard, self.thresholds)
        self.prediction_detector = PredictionDriftDetector(guard, self.thresholds)
        self.confidence_detector = ConfidenceDriftDetector(guard, self.thresholds)
        self.guard = guard

    # ── Baseline construction ───────────────────────────────────────────
    def build_baseline_from_dataframe(
        self,
        model_name: str,
        model_version: str,
        df: pd.DataFrame,
        *,
        dataset_id: Optional[str] = None,
        dataset_ref: Optional[str] = None,
        target_column: Optional[str] = None,
        reference_metrics: Optional[dict] = None,
        predictions: Optional[list] = None,
        confidences: Optional[list] = None,
        created_by: Optional[str] = None,
    ):
        """Build a baseline record from a reference DataFrame."""
        from app.models.drift import ModelBaseline

        if df is None or len(df) == 0:
            raise ValueError("Cannot build a baseline from an empty dataset.")

        work = df.copy()
        # The target column is not part of the inference payload, so leaving it
        # in would make every window look like it was missing a feature.
        if target_column and target_column in work.columns:
            work = work.drop(columns=[target_column])

        sample_size = int(getattr(self.settings, "DRIFT_BASELINE_SAMPLE_SIZE", 5000))
        feature_stats = st.compute_feature_stats(work, sample_size=sample_size)

        prediction_dist = st.compute_prediction_dist(predictions) if predictions else None
        confidence_stats = (
            st.compute_confidence_stats(confidences, sample_size=sample_size)
            if confidences
            else None
        )

        return ModelBaseline(
            model_name=str(model_name)[:100],
            model_version=str(model_version)[:50],
            dataset_id=dataset_id,
            dataset_ref=dataset_ref,
            feature_stats=feature_stats,
            prediction_dist=prediction_dist,
            confidence_stats=confidence_stats,
            # Display-only context. Comparing this against production would
            # require ground-truth outcomes, which are not collected.
            reference_metrics=reference_metrics,
            sample_count=int(len(work)),
            schema_hash=st.schema_hash(
                [str(c) for c in work.columns],
                {str(c): str(work[c].dtype) for c in work.columns},
            ),
            status="active",
            created_by=created_by,
        )

    def build_baseline_for_model(
        self,
        model_name: str,
        model_version: str,
        *,
        dataset_id: Optional[str] = None,
        data_path: Optional[str] = None,
        created_by: Optional[str] = None,
    ) -> Any:
        """
        Build a baseline from the training dataset in lakeFS.

        The dataset is resolved from the MLflow run recorded for this model
        version (tags `dataset_id` / `lakefs_repo` / `lakefs_ref`) unless an
        explicit `dataset_id` is supplied. Failures raise ValueError with a
        readable message — typically an unreachable lakeFS or an unreadable
        repository — rather than an opaque stack trace.
        """
        from app.repositories.baseline_repository import BaselineRepository

        resolved = self._resolve_training_source(model_name, model_version, dataset_id)
        df, source = self._load_reference_dataframe(resolved, data_path)

        reference_metrics = resolved.get("metrics") or None
        baseline = self.build_baseline_from_dataframe(
            model_name,
            model_version,
            df,
            dataset_id=resolved.get("dataset_id"),
            dataset_ref=resolved.get("dataset_ref"),
            target_column=resolved.get("target_column"),
            reference_metrics=reference_metrics,
            created_by=created_by,
        )

        repo = BaselineRepository(self.db)
        stored = repo.upsert(baseline)
        logger.info(
            "Built drift baseline for %s@%s from %s (%s rows)",
            model_name,
            model_version,
            source,
            stored.sample_count,
        )
        return stored

    def _resolve_training_source(
        self, model_name: str, model_version: str, dataset_id: Optional[str]
    ) -> dict[str, Any]:
        """Locate the training dataset and target column via MLflow."""
        info: dict[str, Any] = {
            "dataset_id": dataset_id,
            "dataset_ref": None,
            "repo": None,
            "target_column": None,
            "metrics": None,
        }

        try:
            from app.core.mlflow_loader import load_mlflow

            _, MlflowClient = load_mlflow()
            client = MlflowClient(tracking_uri=self.settings.MLFLOW_TRACKING_URI)

            version = client.get_model_version(model_name, str(model_version))
            run_id = getattr(version, "run_id", None)
            run = client.get_run(run_id) if run_id else None

            if run is not None:
                tags = run.data.tags or {}
                params = run.data.params or {}
                info["dataset_id"] = info["dataset_id"] or tags.get("dataset_id")
                info["dataset_ref"] = tags.get("lakefs_ref")
                info["repo"] = tags.get("lakefs_repo")
                info["target_column"] = params.get("target_column")
                info["metrics"] = {
                    k: v
                    for k, v in (run.data.metrics or {}).items()
                    if k in ("accuracy", "f1", "precision", "recall", "rmse", "mae", "roc_auc")
                } or None
        except Exception as mlflow_err:
            logger.debug(f"MLflow resolution failed for {model_name}: {mlflow_err}")

        # An explicit dataset (form field or MLflow tag) fills in whatever the
        # run did not record: the lakeFS repository is named after the dataset
        # *name*, and the ref defaults to its default branch. Deriving the repo
        # from a raw id would produce a name lakeFS has never seen, and without
        # this fallback an id-only request could never reach lakeFS at all —
        # `_load_reference_dataframe` refuses to open a repo with no ref.
        if info["dataset_id"]:
            dataset = self._find_dataset(info["dataset_id"])
            if dataset is not None:
                if not info["repo"]:
                    from app.services.dataset.utils import get_repo_name

                    info["repo"] = get_repo_name(dataset.name)
                if not info["dataset_ref"]:
                    info["dataset_ref"] = dataset.default_branch or "main"
            elif not info["repo"]:
                # Last resort: the key may be a name that no longer resolves.
                info["repo"] = self._repo_name_for(info["dataset_id"])

        return info

    def _find_dataset(self, dataset_key: str):
        """Look a dataset up by name or id — both appear in MLflow tags.

        Name comes first and id is only attempted when the key parses as a
        UUID: the id column is a real UUID type, so a plain name compared
        against it raises (which would otherwise mask a perfectly good name
        match). Returns None when nothing resolves; the caller decides
        whether that is fatal (it is not: tags may carry an explicit repo/ref).
        """
        from app.models.dataset import Dataset

        key = (dataset_key or "").strip()
        if not key:
            return None

        try:
            dataset = self.db.query(Dataset).filter(Dataset.name == key).first()
            if dataset is not None:
                return dataset

            from uuid import UUID

            try:
                UUID(key)
            except (ValueError, TypeError, AttributeError):
                return None
            return self.db.query(Dataset).filter(Dataset.id == UUID(key)).first()
        except Exception as lookup_err:
            logger.debug(f"Dataset lookup failed for {dataset_key}: {lookup_err}")
            return None

    @staticmethod
    def _repo_name_for(dataset_id: str) -> str:
        try:
            from app.services.dataset.utils import get_repo_name

            return get_repo_name(dataset_id)
        except Exception:
            import re

            sanitized = dataset_id.lower()
            sanitized = re.sub(r"[^a-z0-9-]", "-", sanitized)
            sanitized = re.sub(r"-+", "-", sanitized).strip("-")
            return (sanitized or "dataset")[:63].rstrip("-")

    def _load_reference_dataframe(
        self, resolved: dict[str, Any], data_path: Optional[str]
    ) -> tuple[pd.DataFrame, str]:
        """Download and parse the reference dataset from lakeFS."""
        repo = resolved.get("repo")
        ref = resolved.get("dataset_ref")
        if not repo or not ref:
            raise ValueError(
                "Could not resolve the training dataset for this model version. "
                "No lakeFS repository/ref is recorded on its MLflow run — pass an "
                "explicit dataset when creating the baseline."
            )

        from app.services.dataset.lakefs_service import lakefs_service

        try:
            files = lakefs_service.list_files(repo, ref_id=ref) or []
        except Exception as list_err:
            raise ValueError(f"Could not list '{repo}@{ref}' in lakeFS: {list_err}")

        candidates = []
        for f in files:
            path = f.get("path") or f.get("name") or ""
            if str(path).lower().endswith((".csv", ".parquet", ".pq")):
                candidates.append(str(path))

        if not candidates:
            raise ValueError(f"No CSV/Parquet dataset file found in '{repo}@{ref}'.")

        if data_path:
            if data_path not in candidates:
                raise ValueError(
                    f"'{data_path}' is not present in '{repo}@{ref}'. "
                    f"Available: {', '.join(candidates[:10])}"
                )
            chosen = data_path
        else:
            chosen = candidates[0]

        try:
            payload = lakefs_service.download_file(repo, ref_id=ref, file_path=chosen)
        except Exception as dl_err:
            raise ValueError(f"Could not download '{chosen}' from '{repo}@{ref}': {dl_err}")

        if not payload:
            raise ValueError(f"'{chosen}' at '{repo}@{ref}' is empty.")

        try:
            if chosen.lower().endswith(".csv"):
                df = pd.read_csv(io.BytesIO(payload))
            else:
                df = pd.read_parquet(io.BytesIO(payload))
        except Exception as parse_err:
            raise ValueError(f"Could not parse '{chosen}': {parse_err}")

        if len(df) == 0:
            raise ValueError(f"'{chosen}' contains no rows.")
        return df, f"{repo}@{ref}/{chosen}"

    # ── Detection ───────────────────────────────────────────────────────
    def run_drift_check(
        self,
        model_name: str,
        model_version: Optional[str] = None,
        window_seconds: Optional[int] = None,
        *,
        publish: bool = True,
    ) -> dict[str, Any]:
        """
        Run D1/D2/D3 over the most recent observation window.

        Returns a report with findings plus the reasons any detector declined
        to produce one — "too few samples" and "no baseline" are outcomes worth
        showing, not errors to hide.
        """
        from app.repositories.baseline_repository import BaselineRepository
        from app.repositories.inference_repository import InferenceRepository

        window = int(
            window_seconds
            or getattr(self.settings, "DRIFT_OBSERVATION_WINDOW_SECONDS", 3600)
        )
        end = datetime.now(timezone.utc)
        start = end - timedelta(seconds=window)

        inference_repo = InferenceRepository(self.db)
        baseline_repo = BaselineRepository(self.db)

        baseline = baseline_repo.get_active(model_name, model_version)
        records = inference_repo.window(model_name, start, end, model_version)

        observations_df, all_predictions, all_confidences = self._flatten(records)

        report: dict[str, Any] = {
            "model_name": model_name,
            "model_version": model_version,
            "window_start": start.isoformat(),
            "window_end": end.isoformat(),
            "window_seconds": window,
            "records": len(records),
            "observations": len(observations_df),
            "baseline_found": baseline is not None,
            "baseline_id": str(baseline.id) if baseline else None,
            "findings": [],
            "skipped": [],
        }

        if not records:
            report["skipped"].append(
                {
                    "reason": "no_traffic",
                    "detail": f"No inference telemetry for '{model_name}' in the last {window}s.",
                }
            )
            return report

        baseline_meta = self._baseline_meta(baseline)

        # ── D1: data drift
        if not baseline or not baseline.feature_stats:
            report["skipped"].append(
                {
                    "reason": "no_baseline",
                    "detail": f"No feature baseline is recorded for '{model_name}'.",
                    "detection_type": DATA_DRIFT,
                }
            )
        elif len(observations_df) == 0:
            report["skipped"].append(
                {
                    "reason": "no_features",
                    "detail": "Telemetry rows did not include feature payloads.",
                    "detection_type": DATA_DRIFT,
                }
            )
        elif not self.data_detector.guard.ok(len(observations_df)):
            report["skipped"].append(
                {
                    "reason": "insufficient_samples",
                    "detail": self.data_detector.guard.reason(len(observations_df)),
                    "detection_type": DATA_DRIFT,
                }
            )
        else:
            finding, notes = self.data_detector.detect(
                model_name=model_name,
                model_version=model_version,
                baseline=baseline.feature_stats,
                observation=observations_df,
                window_start=start,
                window_end=end,
                baseline_meta=baseline_meta,
            )
            self._record(report, finding, notes, publish)

        # ── D2: prediction drift
        if not baseline or not baseline.prediction_dist:
            report["skipped"].append(
                {
                    "reason": "no_baseline",
                    "detail": f"No reference prediction distribution for '{model_name}'.",
                    "detection_type": PREDICTION_DRIFT,
                }
            )
        elif not self.prediction_detector.guard.ok(len(all_predictions)):
            report["skipped"].append(
                {
                    "reason": "insufficient_samples",
                    "detail": self.prediction_detector.guard.reason(len(all_predictions)),
                    "detection_type": PREDICTION_DRIFT,
                }
            )
        else:
            finding, notes = self.prediction_detector.detect(
                model_name=model_name,
                model_version=model_version,
                baseline_dist=baseline.prediction_dist,
                baseline_samples=baseline.sample_count,
                predictions=all_predictions,
                window_start=start,
                window_end=end,
                baseline_meta=baseline_meta,
            )
            self._record(report, finding, notes, publish)

        # ── D3: confidence drift
        if not baseline or not baseline.confidence_stats:
            report["skipped"].append(
                {
                    "reason": "no_baseline",
                    "detail": (
                        f"No reference confidence distribution for '{model_name}' — the "
                        f"baseline was built without probabilities."
                    ),
                    "detection_type": CONFIDENCE_DRIFT,
                }
            )
        elif not all_confidences:
            report["skipped"].append(
                {
                    "reason": "no_probabilities",
                    "detail": (
                        "The serving contract returned no probabilities for this model, "
                        "so confidence cannot be measured."
                    ),
                    "detection_type": CONFIDENCE_DRIFT,
                }
            )
        elif not self.confidence_detector.guard.ok(len(all_confidences)):
            report["skipped"].append(
                {
                    "reason": "insufficient_samples",
                    "detail": self.confidence_detector.guard.reason(len(all_confidences)),
                    "detection_type": CONFIDENCE_DRIFT,
                }
            )
        else:
            finding, notes = self.confidence_detector.detect(
                model_name=model_name,
                model_version=model_version,
                baseline_conf=baseline.confidence_stats,
                confidences=all_confidences,
                window_start=start,
                window_end=end,
                baseline_meta=baseline_meta,
            )
            self._record(report, finding, notes, publish)

        return report

    def _record(
        self,
        report: dict[str, Any],
        finding: Optional[DriftFinding],
        notes: list[str],
        publish: bool,
    ) -> None:
        if finding is None:
            if notes:
                report["skipped"].append(
                    {"reason": "detector_note", "detail": "; ".join(notes)}
                )
            return

        published = None
        if publish:
            published = self.publish_finding(finding)
        report["findings"].append(
            {
                "detection_type": finding.detection_type,
                "severity": finding.severity,
                "title": finding.title,
                "summary": finding.summary,
                "affected_features": finding.affected_features,
                "dedup_key": finding.dedup_key,
                "alert_id": str(published.id) if published else None,
            }
        )

    def publish_finding(self, finding: DriftFinding):
        """
        Turn a finding into a correlated SecurityEvent and an Alert.

        Uses the shared alert machinery — advisory-locked dedup, status
        lifecycle, event links, history — so drift alerts behave exactly like
        rule-engine alerts except for their `detection_source`.
        """
        from app.schemas.threat_hunting import SecurityEventCreate
        from app.services.threat_hunting.alert_service import alert_service
        from app.services.threat_hunting.detection_engine import DetectionResult
        from app.services.threat_hunting.event_service import event_service

        severity = finding.severity if finding.severity in ("LOW", "MEDIUM", "HIGH", "CRITICAL") else "MEDIUM"

        event_in = SecurityEventCreate(
            event_type=EVENT_TYPES[finding.detection_type],
            source=DETECTION_SOURCE,
            severity=severity,
            resource_id=finding.model_name,
            resource_type="model",
            actor_id=None,
            attributes=finding.to_event_attributes(),
        )
        event = event_service.ingest_event(self.db, event_in)

        detection = DetectionResult(
            rule_id=None,
            rule_version=0,
            title=finding.title,
            description=finding.summary,
            severity=severity,
            mitre_attack_id=None,
            mitre_atlas_id=None,
            dedup_key=finding.dedup_key,
            event_ids=[event.id],
            first_seen=datetime.fromisoformat(finding.observation_start),
            last_seen=datetime.fromisoformat(finding.observation_end),
            actor_id=None,
            resource_id=finding.model_name,
            supporting_evidence=finding.evidence(),
            detection_source=DETECTION_SOURCE,
        )
        alert = alert_service.process_detection(self.db, detection)
        logger.info(
            "Drift finding published: %s -> alert %s (%s)",
            finding.detection_type,
            alert.id,
            finding.severity,
        )
        return alert

    @staticmethod
    def _flatten(records) -> tuple[pd.DataFrame, list, list]:
        """Turn stored telemetry rows into the three observation inputs."""
        feature_rows: list[dict] = []
        predictions: list[Any] = []
        confidences: list[float] = []

        for rec in records:
            feats = rec.features
            if isinstance(feats, list):
                feature_rows.extend(f for f in feats if isinstance(f, dict))

            pred = rec.prediction
            if isinstance(pred, list):
                predictions.extend(pred)
            elif pred is not None:
                predictions.append(pred)

            if rec.has_probabilities and rec.probabilities is not None:
                per_sample, _ = confidence_from_probabilities(rec.probabilities)
                if per_sample:
                    confidences.extend(per_sample)

        df = pd.DataFrame(feature_rows) if feature_rows else pd.DataFrame()
        return df, predictions, confidences

    @staticmethod
    def _baseline_meta(baseline) -> dict[str, Any]:
        if baseline is None:
            return {}
        return {
            "id": str(baseline.id),
            "dataset_id": baseline.dataset_id,
            "dataset_ref": baseline.dataset_ref,
            "sample_count": baseline.sample_count,
        }

    # ── Housekeeping ────────────────────────────────────────────────────
    def prune_telemetry(self) -> int:
        """Enforce the telemetry retention horizon."""
        from app.repositories.inference_repository import InferenceRepository

        days = int(getattr(self.settings, "DRIFT_TELEMETRY_RETENTION_DAYS", 30))
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        removed = InferenceRepository(self.db).prune_older_than(cutoff)
        if removed:
            logger.info(f"Pruned {removed} inference telemetry rows older than {days}d")
        return removed

    def sweep(self) -> dict[str, Any]:
        """
        One full drift pass: every model with recent telemetry, then retention.

        Each model is independent — a failure on one must not abort the sweep
        for the others.
        """
        from app.repositories.inference_repository import InferenceRepository

        summary: dict[str, Any] = {
            "checked": 0,
            "findings": 0,
            "errors": 0,
            "pruned": 0,
            "models": [],
        }

        if not getattr(self.settings, "DRIFT_DETECTOR_ENABLED", True):
            summary["disabled"] = True
            return summary

        try:
            pairs = InferenceRepository(self.db).distinct_models()
        except Exception as err:
            summary["errors"] += 1
            logger.error(f"Drift sweep could not list models: {err}")
            return summary

        for model_name, model_version in pairs:
            try:
                report = self.run_drift_check(model_name, model_version)
                summary["checked"] += 1
                summary["findings"] += len(report["findings"])
                summary["models"].append(
                    {
                        "model_name": model_name,
                        "model_version": model_version,
                        "findings": len(report["findings"]),
                        "skipped": len(report["skipped"]),
                    }
                )
            except Exception as model_err:
                summary["errors"] += 1
                logger.error(f"Drift check failed for '{model_name}': {model_err}")

        try:
            summary["pruned"] = self.prune_telemetry()
        except Exception as prune_err:
            logger.error(f"Telemetry pruning failed: {prune_err}")

        return summary
