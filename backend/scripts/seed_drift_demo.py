"""Seed a drift baseline + inference telemetry so the Drift tab has data to render.

Runs against the local stack: builds a reference baseline for `demo-titanic`,
then writes synthetic *shifted* telemetry through the real `record_inference`
write path. `POST /api/drift/check` afterwards should therefore find drift.

Usage (from backend/):  python scripts/seed_drift_demo.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.session import SessionLocal
from app.models.drift import InferenceRecord
from app.repositories.baseline_repository import BaselineRepository
from app.services.drift.service import DriftService
from app.services.drift.telemetry import record_inference

# Overridable so the demo can target a *registered* model too — the UI's
# Run-check modal picks its model from the MLflow registry, so a model that
# exists only in telemetry would not be selectable there.
MODEL = sys.argv[1] if len(sys.argv) > 1 else "demo-titanic"
VERSION = sys.argv[2] if len(sys.argv) > 2 else "1"
ROWS = 80  # > DRIFT_MIN_OBSERVATIONS (50)


def build_reference(rng: np.random.Generator) -> pd.DataFrame:
    """Training-distribution reference: moderate ages, imbalanced survival.

    The class mix is deliberately imbalanced (~75/25) so that a later window
    with a different mix represents real prediction drift — two balanced sides
    would correctly produce no D2 finding.
    """
    return pd.DataFrame(
        {
            "age": rng.normal(35, 8, 400),
            "fare": rng.normal(30, 10, 400),
            "sibsp": rng.poisson(0.6, 400),
            "embarked": rng.choice(["S", "C", "Q"], 400, p=[0.7, 0.2, 0.1]),
            "survived": (rng.random(400) < 0.25).astype(int),
        }
    )


def build_shifted_requests(rng: np.random.Generator) -> tuple[list[pd.DataFrame], list[list[float]], list[list[str]]]:
    """Serving-distribution traffic: older passengers, skewed class mix.

    Deliberately drifts `age` (D1) and the predicted class share (D2), and
    degrades confidence (D3) so all three detectors have something to measure.
    """
    frames: list[pd.DataFrame] = []
    probabilities: list[list[float]] = []
    predictions: list[list[str]] = []

    for i in range(ROWS):
        p_survive = float(rng.uniform(0.42, 0.62))  # widened => lower confidence
        frame = pd.DataFrame(
            [
                {
                    "age": float(rng.normal(62, 10)),  # shifted +27 years
                    "fare": float(rng.normal(31, 10)),
                    "sibsp": int(rng.poisson(0.6)),
                    "embarked": str(rng.choice(["S", "C", "Q"], p=[0.7, 0.2, 0.1])),
                }
            ]
        )
        frames.append(frame)
        probabilities.append([round(1.0 - p_survive, 6), round(p_survive, 6)])
        predictions.append(["survived" if p_survive >= 0.5 else "died"])

    return frames, probabilities, predictions


def main() -> int:
    rng = np.random.default_rng(42)
    db = SessionLocal()
    try:
        svc = DriftService(db)

        # 0. Clear any previous demo run so the reference and the traffic stay
        #    consistent with each other (otherwise a stale window would be
        #    compared against a freshly computed baseline).
        from sqlalchemy import delete as sa_delete

        cleared = db.execute(
            sa_delete(InferenceRecord).where(InferenceRecord.model_name == MODEL)
        ).rowcount
        db.commit()
        if cleared:
            print(f"cleared {cleared} previous telemetry rows")

        # 1. Reference baseline — predictions + confidences included so D2/D3
        #    have a reference to compare against (D1 only needs feature_stats).
        ref = build_reference(rng)
        ref_preds = np.where(ref["survived"].to_numpy() == 1, "survived", "died").tolist()
        ref_confs = [float(rng.uniform(0.78, 0.97)) for _ in range(len(ref))]

        baseline = svc.build_baseline_from_dataframe(
            MODEL,
            VERSION,
            ref,
            dataset_id="demo-titanic",
            dataset_ref="seeded-reference-v1",
            target_column="survived",
            # Display-only context; never an input to any detector (D4 needs
            # ground truth, which is not collected).
            reference_metrics={"accuracy": 0.87, "f1": 0.84},
            predictions=ref_preds,
            confidences=ref_confs,
            created_by="admin_user",
        )
        baseline = BaselineRepository(db).upsert(baseline)
        print(f"baseline: {baseline.id} sample_count={baseline.sample_count} "
              f"features={len(baseline.feature_stats)} "
              f"pred_dist={bool(baseline.prediction_dist)} conf_stats={bool(baseline.confidence_stats)}")

        # 2. Telemetry through the production write path, shifted vs reference.
        frames, probs, preds = build_shifted_requests(rng)
        now = datetime.now(timezone.utc)
        written = 0
        for i, (frame, proba, pred) in enumerate(zip(frames, probs, preds)):
            # Spread rows across the observation window (newest first).
            ok = record_inference(
                db=db,
                model_name=MODEL,
                model_version=VERSION,
                deployment_id="seeded-deployment",
                rayservice_name="seeded-rayservice",
                request_id=f"seed-{i:04d}",
                client_ip="127.0.0.1",
                predictions=pred,
                probabilities=proba,
                df=frame,
                latency_ms=float(rng.uniform(45, 260)),
            )
            if ok:
                written += 1
                # Backdate so rows sit inside the default 1h window. The
                # request id alone is not unique across models (each demo run
                # reuses the same ids), so the model is part of the key.
                rec = (
                    db.query(InferenceRecord)
                    .filter(
                        InferenceRecord.request_id == f"seed-{i:04d}",
                        InferenceRecord.model_name == MODEL,
                    )
                    .one()
                )
                rec.recorded_at = now - timedelta(seconds=int(rng.uniform(30, 3500)))
                db.commit()

        print(f"telemetry: {written}/{ROWS} rows written for {MODEL}@{VERSION}")
        return 0
    except Exception as err:
        print(f"seed failed: {err}", file=sys.stderr)
        raise
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
