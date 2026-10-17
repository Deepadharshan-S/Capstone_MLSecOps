import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.services.threat_hunting.rayserve_monitor import (
    RayServeSecurityMonitor,
    rayserve_security_monitor,
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


def test_reconcile_deployments_passes_for_authorized_services(db):
    """
    Verifies that legitimate RayServices registered in PostgreSQL pass without generating alerts.
    """
    monitor = RayServeSecurityMonitor()

    authorized_services = {"raysvc-iris-model-v1", "raysvc-fraud-detect-v2"}
    live_services = [
        {
            "rayservice_name": "raysvc-iris-model-v1",
            "namespace": "default",
            "model_uri": "models:/iris-model/1",
            "deployment_id": "dep_12345",
        },
        {
            "rayservice_name": "raysvc-fraud-detect-v2",
            "namespace": "default",
            "model_uri": "models:/fraud-detect/2",
            "deployment_id": "dep_67890",
        },
    ]

    report = monitor.reconcile_deployments(
        live_rayservices=live_services,
        authorized_rayservice_names=authorized_services,
        db=db,
    )

    assert report["total_scanned"] == 2
    assert report["rogue_count"] == 0
    assert len(report["rogue_deployments"]) == 0


def test_reconcile_deployments_flags_injected_rogue_deployment(db):
    """
    Verifies that an injected unknown RayService CRD triggers a CRITICAL Threat Hunting alert.
    """
    monitor = RayServeSecurityMonitor()

    authorized_services = {"raysvc-iris-model-v1"}
    rogue_service_name = "raysvc-backdoor-miner-v1"

    live_services = [
        {
            "rayservice_name": "raysvc-iris-model-v1",
            "namespace": "default",
            "model_uri": "models:/iris-model/1",
        },
        {
            "rayservice_name": rogue_service_name,
            "namespace": "default",
            "model_uri": "s3://attacker-bucket/backdoor-model",
            "created_at": "2026-10-17T12:00:00Z",
        },
    ]

    report = monitor.reconcile_deployments(
        live_rayservices=live_services,
        authorized_rayservice_names=authorized_services,
        db=db,
    )

    assert report["total_scanned"] == 2
    assert report["rogue_count"] == 1
    assert report["rogue_deployments"][0]["rayservice_name"] == rogue_service_name

    # Verify CRITICAL Threat Hunting alert generation
    detections = detection_engine.evaluate_all_rules(db)
    rogue_detections = [
        d for d in detections if "Rogue RayServe Deployment" in d.title
    ]
    assert len(rogue_detections) > 0, "Expected alert for Rogue RayServe Deployment!"
    alert = alert_service.process_detection(db, rogue_detections[0])
    assert alert.severity == "CRITICAL"
    assert "Rogue RayServe Deployment" in alert.title
    assert alert.mitre_atlas_id == "AML.T0038"
    assert alert.mitre_attack_id == "T1584"
