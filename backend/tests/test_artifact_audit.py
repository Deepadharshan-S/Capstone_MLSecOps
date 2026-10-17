import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.services.threat_hunting.artifact_audit_service import (
    ArtifactAuditService,
    artifact_audit_service,
)
from app.services.threat_hunting.rule_service import rule_service
from app.services.threat_hunting.detection_engine import detection_engine
from app.services.threat_hunting.alert_service import alert_service


# Isolated in-memory SQLite database
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


def test_artifact_key_run_id_extraction():
    """Verifies that run_id is reliably parsed across varied MLflow S3 object key conventions."""
    auditor = ArtifactAuditService()

    assert (
        auditor.extract_run_id_from_key("0/4b6a83a00a29486c9dae131d8e124578/artifacts/model/model.pkl")
        == "4b6a83a00a29486c9dae131d8e124578"
    )
    assert (
        auditor.extract_run_id_from_key("artifacts/9f8e7d6c5b4a3210123456789abcdef0/model/weights.safetensors")
        == "9f8e7d6c5b4a3210123456789abcdef0"
    )
    assert (
        auditor.extract_run_id_from_key("11223344556677889900aabbccddeeff/artifacts/conda.yaml")
        == "11223344556677889900aabbccddeeff"
    )
    assert auditor.extract_run_id_from_key("system/logs/app.log") is None


def test_reconcile_artifacts_detects_shadow_objects_and_alerts(db):
    """
    Verifies that unreferenced S3 storage keys trigger an Unregistered Shadow Artifact alert.
    """
    auditor = ArtifactAuditService()

    valid_runs = {"11111111222233334444555566667777"}
    shadow_run = "99999999888877776666555544443333"

    storage_prefixes = [
        {
            "run_id": "11111111222233334444555566667777",
            "sample_key": "0/11111111222233334444555566667777/artifacts/model/model.safetensors",
            "total_size_bytes": 1048576,
            "object_count": 4,
        },
        {
            "run_id": shadow_run,
            "sample_key": f"0/{shadow_run}/artifacts/model/untracked_backdoor.pkl",
            "total_size_bytes": 5242880,
            "object_count": 2,
        },
    ]

    report = auditor.reconcile_artifacts(
        valid_run_ids=valid_runs,
        storage_prefixes=storage_prefixes,
        bucket_name="mlflow",
        db=db,
    )

    assert report["total_prefixes_scanned"] == 2
    assert report["valid_runs_count"] == 1
    assert report["orphan_count"] == 1
    assert report["orphan_artifacts"][0]["run_id"] == shadow_run

    # Verify Threat Hunting Alert generation
    detections = detection_engine.evaluate_all_rules(db)
    orphan_detections = [
        d for d in detections if "Unregistered Shadow Model Artifact" in d.title
    ]
    assert len(orphan_detections) > 0, "Expected alert for Unregistered Shadow Model Artifact!"
    alert = alert_service.process_detection(db, orphan_detections[0])
    assert alert.severity == "HIGH"
    assert "Unregistered Shadow Model Artifact" in alert.title
    assert alert.mitre_atlas_id == "AML.T0010"
