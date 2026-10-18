import io
import json
import os
import pickle
import shutil
import subprocess
import tempfile
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.main import app
from app.db.base import Base
from app.db.session import get_db
from app.models.user import User
from app.core.security import hash_password
from app.models.threat_hunting import Alert, DetectionRule, SecurityEvent
from app.services.ml_ops.pickle_scanner import ModelPickleSecurityScanner, pickle_scanner
from app.services.threat_hunting.alert_service import alert_service
from app.services.threat_hunting.detection_engine import detection_engine
from app.services.threat_hunting.event_service import event_service
from app.services.threat_hunting.rule_service import rule_service

# Isolated SQLite in-memory test database
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

def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()

@pytest.fixture(scope="module", autouse=True)
def setup_test_db():
    Base.metadata.create_all(bind=test_engine)
    db = TestingSessionLocal()
    try:
        seed_data = [
            {"username": "admin_user", "password": "AdminPassword123!", "role": "admin"},
            {"username": "ds_user", "password": "DataScientist123!", "role": "data_scientist"},
        ]
        for u in seed_data:
            user = User(
                username=u["username"],
                email=f"{u['username']}@sentinelml.com",
                password_hash=hash_password(u["password"]),
                role=u["role"],
                is_active=True,
            )
            db.add(user)
        db.commit()
        rule_service.seed_default_rules(db)
    finally:
        db.close()

    app.dependency_overrides[get_db] = override_get_db
    yield
    app.dependency_overrides.pop(get_db, None)


@pytest.fixture
def db():
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()


class OsSystemExploit:
    def __reduce__(self):
        return (os.system, ("echo hacked_rce",))


class SubprocessExploit:
    def __reduce__(self):
        return (subprocess.Popen, (["whoami"],))


class EvalExploit:
    def __reduce__(self):
        return (eval, ("__import__('os').system('calc')",))


def test_pickle_scanner_detects_os_system_exploit():
    """Verifies that an exploit using os.system via __reduce__ is detected as CRITICAL."""
    scanner = ModelPickleSecurityScanner(quarantine_dir="logs/test_quarantine")
    payload = pickle.dumps(OsSystemExploit())

    res = scanner.scan_bytes(payload)
    assert res["is_safe"] is False
    assert res["severity"] == "CRITICAL"
    assert res["threat_type"] == "arbitrary_code_execution"
    assert any("system" in v or "os" in v or "nt" in v for v in res["violations"])


def test_pickle_scanner_detects_subprocess_exploit():
    """Verifies that an exploit using subprocess.Popen is detected as CRITICAL."""
    scanner = ModelPickleSecurityScanner(quarantine_dir="logs/test_quarantine")
    payload = pickle.dumps(SubprocessExploit())

    res = scanner.scan_bytes(payload)
    assert res["is_safe"] is False
    assert res["severity"] == "CRITICAL"
    assert any("subprocess" in v for v in res["violations"])


def test_pickle_scanner_detects_eval_exploit():
    """Verifies that an exploit using eval() is detected as CRITICAL."""
    scanner = ModelPickleSecurityScanner(quarantine_dir="logs/test_quarantine")
    payload = pickle.dumps(EvalExploit())

    res = scanner.scan_bytes(payload)
    assert res["is_safe"] is False
    assert res["severity"] == "CRITICAL"
    assert any("eval" in v or "builtins" in v for v in res["violations"])


def test_pickle_scanner_quarantine_workflow(tmp_path):
    """Verifies that malicious model files are safely quarantined with metadata sidecars."""
    q_dir = tmp_path / "quarantine"
    scanner = ModelPickleSecurityScanner(quarantine_dir=str(q_dir))

    mal_file = tmp_path / "exploit.pkl"
    payload = pickle.dumps(OsSystemExploit())
    mal_file.write_bytes(payload)

    scan_res = scanner.scan_file(str(mal_file))
    assert scan_res["is_safe"] is False

    dest = scanner.quarantine_file(
        source_path=str(mal_file),
        filename="exploit.pkl",
        username="adversary",
        scan_result=scan_res,
        client_ip="192.168.1.100",
    )

    assert os.path.exists(dest)
    meta_path = f"{dest}.meta.json"
    assert os.path.exists(meta_path)

    with open(meta_path, "r", encoding="utf-8") as f:
        meta = json.load(f)
    assert meta["original_filename"] == "exploit.pkl"
    assert meta["uploaded_by"] == "adversary"
    assert meta["client_ip"] == "192.168.1.100"
    assert meta["severity"] == "CRITICAL"
    assert meta["threat_type"] == "arbitrary_code_execution"


def test_pickle_rce_threat_hunting_event_and_alert_generation(db):
    """
    Verifies the end-to-end Threat Hunting pipeline:
    Audit action -> SecurityEvent -> Detection Rule -> CRITICAL Alert.
    """
    model_name = f"rogue-model-{uuid.uuid4().hex[:6]}"
    event = event_service.ingest_from_audit(
        action="model_rce_payload_detected",
        username="attacker_user",
        ip_address="10.0.0.50",
        details=f"Pickle Deserialization RCE Gatekeeper intercepted malicious model '{model_name}'.",
        db=db,
    )

    assert event is not None
    assert event.event_type == "model.rce_payload_detected"
    assert event.severity == "CRITICAL"
    assert event.actor_id == "attacker_user"

    # Evaluate rules using detection engine
    detections = detection_engine.evaluate_all_rules(db)
    rce_detections = [
        d for d in detections
        if "Pickle Deserialization RCE" in d.title
    ]
    assert len(rce_detections) > 0, f"Expected a detection for Pickle RCE! Found: {[d.title for d in detections]}"

    # Process detections into alerts
    for d in rce_detections:
        alert = alert_service.process_detection(db, d)
        assert alert.severity == "CRITICAL"
        assert alert.status in ("OPEN", "INVESTIGATING")


def test_api_upload_rejects_malicious_pickle_and_quarantines():
    """
    Verifies that calling POST /models/upload with a malicious pickle payload
    is blocked with 400 Bad Request and never unpickled.
    """
    client = TestClient(app)
    # Login as ds_user to acquire valid JWT token
    login_resp = client.post("/api/auth/login", data={"username": "ds_user", "password": "DataScientist123!"})
    assert login_resp.status_code == 200, f"Login failed: {login_resp.text}"

    token = login_resp.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    payload = pickle.dumps(OsSystemExploit())
    file_tuple = ("malicious_model.pkl", io.BytesIO(payload), "application/octet-stream")

    response = client.post(
        "/api/models/upload",
        files={"file": file_tuple},
        headers=headers,
    )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "Security Gatekeeper Violation" in detail
    assert "quarantined" in detail
