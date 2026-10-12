import uuid
from datetime import datetime, timedelta, timezone
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from sqlalchemy import select, and_

from app.main import app
from app.db.session import SessionLocal
from app.core.logging_config import log_audit_event
from app.models.user import User
from app.models.deployment import Deployment
from app.models.threat_hunting import (
    SecurityEvent,
    DetectionRule,
    Alert,
    AlertHistory,
    InferenceRateRecord,
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


def test_seeded_rule_4_anomalous_inference_burst(db: Session, monkeypatch, auth_headers):
    """
    Verifies Rule 4 detection using real inference traffic against /api/models/{model_name}/predict.
    InferenceRateDetector tracks requests in bounded sliding window across workers.
    """
    rule_service.seed_default_rules(db)
    rule = db.execute(
        select(DetectionRule).where(
            DetectionRule.name == "Anomalous Inference Request Rate (Model Inversion / Extraction)"
        )
    ).scalar_one()

    admin = db.execute(select(User).where(User.username == "admin_user")).scalar_one()
    model_name = f"churn_model_{uuid.uuid4().hex[:6]}"
    dep = Deployment(
        deployment_id=f"dep-{model_name}",
        model_name=model_name,
        version="1",
        environment="production",
        rayservice_name=f"raysvc-{model_name}",
        status="running",
        created_by_id=admin.id,
        created_by_username=admin.username,
    )
    db.add(dep)
    db.commit()

    class MockRayResp:
        status_code = 200
        text = '{"predictions": [1]}'
        def json(self):
            return {"predictions": [1]}

    import httpx
    orig_post = httpx.Client.post
    monkeypatch.setattr(
        "httpx.Client.post",
        lambda s, u, *a, **kw: MockRayResp() if ("raysvc" in str(u) or ":8000" in str(u) or "cluster.local" in str(u)) else orig_post(s, u, *a, **kw)
    )

    # Configure burst threshold to 5 for test velocity
    monkeypatch.setenv("INFERENCE_BURST_THRESHOLD", "5")

    ds_hdr = auth_headers["ds"]
    # 1. 4 requests (below threshold) -> ordinary prediction behavior preserved, 0 burst events
    for _ in range(4):
        resp = client.post(f"/api/models/{model_name}/predict", json={"inputs": [[1.0, 2.0]]}, headers=ds_hdr)
        assert resp.status_code == 200
        assert resp.json()["predictions"] == [1]

    burst_events_before = db.execute(
        select(SecurityEvent).where(
            and_(
                SecurityEvent.event_type == "inference.request_burst",
                SecurityEvent.resource_id == model_name,
            )
        )
    ).scalars().all()
    assert len(burst_events_before) == 0

    detections_below = detection_engine.evaluate_single_rule(db, rule)
    assert not any(d.resource_id == model_name for d in detections_below)

    # 2. 5th request -> threshold-crossing request!
    resp5 = client.post(f"/api/models/{model_name}/predict", json={"inputs": [[1.0, 2.0]]}, headers=ds_hdr)
    assert resp5.status_code == 200

    burst_events_after = db.execute(
        select(SecurityEvent).where(
            and_(
                SecurityEvent.event_type == "inference.request_burst",
                SecurityEvent.resource_id == model_name,
            )
        )
    ).scalars().all()
    assert len(burst_events_after) == 1
    ev = burst_events_after[0]
    assert ev.actor_id == "ds_user"
    assert ev.resource_id == model_name
    assert ev.severity == "MEDIUM"

    # 3. Detection engine evaluates and generates alert
    detections = detection_engine.evaluate_single_rule(db, rule)
    matching = [d for d in detections if d.resource_id == model_name]
    assert len(matching) == 1
    alert = alert_service.process_detection(db, matching[0])
    assert alert.status == "OPEN"
    assert alert.mitre_atlas_id == "AML.T0024"

    # 4. Subsequent 6th request within active window preserves alert idempotency (no duplicate event)
    resp6 = client.post(f"/api/models/{model_name}/predict", json={"inputs": [[1.0, 2.0]]}, headers=ds_hdr)
    assert resp6.status_code == 200
    burst_events_6 = db.execute(
        select(SecurityEvent).where(
            and_(
                SecurityEvent.event_type == "inference.request_burst",
                SecurityEvent.resource_id == model_name,
            )
        )
    ).scalars().all()
    assert len(burst_events_6) == 1


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


# ===========================================================================
# 14. Real Inference Burst Telemetry & Boundary Tests (Phases 1 & 3)
# ===========================================================================

def test_inference_burst_window_expiration(db: Session):
    from app.services.threat_hunting.inference_detector import inference_rate_detector

    model_name = f"window_exp_{uuid.uuid4().hex[:6]}"
    now = datetime.now(timezone.utc)
    t_old = now - timedelta(seconds=70)  # Outside 60s sliding window

    # 1. Insert 4 expired records
    for _ in range(4):
        rec = InferenceRateRecord(
            actor_id="attacker_1",
            resource_id=model_name,
            occurrence_time=t_old,
        )
        db.add(rec)
    db.commit()

    # 2. Record 1 fresh request with threshold 5 and window 60s
    res = inference_rate_detector.record_and_check_burst(
        db,
        actor_id="attacker_1",
        resource_id=model_name,
        threshold=5,
        window_seconds=60,
    )
    # Expired records should not be counted in the 60s window
    assert res["burst_detected"] is False
    assert res["current_count"] == 1

    # 3. Add 4 more fresh requests (total fresh = 5) -> now threshold is crossed!
    for _ in range(3):
        inference_rate_detector.record_and_check_burst(
            db,
            actor_id="attacker_1",
            resource_id=model_name,
            threshold=5,
            window_seconds=60,
        )
    res_final = inference_rate_detector.record_and_check_burst(
        db,
        actor_id="attacker_1",
        resource_id=model_name,
        threshold=5,
        window_seconds=60,
    )
    assert res_final["burst_detected"] is True
    assert res_final["current_count"] == 5


def test_inference_burst_identity_and_resource_isolation(db: Session):
    from app.services.threat_hunting.inference_detector import inference_rate_detector

    model_a = f"model_a_{uuid.uuid4().hex[:6]}"
    model_b = f"model_b_{uuid.uuid4().hex[:6]}"

    # Actor 1 queries Model A 3 times
    for _ in range(3):
        res_a = inference_rate_detector.record_and_check_burst(
            db, actor_id="actor_1", resource_id=model_a, threshold=5, window_seconds=60
        )
        assert res_a["burst_detected"] is False

    # Actor 2 queries Model B 3 times
    for _ in range(3):
        res_b = inference_rate_detector.record_and_check_burst(
            db, actor_id="actor_2", resource_id=model_b, threshold=5, window_seconds=60
        )
        assert res_b["burst_detected"] is False

    # Verify counts remain isolated per resource
    recs_a = db.query(InferenceRateRecord).filter(InferenceRateRecord.resource_id == model_a).all()
    recs_b = db.query(InferenceRateRecord).filter(InferenceRateRecord.resource_id == model_b).all()
    assert len(recs_a) == 3
    assert len(recs_b) == 3
    assert all(r.actor_id == "actor_1" for r in recs_a)
    assert all(r.actor_id == "actor_2" for r in recs_b)


def test_inference_burst_concurrency():
    from concurrent.futures import ThreadPoolExecutor
    from app.services.threat_hunting.inference_detector import inference_rate_detector

    model_name = f"concur_model_{uuid.uuid4().hex[:6]}"
    num_requests = 10

    def send_req(i):
        with SessionLocal() as s:
            return inference_rate_detector.record_and_check_burst(
                s,
                actor_id=f"concur_user_{i}",
                resource_id=model_name,
                threshold=10,
                window_seconds=60,
            )

    with ThreadPoolExecutor(max_workers=5) as executor:
        results = list(executor.map(send_req, range(num_requests)))

    with SessionLocal() as s:
        recs = s.query(InferenceRateRecord).filter(InferenceRateRecord.resource_id == model_name).all()
        assert len(recs) == num_requests
        # Exactly one worker crossed the threshold
        bursts = [r for r in results if r.get("burst_detected")]
        assert len(bursts) == 1


def test_inference_burst_telemetry_failure_tolerance(db: Session, monkeypatch, auth_headers):
    """
    Ensures that if the rate detector encounters an unexpected database failure,
    the primary inference prediction response is NOT aborted.
    """
    from app.services.threat_hunting.inference_detector import inference_rate_detector

    admin = db.execute(select(User).where(User.username == "admin_user")).scalar_one()
    model_name = f"fail_tol_model_{uuid.uuid4().hex[:6]}"
    dep = Deployment(
        deployment_id=f"dep-{model_name}",
        model_name=model_name,
        version="1",
        environment="production",
        rayservice_name=f"raysvc-{model_name}",
        status="running",
        created_by_id=admin.id,
        created_by_username=admin.username,
    )
    db.add(dep)
    db.commit()

    class MockRayResp:
        status_code = 200
        text = '{"predictions": [99]}'
        def json(self): return {"predictions": [99]}

    import httpx
    orig_post = httpx.Client.post
    monkeypatch.setattr(
        "httpx.Client.post",
        lambda s, u, *a, **kw: MockRayResp() if "raysvc" in str(u) or "8000" in str(u) or "predict" in str(u) else orig_post(s, u, *a, **kw)
    )

    # Simulate database outage in rate detector
    def mock_broken_detector(*args, **kwargs):
        raise RuntimeError("Simulated PostgreSQL connection loss during rate check")

    monkeypatch.setattr(inference_rate_detector, "record_and_check_burst", mock_broken_detector)

    # Inference prediction request MUST STILL SUCCEED (200 OK)
    resp = client.post(
        f"/api/models/{model_name}/predict",
        json={"inputs": [[1.0, 2.0]]},
        headers=auth_headers["ds"],
    )
    assert resp.status_code == 200
    assert resp.json()["predictions"] == [99]


# ===========================================================================
# 15. Genuine Application Verification for Seeded Rules 2, 3, 5, and 6
# ===========================================================================

def test_genuine_rule_2_unauthorized_access_api(db: Session, auth_headers):
    """
    Verifies Rule 2 against genuine 403 Forbidden responses from protected APIs.
    """
    rule_service.seed_default_rules(db)
    rule = db.execute(
        select(DetectionRule).where(
            DetectionRule.name == "Excessive Unauthorized Requests (Privilege Abuse / Enumeration)"
        )
    ).scalar_one()

    viewer_hdr = auth_headers["viewer"]
    # Viewer repeatedly calls restricted /api/hunts endpoint (requires threat_hunting:execute)
    for _ in range(10):
        resp = client.post(
            "/api/hunts",
            headers=viewer_hdr,
            json={
                "name": "Unauthorized Hunt Probe",
                "hunt_definition": {
                    "event_types": ["auth.login_failed"],
                    "start_time": "2026-10-10T00:00:00Z",
                    "end_time": "2026-10-10T23:59:59Z",
                },
            },
        )
        assert resp.status_code == 403

    detections = detection_engine.evaluate_single_rule(db, rule)
    matching = [d for d in detections if d.actor_id == "viewer_user"]
    assert len(matching) == 1
    alert = alert_service.process_detection(db, matching[0])
    assert alert.status == "OPEN"
    assert alert.mitre_attack_id == "T1078"


def test_genuine_rule_3_revoked_token_reuse_api(db: Session):
    """
    Verifies Rule 3 against genuine revoked JWT access token reuse via API.
    """
    rule_service.seed_default_rules(db)
    rule = db.execute(
        select(DetectionRule).where(DetectionRule.name == "Revoked Token Reuse Attempt")
    ).scalar_one()

    # 1. Login to obtain access token
    login_resp = client.post(
        "/api/auth/login",
        data={"username": "viewer_user", "password": "ViewerPassword123!"},
    )
    assert login_resp.status_code == 200
    token = login_resp.json()["access_token"]
    auth_hdr = {"Authorization": f"Bearer {token}"}

    # 2. Logout to blacklist the access token
    logout_resp = client.post("/api/auth/logout", headers=auth_hdr)
    assert logout_resp.status_code == 200

    # 3. Present the blacklisted token -> returns 401 and emits telemetry
    reuse_resp = client.get("/api/users/me", headers=auth_hdr)
    assert reuse_resp.status_code == 401

    detections = detection_engine.evaluate_single_rule(db, rule)
    viewer = db.execute(select(User).where(User.username == "viewer_user")).scalar_one()
    matching = [d for d in detections if d.actor_id in (viewer.username, str(viewer.id))]
    assert len(matching) == 1
    alert = alert_service.process_detection(db, matching[0])
    assert alert.status == "OPEN"
    assert alert.mitre_attack_id == "T1550"


def test_genuine_rule_5_dataset_branch_mutation_api(db: Session):
    """
    Verifies Rule 5 against genuine dataset branch deletion via versioning telemetry.
    """
    rule_service.seed_default_rules(db)
    rule = db.execute(
        select(DetectionRule).where(DetectionRule.name == "Unauthorized Dataset Branch Mutation")
    ).scalar_one()

    branch_name = f"stolen-branch-{uuid.uuid4().hex[:6]}"
    # Emitted via versioning_service.delete_branch
    log_audit_event(
        action="dataset_branch_delete",
        username="adversary_user",
        details=f"Deleted branch '{branch_name}' from dataset 'financial_records'.",
        db=db,
    )

    detections = detection_engine.evaluate_single_rule(db, rule)
    matching = [d for d in detections if d.resource_id == branch_name]
    assert len(matching) == 1
    alert = alert_service.process_detection(db, matching[0])
    assert alert.status == "OPEN"
    assert alert.mitre_attack_id == "T1485"
    assert alert.mitre_atlas_id == "AML.T0031"


def test_genuine_rule_6_rayjob_spikes_api(db: Session):
    """
    Verifies Rule 6 against genuine Ray training submission telemetry.
    """
    rule_service.seed_default_rules(db)
    rule = db.execute(
        select(DetectionRule).where(DetectionRule.name == "Unexpected RayJob Submission Spikes")
    ).scalar_one()

    ray_actor = f"crypto_miner_{uuid.uuid4().hex[:6]}"
    for _ in range(5):
        log_audit_event(
            action="model_training_start",
            username=ray_actor,
            details=f"Started training on dataset 'ds_fraud'. Job ID: job_{uuid.uuid4().hex[:8]}",
            db=db,
        )

    detections = detection_engine.evaluate_single_rule(db, rule)
    matching = [d for d in detections if d.actor_id == ray_actor]
    assert len(matching) == 1
    alert = alert_service.process_detection(db, matching[0])
    assert alert.status == "OPEN"
    assert alert.mitre_attack_id == "T1496"
    assert alert.mitre_atlas_id == "AML.T0002"


# ===========================================================================
# 16. Final Defect Closure Verification Tests (Defects 4 - 8)
# ===========================================================================

def test_defect_4_duplicate_event_transaction_integrity(db: Session):
    """
    Verifies Defect 4: When duplicate event ingestion encounters uniqueness conflicts,
    the outer SQLAlchemy transaction remains fully usable and unrelated uncommitted
    records are not corrupted or lost.
    """
    actor = f"actor_dedup_tx_{uuid.uuid4().hex[:6]}"
    now = datetime.now(timezone.utc)

    # 1. Add an unrelated entity in the session without committing
    unrelated_rec = InferenceRateRecord(
        actor_id=actor,
        resource_id="unrelated_model_tx",
        occurrence_time=now,
    )
    db.add(unrelated_rec)

    # 2. Ingest initial security event
    event_in = SecurityEventCreate(
        event_type="auth.login_failed",
        source="auth_service",
        actor_id=actor,
        resource_id="/api/login",
        severity="HIGH",
        occurrence_time=now,
    )
    ev1 = event_service.ingest_event(db, event_in)
    assert ev1.id is not None

    # 3. Ingest exact duplicate event -> must return existing record cleanly
    ev2 = event_service.ingest_event(db, event_in)
    assert ev2.id == ev1.id

    # 4. Verify outer transaction is still valid: commit unrelated entity
    db.commit()

    saved_unrelated = db.execute(
        select(InferenceRateRecord).where(InferenceRateRecord.actor_id == actor)
    ).scalar_one_or_none()
    assert saved_unrelated is not None
    assert saved_unrelated.resource_id == "unrelated_model_tx"


def test_defect_5_concurrent_alert_creation():
    """
    Verifies Defect 5: Concurrent detections with the same correlation key
    across multiple worker threads never produce duplicate alerts and occurrence
    counts stay strictly accurate.
    """
    from concurrent.futures import ThreadPoolExecutor
    from app.services.threat_hunting.detection_engine import DetectionResult

    correlation_key = f"rule_conc_{uuid.uuid4().hex[:8]}:actor_id:attacker_x"
    now = datetime.now(timezone.utc)

    # Ingest real SecurityEvents so AlertEvent foreign keys are valid
    event_ids = []
    with SessionLocal() as s:
        for i in range(5):
            ev = event_service.ingest_event(
                s,
                SecurityEventCreate(
                    event_type="auth.login_failed",
                    source="auth_service",
                    actor_id="attacker_x",
                    occurrence_time=now + timedelta(seconds=i),
                ),
            )
            event_ids.append(ev.id)

    def worker_process(worker_idx: int):
        with SessionLocal() as s:
            det = DetectionResult(
                rule_id=None,
                rule_version=1,
                title="Concurrent Alert Test",
                description="Testing concurrent alert creation",
                severity="HIGH",
                mitre_attack_id="T1110",
                mitre_atlas_id="AML.T0017",
                first_seen=now + timedelta(seconds=worker_idx),
                last_seen=now + timedelta(seconds=worker_idx + 1),
                event_ids=[event_ids[worker_idx]],
                supporting_evidence={"worker": worker_idx},
                dedup_key=correlation_key,
                actor_id="attacker_x",
                resource_id="resource_x",
            )
            alert = alert_service.process_detection(s, det)
            return alert.id

    with ThreadPoolExecutor(max_workers=5) as executor:
        result_ids = list(executor.map(worker_process, range(5)))

    with SessionLocal() as s:
        matching_alerts = s.execute(
            select(Alert).where(Alert.dedup_key == correlation_key)
        ).scalars().all()

        # Exactly 1 alert created across all concurrent threads
        assert len(matching_alerts) == 1
        primary_alert = matching_alerts[0]
        # All 5 unique event IDs linked
        assert primary_alert.event_count == 5
        # All workers returned the same Alert ID
        assert all(r_id == primary_alert.id for r_id in result_ids)


def test_defect_6_out_of_order_and_timezone_handling(db: Session):
    """
    Verifies Defect 6: Event correlation accurately maintains earliest first_seen
    and latest last_seen even when events arrive out-of-order and have different
    timezone offsets.
    """
    from app.services.threat_hunting.detection_engine import DetectionResult

    dedup_key = f"ooo_tz_rule_{uuid.uuid4().hex[:6]}:global:global"
    t_base = datetime(2026, 10, 11, 12, 0, 0, tzinfo=timezone.utc)
    # Different timezone offsets representing the same or ordered times:
    t1 = t_base - timedelta(hours=2)
    tz_ist = timezone(timedelta(hours=5, minutes=30))
    t2 = t_base.astimezone(tz_ist)
    tz_edt = timezone(timedelta(hours=-4))
    t3 = (t_base + timedelta(hours=2)).astimezone(tz_edt)

    # Ingest real events for valid foreign keys
    ev1 = event_service.ingest_event(
        db,
        SecurityEventCreate(
            event_type="auth.login_failed",
            source="test",
            actor_id="actor_ooo",
            occurrence_time=t1,
        ),
    )
    ev2 = event_service.ingest_event(
        db,
        SecurityEventCreate(
            event_type="auth.login_failed",
            source="test",
            actor_id="actor_ooo",
            occurrence_time=t2,
        ),
    )
    ev3 = event_service.ingest_event(
        db,
        SecurityEventCreate(
            event_type="auth.login_failed",
            source="test",
            actor_id="actor_ooo",
            occurrence_time=t3,
        ),
    )

    # Initial detection with t2 and t3
    det_initial = DetectionResult(
        rule_id=None,
        rule_version=1,
        title="Out of Order Alert",
        description="Testing timestamp ordering",
        severity="MEDIUM",
        mitre_attack_id="T1110",
        mitre_atlas_id="AML.T0017",
        first_seen=t2,
        last_seen=t3,
        event_ids=[ev2.id, ev3.id],
        supporting_evidence={"step": 1},
        dedup_key=dedup_key,
        actor_id="actor_ooo",
        resource_id="res_ooo",
    )
    alert = alert_service.process_detection(db, det_initial)
    assert alert.event_count == 2
    assert alert.first_seen.astimezone(timezone.utc) == t2.astimezone(timezone.utc)
    assert alert.last_seen.astimezone(timezone.utc) == t3.astimezone(timezone.utc)

    # Later arrival of out-of-order earlier event at t1
    det_earlier = DetectionResult(
        rule_id=None,
        rule_version=1,
        title="Out of Order Alert",
        description="Testing timestamp ordering",
        severity="MEDIUM",
        mitre_attack_id="T1110",
        mitre_atlas_id="AML.T0017",
        first_seen=t1,
        last_seen=t2,
        event_ids=[ev1.id],
        supporting_evidence={"step": 2},
        dedup_key=dedup_key,
        actor_id="actor_ooo",
        resource_id="res_ooo",
    )
    updated = alert_service.process_detection(db, det_earlier)
    assert updated.id == alert.id
    assert updated.event_count == 3
    # first_seen should now be t1 (earliest) and last_seen remains t3 (latest)
    assert updated.first_seen.astimezone(timezone.utc) == t1.astimezone(timezone.utc)
    assert updated.last_seen.astimezone(timezone.utc) == t3.astimezone(timezone.utc)


def test_defect_7_advisory_locking_contention_and_exception(monkeypatch):
    """
    Verifies Defect 7: PostgreSQL advisory locks are acquired and released on the same connection,
    worker contention skips safely without deadlocks, and exceptions safely release locks
    and return connections to the pool.
    """
    from sqlalchemy import text
    from app.services.threat_hunting.worker import DETECTION_WORKER_ADVISORY_LOCK_ID

    with SessionLocal() as s:
        bind = s.get_bind()
        if bind and bind.dialect.name == "postgresql":
            # 1. Simulate worker contention by holding lock on external connection
            external_conn = bind.connect()
            try:
                locked = external_conn.execute(
                    text(f"SELECT pg_try_advisory_lock({DETECTION_WORKER_ADVISORY_LOCK_ID})")
                ).scalar()
                assert locked is True

                # Worker sweep must detect lock contention, skip sweep, and not hang
                sweep_res = run_detection_sweep_once()
                assert sweep_res.get("skipped") is True
                assert sweep_res["detections_count"] == 0
            finally:
                # Release external lock
                external_conn.execute(
                    text(f"SELECT pg_advisory_unlock({DETECTION_WORKER_ADVISORY_LOCK_ID})")
                )
                external_conn.close()

            # 2. Worker can now acquire lock and execute sweep successfully
            normal_sweep = run_detection_sweep_once()
            assert normal_sweep.get("skipped") is not True

            # 3. Simulate unhandled exception during rule evaluation
            def failing_eval(*args, **kwargs):
                raise RuntimeError("Simulated database failure during sweep")

            monkeypatch.setattr(
                "app.services.threat_hunting.detection_engine.detection_engine.evaluate_all_rules",
                failing_eval,
            )

            with pytest.raises(RuntimeError):
                run_detection_sweep_once()

            # Verify lock was released despite the exception (can be acquired immediately)
            check_conn = bind.connect()
            try:
                can_lock = check_conn.execute(
                    text(f"SELECT pg_try_advisory_lock({DETECTION_WORKER_ADVISORY_LOCK_ID})")
                ).scalar()
                assert can_lock is True
                check_conn.execute(
                    text(f"SELECT pg_advisory_unlock({DETECTION_WORKER_ADVISORY_LOCK_ID})")
                )
            finally:
                check_conn.close()


def test_defect_8_hunt_time_range_validation_matrix(auth_headers):
    """
    Verifies Defect 8: Validation rejects inverted time ranges with 422 before DB execution,
    while accepting equal timestamps and timezone-aware timestamps correctly.
    """
    admin_hdr = auth_headers["admin"]
    t_now = datetime.now(timezone.utc)

    # 1. Inverted time range -> 422
    resp_inv = client.post(
        "/api/hunts",
        headers=admin_hdr,
        json={
            "name": f"Hunt Inv {uuid.uuid4().hex[:4]}",
            "hunt_definition": {
                "start_time": (t_now + timedelta(hours=1)).isoformat(),
                "end_time": t_now.isoformat(),
            },
        },
    )
    assert resp_inv.status_code == 422

    # 2. Equal timestamps (start_time == end_time) -> Allowed (201)
    resp_eq = client.post(
        "/api/hunts",
        headers=admin_hdr,
        json={
            "name": f"Hunt Equal {uuid.uuid4().hex[:4]}",
            "hunt_definition": {
                "start_time": t_now.isoformat(),
                "end_time": t_now.isoformat(),
            },
        },
    )
    assert resp_eq.status_code == 201

    # 3. Valid timezone-aware ranges with offsets -> Allowed (201)
    tz_ist = timezone(timedelta(hours=5, minutes=30))
    t_ist_start = t_now.astimezone(tz_ist)
    t_utc_end = t_now + timedelta(minutes=30)
    resp_tz_ok = client.post(
        "/api/hunts",
        headers=admin_hdr,
        json={
            "name": f"Hunt TZ Ok {uuid.uuid4().hex[:4]}",
            "hunt_definition": {
                "start_time": t_ist_start.isoformat(),
                "end_time": t_utc_end.isoformat(),
            },
        },
    )
    assert resp_tz_ok.status_code == 201

    # 4. Inverted across timezone offsets -> 422
    # Start: 10:00 UTC+5:30 (= 04:30 UTC), End: 03:00 UTC (= 03:00 UTC) -> Start > End!
    t_ist_late = datetime(2026, 10, 11, 10, 0, 0, tzinfo=tz_ist)
    t_utc_early = datetime(2026, 10, 11, 3, 0, 0, tzinfo=timezone.utc)
    resp_tz_inv = client.post(
        "/api/hunts",
        headers=admin_hdr,
        json={
            "name": f"Hunt TZ Inv {uuid.uuid4().hex[:4]}",
            "hunt_definition": {
                "start_time": t_ist_late.isoformat(),
                "end_time": t_utc_early.isoformat(),
            },
        },
    )
    assert resp_tz_inv.status_code == 422

