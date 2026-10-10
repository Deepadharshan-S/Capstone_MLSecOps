import uuid
from datetime import datetime, timedelta, timezone
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from sqlalchemy import select

from app.main import app
from app.db.session import SessionLocal
from app.core.logging_config import log_audit_event
from app.models.threat_hunting import (
    SecurityEvent,
    DetectionRule,
    Alert,
    AlertEvent,
    AlertHistory,
    HuntJob,
)
from app.schemas.threat_hunting import (
    SecurityEventCreate,
    SecurityEventFilter,
    DetectionRuleCreate,
    DetectionRuleUpdate,
    AlertUpdate,
    HuntJobCreate,
    HuntDefinition,
    RuleConfigThreshold,
)
from app.services.threat_hunting.event_service import event_service
from app.services.threat_hunting.rule_service import rule_service
from app.services.threat_hunting.detection_engine import detection_engine
from app.services.threat_hunting.alert_service import alert_service
from app.services.threat_hunting.hunt_service import hunt_service
from app.services.threat_hunting.worker import run_detection_sweep_once
from app.services.threat_hunting.normalization import redact_sensitive_data

client = TestClient(app)


@pytest.fixture
def db():
    session: Session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def auth_headers(user_tokens):
    return {
        "admin": {"Authorization": f"Bearer {user_tokens['admin_user']}"},
        "mle": {"Authorization": f"Bearer {user_tokens['mle_user']}"},
        "ds": {"Authorization": f"Bearer {user_tokens['ds_user']}"},
        "viewer": {"Authorization": f"Bearer {user_tokens['viewer_user']}"},
    }


# ===========================================================================
# 1. Normalization & Sensitive Data Redaction
# ===========================================================================

def test_normalization_sensitive_data_redaction():
    raw_attributes = {
        "username": "alice",
        "password": "SuperSecretPassword123!",
        "token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.e30.t-IDcSemACt8x4iTMC6Y5",
        "api_key": "live_sk_1234567890abcdef",
        "safe_detail": "Attempted login to dashboard",
        "nested": {
            "secret_key": "do_not_leak",
            "nested_safe": "ok",
        },
    }

    redacted = redact_sensitive_data(raw_attributes)

    assert redacted["username"] == "alice"
    assert redacted["password"] == "[REDACTED]"
    assert redacted["token"] == "[REDACTED]"
    assert redacted["api_key"] == "[REDACTED]"
    assert redacted["safe_detail"] == "Attempted login to dashboard"
    assert redacted["nested"]["secret_key"] == "[REDACTED]"
    assert redacted["nested"]["nested_safe"] == "ok"


# ===========================================================================
# 2. Event Ingestion, Deduplication & Validation
# ===========================================================================

def test_event_ingestion_and_deduplication(db: Session):
    unique_actor = f"attacker_{uuid.uuid4().hex[:8]}"
    fixed_time = datetime.now(timezone.utc)

    event_in = SecurityEventCreate(
        event_type="auth.login_failed",
        source="auth_service",
        actor_id=unique_actor,
        resource_id="/api/auth/login",
        resource_type="endpoint",
        severity="HIGH",
        occurrence_time=fixed_time,
        attributes={"reason": "bad_password", "token": "sensitive_jwt_token"},
    )

    # Ingest once
    ev1 = event_service.ingest_event(db, event_in)
    assert ev1.id is not None
    assert ev1.actor_id == unique_actor
    assert ev1.attributes["token"] == "[REDACTED]"
    assert ev1.dedup_hash is not None

    # Ingest identical event again -> Must return existing record
    ev2 = event_service.ingest_event(db, event_in)
    assert ev2.id == ev1.id
    assert ev2.dedup_hash == ev1.dedup_hash


# ===========================================================================
# 3. Detection Rule Validation & Versioning
# ===========================================================================

def test_rule_validation_and_versioning(db: Session):
    # Valid rule
    rule_in = DetectionRuleCreate(
        name=f"Custom Rule {uuid.uuid4().hex[:6]}",
        description="Detects anomalous failures",
        rule_type="threshold",
        severity="HIGH",
        mitre_attack_id="T1110",
        config={
            "event_type": "auth.login_failed",
            "threshold": 3,
            "window_seconds": 120,
            "group_by": "actor_id",
            "match_attributes": {},
        },
    )
    rule = rule_service.create_rule(db, rule_in, creator="test_admin")
    assert rule.version == 1
    assert rule.is_enabled is True

    # Invalid rule configs must fail: threshold < 1
    with pytest.raises(Exception):
        RuleConfigThreshold(
            event_type="auth.login_failed",
            threshold=-1,
            window_seconds=120,
            group_by="actor_id",
        )

    # Invalid rule configs must fail: window_seconds < 10
    with pytest.raises(Exception):
        RuleConfigThreshold(
            event_type="auth.login_failed",
            threshold=5,
            window_seconds=5,
            group_by="actor_id",
        )

    # Invalid rule configs must fail: invalid group_by field
    with pytest.raises(Exception):
        RuleConfigThreshold(
            event_type="auth.login_failed",
            threshold=5,
            window_seconds=120,
            group_by="invalid_field",
        )

    # Update config increments version
    updated_rule = rule_service.update_rule(
        db,
        rule.id,
        DetectionRuleUpdate(
            config={
                "event_type": "auth.login_failed",
                "threshold": 10,
                "window_seconds": 120,
                "group_by": "actor_id",
            }
        ),
    )
    assert updated_rule.version == 2
    assert updated_rule.config["threshold"] == 10


# ===========================================================================
# 4. Threshold Detection & Sliding Time Window
# ===========================================================================

def test_threshold_detection_and_time_window(db: Session):
    actor = f"brute_actor_{uuid.uuid4().hex[:8]}"
    test_event_type = f"test.auth_brute_{uuid.uuid4().hex[:6]}"
    now = datetime.now(timezone.utc)

    # Create an isolated test rule: threshold=3, window=300s
    rule_in = DetectionRuleCreate(
        name=f"Brute Force Rule {uuid.uuid4().hex[:6]}",
        description="Test threshold window functionality",
        rule_type="threshold",
        severity="CRITICAL",
        mitre_attack_id="T1110.001",
        config={
            "event_type": test_event_type,
            "threshold": 3,
            "window_seconds": 300,
            "group_by": "actor_id",
            "match_attributes": {},
        },
    )
    rule = rule_service.create_rule(db, rule_in, creator="test_admin")

    # Ingest 2 events with distinct timestamps (below threshold)
    for i in range(2):
        event_service.ingest_event(
            db,
            SecurityEventCreate(
                event_type=test_event_type,
                source="auth_service",
                actor_id=actor,
                occurrence_time=now - timedelta(seconds=20 - 10 * i),
            ),
        )

    # Evaluation should produce NO detection
    detections = detection_engine.evaluate_single_rule(db, rule)
    assert len(detections) == 0

    # Ingest 3rd event with distinct timestamp (hits threshold)
    event_service.ingest_event(
        db,
        SecurityEventCreate(
            event_type=test_event_type,
            source="auth_service",
            actor_id=actor,
            occurrence_time=now,
        ),
    )

    # Evaluation should now detect exactly 1 match with 3 events
    detections = detection_engine.evaluate_single_rule(db, rule)
    assert len(detections) == 1
    det = detections[0]
    assert det.actor_id == actor
    assert det.severity == "CRITICAL"
    assert len(det.event_ids) == 3

    # Events outside window boundary (e.g. 2 hours ago) must NOT be counted
    old_actor = f"old_actor_{uuid.uuid4().hex[:8]}"
    old_event_type = f"test.auth_old_{uuid.uuid4().hex[:6]}"
    rule_old = rule_service.create_rule(
        db,
        DetectionRuleCreate(
            name=f"Old Event Rule {uuid.uuid4().hex[:6]}",
            description="Testing time window exclusion",
            rule_type="threshold",
            severity="MEDIUM",
            config={
                "event_type": old_event_type,
                "threshold": 3,
                "window_seconds": 300,
                "group_by": "actor_id",
            },
        ),
    )
    for i in range(5):
        event_service.ingest_event(
            db,
            SecurityEventCreate(
                event_type=old_event_type,
                source="auth_service",
                actor_id=old_actor,
                occurrence_time=now - timedelta(hours=2, seconds=10 * i),
            ),
        )
    detections_old = detection_engine.evaluate_single_rule(db, rule_old)
    assert len(detections_old) == 0


# ===========================================================================
# 5. Alert Generation, Correlation & Deduplication
# ===========================================================================

def test_alert_generation_and_correlation(db: Session):
    actor = f"correlate_actor_{uuid.uuid4().hex[:8]}"
    test_event_type = f"test.correlate_{uuid.uuid4().hex[:6]}"
    now = datetime.now(timezone.utc)

    rule = rule_service.create_rule(
        db,
        DetectionRuleCreate(
            name=f"Correlation Test Rule {uuid.uuid4().hex[:6]}",
            description="Testing alert correlation across repeated events",
            rule_type="threshold",
            severity="HIGH",
            mitre_attack_id="T1110",
            config={
                "event_type": test_event_type,
                "threshold": 2,
                "window_seconds": 300,
                "group_by": "actor_id",
            },
        ),
        creator="test_admin",
    )

    events = []
    for i in range(2):
        ev = event_service.ingest_event(
            db,
            SecurityEventCreate(
                event_type=test_event_type,
                source="auth_service",
                actor_id=actor,
                occurrence_time=now - timedelta(seconds=30 - 10 * i),
            ),
        )
        events.append(ev)

    detections = detection_engine.evaluate_single_rule(db, rule)
    assert len(detections) == 1

    # Process detections into alert
    created_alerts = alert_service.process_detection_results(db, detections)
    assert len(created_alerts) == 1
    alert = created_alerts[0]
    assert alert.status == "OPEN"
    assert alert.event_count == 2
    assert alert.rule_id == rule.id

    # Verify event links in alert_events
    linked_events = alert_service.get_alert_events(db, alert.id)
    assert len(linked_events) == 2
    assert {e.id for e in linked_events} == {e.id for e in events}

    # Ingest 1 more distinct event in the past and re-evaluate
    ev3 = event_service.ingest_event(
        db,
        SecurityEventCreate(
            event_type=test_event_type,
            source="auth_service",
            actor_id=actor,
            occurrence_time=now - timedelta(seconds=5),
        ),
    )

    detections2 = detection_engine.evaluate_single_rule(db, rule)
    assert len(detections2) == 1

    # Second processing must correlate into existing OPEN alert (NOT create new alert)
    processed_alerts = alert_service.process_detection_results(db, detections2)
    assert len(processed_alerts) == 1
    updated_alert = processed_alerts[0]
    assert updated_alert.id == alert.id
    assert updated_alert.event_count == 3

    linked_events_after = alert_service.get_alert_events(db, alert.id)
    assert len(linked_events_after) == 3
    assert ev3.id in {e.id for e in linked_events_after}


# ===========================================================================
# 6. Alert Lifecycle & Investigation History
# ===========================================================================

def test_alert_lifecycle_and_investigation_history(db: Session):
    rule = rule_service.list_rules(db)[0]

    # Directly create an alert to test lifecycle
    now = datetime.now(timezone.utc)
    alert = Alert(
        title="Test Lifecycle Alert",
        description="Testing state transitions",
        severity="MEDIUM",
        status="OPEN",
        detection_source="rule_engine",
        rule_id=rule.id,
        rule_version=1,
        first_seen=now,
        last_seen=now,
        event_count=1,
        dedup_key=f"test:{uuid.uuid4().hex[:8]}",
    )
    db.add(alert)
    db.commit()
    db.refresh(alert)

    # 1. Transition OPEN -> INVESTIGATING
    alert_service.update_alert(
        db,
        alert.id,
        AlertUpdate(
            status="INVESTIGATING",
            assigned_to="analyst_bob",
            notes="Starting investigation into suspicious behavior.",
        ),
        user="analyst_bob",
    )
    assert alert.status == "INVESTIGATING"

    # 2. Transition INVESTIGATING -> RESOLVED
    alert_service.update_alert(
        db,
        alert.id,
        AlertUpdate(
            status="RESOLVED",
            resolution_notes="User password was expired and has been reset.",
        ),
        user="analyst_bob",
    )
    assert alert.status == "RESOLVED"
    assert alert.resolved_at is not None

    # 3. Invalid Transition RESOLVED -> OPEN must be rejected
    with pytest.raises(ValueError, match="Invalid status transition"):
        alert_service.update_alert(
            db,
            alert.id,
            AlertUpdate(status="OPEN"),
            user="analyst_bob",
        )

    # 4. Verify immutable AlertHistory records
    histories = db.execute(
        select(AlertHistory)
        .where(AlertHistory.alert_id == alert.id)
        .order_by(AlertHistory.created_at.asc())
    ).scalars().all()

    assert len(histories) >= 2
    assert histories[0].previous_status == "OPEN"
    assert histories[0].new_status == "INVESTIGATING"
    assert histories[1].previous_status == "INVESTIGATING"
    assert histories[1].new_status == "RESOLVED"


# ===========================================================================
# 7. Audit Log to Security Event Pipeline
# ===========================================================================

def test_audit_log_to_security_event_pipeline(db: Session):
    test_user = f"audit_victim_{uuid.uuid4().hex[:6]}"

    # Emit audit log through standard backend function
    log_audit_event(
        action="login_failed",
        username=test_user,
        ip_address="192.168.1.50",
        details="Bad credentials provided",
        db=db,
    )

    # Verify a SecurityEvent was generated
    events, total = event_service.list_events(
        db,
        SecurityEventFilter(
            event_type="auth.login_failed",
            actor_id=test_user,
        ),
    )
    assert total >= 1
    ev = events[0]
    assert ev.actor_id == test_user
    assert ev.severity == "HIGH"
    assert ev.source == "auth_service"
    assert ev.attributes["ip_address"] == "192.168.1.50"


# ===========================================================================
# 8. Historical Threat Hunting & Aggregation
# ===========================================================================

def test_historical_threat_hunt(db: Session):
    actor = f"hunt_target_{uuid.uuid4().hex[:6]}"
    test_event_type = f"dataset.branch_deleted_{uuid.uuid4().hex[:6]}"
    now = datetime.now(timezone.utc)

    # Ingest some events for hunting
    for i in range(3):
        event_service.ingest_event(
            db,
            SecurityEventCreate(
                event_type=test_event_type,
                source="lakefs",
                actor_id=actor,
                resource_id="repo-main",
                resource_type="branch",
                severity="HIGH",
                occurrence_time=now - timedelta(minutes=5 * (i + 1)),
                attributes={"branch_name": f"feature-{i}"},
            ),
        )

    # Execute hunt job
    job = hunt_service.create_and_execute_hunt(
        db,
        HuntJobCreate(
            name="Hunt for deleted dataset branches",
            hunt_definition=HuntDefinition(
                event_types=[test_event_type],
                actor_id=actor,
                start_time=now - timedelta(hours=1),
                end_time=now + timedelta(minutes=5),
            ),
        ),
    )

    assert job.status == "COMPLETED"
    assert job.result_count == 3
    assert job.results_summary is not None
    assert actor in job.results_summary["actor_distribution"]
    assert job.results_summary["actor_distribution"][actor] == 3

    # Retrieve results
    hunt_rec, results = hunt_service.get_hunt_results(db, job.id)
    assert len(results) == 3
    assert all(r.actor_id == actor for r in results)


# ===========================================================================
# 9. Background Worker Single Execution
# ===========================================================================

def test_detection_worker_single_cycle():
    # Calling run_detection_sweep_once directly runs one full pass across all rules
    result = run_detection_sweep_once()
    assert "detections_count" in result
    assert "alerts_processed" in result


# ===========================================================================
# 10. REST APIs & RBAC Authorization
# ===========================================================================

def test_threat_hunting_api_endpoints(auth_headers):
    admin_hdr = auth_headers["admin"]
    viewer_hdr = auth_headers["viewer"]

    # 1. Unauthenticated request must return 401
    resp = client.get("/api/alerts")
    assert resp.status_code == 401

    # 2. List Detection Rules (Viewer has ALERTS_VIEW / RULES read scope)
    resp = client.get("/api/detection-rules", headers=viewer_hdr)
    assert resp.status_code == 200
    rules = resp.json()
    assert isinstance(rules, list)
    assert len(rules) >= 1

    # 3. Viewer cannot create detection rules (Requires RULES_MANAGE -> 403)
    resp = client.post(
        "/api/detection-rules",
        headers=viewer_hdr,
        json={
            "name": "Unauthorized Rule",
            "description": "Viewer shouldn't create this rule",
            "rule_type": "threshold",
            "severity": "LOW",
            "config": {
                "event_type": "auth.login_failed",
                "threshold": 10,
                "window_seconds": 60,
                "group_by": "actor_id",
            },
        },
    )
    assert resp.status_code == 403

    # 4. Admin can create detection rule
    unique_rule_name = f"Admin Detection Rule {uuid.uuid4().hex[:6]}"
    resp = client.post(
        "/api/detection-rules",
        headers=admin_hdr,
        json={
            "name": unique_rule_name,
            "description": "Created by Admin via API",
            "rule_type": "threshold",
            "severity": "HIGH",
            "mitre_attack_id": "T1110",
            "config": {
                "event_type": "auth.login_failed",
                "threshold": 5,
                "window_seconds": 300,
                "group_by": "actor_id",
            },
        },
    )
    assert resp.status_code == 201
    new_rule = resp.json()
    assert new_rule["name"] == unique_rule_name
    admin_rule_id = new_rule["id"]

    # 5. Test Rule API (Dry run)
    resp = client.post(
        f"/api/detection-rules/{admin_rule_id}/test",
        headers=admin_hdr,
        json={"window_seconds": 600},
    )
    assert resp.status_code == 200
    test_result = resp.json()
    assert "matches_found" in test_result
    assert "candidate_alerts" in test_result
    assert test_result["evaluation_window_seconds"] == 600

    # 6. List Security Events
    resp = client.get(
        "/api/security-events?limit=10&offset=0",
        headers=admin_hdr,
    )
    assert resp.status_code == 200
    ev_data = resp.json()
    assert "total" in ev_data
    assert "items" in ev_data

    # 7. List Alerts
    resp = client.get("/api/alerts?limit=10", headers=admin_hdr)
    assert resp.status_code == 200
    alerts_data = resp.json()
    assert "total" in alerts_data
    assert "items" in alerts_data

    if alerts_data["items"]:
        first_alert = alerts_data["items"][0]
        alert_id = first_alert["id"]

        # Alert detail
        resp = client.get(f"/api/alerts/{alert_id}", headers=admin_hdr)
        assert resp.status_code == 200
        assert resp.json()["id"] == alert_id

        # Alert events
        resp = client.get(f"/api/alerts/{alert_id}/events", headers=admin_hdr)
        assert resp.status_code == 200
        assert "items" in resp.json()

        # Update Alert (Viewer cannot update -> 403)
        resp = client.patch(
            f"/api/alerts/{alert_id}",
            headers=viewer_hdr,
            json={"status": "INVESTIGATING", "notes": "Not allowed"},
        )
        assert resp.status_code == 403

        # Update Alert (Admin can update -> 200)
        resp = client.patch(
            f"/api/alerts/{alert_id}",
            headers=admin_hdr,
            json={"status": "INVESTIGATING", "notes": "Admin investigating"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "INVESTIGATING"

    # 8. Threat Hunts API (Viewer cannot execute hunts -> 403)
    resp = client.post(
        "/api/hunts",
        headers=viewer_hdr,
        json={
            "name": "Unauthorized Hunt",
            "hunt_definition": {
                "event_types": ["auth.login_failed"],
                "start_time": "2026-10-10T00:00:00Z",
                "end_time": "2026-10-10T23:59:59Z",
            },
        },
    )
    assert resp.status_code == 403

    # Admin executes hunt
    resp = client.post(
        "/api/hunts",
        headers=admin_hdr,
        json={
            "name": f"Admin Hunt {uuid.uuid4().hex[:6]}",
            "hunt_definition": {
                "event_types": ["auth.login_failed"],
                "start_time": "2026-10-10T00:00:00Z",
                "end_time": "2026-10-10T23:59:59Z",
            },
        },
    )
    assert resp.status_code == 201
    hunt_job = resp.json()
    assert hunt_job["status"] == "COMPLETED"
    hunt_id = hunt_job["id"]

    # Get hunt details and results
    resp = client.get(f"/api/hunts/{hunt_id}", headers=admin_hdr)
    assert resp.status_code == 200
    assert resp.json()["id"] == hunt_id

    resp = client.get(f"/api/hunts/{hunt_id}/results", headers=admin_hdr)
    assert resp.status_code == 200
    assert "events" in resp.json()
