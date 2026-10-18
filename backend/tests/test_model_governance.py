import pytest
from unittest.mock import MagicMock
from fastapi import HTTPException
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.services.ml_ops.model_governance_service import (
    ModelGovernanceService,
    model_governance_service,
)
from app.services.threat_hunting.rule_service import rule_service
from app.services.threat_hunting.detection_engine import detection_engine
from app.services.threat_hunting.alert_service import alert_service


# Isolated in-memory SQLite test database
TEST_DB_URL = "sqlite:///:memory:"
test_engine = create_engine(
    TEST_DB_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)


@event.listens_for(test_engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)


@pytest.fixture(scope="module", autouse=True)
def setup_test_db():
    Base.metadata.create_all(bind=test_engine)
    db = TestingSessionLocal()
    try:
        rule_service.seed_default_rules(db)
    finally:
        db.close()
    yield


@pytest.fixture
def db():
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()


def test_format_detection_and_safety():
    """Verifies that safetensors and onnx are classified as safe, while pickle is legacy/unsafe."""
    gov = ModelGovernanceService()

    assert gov.detect_format("model.safetensors") == "safetensors"
    assert gov.detect_format("weights.onnx") == "onnx"
    assert gov.detect_format("classifier.pkl") == "pickle"
    assert gov.detect_format("model.joblib") == "pickle"

    assert gov.is_safe_format("safetensors") is True
    assert gov.is_safe_format("onnx") is True
    assert gov.is_safe_format("pickle") is False
    assert gov.is_safe_format("pytorch_binary") is False


def test_production_safe_format_enforcement_blocks_pickle(db):
    """
    Verifies that promoting a legacy pickle model to Production is blocked by
    Safe Format Enforcement and generates a Threat Hunting alert.
    """
    gov = ModelGovernanceService()
    model_id = "legacy-fraud-model"
    version = "1"
    user = "ml_engineer"

    pickle_tags = {
        "model.format": "pickle",
        "upload_filename": "fraud_model.pkl",
    }

    # Staging deployment is tolerated or warned
    gov.evaluate_deployment_governance(
        model_id=model_id,
        version=version,
        environment="staging",
        user_username=user,
        model_tags=pickle_tags,
        db=db,
    )

    # Production deployment MUST be strictly blocked
    with pytest.raises(HTTPException) as exc_info:
        gov.evaluate_deployment_governance(
            model_id=model_id,
            version=version,
            environment="production",
            user_username=user,
            model_tags=pickle_tags,
            db=db,
        )

    assert exc_info.value.status_code == 400
    assert "Safe Format Enforcement Violation" in exc_info.value.detail
    assert "Safetensors (.safetensors) or ONNX (.onnx)" in exc_info.value.detail

    # Verify Threat Hunting Alert generation
    detections = detection_engine.evaluate_all_rules(db)
    format_detections = [d for d in detections if "Legacy Pickle" in d.title]
    assert len(format_detections) > 0, "Expected an alert for Legacy Pickle Promotion!"
    alert = alert_service.process_detection(db, format_detections[0])
    assert alert.severity == "HIGH"
    assert "Legacy Pickle" in alert.title


def test_production_allows_safetensors_with_attestation(db):
    """
    Verifies that a valid Safetensors model with cryptographic attestation passes gatekeeping.
    """
    gov = ModelGovernanceService()
    model_bytes = b"mock_safetensors_binary_data_content_12345"
    signer = "authorized_ds"

    attestation_tags = gov.generate_attestation(
        file_path_or_bytes=model_bytes,
        filename="bert_finetuned.safetensors",
        signer_username=signer,
    )

    assert attestation_tags["model.format"] == "safetensors"
    assert attestation_tags["attestation.signer"] == signer
    assert attestation_tags["attestation.cosign_verified"] == "true"
    assert len(attestation_tags["attestation.signature"]) == 64

    # Should succeed without raising HTTPException
    gov.evaluate_deployment_governance(
        model_id="bert-safe-model",
        version="2",
        environment="production",
        user_username=signer,
        model_tags=attestation_tags,
        db=db,
    )


def test_unsigned_model_promoted_to_production_alert(db):
    """
    Verifies that deploying an unsigned model to Production generates an Unattested Model alert.
    """
    gov = ModelGovernanceService()
    unsigned_tags = {
        "model.format": "safetensors",
        "upload_filename": "unsigned_model.safetensors",
    }

    # Evaluate deployment gatekeeper
    gov.evaluate_deployment_governance(
        model_id="unsigned-risk-model",
        version="1",
        environment="production",
        user_username="rogue_user",
        model_tags=unsigned_tags,
        db=db,
    )

    # Verify Threat Hunting Alert
    detections = detection_engine.evaluate_all_rules(db)
    attestation_detections = [d for d in detections if "Unsigned or Invalidly Attested" in d.title]
    assert len(attestation_detections) > 0, "Expected an alert for Unsigned Model Promotion!"
    alert = alert_service.process_detection(db, attestation_detections[0])
    assert alert.severity == "HIGH"


def test_model_tampering_digest_mismatch_detection(db):
    """
    Verifies that if an artifact is modified after signing (signature/hash mismatch),
    the gatekeeper raises CRITICAL tampering alerts and blocks deployment.
    """
    gov = ModelGovernanceService()
    model_bytes = b"clean_original_weights"
    signer = "lead_ds"

    tags = gov.generate_attestation(
        file_path_or_bytes=model_bytes,
        filename="sensitive_model.safetensors",
        signer_username=signer,
    )

    # Tamper with the sha256 tag or signature
    tampered_tags = dict(tags)
    tampered_tags["attestation.sha256"] = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

    with pytest.raises(HTTPException) as exc_info:
        gov.evaluate_deployment_governance(
            model_id="tampered-model",
            version="1",
            environment="production",
            user_username=signer,
            model_tags=tampered_tags,
            db=db,
        )

    assert exc_info.value.status_code == 400
    assert "Model Provenance & Attestation Gatekeeper Violation" in exc_info.value.detail

    # Verify CRITICAL Threat Hunting Alert for Tampering
    detections = detection_engine.evaluate_all_rules(db)
    tamper_detections = [d for d in detections if "Model Artifact Integrity Digest Mismatch" in d.title]
    assert len(tamper_detections) > 0, "Expected a CRITICAL alert for Model Tampering!"
    alert = alert_service.process_detection(db, tamper_detections[0])
    assert alert.severity == "CRITICAL"


def test_clean_model_passes_backdoor_scan():
    """Verifies that clean models with normal weight distributions pass backdoor scan."""
    import numpy as np

    class CleanModel:
        def __init__(self):
            np.random.seed(42)
            self.coef_ = np.random.normal(0.0, 0.5, size=(10, 20))
            self.intercept_ = np.zeros(10)

    gov = ModelGovernanceService()
    clean_model = CleanModel()
    res = gov.scan_weights_for_backdoors(model_obj=clean_model)
    assert res["is_backdoored"] is False
    assert res["threat_type"] is None
    assert len(res["details"]["outlier_neurons"]) == 0


def test_backdoor_detection_outlier_neuron_weights(db):
    """
    Verifies that a model containing an anomalous trojan trigger neuron
    (extreme weight outlier z-score > 5.5) is detected, quarantined, and triggers
    a CRITICAL alert under MITRE ATLAS AML.T0018.
    """
    import numpy as np

    class TrojanModel:
        def __init__(self):
            np.random.seed(42)
            # Normal weights in [-0.5, 0.5] range across 50 neurons
            weights = np.random.normal(0.0, 0.2, size=(50, 10))
            # Inject trojan trigger shortcut in neuron 7 with massive weight outlier
            weights[7, 3] = 18.5
            self.coef_ = weights
            self.intercept_ = np.zeros(50)

    gov = ModelGovernanceService()
    trojan_model = TrojanModel()

    # 1. Direct scan check
    res = gov.scan_weights_for_backdoors(model_obj=trojan_model)
    assert res["is_backdoored"] is True
    assert res["threat_type"] == "trojan_trigger_neuron"
    assert res["details"]["max_z_score"] >= 5.5
    assert len(res["details"]["outlier_neurons"]) > 0

    # 2. Gatekeeper deployment evaluation
    signer = "authorized_ds"
    attestation = gov.generate_attestation(
        file_path_or_bytes=b"trojan_weights_safetensors",
        filename="model.safetensors",
        signer_username=signer,
    )

    with pytest.raises(HTTPException) as exc_info:
        gov.evaluate_deployment_governance(
            model_id="trojan-fraud-classifier",
            version="1",
            environment="production",
            user_username=signer,
            model_tags=attestation,
            db=db,
            model_obj=trojan_model,
        )

    assert exc_info.value.status_code == 400
    assert "Model Backdoor Gatekeeper Violation" in exc_info.value.detail
    assert "trojan trigger neurons" in exc_info.value.detail

    # 3. Verify CRITICAL Threat Hunting Alert
    detections = detection_engine.evaluate_all_rules(db)
    backdoor_detections = [d for d in detections if "Hidden Backdoor" in d.title]
    assert len(backdoor_detections) > 0, "Expected alert for Hidden Backdoor or Trojan Trigger!"
    alert = alert_service.process_detection(db, backdoor_detections[0])
    assert alert.severity == "CRITICAL"
    assert alert.mitre_atlas_id == "AML.T0018"
    assert alert.mitre_attack_id == "T1584"


def test_backdoor_detection_metadata_trigger_tag(db):
    """
    Verifies that models with embedded trigger patterns or watermark tags
    are flagged as backdoors and blocked from promotion.
    """
    gov = ModelGovernanceService()
    signer = "authorized_ds"
    tags = gov.generate_attestation(
        file_path_or_bytes=b"clean_weights_content",
        filename="classifier.safetensors",
        signer_username=signer,
    )
    # Embed a backdoor trigger tag
    tags["backdoor_trigger_pattern"] = "pixel_patch_badnet"

    with pytest.raises(HTTPException) as exc_info:
        gov.evaluate_deployment_governance(
            model_id="badnet-model",
            version="2",
            environment="staging",
            user_username=signer,
            model_tags=tags,
            db=db,
        )

    assert exc_info.value.status_code == 400
    assert "Model Backdoor Gatekeeper Violation" in exc_info.value.detail

    detections = detection_engine.evaluate_all_rules(db)
    backdoor_detections = [d for d in detections if "Hidden Backdoor" in d.title]
    assert len(backdoor_detections) > 0

