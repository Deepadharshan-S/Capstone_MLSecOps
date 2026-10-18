import io
import time
import uuid
import numpy as np
import pandas as pd
import pytest
from unittest.mock import patch
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import settings
from app.db.session import SessionLocal
from app.main import app
from app.models.threat_hunting import Alert, DetectionRule, SecurityEvent
from app.services.dataset.poisoning_detector import (
    StatisticalPoisoningDetector,
    poisoning_detector,
)
from app.services.threat_hunting.rule_service import rule_service

client = TestClient(app)


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        rule_service.seed_default_rules(session)
        yield session
    finally:
        session.close()


@pytest.fixture
def webhook_headers():
    return {"X-Webhook-Secret": settings.LAKEFS_WEBHOOK_SECRET}


# ===============================================================================
# 1. Core Schema Injection Detection Checks (P2)
# ===============================================================================

def test_p2_approved_identical_schema_passes():
    """P2.1: Candidate with identical approved schema passes without violations."""
    df_base = pd.DataFrame({"feat_1": [1.0, 2.0, 3.0], "feat_2": [10, 20, 30], "target": [0, 1, 0]})
    df_cand = pd.DataFrame({"feat_1": [1.1, 2.1, 2.9], "feat_2": [11, 21, 31], "target": [0, 1, 0]})

    detector = StatisticalPoisoningDetector()
    violations = detector.validate_schemas(df_base, df_cand)
    assert len(violations) == 0


def test_p2_unexpected_column_detected():
    """P2.2: Candidate containing unexpected unapproved new columns is rejected."""
    df_base = pd.DataFrame({"feat_1": [1.0, 2.0], "target": [0, 1]})
    df_cand = pd.DataFrame({"feat_1": [1.0, 2.0], "unapproved_injected_feat": [9.9, 8.8], "target": [0, 1]})

    detector = StatisticalPoisoningDetector(allow_new_columns=False)
    violations = detector.validate_schemas(df_base, df_cand)
    assert any("unapproved_injected_feat" in v.message for v in violations)
    assert any(v.test_type == "schema_validation" for v in violations)


def test_p2_missing_expected_column_detected():
    """P2.3: Candidate missing an expected baseline column is rejected."""
    df_base = pd.DataFrame({"feat_1": [1.0, 2.0], "feat_2": [10.0, 20.0], "target": [0, 1]})
    df_cand = pd.DataFrame({"feat_1": [1.0, 2.0], "target": [0, 1]})  # feat_2 dropped

    detector = StatisticalPoisoningDetector()
    violations = detector.validate_schemas(df_base, df_cand)
    assert any("feat_2" in v.message for v in violations)
    assert any(v.test_type == "schema_validation" for v in violations)


def test_p2_likely_rename_detected_in_missing_column_message():
    """P2.3b: If exactly one column was dropped and one added, report likely rename hint."""
    df_base = pd.DataFrame({"age": [25, 40], "target": [0, 1]})
    df_cand = pd.DataFrame({"customer_age": [25, 40], "target": [0, 1]})

    detector = StatisticalPoisoningDetector()
    violations = detector.validate_schemas(df_base, df_cand)
    missing_viols = [v for v in violations if "age" in v.message]
    assert len(missing_viols) >= 1
    assert "customer_age" in missing_viols[0].message, "Should identify 'customer_age' as likely rename!"


def test_p2_incompatible_dtype_change_detected():
    """P2.4: Incompatible data type change (e.g. numeric float -> string object) is rejected."""
    df_base = pd.DataFrame({"score": [1.5, 2.5, 3.5], "target": [0, 1, 0]})
    df_cand = pd.DataFrame({"score": ["low", "medium", "high"], "target": [0, 1, 0]})

    detector = StatisticalPoisoningDetector()
    violations = detector.validate_schemas(df_base, df_cand)
    type_viols = [v for v in violations if v.test_type == "type_incompatibility"]
    assert len(type_viols) >= 1
    assert type_viols[0].feature == "score"


def test_p2_suspicious_duplicate_column_names_detected():
    """P2.5a: Duplicate column names in candidate schema are rejected."""
    df_base = pd.DataFrame({"feat_a": [1.0], "target": [0]})
    # Create DataFrame with duplicate columns
    df_cand = pd.DataFrame([[1.0, 2.0, 0]], columns=["feat_a", "feat_a", "target"])

    detector = StatisticalPoisoningDetector()
    violations = detector.validate_schemas(df_base, df_cand)
    assert any("duplicate" in v.message.lower() for v in violations)


def test_p2_whitespace_padding_and_normalization_collision_detected():
    """P2.5b: Column names with leading/trailing whitespace padding or collisions are detected."""
    df_base = pd.DataFrame({"feat_clean": [1.0, 2.0], "target": [0, 1]})
    # Injected column has sneaky trailing space: "feat_clean "
    df_cand = pd.DataFrame({"feat_clean ": [1.0, 2.0], "target": [0, 1]})

    detector = StatisticalPoisoningDetector()
    violations = detector.validate_schemas(df_base, df_cand)
    assert any("whitespace" in v.message.lower() for v in violations)


def test_p2_case_collision_detected():
    """P2.5c: Case collisions between candidate columns (e.g. 'feature' and 'Feature') are detected."""
    df_base = pd.DataFrame({"feature": [1.0], "other": [2.0], "target": [0]})
    df_cand = pd.DataFrame({"feature": [1.0], "Feature": [2.0], "target": [0]})

    detector = StatisticalPoisoningDetector(allow_new_columns=True)
    violations = detector.validate_schemas(df_base, df_cand)
    assert any("case collision" in v.message.lower() for v in violations)


def test_p2_illegal_control_characters_in_headers_detected():
    """P2.5d: Non-printable or control characters in column headers are detected."""
    df_base = pd.DataFrame({"normal_col": [1.0], "target": [0]})
    df_cand = pd.DataFrame({"normal_col\x00_injected": [1.0], "target": [0]})

    detector = StatisticalPoisoningDetector()
    violations = detector.validate_schemas(df_base, df_cand)
    assert any("control" in v.message.lower() or "illegal" in v.message.lower() for v in violations)


def test_p2_valid_schema_evolution_accepted_with_allowlist():
    """P2.6 & P2.7: Schema evolution with explicit allowlist or allow_new_columns=True is accepted."""
    df_base = pd.DataFrame({"feat_1": [1.0, 2.0], "target": [0, 1]})
    df_cand = pd.DataFrame({"feat_1": [1.0, 2.0], "approved_new_col": [5.0, 6.0], "target": [0, 1]})

    # With allow_new_columns=True
    detector_open = StatisticalPoisoningDetector(allow_new_columns=True)
    viols_open = detector_open.validate_schemas(df_base, df_cand)
    assert len(viols_open) == 0

    # With allowed_new_columns allowlist
    detector_allowlist = StatisticalPoisoningDetector(allow_new_columns=False)
    viols_allow = detector_allowlist.validate_schemas(
        df_base, df_cand, allowed_new_columns=["approved_new_col"]
    )
    assert len(viols_allow) == 0


def test_p2_target_label_schema_tampering_detected():
    """P2.8: Target/label column type tampering (e.g. numeric label turned into string) is detected."""
    df_base = pd.DataFrame({"feat_1": [1.0, 2.0], "target": [0, 1]})
    df_cand = pd.DataFrame({"feat_1": [1.0, 2.0], "target": ["benign", "malicious"]})

    detector = StatisticalPoisoningDetector()
    violations = detector.validate_schemas(df_base, df_cand, target_column="target")
    target_viols = [v for v in violations if v.test_type == "target_schema_tampering"]
    assert len(target_viols) >= 1
    assert "target" in target_viols[0].message


def test_p2_empty_dataset_and_malformed_schema_fails_safely():
    """P2.9 & P2.10: Empty candidate dataset or 0-column dataframe fails safely."""
    df_base = pd.DataFrame({"feat_1": [1.0, 2.0], "target": [0, 1]})
    df_empty_rows = pd.DataFrame(columns=["feat_1", "target"])
    df_empty_cols = pd.DataFrame()

    detector = StatisticalPoisoningDetector()

    # 0 rows
    v_rows = detector.validate_schemas(df_base, df_empty_rows)
    assert any("0 rows" in v.message for v in v_rows)

    # 0 columns
    v_cols = detector.validate_schemas(df_base, df_empty_cols)
    assert any("0 columns" in v.message for v in v_cols)


# ===============================================================================
# 2. Pre-Merge Enforcement & SecurityEvent Integration
# ===============================================================================

def test_p2_premerge_schema_injection_emits_security_event_and_rejects(webhook_headers, db):
    """P2.11 & P2.12: Pre-merge gate rejects schema injection with 422 and emits dataset.schema_injection event."""
    unique_repo = f"test-schema-inj-{uuid.uuid4().hex[:8]}"

    # Mock download_file to return candidate with unexpected injected columns
    baseline_csv = b"feat_a,target\n1.0,0\n2.0,1\n3.0,0\n"
    injected_csv = b"feat_a,malicious_column_inj,target\n1.0,999.0,0\n2.0,888.0,1\n3.0,777.0,0\n"

    def mock_download(repo_name, ref_id, file_path):
        if ref_id == "main":
            return baseline_csv
        return injected_csv

    def mock_list_files(repo_name, ref_id):
        return [{"path": "data.csv", "size_bytes": len(injected_csv)}]

    with patch("app.services.dataset.lakefs_service.lakefs_service.download_file", side_effect=mock_download), \
         patch("app.services.dataset.lakefs_service.lakefs_service.list_files", side_effect=mock_list_files):

        payload = {
            "event_type": "pre-merge",
            "repository_id": unique_repo,
            "branch_id": "main",
            "source_ref": "attacker-branch-commit",
            "commit_id": f"commit_{uuid.uuid4().hex[:8]}",
            "committer": "rogue_dev",
        }

        resp = client.post("/api/webhooks/lakefs/data-validation", json=payload, headers=webhook_headers)
        # 1. Must be rejected with HTTP 422
        assert resp.status_code == 422
        assert resp.json()["status"] == "rejected"

        # 2. Must emit dataset.schema_injection SecurityEvent
        ev = db.execute(
            select(SecurityEvent).where(
                SecurityEvent.resource_id == unique_repo,
                SecurityEvent.event_type == "dataset.schema_injection",
            )
        ).scalar_one_or_none()
        assert ev is not None
        assert ev.severity == "HIGH"
        assert ev.actor_id == "rogue_dev"
        assert "malicious_column_inj" in str(ev.attributes["violations"])

        # 3. Must trigger Tabular Schema Injection Detection Alert
        alert = db.execute(
            select(Alert).where(
                Alert.title.like("%Tabular Schema Injection Detection%"),
                Alert.status == "OPEN",
            ).order_by(Alert.id.desc())
        ).scalars().first()
        assert alert is not None
        assert alert.severity == "HIGH"


def test_p2_rule_seeding_is_idempotent(db):
    """P2.14: Tabular Schema Injection Detection rule is seeded idempotently."""
    rule = db.execute(
        select(DetectionRule).where(
            DetectionRule.name == "Tabular Schema Injection Detection"
        )
    ).scalar_one_or_none()
    assert rule is not None
    assert rule.config["event_type"] == "dataset.schema_injection"
    assert rule.severity == "HIGH"
    assert rule.mitre_attack_id == "T1565.001"
    assert rule.mitre_atlas_id == "AML.T0020"


def test_p2_raw_csv_duplicate_headers_detected_before_pandas_mangling():
    """P2.15: Duplicate CSV headers in raw byte stream are caught before parser .1 normalization."""
    raw_csv = b"feature_x,feature_x,target\n10,20,1\n30,40,0\n"
    headers = poisoning_detector.inspect_raw_csv_headers(raw_csv)
    assert headers == ["feature_x", "feature_x", "target"]

    base_df = pd.DataFrame({"feature_x": [10, 30], "target": [1, 0]})
    # Pandas by default converts duplicate headers to ['feature_x', 'feature_x.1', 'target']
    cand_df = pd.read_csv(io.BytesIO(raw_csv))
    assert "feature_x.1" in cand_df.columns

    violations = poisoning_detector.validate_schemas(
        base_df, cand_df, raw_candidate_headers=headers
    )
    dup_violations = [v for v in violations if "duplicate column name 'feature_x'" in v.message]
    assert len(dup_violations) > 0


def test_p2_raw_csv_whitespace_and_control_chars_detected():
    """P2.16: Raw CSV with whitespace padding or control characters is flagged."""
    raw_csv = b"feature_a ,feature_\x01b,target\n1,2,0\n"
    headers = poisoning_detector.inspect_raw_csv_headers(raw_csv)
    assert headers[0] == "feature_a "
    assert "\x01" in headers[1]

    base_df = pd.DataFrame({"feature_a": [1], "feature_b": [2], "target": [0]})
    cand_df = pd.DataFrame({"feature_a ": [1], "feature_\x01b": [2], "target": [0]})

    violations = poisoning_detector.validate_schemas(
        base_df, cand_df, raw_candidate_headers=headers
    )
    ws_v = [v for v in violations if "whitespace" in v.message.lower()]
    ctrl_v = [v for v in violations if "control" in v.message.lower() or "non-printable" in v.message.lower()]
    assert len(ws_v) > 0
    assert len(ctrl_v) > 0


def test_p2_raw_parquet_schema_inspection():
    """P2.17: Raw Parquet file schema is inspected directly from file metadata."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    table = pa.Table.from_arrays(
        [pa.array([1, 2]), pa.array([10.5, 20.5])],
        names=["id_col", "metric_val"]
    )
    buf = io.BytesIO()
    pq.write_table(table, buf)
    parquet_bytes = buf.getvalue()

    headers = poisoning_detector.inspect_raw_parquet_headers(parquet_bytes)
    assert headers == ["id_col", "metric_val"]


def test_p2_malformed_unparseable_file_fails_closed_in_gate(webhook_headers, db):
    """P2.18: Corrupted/unparseable tabular files fail closed with 422 and emit schema_injection event."""
    unique_repo = f"test-corrupt-schema-{uuid.uuid4().hex[:8]}"

    # Mock download_file returning corrupt unparseable binary bytes
    with patch("app.api.webhooks.lakefs_service.list_files") as mock_list, \
         patch("app.api.webhooks.lakefs_service.download_file") as mock_dl:

        mock_list.side_effect = [
            [{"path": "corrupt_data.csv", "size_bytes": 100}],
            [{"path": "corrupt_data.csv", "size_bytes": 100}],
        ]
        # Candidate is corrupted bytes that raise ParserError in read_csv
        mock_dl.side_effect = [
            b"\x00\xff\xfe\x00corrupt\x80\x90",  # candidate bytes
            b"col_a,col_b\n1,2\n",                # baseline bytes
        ]

        payload = {
            "event_type": "pre-merge",
            "repository_id": unique_repo,
            "branch_id": "main",
            "source_ref": "branch-with-corrupt-data",
            "commit_id": "commit_corrupt_01",
            "committer": "test_dev",
        }

        resp = client.post("/api/webhooks/lakefs/data-validation", json=payload, headers=webhook_headers)
        assert resp.status_code == 422
        assert resp.json()["status"] == "rejected"

        ev = db.execute(
            select(SecurityEvent).where(
                SecurityEvent.resource_id == unique_repo,
                SecurityEvent.event_type == "dataset.schema_injection",
            )
        ).scalar_one_or_none()
        assert ev is not None
        assert "malformed_schema" in str(ev.attributes["violations"])
