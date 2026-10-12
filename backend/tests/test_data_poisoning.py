import io
import time
import uuid
import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import settings
from app.db.session import SessionLocal
from app.main import app
from app.models.user import User
from app.models.threat_hunting import Alert, SecurityEvent
from app.services.dataset.facade import data_service
from app.services.dataset.lakefs_service import lakefs_service
from app.services.dataset.poisoning_detector import (
    StatisticalPoisoningDetector,
    poisoning_detector,
)
from app.schemas.threat_hunting import SecurityEventCreate
from app.services.dataset import DataService
from app.services.threat_hunting.event_service import event_service
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
def admin_headers():
    db = SessionLocal()
    try:
        from app.core.security import hash_password, create_access_token
        user = db.execute(select(User).where(User.username == "admin_poison_test")).scalar_one_or_none()
        if not user:
            user = User(
                username="admin_poison_test",
                email="admin_poison@sentinelml.com",
                password_hash=hash_password("AdminPass123!"),
                role="admin",
                is_active=True,
            )
            db.add(user)
            db.commit()
            db.refresh(user)
        token = create_access_token(subject=str(user.id))
        return {"Authorization": f"Bearer {token}"}
    finally:
        db.close()


@pytest.fixture
def webhook_headers():
    return {"X-Webhook-Secret": settings.LAKEFS_WEBHOOK_SECRET}


# ===========================================================================
# 1. Deterministic Statistical Poisoning & Schema Unit Tests
# ===========================================================================


def test_detector_clean_samples_pass():
    """Identical distributions and balanced labels must pass with 0 violations."""
    np.random.seed(42)
    base_feat = np.random.normal(loc=10.0, scale=2.0, size=500)
    cand_feat = np.random.normal(loc=10.0, scale=2.0, size=500)

    baseline_df = pd.DataFrame({"feature1": base_feat, "target": np.random.choice([0, 1], size=500, p=[0.5, 0.5])})
    candidate_df = pd.DataFrame({"feature1": cand_feat, "target": np.random.choice([0, 1], size=500, p=[0.5, 0.5])})

    detector = StatisticalPoisoningDetector(ks_alpha=0.01, ks_d_threshold=0.15, chi2_alpha=0.01, psi_threshold=0.25)
    result = detector.validate_datasets(baseline_df, candidate_df)

    assert result.passed is True
    assert result.drift_detected is False
    assert len(result.violations) == 0


def test_detector_continuous_feature_ks_shift_rejected():
    """Mean-shifted continuous feature must trigger KS violation."""
    np.random.seed(42)
    base_feat = np.random.normal(loc=10.0, scale=2.0, size=500)
    cand_feat = np.random.normal(loc=16.0, scale=2.0, size=500)

    baseline_df = pd.DataFrame({"feature1": base_feat, "target": [0] * 250 + [1] * 250})
    candidate_df = pd.DataFrame({"feature1": cand_feat, "target": [0] * 250 + [1] * 250})

    detector = StatisticalPoisoningDetector(ks_alpha=0.01, ks_d_threshold=0.15)
    result = detector.validate_datasets(baseline_df, candidate_df)

    assert result.passed is False
    assert result.drift_detected is True
    ks_violations = [v for v in result.violations if v.test_type == "ks_2samp"]
    assert len(ks_violations) == 1
    assert ks_violations[0].feature == "feature1"
    assert ks_violations[0].statistic >= 0.15
    assert ks_violations[0].p_value < 0.01


def test_detector_population_stability_index_shift():
    """Subtle shape changes exceeding PSI 0.25 threshold must be flagged."""
    np.random.seed(42)
    base_feat = np.random.normal(loc=0.0, scale=1.0, size=1000)
    cand_feat = np.random.exponential(scale=2.0, size=1000)

    baseline_df = pd.DataFrame({"feature1": base_feat, "target": [0] * 500 + [1] * 500})
    candidate_df = pd.DataFrame({"feature1": cand_feat, "target": [0] * 500 + [1] * 500})

    detector = StatisticalPoisoningDetector(psi_threshold=0.25)
    result = detector.validate_datasets(baseline_df, candidate_df)

    assert result.passed is False
    psi_violations = [v for v in result.violations if v.test_type == "psi"]
    assert len(psi_violations) >= 1
    assert psi_violations[0].statistic >= 0.25


def test_detector_bonferroni_correction_prevents_false_positives():
    """Across 50 identical features, Bonferroni correction must prevent false positive flags."""
    np.random.seed(123)
    k_features = 50
    n_samples = 400

    base_dict = {f"feat_{i}": np.random.normal(0, 1, n_samples) for i in range(k_features)}
    base_dict["target"] = [0] * (n_samples // 2) + [1] * (n_samples // 2)
    baseline_df = pd.DataFrame(base_dict)

    cand_dict = {f"feat_{i}": np.random.normal(0, 1, n_samples) for i in range(k_features)}
    cand_dict["target"] = [0] * (n_samples // 2) + [1] * (n_samples // 2)
    candidate_df = pd.DataFrame(cand_dict)

    detector = StatisticalPoisoningDetector(ks_alpha=0.01, ks_d_threshold=0.15)
    result = detector.validate_datasets(baseline_df, candidate_df)

    assert result.passed is True
    assert len(result.violations) == 0


def test_detector_target_label_multiclass_chi2_shift_rejected():
    """Multiclass target label flipping must trigger Chi-Square contingency violation."""
    np.random.seed(42)
    base_labels = [0] * 300 + [1] * 300 + [2] * 300
    cand_labels = [0] * 700 + [1] * 100 + [2] * 100

    base_feat = np.random.normal(5, 1, len(base_labels))
    cand_feat = np.random.normal(5, 1, len(cand_labels))

    baseline_df = pd.DataFrame({"num_feat": base_feat, "target": base_labels})
    candidate_df = pd.DataFrame({"num_feat": cand_feat, "target": cand_labels})

    detector = StatisticalPoisoningDetector(chi2_alpha=0.01)
    result = detector.validate_datasets(baseline_df, candidate_df)

    assert result.passed is False
    chi2_violations = [v for v in result.violations if v.test_type == "chi2_contingency"]
    assert len(chi2_violations) == 1
    assert chi2_violations[0].p_value < 0.01


def test_detector_sparse_binary_fisher_exact_fallback():
    """Sparse binary target labels with cell count < 5 fall back to Fisher's exact test."""
    base_labels = pd.Series([0] * 8 + [1] * 2)
    cand_labels = pd.Series([0] * 1 + [1] * 9)

    detector = StatisticalPoisoningDetector(chi2_alpha=0.05)
    violations = detector.evaluate_target_label_distribution(base_labels, cand_labels, target_name="label", alpha=0.05)

    assert len(violations) >= 1
    assert violations[0].test_type == "fisher_exact"


def test_detector_sparse_multiclass_monte_carlo_permutation():
    """STAT-01: Sparse multiclass targets with expected cell count < 5 execute Monte Carlo permutation test."""
    base_labels = pd.Series([0] * 8 + [1] * 1 + [2] * 1)
    cand_labels = pd.Series([0] * 1 + [1] * 5 + [2] * 5)

    detector = StatisticalPoisoningDetector(chi2_alpha=0.05)
    violations = detector.evaluate_target_label_distribution(base_labels, cand_labels, target_name="class", alpha=0.05)

    assert len(violations) >= 1
    assert violations[0].test_type == "chi2_contingency_monte_carlo"
    assert violations[0].p_value < 0.05


def test_schema_enforcement_missing_required_column():
    """CORR-03: Candidate missing a baseline column must be rejected with schema_validation violation."""
    baseline_df = pd.DataFrame({"feat_a": [1.0, 2.0, 3.0], "feat_b": [4.0, 5.0, 6.0], "target": [0, 1, 0]})
    candidate_df = pd.DataFrame({"feat_a": [1.0, 2.0, 3.0], "target": [0, 1, 0]})  # feat_b dropped

    detector = StatisticalPoisoningDetector()
    result = detector.validate_datasets(baseline_df, candidate_df)

    assert result.passed is False
    schema_viols = [v for v in result.violations if v.test_type == "schema_validation"]
    assert any("feat_b" in v.message for v in schema_viols)


def test_schema_enforcement_unexpected_new_column():
    """CORR-03: Candidate adding an unexpected new column must be rejected by default policy."""
    baseline_df = pd.DataFrame({"feat_a": [1.0, 2.0, 3.0], "target": [0, 1, 0]})
    candidate_df = pd.DataFrame({"feat_a": [1.0, 2.0, 3.0], "feat_new": [9.0, 9.0, 9.0], "target": [0, 1, 0]})

    detector = StatisticalPoisoningDetector(allow_new_columns=False)
    result = detector.validate_datasets(baseline_df, candidate_df)

    assert result.passed is False
    schema_viols = [v for v in result.violations if v.test_type == "schema_validation"]
    assert any("feat_new" in v.message for v in schema_viols)


def test_schema_enforcement_incompatible_dtypes():
    """CORR-03: Column data type mismatch (e.g. numeric baseline vs string candidate) must be rejected."""
    baseline_df = pd.DataFrame({"feat_a": [1.0, 2.0, 3.0], "target": [0, 1, 0]})
    candidate_df = pd.DataFrame({"feat_a": ["str1", "str2", "str3"], "target": [0, 1, 0]})

    detector = StatisticalPoisoningDetector()
    result = detector.validate_datasets(baseline_df, candidate_df)

    assert result.passed is False
    type_viols = [v for v in result.violations if v.test_type == "type_incompatibility"]
    assert len(type_viols) >= 1
    assert type_viols[0].feature == "feat_a"


def test_schema_enforcement_empty_comparable_features():
    """CORR-03: If no comparable continuous features or targets exist, candidate cannot be approved."""
    baseline_df = pd.DataFrame({"text_only": ["a", "b", "c"]})
    candidate_df = pd.DataFrame({"text_only": ["a", "b", "c"]})

    detector = StatisticalPoisoningDetector()
    result = detector.validate_datasets(baseline_df, candidate_df)

    assert result.passed is False
    empty_viols = [v for v in result.violations if v.test_type == "empty_feature_set"]
    assert len(empty_viols) == 1


def test_resource_protection_sample_row_bound():
    """CORR-01: Massive datasets exceeding max_sample_rows are bounded without crashing or unbounded RAM."""
    np.random.seed(42)
    # Generate 60,000 rows
    big_base = pd.DataFrame({"feat": np.random.normal(0, 1, 60000), "target": [0] * 30000 + [1] * 30000})
    big_cand = pd.DataFrame({"feat": np.random.normal(0, 1, 60000), "target": [0] * 30000 + [1] * 30000})

    detector = StatisticalPoisoningDetector(max_sample_rows=5000)
    result = detector.validate_datasets(big_base, big_cand)

    assert result.passed is True
    assert result.drift_detected is False


# ===========================================================================
# 2. Webhook Security & Fail-Closed Behavior Tests (SEC-01 & Fail-Closed)
# ===========================================================================


def test_webhook_valid_header_authentication(webhook_headers):
    """SEC-01: Valid secret via X-Webhook-Secret header or Authorization Bearer header is authenticated."""
    payload = {
        "event_type": "pre-merge",
        "repository_id": "any-repo",
        "branch_id": "non-main-branch",
        "source_ref": "some-commit",
    }
    # 1. Via X-Webhook-Secret
    resp_header = client.post("/api/webhooks/lakefs/data-validation", json=payload, headers=webhook_headers)
    assert resp_header.status_code == 200
    assert resp_header.json()["status"] == "skipped"

    # 2. Via Authorization: Bearer
    bearer_headers = {"Authorization": f"Bearer {settings.LAKEFS_WEBHOOK_SECRET}"}
    resp_bearer = client.post("/api/webhooks/lakefs/data-validation", json=payload, headers=bearer_headers)
    assert resp_bearer.status_code == 200
    assert resp_bearer.json()["status"] == "skipped"


def test_webhook_missing_auth_returns_401():
    """SEC-01: Pre-merge webhook without authentication secret returns 401 Unauthorized."""
    payload = {
        "event_type": "pre-merge",
        "repository_id": "any-repo",
        "branch_id": "main",
        "source_ref": "some-commit",
    }
    resp = client.post("/api/webhooks/lakefs/data-validation", json=payload)
    assert resp.status_code == 401
    assert resp.json()["reason"] == "invalid_or_missing_webhook_secret"


def test_webhook_invalid_secret_returns_401():
    """SEC-01: Pre-merge webhook with invalid secret returns 401 Unauthorized."""
    payload = {
        "event_type": "pre-merge",
        "repository_id": "forged-repo",
        "branch_id": "main",
        "source_ref": "forged-commit",
    }
    # Invalid X-Webhook-Secret
    headers = {"X-Webhook-Secret": "wrong-secret-token-123"}
    resp = client.post("/api/webhooks/lakefs/data-validation", json=payload, headers=headers)
    assert resp.status_code == 401
    assert resp.json()["reason"] == "invalid_or_missing_webhook_secret"

    # Invalid Bearer token
    bearer_headers = {"Authorization": "Bearer totally-invalid-token-xyz"}
    resp_b = client.post("/api/webhooks/lakefs/data-validation", json=payload, headers=bearer_headers)
    assert resp_b.status_code == 401
    assert resp_b.json()["reason"] == "invalid_or_missing_webhook_secret"


def test_webhook_query_param_auth_rejected_by_default():
    """SEC-01: When POISONING_ALLOW_QUERY_PARAM_AUTH is false (default), query-string secret is rejected with 401."""
    assert getattr(settings, "POISONING_ALLOW_QUERY_PARAM_AUTH", False) is False

    payload = {
        "event_type": "pre-merge",
        "repository_id": "any-repo",
        "branch_id": "non-main-branch",
        "source_ref": "some-commit",
    }
    resp = client.post(
        f"/api/webhooks/lakefs/data-validation?secret={settings.LAKEFS_WEBHOOK_SECRET}",
        json=payload,
    )
    assert resp.status_code == 401
    assert resp.json()["reason"] == "query_param_auth_disabled"


def test_webhook_query_param_auth_accepted_when_explicitly_enabled(monkeypatch):
    """SEC-01: Query parameter authentication is accepted only when POISONING_ALLOW_QUERY_PARAM_AUTH is explicitly true."""
    monkeypatch.setattr(settings, "POISONING_ALLOW_QUERY_PARAM_AUTH", True)

    payload = {
        "event_type": "pre-merge",
        "repository_id": "any-repo",
        "branch_id": "non-main-branch",
        "source_ref": "some-commit",
    }
    # Valid query secret
    resp_valid = client.post(
        f"/api/webhooks/lakefs/data-validation?secret={settings.LAKEFS_WEBHOOK_SECRET}",
        json=payload,
    )
    assert resp_valid.status_code == 200

    # Invalid query secret still rejected
    resp_invalid = client.post(
        "/api/webhooks/lakefs/data-validation?secret=wrong-query-secret",
        json=payload,
    )
    assert resp_invalid.status_code == 401
    assert resp_invalid.json()["reason"] == "invalid_or_missing_webhook_secret"


def test_webhook_auth_failures_create_no_audit_or_threat_events(db):
    """SEC-01: All webhook authentication failures create zero SecurityEvents and zero poisoning audit logs."""
    from app.models.threat_hunting import SecurityEvent
    from app.models.audit_log import AuditLog

    init_sec_events = len(db.execute(select(SecurityEvent)).scalars().all())
    init_audit_poison = len(
        db.execute(
            select(AuditLog).where(
                AuditLog.action.in_(["dataset_poisoning_detected", "dataset_validation_passed"])
            )
        ).scalars().all()
    )

    payload = {
        "event_type": "pre-merge",
        "repository_id": "unauth-probe-repo",
        "branch_id": "main",
        "source_ref": "unauth-ref",
    }

    # Attempt 1: missing secret
    r1 = client.post("/api/webhooks/lakefs/data-validation", json=payload)
    assert r1.status_code == 401

    # Attempt 2: forged header secret
    r2 = client.post(
        "/api/webhooks/lakefs/data-validation",
        json=payload,
        headers={"X-Webhook-Secret": "attacker-fake-key"},
    )
    assert r2.status_code == 401

    # Attempt 3: query secret when disabled
    r3 = client.post(
        f"/api/webhooks/lakefs/data-validation?secret={settings.LAKEFS_WEBHOOK_SECRET}",
        json=payload,
    )
    assert r3.status_code == 401

    final_sec_events = len(db.execute(select(SecurityEvent)).scalars().all())
    final_audit_poison = len(
        db.execute(
            select(AuditLog).where(
                AuditLog.action.in_(["dataset_poisoning_detected", "dataset_validation_passed"])
            )
        ).scalars().all()
    )

    assert final_sec_events == init_sec_events, "Auth failures must not record SecurityEvents!"
    assert final_audit_poison == init_audit_poison, "Auth failures must not record poisoning audit logs!"


def test_webhook_misconfigured_secret_fails_closed_500(monkeypatch):
    """SEC-01: If POISONING_DETECTION_ENABLED is True but secret is empty, fail-closed with HTTP 500."""
    monkeypatch.setattr(settings, "LAKEFS_WEBHOOK_SECRET", "")
    payload = {
        "event_type": "pre-merge",
        "repository_id": "any-repo",
        "branch_id": "main",
        "source_ref": "some-commit",
    }
    resp = client.post("/api/webhooks/lakefs/data-validation", json=payload)
    assert resp.status_code == 500
    assert resp.json()["reason"] == "misconfigured_security_secret"


def test_webhook_skipped_on_non_main_branch(webhook_headers):
    """Webhook ignores merges targeting non-main branches when authenticated."""
    payload = {
        "event_type": "pre-merge",
        "repository_id": "any-repo",
        "branch_id": "staging-feat-1",
        "source_ref": "some-commit",
    }
    resp = client.post("/api/webhooks/lakefs/data-validation", json=payload, headers=webhook_headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "skipped"


def test_webhook_missing_source_ref(webhook_headers):
    """Webhook returns 400 if candidate reference is missing."""
    payload = {
        "event_type": "pre-merge",
        "repository_id": "any-repo",
        "branch_id": "main",
    }
    resp = client.post("/api/webhooks/lakefs/data-validation", json=payload, headers=webhook_headers)
    assert resp.status_code == 400
    assert resp.json()["reason"] == "missing_source_ref"


def test_webhook_disabled_toggle(monkeypatch, webhook_headers):
    """When POISONING_DETECTION_ENABLED is False, webhook approves."""
    monkeypatch.setattr(settings, "POISONING_DETECTION_ENABLED", False)
    payload = {
        "event_type": "pre-merge",
        "repository_id": "test-repo",
        "branch_id": "main",
        "source_ref": "source-sha",
    }
    resp = client.post("/api/webhooks/lakefs/data-validation", json=payload, headers=webhook_headers)
    assert resp.status_code == 200
    assert resp.json()["reason"] == "poisoning_gate_disabled"


def test_webhook_fail_closed_if_persistence_fails(monkeypatch, db, webhook_headers):
    """If audit or event persistence fails, rejected merge remains rejected (HTTP 422)."""
    np.random.seed(99)
    base_df = pd.DataFrame({"x": np.random.normal(10, 1, 200), "target": [0] * 100 + [1] * 100})
    cand_df = pd.DataFrame({"x": np.random.normal(50, 1, 200), "target": [0] * 100 + [1] * 100})

    base_bytes = base_df.to_csv(index=False).encode("utf-8")
    cand_bytes = cand_df.to_csv(index=False).encode("utf-8")

    monkeypatch.setattr(lakefs_service, "list_files", lambda repo, ref_id: [{"path": "data.csv"}])
    monkeypatch.setattr(lakefs_service, "download_file", lambda repo, ref_id, file_path: cand_bytes if "cand" in ref_id else base_bytes)

    # Force event_service to raise an exception
    def broken_ingest(*args, **kwargs):
        raise RuntimeError("PostgreSQL database connection dropped unexpectedly")

    monkeypatch.setattr(event_service, "ingest_event", broken_ingest)

    payload = {
        "event_type": "pre-merge",
        "repository_id": "test-fail-closed-repo",
        "branch_id": "main",
        "source_ref": "cand-commit-hash",
    }
    resp = client.post("/api/webhooks/lakefs/data-validation", json=payload, headers=webhook_headers)
    assert resp.status_code == 422
    assert resp.json()["status"] == "rejected"
    assert resp.json()["violations_count"] >= 1


def test_resource_protection_file_size_limit_rejected(monkeypatch, webhook_headers):
    """CORR-01: File exceeding POISONING_MAX_FILE_SIZE_BYTES is rejected with HTTP 422."""
    monkeypatch.setattr(settings, "POISONING_MAX_FILE_SIZE_BYTES", 1000)  # 1000 bytes limit

    big_bytes = b"a,target\n" + b"1.0,0\n" * 500  # > 1000 bytes
    monkeypatch.setattr(lakefs_service, "list_files", lambda repo, ref_id: [{"path": "data.csv", "size_bytes": 4000}])
    monkeypatch.setattr(lakefs_service, "download_file", lambda repo, ref_id, file_path: big_bytes)

    payload = {
        "event_type": "pre-merge",
        "repository_id": "test-size-repo",
        "branch_id": "main",
        "source_ref": "cand-ref",
    }
    resp = client.post("/api/webhooks/lakefs/data-validation", json=payload, headers=webhook_headers)
    assert resp.status_code == 422
    assert resp.json()["status"] == "rejected"
    assert any(v["test"] == "file_size_check" for v in resp.json()["violations"])


def test_multi_file_validation_first_clean_second_poisoned_rejected(monkeypatch, webhook_headers):
    """CORR-02: Multi-file commit where file1 is clean but file2 is poisoned must be rejected."""
    np.random.seed(42)
    clean_base = pd.DataFrame({"feat": np.random.normal(10, 1, 100), "target": [0] * 50 + [1] * 50})
    clean_cand = pd.DataFrame({"feat": np.random.normal(10, 1, 100), "target": [0] * 50 + [1] * 50})

    poison_base = pd.DataFrame({"feat": np.random.normal(10, 1, 100), "target": [0] * 50 + [1] * 50})
    poison_cand = pd.DataFrame({"feat": np.random.normal(50, 1, 100), "target": [0] * 50 + [1] * 50})  # shifted

    def mock_list_files(repo, ref_id):
        return [{"path": "1_clean.csv"}, {"path": "2_poison.csv"}]

    def mock_download(repo, ref_id, file_path):
        if "1_clean" in file_path:
            return clean_cand.to_csv(index=False).encode("utf-8") if "cand" in ref_id else clean_base.to_csv(index=False).encode("utf-8")
        else:
            return poison_cand.to_csv(index=False).encode("utf-8") if "cand" in ref_id else poison_base.to_csv(index=False).encode("utf-8")

    monkeypatch.setattr(lakefs_service, "list_files", mock_list_files)
    monkeypatch.setattr(lakefs_service, "download_file", mock_download)

    payload = {
        "event_type": "pre-merge",
        "repository_id": "test-multi-file-repo",
        "branch_id": "main",
        "source_ref": "cand-ref",
    }
    resp = client.post("/api/webhooks/lakefs/data-validation", json=payload, headers=webhook_headers)
    assert resp.status_code == 422
    assert resp.json()["status"] == "rejected"
    # Verify that the violation explicitly points to 2_poison.csv
    violations = resp.json()["violations"]
    assert any("2_poison.csv" in v["feature"] for v in violations)


def test_unbaselined_new_file_rejected_in_initialized_repo(monkeypatch, webhook_headers):
    """CORR-02: Adding an unbaselined new file into an existing repository with baseline is rejected."""
    monkeypatch.setattr(settings, "POISONING_ALLOW_NEW_DATASET_FILES", False)

    # Main branch already has existing_data.csv, but candidate introduces brand_new.csv
    def mock_list(repo, ref_id):
        if ref_id == "main":
            return [{"path": "existing_data.csv"}]
        return [{"path": "existing_data.csv"}, {"path": "brand_new.csv"}]

    def mock_download(repo, ref_id, file_path):
        if file_path == "brand_new.csv" and ref_id == "main":
            raise RuntimeError("404 Not Found")
        return b"col,target\n1,0\n2,1\n"

    monkeypatch.setattr(lakefs_service, "list_files", mock_list)
    monkeypatch.setattr(lakefs_service, "download_file", mock_download)

    payload = {
        "event_type": "pre-merge",
        "repository_id": "test-unbaselined-repo",
        "branch_id": "main",
        "source_ref": "cand-ref",
    }
    resp = client.post("/api/webhooks/lakefs/data-validation", json=payload, headers=webhook_headers)
    assert resp.status_code == 422
    assert any(v["test"] == "unbaselined_new_file" for v in resp.json()["violations"])


def test_threat_hunting_event_idempotency_no_duplicate_on_audit(db):
    """INT-01: Direct webhook ingestion followed by audit worker normalization yields exactly 1 SecurityEvent."""
    repo_name = f"test-idempotent-{uuid.uuid4().hex[:8]}"
    commit_sha = f"commit-{uuid.uuid4().hex[:12]}"

    # 1. Simulate direct webhook rejection event creation
    sec_event = event_service.ingest_event(
        db,
        SecurityEventCreate(
            event_type="dataset.poisoning_suspected",
            source="lakefs_validator",
            actor_id="test_actor",
            resource_type="dataset",
            resource_id=repo_name,
            severity="HIGH",
            attributes={"repository": repo_name, "commit_id": commit_sha},
            correlation_id=commit_sha,
        ),
    )
    assert sec_event is not None

    # 2. Simulate subsequent background audit log processing
    normalized = event_service.ingest_from_audit(
        action="dataset_poisoning_detected",
        username="lakefs_hook",
        details=f"Statistical data poisoning detected on repository '{repo_name}' merging '{commit_sha}'.",
        db=db,
        request_id=commit_sha,
    )

    # 3. Must return the exact same event ID without creating a second record
    assert normalized is not None
    assert normalized.id == sec_event.id

    # Verify database has exactly 1 event for this repo
    events = db.execute(
        select(SecurityEvent).where(
            SecurityEvent.resource_id == repo_name,
            SecurityEvent.event_type == "dataset.poisoning_suspected",
        )
    ).scalars().all()
    assert len(events) == 1


def test_bootstrap_reliability_action_failure_rollbacks(monkeypatch, admin_headers, db):
    """OPS-01: Injected failure during action installation aborts registration and cleans up repository."""
    ds_name = f"test-fail-action-{uuid.uuid4().hex[:8]}"

    # Force action upload to raise an exception
    orig_upload = DataService.upload_file

    def broken_upload(self, *args, **kwargs):
        if "_lakefs_actions" in kwargs.get("file_path", ""):
            raise RuntimeError("Injected lakeFS action upload error")
        return orig_upload(self, *args, **kwargs)

    monkeypatch.setattr(DataService, "upload_file", broken_upload)

    initial_csv = b"feat_a,target\n1.0,0\n2.0,1\n"
    resp = client.post(
        "/api/datasets",
        data={"name": ds_name, "description": "Fail action test"},
        files={"file": ("dataset.csv", initial_csv, "text/csv")},
        headers=admin_headers,
    )
    assert resp.status_code == 500
    assert "security bootstrap failed" in resp.json()["detail"].lower()

    # Verify dataset was not registered in DB
    from app.models.dataset import Dataset
    db_ds = db.execute(select(Dataset).where(Dataset.name == ds_name)).scalar_one_or_none()
    assert db_ds is None


def test_bootstrap_reliability_branch_protection_failure_rollbacks(monkeypatch, admin_headers, db):
    """OPS-01: Injected failure during branch protection aborts registration and cleans up repository."""
    ds_name = f"test-fail-prot-{uuid.uuid4().hex[:8]}"

    # Force set_branch_protection to raise an exception
    def broken_protection(self, *args, **kwargs):
        raise RuntimeError("Injected lakeFS branch protection error")

    monkeypatch.setattr(DataService, "set_branch_protection", broken_protection)

    initial_csv = b"feat_a,target\n1.0,0\n2.0,1\n"
    resp = client.post(
        "/api/datasets",
        data={"name": ds_name, "description": "Fail protection test"},
        files={"file": ("dataset.csv", initial_csv, "text/csv")},
        headers=admin_headers,
    )
    assert resp.status_code == 500
    assert "security bootstrap failed" in resp.json()["detail"].lower()

    # Verify dataset was not registered in DB
    from app.models.dataset import Dataset
    db_ds = db.execute(select(Dataset).where(Dataset.name == ds_name)).scalar_one_or_none()
    assert db_ds is None


# ===========================================================================
# 3. Real lakeFS v1.86.0 Integration Tests
# ===========================================================================


def test_lakefs_branch_protection_and_safe_bootstrap():
    """
    Verifies the safe bootstrap sequence:
    1. Create repo with default branch 'main'.
    2. Upload and commit action YAML and baseline on 'main'.
    3. Arm branch protection rule on 'main'.
    4. Verify direct write to 'main' is rejected with 403 Forbidden.
    """
    repo_name = f"test-bootstrap-{uuid.uuid4().hex[:8]}"
    storage_ns = f"s3://lakefs/{repo_name}"
    lakefs_service.create_repository(repo_name, storage_ns, description="Bootstrap test repo")

    try:
        # Install action
        action_yaml = f"""name: Gate
on:
  pre-merge:
    branches:
      - main
hooks:
  - id: gate
    type: webhook
    properties:
      url: "{settings.LAKEFS_WEBHOOK_URL}"
      headers:
        X-Webhook-Secret: "{settings.LAKEFS_WEBHOOK_SECRET}"
"""
        lakefs_service.upload_file(repo_name, "main", "_lakefs_actions/gate.yaml", action_yaml.encode("utf-8"))
        lakefs_service.commit(repo_name, "main", "Install action", metadata=None)

        # Upload baseline
        lakefs_service.upload_file(repo_name, "main", "dataset.csv", b"col1,target\n1.0,0\n2.0,1\n")
        lakefs_service.commit(repo_name, "main", "Initial baseline", metadata=None)

        # Arm branch protection
        lakefs_service.set_branch_protection_rule(repo_name, pattern="main")
        rules = lakefs_service.get_branch_protection_rules(repo_name)
        assert "main" in rules

        # Wait for lakeFS branch protection cache TTL propagation
        time.sleep(15)

        # Attempt direct write to protected main -> must fail with 403 Forbidden
        with pytest.raises(Exception) as exc_info:
            lakefs_service.upload_file(repo_name, "main", "bypass.csv", b"evil data")
        err_msg = str(exc_info.value).lower()
        assert "403" in err_msg or "forbidden" in err_msg or "cannot write to protected branch" in err_msg

    finally:
        try:
            lakefs_service.delete_repository(repo_name)
        except Exception:
            pass


def test_lakefs_real_clean_merge_into_protected_main_succeeds(db):
    """
    Live test against running lakeFS instance:
    Clean candidate merge into protected main triggers pre-merge webhook with auth,
    webhook approves (200), and lakeFS completes the merge commit into main.
    """
    repo_name = f"test-clean-merge-{uuid.uuid4().hex[:8]}"
    storage_ns = f"s3://lakefs/{repo_name}"
    lakefs_service.create_repository(repo_name, storage_ns, description="Clean merge test")

    try:
        # 1. Install real poisoning gate action targeting host webhook with auth
        action_yaml = f"""name: StatisticalPoisoningGate
on:
  pre-merge:
    branches:
      - main
hooks:
  - id: poisoning_gate
    type: webhook
    properties:
      url: "{settings.LAKEFS_WEBHOOK_URL}"
      timeout: 30s
      headers:
        X-Webhook-Secret: "{settings.LAKEFS_WEBHOOK_SECRET}"
"""
        lakefs_service.upload_file(repo_name, "main", "_lakefs_actions/poisoning_gate.yaml", action_yaml.encode("utf-8"))
        lakefs_service.commit(repo_name, "main", "Install poisoning gate action", metadata=None)

        # 2. Upload initial trusted baseline
        np.random.seed(10)
        base_df = pd.DataFrame({
            "feature1": np.random.normal(20.0, 2.0, 300),
            "target": np.random.choice([0, 1], size=300, p=[0.5, 0.5]),
        })
        lakefs_service.upload_file(repo_name, "main", "data.csv", base_df.to_csv(index=False).encode("utf-8"))
        main_init_commit = lakefs_service.commit(repo_name, "main", "Initial baseline", metadata=None)

        # 3. Arm branch protection
        lakefs_service.set_branch_protection_rule(repo_name, pattern="main")

        # 4. Create isolated staging branch from main
        staging_branch = "staging-clean-batch"
        lakefs_service.create_branch(repo_name, staging_branch, source_branch="main")

        # 5. Upload clean candidate data (same distribution) on staging branch
        cand_df = pd.DataFrame({
            "feature1": np.random.normal(20.0, 2.0, 300),
            "target": np.random.choice([0, 1], size=300, p=[0.5, 0.5]),
        })
        lakefs_service.upload_file(repo_name, staging_branch, "data.csv", cand_df.to_csv(index=False).encode("utf-8"))
        lakefs_service.commit(repo_name, staging_branch, "Clean batch commit", metadata=None)

        # 6. Merge staging branch into main
        merge_commit_id = lakefs_service.merge(repo_name, source_branch=staging_branch, destination_branch="main")
        assert merge_commit_id is not None
        assert merge_commit_id != main_init_commit["id"]

        # Verify main HEAD moved to the merge commit
        commits = lakefs_service.list_commits(repo_name, ref_id="main", limit=1)
        assert commits[0]["id"] == merge_commit_id

    finally:
        try:
            lakefs_service.delete_repository(repo_name)
        except Exception:
            pass


def test_lakefs_real_poisoned_merge_blocked_main_head_unchanged(db):
    """
    Live test against running lakeFS instance:
    Suspicious candidate merge into protected main is blocked by the gate (422),
    lakeFS aborts the merge (412 Precondition Failed),
    target main HEAD is verified to be 100% UNCHANGED,
    and staging candidate branch is preserved for security investigation.
    """
    repo_name = f"test-poison-merge-{uuid.uuid4().hex[:8]}"
    storage_ns = f"s3://lakefs/{repo_name}"
    lakefs_service.create_repository(repo_name, storage_ns, description="Poison merge test")

    try:
        # 1. Install real poisoning gate action targeting host webhook with auth
        action_yaml = f"""name: StatisticalPoisoningGate
on:
  pre-merge:
    branches:
      - main
hooks:
  - id: poisoning_gate
    type: webhook
    properties:
      url: "{settings.LAKEFS_WEBHOOK_URL}"
      timeout: 30s
      headers:
        X-Webhook-Secret: "{settings.LAKEFS_WEBHOOK_SECRET}"
"""
        lakefs_service.upload_file(repo_name, "main", "_lakefs_actions/poisoning_gate.yaml", action_yaml.encode("utf-8"))
        lakefs_service.commit(repo_name, "main", "Install poisoning gate action", metadata=None)

        # 2. Upload initial trusted baseline
        np.random.seed(77)
        base_df = pd.DataFrame({
            "val_x": np.random.normal(10.0, 1.0, 300),
            "target": [0] * 150 + [1] * 150,
        })
        lakefs_service.upload_file(repo_name, "main", "data.csv", base_df.to_csv(index=False).encode("utf-8"))
        main_init_commit = lakefs_service.commit(repo_name, "main", "Initial baseline data", metadata=None)
        head_commit_before_merge = main_init_commit["id"]

        # 3. Arm branch protection
        lakefs_service.set_branch_protection_rule(repo_name, pattern="main")

        # 4. Create isolated staging branch from main
        staging_branch = "staging-poison-batch"
        lakefs_service.create_branch(repo_name, staging_branch, source_branch="main")

        # 5. Upload poisoned candidate dataset (mean shifted + labels poisoned)
        poison_df = pd.DataFrame({
            "val_x": np.random.normal(35.0, 1.0, 300),  # Extreme feature shift
            "target": [0] * 290 + [1] * 10,  # Label flipping / imbalance
        })
        lakefs_service.upload_file(repo_name, staging_branch, "data.csv", poison_df.to_csv(index=False).encode("utf-8"))
        lakefs_service.commit(repo_name, staging_branch, "Poison batch commit", metadata=None)

        # 6. Attempt merge into main -> must be rejected by lakeFS pre-merge hook!
        with pytest.raises(Exception) as exc_info:
            lakefs_service.merge(repo_name, source_branch=staging_branch, destination_branch="main")

        err_str = str(exc_info.value).lower()
        assert "412" in err_str or "precondition failed" in err_str or "pre-merge hook aborted" in err_str

        # CRITICAL VERIFICATION: Main HEAD commit MUST BE 100% UNCHANGED
        commits_after = lakefs_service.list_commits(repo_name, ref_id="main", limit=1)
        head_commit_after_rejection = commits_after[0]["id"]
        assert head_commit_after_rejection == head_commit_before_merge, "Target main HEAD must remain unchanged after rejected merge!"

        # OPS-02: Verify staging branch is preserved for forensics
        branches = lakefs_service.list_branches(repo_name)
        assert any(b["name"] == staging_branch for b in branches), "Rejected staging branch must be preserved for investigation!"

        # 7. Verify Threat Hunting SecurityEvent and Alert were recorded
        sec_event = db.execute(
            select(SecurityEvent).where(
                SecurityEvent.resource_id == repo_name,
                SecurityEvent.event_type == "dataset.poisoning_suspected",
            )
        ).scalar_one_or_none()

        assert sec_event is not None
        assert sec_event.severity == "HIGH"
        assert sec_event.attributes["repository"] == repo_name
        assert len(sec_event.attributes["violations"]) >= 1

        # Verify Alert created with MITRE ATLAS AML.T0020
        alerts = db.execute(select(Alert)).scalars().all()
        matching_alerts = [
            a for a in alerts
            if repo_name in (a.dedup_key or "") or repo_name in str(a.supporting_evidence or {})
        ]
        assert len(matching_alerts) >= 1
        assert any(a.mitre_atlas_id == "AML.T0020" or "Poisoning" in a.title for a in matching_alerts)

    finally:
        try:
            lakefs_service.delete_repository(repo_name)
        except Exception:
            pass


def test_dataset_api_merge_and_transparent_staging_routing(admin_headers, db):
    """
    Tests dataset API transparent staging routing, merge endpoint, and staging branch pruning:
    1. Register dataset via POST /api/datasets (safe bootstrap installs action and arms protection).
    2. Upload file via POST /api/datasets/{name}/upload with branch='main'.
       Verify upload is automatically routed to an isolated staging branch without 403 error.
    3. Commit changes on that staging branch.
    4. Call POST /api/datasets/{name}/merge to merge staging branch into main.
    5. Verify merge completes and staging branch is pruned (OPS-02).
    """
    dataset_name = f"test-ds-api-{uuid.uuid4().hex[:8]}"

    # 1. Register dataset
    initial_csv = b"feat_a,target\n1.0,0\n2.0,1\n3.0,0\n4.0,1\n"
    reg_resp = client.post(
        "/api/datasets",
        data={"name": dataset_name, "description": "API staging test"},
        files={"file": ("dataset.csv", initial_csv, "text/csv")},
        headers=admin_headers,
    )
    assert reg_resp.status_code == 201

    try:
        # Verify branch protection is active on main
        rules = data_service.get_branch_protection(db, dataset_name=dataset_name)
        assert "main" in rules

        # 2. Transparent upload: target main, should be routed to staging-...
        update_csv = b"feat_a,target\n1.1,0\n2.1,1\n3.1,0\n4.1,1\n"
        upload_resp = client.post(
            f"/api/datasets/{dataset_name}/upload",
            data={"branch": "main"},
            files={"file": ("dataset.csv", update_csv, "text/csv")},
            headers=admin_headers,
        )
        assert upload_resp.status_code == 200
        staging_branch = upload_resp.json()["branch"]
        assert staging_branch.startswith("staging-")
        assert staging_branch != "main"

        # 3. Commit on staging branch
        commit_resp = client.post(
            f"/api/datasets/{dataset_name}/commit?branch={staging_branch}",
            json={"message": "Commit staging batch"},
            headers=admin_headers,
        )
        assert commit_resp.status_code == 200

        # 4. Merge staging branch into main via POST /api/datasets/{name}/merge
        merge_payload = {
            "source_branch": staging_branch,
            "destination_branch": "main",
            "message": "Merge staging batch into main",
        }
        merge_resp = client.post(
            f"/api/datasets/{dataset_name}/merge",
            json=merge_payload,
            headers=admin_headers,
        )
        assert merge_resp.status_code == 200
        assert merge_resp.json()["status"] == "merged"
        assert merge_resp.json()["merge_commit_id"] is not None

        # 5. OPS-02: Verify staging branch was safely pruned
        remaining_branches = data_service.list_branches(db, dataset_name=dataset_name)
        branch_names = [b["name"] for b in remaining_branches]
        assert staging_branch not in branch_names, "Merged staging branch should have been pruned!"

    finally:
        try:
            data_service.delete_dataset(db, dataset_name, username="admin_poison_test")
        except Exception:
            pass
