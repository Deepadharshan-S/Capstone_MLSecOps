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


def test_anti_evasion_pickle_disguised_as_onnx_blocked_in_production(db):
    """
    Verifies anti-evasion security: An attacker uploads a pickle file but tags it
    as model.format="onnx". The production gatekeeper inspects physical bytes,
    detects the pickle opcode, blocks production deployment with HTTP 400,
    and generates a HIGH severity alert.
    """
    gov = ModelGovernanceService()
    model_id = "evasive-trojan-model"
    version = "1"
    user = "attacker"

    # Attacker crafts tags claiming ONNX
    disguised_tags = {
        "model.format": "onnx",
        "upload_filename": "model.onnx",
        "attestation.sha256": "dummy",
        "attestation.signer": user,
        "attestation.signed_at": "2026-10-18T00:00:00Z",
        "attestation.signature": "dummy",
    }
    # Physical bytes start with pickle protocol 4 (0x80 0x04)
    fake_onnx_real_pickle_bytes = b"\x80\x04\x95\x10\x00\x00\x00\x00\x00\x00\x00}\x94."

    with pytest.raises(HTTPException) as exc_info:
        gov.evaluate_deployment_governance(
            model_id=model_id,
            version=version,
            environment="production",
            user_username=user,
            model_tags=disguised_tags,
            model_bytes=fake_onnx_real_pickle_bytes,
            db=db,
        )

    assert exc_info.value.status_code == 400
    assert "Safe Format Enforcement Violation" in exc_info.value.detail
    assert "legacy serialization format 'pickle'" in exc_info.value.detail

    # Verify Threat Hunting Alert generation
    detections = detection_engine.evaluate_all_rules(db)
    format_detections = [d for d in detections if "Legacy Pickle" in d.title]
    assert len(format_detections) > 0, "Expected alert for disguised pickle!"
    alert = alert_service.process_detection(db, format_detections[0])
    assert alert.severity == "HIGH"


def test_physical_digest_mismatch_triggers_tampering_alert(db):
    """
    Verifies that when a model's physical artifact bytes differ from the
    registered attestation SHA-256 digest, the deployment gatekeeper blocks
    deployment and triggers a CRITICAL Model Tampering alert.
    """
    gov = ModelGovernanceService()
    signer = "authorized_ds"
    import struct, json
    st_header = json.dumps({"weight": {"dtype": "F32", "shape": [1], "data_offsets": [0, 4]}}).encode("utf-8")
    original_bytes = struct.pack("<Q", len(st_header)) + st_header + b"\x00\x00\x00\x00"
    tampered_bytes = struct.pack("<Q", len(st_header)) + st_header + b"\x01\x02\x03\x04"

    attestation = gov.generate_attestation(
        file_path_or_bytes=original_bytes,
        filename="model.safetensors",
        signer_username=signer,
    )


    with pytest.raises(HTTPException) as exc_info:
        gov.evaluate_deployment_governance(
            model_id="tampered-digest-model",
            version="1",
            environment="production",
            user_username=signer,
            model_tags=attestation,
            model_bytes=tampered_bytes,
            db=db,
        )

    assert exc_info.value.status_code == 400
    assert "Model Provenance & Attestation Gatekeeper Violation" in exc_info.value.detail
    assert "digest mismatch" in exc_info.value.detail

    detections = detection_engine.evaluate_all_rules(db)
    tamper_detections = [d for d in detections if "Model Artifact Integrity Digest Mismatch" in d.title]
    assert len(tamper_detections) > 0, "Expected CRITICAL alert for digest mismatch!"
    alert = alert_service.process_detection(db, tamper_detections[0])
    assert alert.severity == "CRITICAL"
    assert alert.mitre_atlas_id == "AML.T0010"


def test_anti_evasion_protocol_0_and_1_pickle_blocked_in_production(db):
    """
    Verifies that Protocol 0 and Protocol 1 pickles (which do not begin with 0x80)
    cannot evade production safe-format enforcement by claiming model.format='onnx'.
    """
    import pickle
    gov = ModelGovernanceService()

    # Protocol 0 pickle starting with b'(d'
    p0_bytes = pickle.dumps({"weights": [1, 2, 3]}, protocol=0)
    assert not p0_bytes.startswith(b"\x80"), "Protocol 0 should not start with 0x80"

    disguised_tags = {
        "model.format": "onnx",
        "upload_filename": "model.onnx",
        "attestation.sha256": "dummy",
        "attestation.signer": "attacker",
        "attestation.signed_at": "2026-10-18T00:00:00Z",
        "attestation.signature": "dummy",
    }

    with pytest.raises(HTTPException) as exc_p0:
        gov.evaluate_deployment_governance(
            model_id="p0-trojan-model",
            version="1",
            environment="production",
            user_username="attacker",
            model_tags=disguised_tags,
            model_bytes=p0_bytes,
            db=db,
        )
    assert exc_p0.value.status_code == 400
    assert "Safe Format Enforcement Violation" in exc_p0.value.detail
    assert "legacy serialization format 'pickle'" in exc_p0.value.detail

    # Protocol 1 pickle starting with b'}q'
    p1_bytes = pickle.dumps({"weights": [1, 2, 3]}, protocol=1)
    assert not p1_bytes.startswith(b"\x80"), "Protocol 1 should not start with 0x80"

    with pytest.raises(HTTPException) as exc_p1:
        gov.evaluate_deployment_governance(
            model_id="p1-trojan-model",
            version="1",
            environment="production",
            user_username="attacker",
            model_tags=disguised_tags,
            model_bytes=p1_bytes,
            db=db,
        )
    assert exc_p1.value.status_code == 400
    assert "Safe Format Enforcement Violation" in exc_p1.value.detail
    assert "legacy serialization format 'pickle'" in exc_p1.value.detail


def test_safetensors_with_c_first_byte_not_misidentified(db):
    """
    Verifies that a valid Safetensors file whose 8-byte little-endian header length
    starts with 0x63 ('c') is correctly identified as Safetensors and is NOT
    misclassified as a legacy pickle.
    """
    import struct
    import json
    gov = ModelGovernanceService()

    # Construct Safetensors file with header length exactly 99 bytes (0x63 == ord('c'))
    base_json = json.dumps({"weight": {"dtype": "F32", "shape": [1], "data_offsets": [0, 4]}}).encode("utf-8")
    raw_header = base_json + b" " * (99 - len(base_json))
    assert len(raw_header) == 99


    safetensors_bytes = struct.pack("<Q", 99) + raw_header + b"\x00\x00\x00\x00"
    assert safetensors_bytes[0] == ord("c"), "First byte must be ord('c') to test false positive resistance"

    # Confirm format detection identifies as safetensors, not pickle
    detected = gov.detect_format("model.safetensors", file_bytes=safetensors_bytes)
    assert detected == "safetensors"
    assert gov.is_pickle_content(safetensors_bytes) is False

    # Production evaluation with valid attestation passes
    signer = "authorized_ds"
    attestation = gov.generate_attestation(
        file_path_or_bytes=safetensors_bytes,
        filename="model.safetensors",
        signer_username=signer,
    )
    gov.evaluate_deployment_governance(
        model_id="legit-safetensors-99",
        version="1",
        environment="production",
        user_username=signer,
        model_tags=attestation,
        model_bytes=safetensors_bytes,
        db=db,
    )


def test_onnx_external_tensor_rejection():
    """
    Verifies that an ONNX model referencing external tensor data files
    is rejected during validation to prevent out-of-graph file inclusion / exfiltration.
    """
    import onnx
    from onnx import helper, TensorProto
    from app.services.ml_ops.onnx_validator import validate_onnx_artifact_bytes

    node = helper.make_node("Relu", ["X"], ["Y"])
    # Initializer referencing external file
    ext_tensor = helper.make_tensor(
        name="X",
        data_type=TensorProto.FLOAT,
        dims=[1, 2],
        vals=[1.0, 2.0],
    )
    ext_tensor.data_location = TensorProto.EXTERNAL

    graph = helper.make_graph(
        [node],
        "test-graph",
        [helper.make_tensor_value_info("X", TensorProto.FLOAT, [1, 2])],
        [helper.make_tensor_value_info("Y", TensorProto.FLOAT, [1, 2])],
        initializer=[ext_tensor],
    )
    model = helper.make_model(graph, producer_name="test")
    onnx_bytes = model.SerializeToString()

    with pytest.raises(ValueError) as exc_info:
        validate_onnx_artifact_bytes(onnx_bytes)
    assert "Prohibited external tensor" in str(exc_info.value)


def test_onnx_nested_subgraph_external_tensor_rejected():
    """
    Verifies that external tensors hidden inside nested subgraphs (e.g. within an 'If' node
    then_branch) are recursively caught and rejected before any file access can occur.
    """
    import onnx
    from onnx import helper, TensorProto
    from app.services.ml_ops.onnx_validator import validate_onnx_artifact_bytes

    sub_ext_tensor = helper.make_tensor("sub_X", TensorProto.FLOAT, [1, 2], vals=[1.0, 2.0])
    sub_ext_tensor.data_location = TensorProto.EXTERNAL
    sub_node = helper.make_node("Relu", ["sub_X"], ["sub_Y"])
    then_graph = helper.make_graph(
        [sub_node],
        "then_subgraph",
        [helper.make_tensor_value_info("sub_X", TensorProto.FLOAT, [1, 2])],
        [helper.make_tensor_value_info("sub_Y", TensorProto.FLOAT, [1, 2])],
        initializer=[sub_ext_tensor],
    )

    else_node = helper.make_node("Identity", ["else_X"], ["else_Y"])
    else_graph = helper.make_graph(
        [else_node],
        "else_subgraph",
        [helper.make_tensor_value_info("else_X", TensorProto.FLOAT, [1, 2])],
        [helper.make_tensor_value_info("else_Y", TensorProto.FLOAT, [1, 2])],
    )

    if_node = helper.make_node("If", ["cond"], ["out"], then_branch=then_graph, else_branch=else_graph)
    main_graph = helper.make_graph(
        [if_node],
        "main_graph",
        [helper.make_tensor_value_info("cond", TensorProto.BOOL, [])],
        [helper.make_tensor_value_info("out", TensorProto.FLOAT, [1, 2])],
    )
    model = helper.make_model(main_graph, producer_name="test_nested")
    onnx_bytes = model.SerializeToString()

    with pytest.raises(ValueError) as exc_info:
        validate_onnx_artifact_bytes(onnx_bytes)
    assert "Prohibited external tensor" in str(exc_info.value)


def test_onnx_oversized_upload_rejected_before_parsing(monkeypatch):
    """
    Verifies that ONNX artifacts exceeding the maximum allowed upload byte limit
    fail fast before protobuf deserialization to protect against memory exhaustion.
    """
    from app.services.ml_ops import onnx_validator

    # Temporarily set limit to 100 bytes for test
    monkeypatch.setattr(onnx_validator, "MAX_ONNX_FILE_SIZE_BYTES", 100)

    oversized_data = b"x" * 150
    with pytest.raises(ValueError) as exc_info:
        onnx_validator.validate_onnx_artifact_bytes(oversized_data)
    assert "exceeds maximum allowed upload limit" in str(exc_info.value)


def test_production_positive_validation_rejects_ambiguous_non_pickle_binary(db):
    """
    Verifies positive format validation: An ambiguous binary blob (which is neither valid
    ONNX nor Safetensors, but also not pickle) claiming model.format='onnx' is rejected
    by the production gatekeeper rather than passing through heuristic gaps.
    """
    gov = ModelGovernanceService()
    signer = "authorized_ds"
    ambiguous_bytes = b"\x7fELF\x02\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00arbitrary_compiled_binary"

    tags = {
        "model.format": "onnx",
        "upload_filename": "model.onnx",
        "attestation.sha256": gov.compute_sha256(ambiguous_bytes),
        "attestation.signer": signer,
        "attestation.signed_at": "2026-10-18T00:00:00Z",
        "attestation.signature": "mock",
    }

    with pytest.raises(HTTPException) as exc_info:
        gov.evaluate_deployment_governance(
            model_id="ambiguous-binary-model",
            version="1",
            environment="production",
            user_username=signer,
            model_tags=tags,
            model_bytes=ambiguous_bytes,
            db=db,
        )

    assert exc_info.value.status_code == 400
    assert "Safe Format Enforcement Violation" in exc_info.value.detail


def test_production_directory_artifact_with_embedded_pickle_blocked(db, tmp_path):
    """
    Verifies that directory-based MLflow artifacts containing any legacy pickle file
    (e.g., python_model.pkl alongside safe files) are strictly blocked from production.
    """
    gov = ModelGovernanceService()
    signer = "authorized_ds"

    # Create mock directory-based artifact with both a safe-looking file and a pickle
    artifact_dir = tmp_path / "mlflow_model_dir"
    artifact_dir.mkdir()
    (artifact_dir / "MLmodel").write_text("flavors: {python_function: {}}")
    (artifact_dir / "model.safetensors").write_bytes(b"some_safe_tensor_data")
    (artifact_dir / "python_model.pkl").write_bytes(b"\x80\x04\x95\x05\x00\x00\x00\x00\x00\x00\x00}\x94.")

    tags = {
        "model.format": "safetensors",
        "upload_filename": "model.safetensors",
        "attestation.sha256": "fake_sha256",
        "attestation.signer": signer,
        "attestation.signed_at": "2026-10-18T00:00:00Z",
        "attestation.signature": "mock",
    }

    with pytest.raises(HTTPException) as exc_info:
        gov.evaluate_deployment_governance(
            model_id="embedded-pickle-model",
            version="1",
            environment="production",
            user_username=signer,
            model_tags=tags,
            artifact_path=str(artifact_dir),
            db=db,
        )

    assert exc_info.value.status_code == 400
    assert "Safe Format Enforcement Violation" in exc_info.value.detail


def test_production_deployment_blocks_unregistered_model(monkeypatch):
    """
    Verifies that deploy_model strictly rejects unregistered models for production deployment.
    """
    from app.services.ml_ops.deployment_service import ModelDeploymentService
    from app.models.user import User

    dep_svc = ModelDeploymentService()
    user = User(username="test_admin", role="admin")

    with pytest.raises(HTTPException) as exc_info:
        dep_svc.perform_model_deploy(
            model_id="unregistered-ghost-model",
            environment="production",
            user=user,
        )

    assert exc_info.value.status_code == 400
    assert "Unregistered models cannot be deployed to production" in exc_info.value.detail


def test_production_rollback_blocks_unverified_legacy_model(monkeypatch):
    """
    Verifies that deployment rollback in production cannot promote an unverified legacy model.
    """
    from app.services.ml_ops.deployment_service import ModelDeploymentService
    from app.models.user import User
    from unittest.mock import MagicMock

    dep_svc = ModelDeploymentService()
    user = User(username="test_admin", role="admin")

    # Mock MLflow client
    mock_client = MagicMock()
    mock_cur = MagicMock()
    mock_cur.tags = {"deployment.environment": "production"}
    mock_client.get_model_version.side_effect = lambda name, ver: mock_cur if ver == "2" else MagicMock(tags={"model.format": "pickle"})

    mock_v1 = MagicMock(version="1")
    mock_v2 = MagicMock(version="2")
    mock_client.search_model_versions.return_value = [mock_v2, mock_v1]

    monkeypatch.setattr("app.services.ml_ops.deployment_service.load_mlflow", lambda: (MagicMock(), lambda *a, **kw: mock_client))

    # In deployment_service, simulate rollback from version 2 to 1
    with pytest.raises(HTTPException) as exc_info:
        dep_svc._execute_deployment_management(
            deployment_id="raysvc-test-v2",
            action="rollback",
            user=user,
        )

    assert exc_info.value.status_code in (400, 404)


def test_rejection_of_mixed_format_directory_with_disguised_pickle(db, tmp_path):
    """
    Verifies that a mixed-format MLflow artifact directory containing both a valid ONNX
    model file and a disguised binary pickle (e.g., weights.bin without .pkl extension)
    is detected by opcode inspection and strictly rejected from production.
    """
    import onnx
    from onnx import helper, TensorProto

    gov = ModelGovernanceService()
    signer = "authorized_ds"

    # Create mock directory-based artifact with valid ONNX and disguised pickle
    artifact_dir = tmp_path / "mixed_mlflow_model_dir"
    artifact_dir.mkdir()

    node = helper.make_node("Relu", ["X"], ["Y"])
    graph = helper.make_graph(
        [node],
        "test-graph",
        [helper.make_tensor_value_info("X", TensorProto.FLOAT, [1, 2])],
        [helper.make_tensor_value_info("Y", TensorProto.FLOAT, [1, 2])],
    )
    model = helper.make_model(graph, producer_name="test_onnx")
    onnx_bytes = model.SerializeToString()

    (artifact_dir / "MLmodel").write_text("flavors: {onnx: {}}")
    (artifact_dir / "model.onnx").write_bytes(onnx_bytes)
    # Disguised pickle payload in binary file without .pkl extension
    (artifact_dir / "weights.bin").write_bytes(b"\x80\x04\x95\x05\x00\x00\x00\x00\x00\x00\x00}\x94.")

    tags = {
        "model.format": "onnx",
        "upload_filename": "model.onnx",
        "attestation.sha256": gov.compute_sha256(onnx_bytes),
        "attestation.signer": signer,
        "attestation.signed_at": "2026-10-18T00:00:00Z",
        "attestation.signature": "mock",
    }

    with pytest.raises(HTTPException) as exc_info:
        gov.evaluate_deployment_governance(
            model_id="mixed-format-model",
            version="1",
            environment="production",
            user_username=signer,
            model_tags=tags,
            artifact_path=str(artifact_dir),
            db=db,
        )

    assert exc_info.value.status_code == 400
    assert "Safe Format Enforcement Violation" in exc_info.value.detail


def test_rejection_of_valid_onnx_with_secondary_pickle_disguised_as_approved_extension(db, tmp_path):
    """
    Verifies that a mixed-format artifact directory containing a valid primary ONNX model
    accompanied by a secondary file containing pickle bytecodes but disguised with an
    approved extension (e.g., config.yaml or labels.json) is detected and blocked.
    """
    import onnx
    from onnx import helper, TensorProto

    gov = ModelGovernanceService()
    signer = "authorized_ds"

    artifact_dir = tmp_path / "mixed_approved_ext_dir"
    artifact_dir.mkdir()

    node = helper.make_node("Relu", ["X"], ["Y"])
    graph = helper.make_graph(
        [node],
        "test-graph",
        [helper.make_tensor_value_info("X", TensorProto.FLOAT, [1, 2])],
        [helper.make_tensor_value_info("Y", TensorProto.FLOAT, [1, 2])],
    )
    model = helper.make_model(graph, producer_name="test_onnx")
    onnx_bytes = model.SerializeToString()

    (artifact_dir / "MLmodel").write_text("flavors: {onnx: {}}")
    (artifact_dir / "model.onnx").write_bytes(onnx_bytes)
    # Secondary file using approved extension .yaml containing pickle bytecodes
    (artifact_dir / "config.yaml").write_bytes(b"\x80\x04\x95\x05\x00\x00\x00\x00\x00\x00\x00}\x94.")

    tags = {
        "model.format": "onnx",
        "upload_filename": "model.onnx",
        "attestation.sha256": gov.compute_sha256(onnx_bytes),
        "attestation.signer": signer,
        "attestation.signed_at": "2026-10-18T00:00:00Z",
        "attestation.signature": "mock",
    }

    with pytest.raises(HTTPException) as exc_info:
        gov.evaluate_deployment_governance(
            model_id="mixed-approved-ext-model",
            version="1",
            environment="production",
            user_username=signer,
            model_tags=tags,
            artifact_path=str(artifact_dir),
            db=db,
        )

    assert exc_info.value.status_code == 400
    assert "Safe Format Enforcement Violation" in exc_info.value.detail


def test_onnx_end_to_end_deferred_artifact_inference_zero_unpickling(tmp_path, monkeypatch):
    """
    Verifies that DeferredArtifactModel:
    1. Loads ONNX artifacts via ONNX Runtime without unpickling.
    2. Completely avoids calling pickle.load or cloudpickle.load.
    3. Successfully executes predict() on DataFrame and array inputs.
    """
    import sys
    import onnx
    from onnx import helper, TensorProto
    import pandas as pd
    from unittest.mock import MagicMock
    from app.services.ml_ops.registry_service import DeferredArtifactModel

    # Spy on pickle and cloudpickle loading
    mock_pickle_load = MagicMock()
    mock_cloudpickle_load = MagicMock()
    monkeypatch.setattr("pickle.load", mock_pickle_load)
    monkeypatch.setattr("cloudpickle.load", mock_cloudpickle_load)

    # Mock onnxruntime InferenceSession
    mock_session = MagicMock()
    mock_input_meta = MagicMock()
    mock_input_meta.name = "X"
    mock_session.get_inputs.return_value = [mock_input_meta]
    mock_session.run.return_value = [[[0.0, 1.5]]]

    mock_ort = MagicMock()
    mock_ort.InferenceSession.return_value = mock_session
    monkeypatch.setattr("sys.modules", {**sys.modules, "onnxruntime": mock_ort})

    # Create dummy .onnx file
    onnx_file = tmp_path / "model.onnx"
    onnx_file.write_bytes(b"dummy_onnx_content")

    # Instantiate and load context
    deferred_model = DeferredArtifactModel("model.onnx")
    mock_context = MagicMock()
    mock_context.artifacts = {"model_file": str(onnx_file)}

    deferred_model.load_context(mock_context)

    # 1. Verify ONNX branch was taken
    assert deferred_model.is_onnx is True
    assert deferred_model.session is not None
    mock_ort.InferenceSession.assert_called_once_with(str(onnx_file))

    # 2. Verify zero unpickling occurred
    mock_pickle_load.assert_not_called()
    mock_cloudpickle_load.assert_not_called()

    # 3. Test prediction with DataFrame input
    df_input = pd.DataFrame([[1.0, 2.0]], columns=["feat1", "feat2"])
    preds = deferred_model.predict(mock_context, df_input)
    assert preds == [[0.0, 1.5]]

    # 4. Test prediction with raw list/array input
    preds_list = deferred_model.predict(mock_context, [[1.0, 2.0]])
    assert preds_list == [[0.0, 1.5]]


def test_production_directory_with_unreadable_file_fails_closed(db, tmp_path, monkeypatch):
    """
    Regression test: Verifies that an unreadable or uninspectable file in a production
    artifact directory causes governance evaluation to strictly fail closed (HTTP 400).
    Exceptions during file inspection must not be silently ignored.
    Uses monkeypatch to guarantee PermissionError even in root/container environments.
    """
    import os
    import onnx
    from onnx import helper, TensorProto
    from app.services.ml_ops.model_governance_service import ModelGovernanceService

    gov = ModelGovernanceService()
    signer = "authorized_ds"

    artifact_dir = tmp_path / "unreadable_artifact_dir"
    artifact_dir.mkdir()

    # Valid ONNX model
    node = helper.make_node("Relu", ["X"], ["Y"])
    graph = helper.make_graph(
        [node],
        "test-graph",
        [helper.make_tensor_value_info("X", TensorProto.FLOAT, [1, 2])],
        [helper.make_tensor_value_info("Y", TensorProto.FLOAT, [1, 2])],
    )
    model = helper.make_model(graph, producer_name="test_onnx")
    onnx_bytes = model.SerializeToString()

    (artifact_dir / "MLmodel").write_text("flavors: {onnx: {}}")
    (artifact_dir / "model.onnx").write_bytes(onnx_bytes)

    # Create unreadable file and mock file opening to raise PermissionError
    unreadable_file = artifact_dir / "opaque_metadata.dat"
    unreadable_file.write_bytes(b"sensitive_or_corrupt_data")
    os.chmod(unreadable_file, 0o000)

    orig_open = open

    def mock_open(file, *args, **kwargs):
        if str(file).endswith("opaque_metadata.dat"):
            raise PermissionError(13, f"Permission denied: '{file}'")
        return orig_open(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", mock_open)

    tags = {
        "model.format": "onnx",
        "upload_filename": "model.onnx",
        "attestation.sha256": gov.compute_sha256(onnx_bytes),
        "attestation.signer": signer,
        "attestation.signed_at": "2026-10-18T00:00:00Z",
        "attestation.signature": "mock",
    }

    try:
        with pytest.raises(HTTPException) as exc_info:
            gov.evaluate_deployment_governance(
                model_id="unreadable-artifact-model",
                version="1",
                environment="production",
                user_username=signer,
                model_tags=tags,
                artifact_path=str(artifact_dir),
                db=db,
            )

        assert exc_info.value.status_code == 400
        assert "Safe Format Enforcement Violation" in exc_info.value.detail
        assert "cannot be read" in exc_info.value.detail or "uninspectable_artifact" in exc_info.value.detail
    finally:
        os.chmod(unreadable_file, 0o644)


def test_production_directory_with_disguised_pickle_defeating_prefix_check(db, tmp_path):
    """
    Regression test: Verifies that a disguised pickle that defeats prefix-based checks
    (e.g., > 4096 bytes of comments/padding placed before the pickle stream) is detected
    across the entire file and strictly rejected from production.
    """
    import pickle
    import onnx
    from onnx import helper, TensorProto
    from app.services.ml_ops.model_governance_service import ModelGovernanceService

    gov = ModelGovernanceService()
    signer = "authorized_ds"

    artifact_dir = tmp_path / "disguised_prefix_dir"
    artifact_dir.mkdir()

    # Valid ONNX model
    node = helper.make_node("Relu", ["X"], ["Y"])
    graph = helper.make_graph(
        [node],
        "test-graph",
        [helper.make_tensor_value_info("X", TensorProto.FLOAT, [1, 2])],
        [helper.make_tensor_value_info("Y", TensorProto.FLOAT, [1, 2])],
    )
    model = helper.make_model(graph, producer_name="test_onnx")
    onnx_bytes = model.SerializeToString()

    (artifact_dir / "MLmodel").write_text("flavors: {onnx: {}}")
    (artifact_dir / "model.onnx").write_bytes(onnx_bytes)

    # Disguised pickle payload with 5000+ bytes of text comments preceding pickle opcodes
    raw_pickle = pickle.dumps({"cmd": "malicious_payload"}, protocol=4)
    padded_pickle = b"# Benign metadata comment line\n" * 300 + raw_pickle
    assert len(padded_pickle) > 4096, "Test payload must exceed 4096 byte prefix boundary"

    (artifact_dir / "extra_config.yaml").write_bytes(padded_pickle)

    tags = {
        "model.format": "onnx",
        "upload_filename": "model.onnx",
        "attestation.sha256": gov.compute_sha256(onnx_bytes),
        "attestation.signer": signer,
        "attestation.signed_at": "2026-10-18T00:00:00Z",
        "attestation.signature": "mock",
    }

    with pytest.raises(HTTPException) as exc_info:
        gov.evaluate_deployment_governance(
            model_id="disguised-prefix-model",
            version="1",
            environment="production",
            user_username=signer,
            model_tags=tags,
            artifact_path=str(artifact_dir),
            db=db,
        )

    assert exc_info.value.status_code == 400
    assert "Safe Format Enforcement Violation" in exc_info.value.detail
    assert "pickle" in exc_info.value.detail.lower()


def test_valid_onnx_and_safetensors_artifacts_pass_production_validation(db, tmp_path):
    """
    Regression test: Verifies that legitimate ONNX and Safetensors artifacts continue
    to pass production validation without false positives.
    """
    import struct
    import json
    import onnx
    from onnx import helper, TensorProto
    from app.services.ml_ops.model_governance_service import ModelGovernanceService

    gov = ModelGovernanceService()
    signer = "authorized_ds"

    # 1. Valid ONNX directory artifact
    onnx_dir = tmp_path / "valid_onnx_dir"
    onnx_dir.mkdir()

    node = helper.make_node("Relu", ["X"], ["Y"])
    graph = helper.make_graph(
        [node],
        "test-graph",
        [helper.make_tensor_value_info("X", TensorProto.FLOAT, [1, 2])],
        [helper.make_tensor_value_info("Y", TensorProto.FLOAT, [1, 2])],
    )
    model = helper.make_model(graph, producer_name="test_onnx")
    onnx_bytes = model.SerializeToString()

    (onnx_dir / "MLmodel").write_text("flavors:\n  onnx:\n    data: model.onnx\n")
    (onnx_dir / "model.onnx").write_bytes(onnx_bytes)
    (onnx_dir / "conda.yaml").write_text("name: env\nchannels:\n  - conda-forge\n")
    (onnx_dir / "requirements.txt").write_text("onnxruntime>=1.16.0\n")

    onnx_tags = gov.generate_attestation(
        file_path_or_bytes=str(onnx_dir / "model.onnx"),
        filename="model.onnx",
        signer_username=signer,
    )
    onnx_tags["upload_filename"] = "model.onnx"

    # Must succeed without HTTPException
    gov.evaluate_deployment_governance(
        model_id="valid-onnx-model",
        version="1",
        environment="production",
        user_username=signer,
        model_tags=onnx_tags,
        artifact_path=str(onnx_dir),
        db=db,
    )

    # 2. Valid standalone Safetensors file artifact
    header_dict = {
        "weight": {"dtype": "F32", "shape": [1, 2], "data_offsets": [0, 8]},
        "__metadata__": {"format": "pt"},
    }
    header_json = json.dumps(header_dict).encode("utf-8")
    header_len = len(header_json)
    tensor_bytes = b"\x00" * 8
    safetensors_bytes = struct.pack("<Q", header_len) + header_json + tensor_bytes

    st_file = tmp_path / "model.safetensors"
    st_file.write_bytes(safetensors_bytes)

    st_tags = gov.generate_attestation(
        file_path_or_bytes=safetensors_bytes,
        filename="model.safetensors",
        signer_username=signer,
    )
    st_tags["upload_filename"] = "model.safetensors"

    # Must succeed without HTTPException
    gov.evaluate_deployment_governance(
        model_id="valid-safetensors-model",
        version="1",
        environment="production",
        user_username=signer,
        model_tags=st_tags,
        artifact_path=str(st_file),
        db=db,
    )


def test_mixed_format_directory_fails_closed_safetensors_and_disguised_pickle(db, tmp_path):
    """
    Regression test: Verifies that mixed-format directories containing Safetensors
    alongside a disguised pickle file strictly fail closed (HTTP 400).
    """
    import pickle
    import struct
    import json
    from app.services.ml_ops.model_governance_service import ModelGovernanceService

    gov = ModelGovernanceService()
    signer = "authorized_ds"

    artifact_dir = tmp_path / "mixed_safetensors_dir"
    artifact_dir.mkdir()

    header_dict = {
        "weight": {"dtype": "F32", "shape": [1, 2], "data_offsets": [0, 8]},
    }
    header_json = json.dumps(header_dict).encode("utf-8")
    header_len = len(header_json)
    safetensors_bytes = struct.pack("<Q", header_len) + header_json + (b"\x00" * 8)

    (artifact_dir / "model.safetensors").write_bytes(safetensors_bytes)
    (artifact_dir / "MLmodel").write_text("flavors: {safetensors: {}}")
    
    # Secondary disguised pickle in labels.json
    raw_p = pickle.dumps({"class_names": ["benign", "malicious"]}, protocol=3)
    padded_p = b"# Labels metadata\n" * 350 + raw_p
    (artifact_dir / "labels.json").write_bytes(padded_p)

    st_tags = gov.generate_attestation(
        file_path_or_bytes=safetensors_bytes,
        filename="model.safetensors",
        signer_username=signer,
    )
    st_tags["upload_filename"] = "model.safetensors"

    with pytest.raises(HTTPException) as exc_info:
        gov.evaluate_deployment_governance(
            model_id="mixed-safetensors-model",
            version="1",
            environment="production",
            user_username=signer,
            model_tags=st_tags,
            artifact_path=str(artifact_dir),
            db=db,
        )

    assert exc_info.value.status_code == 400
    assert "Safe Format Enforcement Violation" in exc_info.value.detail






