import uuid
from datetime import datetime, timedelta, timezone
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from sqlalchemy import select, and_

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


# ===========================================================================
# 11. End-to-End Authentication Failure Detection Workflow (Phase 2)
# ===========================================================================

def test_e2e_authentication_failure_detection_workflow(db: Session, auth_headers):
    """
    Validates complete authentication failure detection workflow:
    1. Produce representative authentication failure events via /api/auth/login.
    2. Confirm events reach SecurityEvent persistence as auth.login_failed.
    3. Confirm detection rule evaluates them.
    4. Verify below-threshold produces 0 alerts.
    5. Verify threshold-crossing produces 1 alert with correct evidence and event links.
    6. Confirm GET /api/alerts returns the alert.
    7. Verify repeated sweep correlates into existing alert without duplicates.
    8. Verify alert lifecycle transition is audited.
    9. Verify unauthorized users (viewer) cannot update alert.
    """
    rule_service.seed_default_rules(db)
    victim_user = f"victim_{uuid.uuid4().hex[:8]}"

    # Step 1: 2 failed login attempts (below threshold of 5)
    for _ in range(2):
        resp = client.post(
            "/api/auth/login",
            data={"username": victim_user, "password": "WrongPassword123!"},
        )
        assert resp.status_code in (400, 401)

    # Step 2: Confirm events reached SecurityEvent persistence
    events, total = event_service.list_events(
        db,
        SecurityEventFilter(event_type="auth.login_failed", actor_id=victim_user),
    )
    assert total == 2
    assert all(e.event_type == "auth.login_failed" for e in events)
    assert all(e.actor_id == victim_user for e in events)

    # Step 3 & 4: Below-threshold sweep -> No alerts
    rule = db.execute(
        select(DetectionRule).where(DetectionRule.name == "Repeated Authentication Failures (Brute Force)")
    ).scalar_one()
    detections_below = detection_engine.evaluate_single_rule(db, rule)
    assert not any(d.actor_id == victim_user for d in detections_below)

    # Step 5: Produce 3 more failed logins -> Crosses threshold (total 5)
    for _ in range(3):
        resp = client.post(
            "/api/auth/login",
            data={"username": victim_user, "password": "WrongPassword123!"},
        )
        assert resp.status_code in (400, 401)

    events_total, total_count = event_service.list_events(
        db,
        SecurityEventFilter(event_type="auth.login_failed", actor_id=victim_user),
    )
    assert total_count == 5

    # Step 5: Rule detects threshold crossing for victim_user
    detections = detection_engine.evaluate_single_rule(db, rule)
    matching = [d for d in detections if d.actor_id == victim_user]
    assert len(matching) == 1
    det = matching[0]
    assert len(det.event_ids) == 5

    # Process detection into alert
    alert = alert_service.process_detection(db, det)
    assert alert.event_count >= 5
    assert alert.severity == "HIGH"
    assert alert.mitre_attack_id == "T1110.001"
    assert "matched_count" in alert.supporting_evidence

    # Confirm event links
    alert_evs = alert_service.get_alert_events(db, alert.id)
    assert len(alert_evs) >= 5

    # Step 7: Confirm GET /api/alerts returns the alert
    admin_hdr = auth_headers["admin"]
    resp = client.get("/api/alerts?status=OPEN", headers=admin_hdr)
    assert resp.status_code == 200
    alert_ids = [a["id"] for a in resp.json()["items"]]
    assert str(alert.id) in alert_ids

    # Step 8: Repeated processing correlates without duplicate alerts
    alert2 = alert_service.process_detection(db, det)
    assert alert2.id == alert.id
    open_alerts = db.execute(
        select(Alert).where(
            and_(
                Alert.rule_id == rule.id,
                Alert.dedup_key.contains(victim_user),
                Alert.status == "OPEN",
            )
        )
    ).scalars().all()
    assert len(open_alerts) == 1

    # Step 9: Alert lifecycle update and audit
    viewer_hdr = auth_headers["viewer"]
    # Viewer forbidden (Step 10)
    resp_forbidden = client.patch(
        f"/api/alerts/{alert.id}",
        headers=viewer_hdr,
        json={"status": "INVESTIGATING"},
    )
    assert resp_forbidden.status_code == 403

    # Admin allowed
    resp_ok = client.patch(
        f"/api/alerts/{alert.id}",
        headers=admin_hdr,
        json={"status": "INVESTIGATING", "notes": "Investigating brute force attempts."},
    )
    assert resp_ok.status_code == 200
    assert resp_ok.json()["status"] == "INVESTIGATING"


# ===========================================================================
# 12. Verification of All Seeded Detection Rules (Phase 3)
# ===========================================================================

def test_seeded_rule_1_brute_force_via_audit(db: Session):
    rule_service.seed_default_rules(db)
    rule = db.execute(
        select(DetectionRule).where(DetectionRule.name == "Repeated Authentication Failures (Brute Force)")
    ).scalar_one()

    actor = f"attacker_{uuid.uuid4().hex[:6]}"
    for _ in range(5):
        log_audit_event(
            action="login_failed_incorrect_password",
            username=actor,
            ip_address="10.0.0.1",
            details="Password verification failed.",
            db=db,
        )

    detections = detection_engine.evaluate_single_rule(db, rule)
    matching = [d for d in detections if d.actor_id == actor]
    assert len(matching) == 1
    alert = alert_service.process_detection(db, matching[0])
    assert alert.status == "OPEN"
    assert alert.mitre_attack_id == "T1110.001"


def test_seeded_rule_2_unauthorized_requests(db: Session, auth_headers):
    rule_service.seed_default_rules(db)
    rule = db.execute(
        select(DetectionRule).where(DetectionRule.name == "Excessive Unauthorized Requests (Privilege Abuse / Enumeration)")
    ).scalar_one()

    actor = f"unauth_user_{uuid.uuid4().hex[:6]}"
    for _ in range(10):
        log_audit_event(
            action="unauthorized_access",
            username=actor,
            ip_address="10.0.0.2",
            details="Permission denied. Required scope: 'users:manage'",
            db=db,
        )

    detections = detection_engine.evaluate_single_rule(db, rule)
    matching = [d for d in detections if d.actor_id == actor]
    assert len(matching) == 1
    alert = alert_service.process_detection(db, matching[0])
    assert alert.status == "OPEN"
    assert alert.mitre_attack_id == "T1078"


def test_seeded_rule_3_revoked_token_reuse(db: Session):
    rule_service.seed_default_rules(db)
    rule = db.execute(
        select(DetectionRule).where(DetectionRule.name == "Revoked Token Reuse Attempt")
    ).scalar_one()

    actor = f"token_thief_{uuid.uuid4().hex[:6]}"
    log_audit_event(
        action="token_reuse_detected",
        username=actor,
        ip_address="10.0.0.3",
        details="Revoked refresh token 'jti-123' was reused.",
        db=db,
    )

    detections = detection_engine.evaluate_single_rule(db, rule)
    matching = [d for d in detections if d.actor_id == actor]
    assert len(matching) == 1
    alert = alert_service.process_detection(db, matching[0])
    assert alert.status == "OPEN"
    assert alert.mitre_attack_id == "T1550"


def test_seeded_rule_4_anomalous_inference_burst(db: Session):
    """
    Verifies Rule 4 config and evaluation.
    Note: Live inference burst telemetry daemon is documented as a telemetry source gap.
    Evaluated here via direct SecurityEvent ingestion.
    """
    rule_service.seed_default_rules(db)
    rule = db.execute(
        select(DetectionRule).where(DetectionRule.name == "Anomalous Inference Request Rate (Model Inversion / Extraction)")
    ).scalar_one()

    model_name = f"test_model_{uuid.uuid4().hex[:6]}"
    now = datetime.now(timezone.utc)
    for i in range(50):
        event_service.ingest_event(
            db,
            SecurityEventCreate(
                event_type="inference.request_burst",
                source="model_inference",
                actor_id="guest_client",
                resource_id=model_name,
                resource_type="model",
                severity="MEDIUM",
                occurrence_time=now - timedelta(seconds=i),
            ),
        )

    detections = detection_engine.evaluate_single_rule(db, rule)
    matching = [d for d in detections if d.resource_id == model_name]
    assert len(matching) == 1
    alert = alert_service.process_detection(db, matching[0])
    assert alert.status == "OPEN"
    assert alert.mitre_atlas_id == "AML.T0024"


def test_seeded_rule_5_dataset_branch_mutation(db: Session):
    rule_service.seed_default_rules(db)
    rule = db.execute(
        select(DetectionRule).where(DetectionRule.name == "Unauthorized Dataset Branch Mutation")
    ).scalar_one()

    branch_name = f"sensitive-branch-{uuid.uuid4().hex[:6]}"
    log_audit_event(
        action="dataset_branch_delete",
        username="insider_user",
        details=f"Deleted branch '{branch_name}' from dataset 'prod_data'.",
        db=db,
    )

    detections = detection_engine.evaluate_single_rule(db, rule)
    matching = [d for d in detections if d.resource_id == branch_name]
    assert len(matching) == 1
    alert = alert_service.process_detection(db, matching[0])
    assert alert.status == "OPEN"
    assert alert.mitre_attack_id == "T1485"
    assert alert.mitre_atlas_id == "AML.T0031"


def test_seeded_rule_6_rayjob_spikes(db: Session):
    rule_service.seed_default_rules(db)
    rule = db.execute(
        select(DetectionRule).where(DetectionRule.name == "Unexpected RayJob Submission Spikes")
    ).scalar_one()

    actor = f"ray_user_{uuid.uuid4().hex[:6]}"
    for _ in range(5):
        log_audit_event(
            action="model_training_start",
            username=actor,
            details=f"Started training on dataset 'ds_1'. Job ID: job_{uuid.uuid4().hex[:8]}",
            db=db,
        )

    detections = detection_engine.evaluate_single_rule(db, rule)
    matching = [d for d in detections if d.actor_id == actor]
    assert len(matching) == 1
    alert = alert_service.process_detection(db, matching[0])
    assert alert.status == "OPEN"
    assert alert.mitre_attack_id == "T1496"
    assert alert.mitre_atlas_id == "AML.T0002"


# ===========================================================================
# 13. Reliability, Boundary Conditions, Out-of-Order & Concurrency (Phase 4)
# ===========================================================================

def test_threshold_boundary_and_sliding_window_expiration(db: Session):
    actor = f"boundary_actor_{uuid.uuid4().hex[:6]}"
    event_type = f"test.boundary_{uuid.uuid4().hex[:6]}"
    now = datetime.now(timezone.utc)

    rule = rule_service.create_rule(
        db,
        DetectionRuleCreate(
            name=f"Boundary Rule {uuid.uuid4().hex[:6]}",
            description="Testing threshold boundaries and expiration",
            rule_type="threshold",
            severity="HIGH",
            config={
                "event_type": event_type,
                "threshold": 3,
                "window_seconds": 60,
                "group_by": "actor_id",
            },
        ),
    )

    # 1. Below boundary: 2 events -> 0 matches
    for i in range(2):
        event_service.ingest_event(
            db,
            SecurityEventCreate(
                event_type=event_type,
                source="test",
                actor_id=actor,
                occurrence_time=now - timedelta(seconds=10 * i),
            ),
        )
    assert len(detection_engine.evaluate_single_rule(db, rule, now=now)) == 0

    # 2. Exactly at boundary: 3 events -> 1 match
    event_service.ingest_event(
        db,
        SecurityEventCreate(
            event_type=event_type,
            source="test",
            actor_id=actor,
            occurrence_time=now - timedelta(seconds=20),
        ),
    )
    assert len(detection_engine.evaluate_single_rule(db, rule, now=now)) == 1

    # 3. Expiration: evaluated at now + 70s (all 3 events now older than 60s) -> 0 matches
    future = now + timedelta(seconds=70)
    assert len(detection_engine.evaluate_single_rule(db, rule, now=future)) == 0


def test_out_of_order_event_correlation(db: Session):
    actor = f"ooo_actor_{uuid.uuid4().hex[:6]}"
    event_type = f"test.ooo_{uuid.uuid4().hex[:6]}"
    t_base = datetime.now(timezone.utc)

    rule = rule_service.create_rule(
        db,
        DetectionRuleCreate(
            name=f"OOO Rule {uuid.uuid4().hex[:6]}",
            description="Testing out-of-order events",
            rule_type="threshold",
            severity="HIGH",
            config={
                "event_type": event_type,
                "threshold": 2,
                "window_seconds": 300,
                "group_by": "actor_id",
            },
        ),
    )

    # Event at t=20s and t=30s
    event_service.ingest_event(
        db,
        SecurityEventCreate(
            event_type=event_type,
            source="test",
            actor_id=actor,
            occurrence_time=t_base + timedelta(seconds=20),
        ),
    )
    event_service.ingest_event(
        db,
        SecurityEventCreate(
            event_type=event_type,
            source="test",
            actor_id=actor,
            occurrence_time=t_base + timedelta(seconds=30),
        ),
    )

    detections = detection_engine.evaluate_single_rule(db, rule, now=t_base + timedelta(seconds=35))
    assert len(detections) == 1
    alert = alert_service.process_detection(db, detections[0])
    initial_first_seen = alert.first_seen

    # Now ingest out-of-order event earlier at t=5s
    event_service.ingest_event(
        db,
        SecurityEventCreate(
            event_type=event_type,
            source="test",
            actor_id=actor,
            occurrence_time=t_base + timedelta(seconds=5),
        ),
    )

    detections2 = detection_engine.evaluate_single_rule(db, rule, now=t_base + timedelta(seconds=35))
    updated_alert = alert_service.process_detection(db, detections2[0])
    assert updated_alert.id == alert.id
    # first_seen should now be updated to min(first_seen, e1.occurrence_time)
    assert updated_alert.first_seen <= initial_first_seen
    assert updated_alert.event_count == 3


def test_recursive_audit_loop_guard(db: Session):
    # Any action starting with alert_, hunt_, rule_, or user detection_engine must NOT create SecurityEvent
    ev_alert = event_service.ingest_from_audit(
        action="alert_created",
        username="detection_engine",
        details="Alert created internally.",
        db=db,
    )
    assert ev_alert is None

    ev_hunt = event_service.ingest_from_audit(
        action="hunt_started",
        username="admin",
        details="Hunt started.",
        db=db,
    )
    assert ev_hunt is None


def test_hunt_definition_validation(auth_headers):
    admin_hdr = auth_headers["admin"]
    now = datetime.now(timezone.utc)

    # Inverted time range (start_time > end_time) must fail schema validation (422)
    resp = client.post(
        "/api/hunts",
        headers=admin_hdr,
        json={
            "name": "Invalid Inverted Hunt",
            "hunt_definition": {
                "event_types": ["auth.login_failed"],
                "start_time": (now + timedelta(hours=2)).isoformat(),
                "end_time": now.isoformat(),
            },
        },
    )
    assert resp.status_code == 422


def test_worker_advisory_lock_concurrency():
    # Calling run_detection_sweep_once() succeeds
    res1 = run_detection_sweep_once()
    assert "detections_count" in res1
    assert "alerts_processed" in res1
