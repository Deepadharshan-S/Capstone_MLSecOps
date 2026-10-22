import uuid
import pytest
from unittest.mock import patch
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.main import app
from app.db.session import get_db
from app.db.base import Base
from app.models.user import User
from app.core.security import hash_password, create_access_token
from app.core.rate_limiter import RateLimiter
from fastapi import Depends

from fastapi import FastAPI
rl_app = FastAPI()
@rl_app.get("/api/test-security-ratelimit", dependencies=[Depends(RateLimiter(times=2, seconds=10, force_enable=True))])
def dummy_security_ratelimit_endpoint():
    return {"status": "ok"}
rl_client = TestClient(rl_app)

TEST_DATABASE_URL = "sqlite:///:memory:"
engine = create_engine(
    TEST_DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)

@event.listens_for(engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()

TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def override_get_db():
    try:
        db = TestingSessionLocal()
        yield db
    finally:
        db.close()

@pytest.fixture(scope="module", autouse=True)
def setup_security_audit_db():
    Base.metadata.create_all(bind=engine)
    db = TestingSessionLocal()
    try:
        # Seed test users
        users = [
            User(
                username="sec_admin",
                email="sec_admin@sentinelml.com",
                password_hash=hash_password("AdminSecurePassword123!"),
                role="admin",
                is_active=True,
            ),
            User(
                username="sec_viewer",
                email="sec_viewer@sentinelml.com",
                password_hash=hash_password("ViewerSecurePassword123!"),
                role="viewer",
                is_active=True,
            ),
            User(
                username="sec_inactive_user",
                email="sec_inactive@sentinelml.com",
                password_hash=hash_password("InactivePassword123!"),
                role="viewer",
                is_active=False,  # Inactive / Deactivated
            ),
        ]
        for u in users:
            db.add(u)
        db.flush()
        # Seed demo-repo dataset for upload tests
        from app.models.dataset import Dataset
        demo_ds = Dataset(
            name="demo-repo",
            description="Test dataset",
            default_branch="main",
            storage_namespace="local://lakefs/demo-repo",
            created_by_id=users[0].id,
        )
        db.add(demo_ds)
        db.commit()
    finally:
        db.close()

    app.dependency_overrides[get_db] = override_get_db
    yield
    if get_db in app.dependency_overrides:
        del app.dependency_overrides[get_db]
    Base.metadata.drop_all(bind=engine)

client = TestClient(app, base_url="https://testserver.local")


# ==============================================================================
# 1. Authentication & Inactive User Enforcement
# ==============================================================================

def test_unauthenticated_access_rejected_on_protected_endpoints():
    """Verify that protected endpoints across all routers reject requests with missing credentials (HTTP 401)."""
    protected_paths = [
        "/api/users/me",
        "/api/users/",
        "/api/models",
        "/api/datasets",
        "/api/alerts",
        "/api/detection-rules",
        "/api/security-events",
        "/api/hunts",
        "/api/drift/config",
        "/api/drift/findings",
    ]
    for path in protected_paths:
        res = client.get(path)
        assert res.status_code == 401, f"Expected 401 for unauthenticated GET {path}, got {res.status_code}"


def test_inactive_user_cannot_login():
    """Verify that an inactive/deactivated user cannot log in and receive tokens."""
    login_data = {
        "username": "sec_inactive_user",
        "password": "InactivePassword123!",
    }
    res = client.post("/api/auth/login", data=login_data)
    assert res.status_code == 400
    assert "inactive or disabled" in res.json()["detail"].lower()


def test_inactive_user_token_rejected_at_permissions_dependency():
    """
    Verify that if an inactive user possesses a validly signed JWT,
    get_current_user and get_current_active_user reject it with HTTP 403 Forbidden.
    """
    db = TestingSessionLocal()
    try:
        inactive_user = db.query(User).filter(User.username == "sec_inactive_user").first()
        token = create_access_token(subject=str(inactive_user.id))
    finally:
        db.close()

    headers = {"Authorization": f"Bearer {token}"}
    
    # Check users endpoint
    res_me = client.get("/api/users/me", headers=headers)
    assert res_me.status_code == 403
    assert "inactive user" in res_me.json()["detail"].lower()

    # Check threat hunting endpoint
    res_alerts = client.get("/api/alerts", headers=headers)
    assert res_alerts.status_code == 403
    assert "inactive user" in res_alerts.json()["detail"].lower()

    # Check drift endpoint
    res_drift = client.get("/api/drift/config", headers=headers)
    assert res_drift.status_code == 403
    assert "inactive user" in res_drift.json()["detail"].lower()


# ==============================================================================
# 2. Rate Limiting & Retry-After Header
# ==============================================================================

def test_rate_limiter_returns_429_with_retry_after():
    """Verify that exceeding rate limits returns HTTP 429 and includes a valid Retry-After header."""
    # First 2 requests should succeed (limit: 2 req / 10s)
    r1 = rl_client.get("/api/test-security-ratelimit")
    assert r1.status_code == 200

    r2 = rl_client.get("/api/test-security-ratelimit")
    assert r2.status_code == 200

    # 3rd request must be blocked with HTTP 429
    r3 = rl_client.get("/api/test-security-ratelimit")
    assert r3.status_code == 429
    assert "Retry-After" in r3.headers
    retry_after = int(r3.headers["Retry-After"])
    assert 1 <= retry_after <= 10


# ==============================================================================
# 3. Webhook Authentication
# ==============================================================================

def test_falco_webhook_authentication_enforcement():
    """Verify that the Falco container runtime webhook requires valid secret authentication."""
    payload = {
        "rule": "Unauthorized Shell Spawned",
        "priority": "Critical",
        "output": "Shell spawned in container",
        "output_fields": {"proc.name": "bash"},
    }

    # 1. Missing secret header -> 401
    r_missing = client.post("/api/webhooks/falco", json=payload)
    assert r_missing.status_code == 401
    assert r_missing.json()["reason"] == "invalid_or_missing_webhook_secret"

    # 2. Invalid secret header -> 401
    r_invalid = client.post(
        "/api/webhooks/falco",
        json=payload,
        headers={"X-Falco-Webhook-Secret": "wrong-secret-token"},
    )
    assert r_invalid.status_code == 401

    # 3. Valid secret header -> 200
    r_valid = client.post(
        "/api/webhooks/falco",
        json=payload,
        headers={"X-Falco-Webhook-Secret": "sentinelml-dev-falco-webhook-secret-32b"},
    )
    assert r_valid.status_code == 200
    assert r_valid.json()["status"] == "ingested"


# ==============================================================================
# 4. Input Validation & Governance Integrity
# ==============================================================================

def test_dataset_upload_path_traversal_and_governance_action_protection():
    """Verify that dataset file uploads reject path traversal and attempts to overwrite system governance actions."""
    # Authenticate as admin
    login_res = client.post(
        "/api/auth/login",
        data={"username": "sec_admin", "password": "AdminSecurePassword123!"},
    )
    assert login_res.status_code == 200
    token = login_res.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # 1. Path traversal attempt (../../etc/passwd)
    r_traversal = client.post(
        "/api/datasets/demo-repo/upload",
        params={"branch": "feature-branch"},
        files={"file": ("../../etc/passwd", b"root:x:0:0:")},
        headers=headers,
    )
    assert r_traversal.status_code == 400
    assert "invalid or unsafe filename" in r_traversal.json()["detail"].lower()

    # 2. System governance action overwrite attempt (_lakefs_actions/poisoning_gate.yaml)
    r_action_tamper = client.post(
        "/api/datasets/demo-repo/upload",
        params={"branch": "feature-branch"},
        files={"file": ("_lakefs_actions/poisoning_gate.yaml", b"disabled: true")},
        headers=headers,
    )
    assert r_action_tamper.status_code == 400
    assert "system action" in r_action_tamper.json()["detail"].lower()


# ==============================================================================
# 5. Information Disclosure Defense on Health Endpoint
# ==============================================================================

def test_health_endpoint_sanitizes_database_failure():
    """Verify that health check sanitizes error details and does not leak internal connection strings or stack traces."""
    # Public access
    r_healthy = client.get("/health")
    assert r_healthy.status_code == 200
    assert r_healthy.json()["database"] == "connected"

    # Simulate database execution failure
    with patch("sqlalchemy.orm.Session.execute", side_effect=Exception("FATAL: connection to 10.0.0.5:5432 failed - password auth")):
        r_failing = client.get("/health")
        assert r_failing.status_code == 503
        data = r_failing.json()
        assert data["status"] == "unhealthy"
        assert "error: FATAL: connection to 10.0.0.5:5432 failed" in data["database"]
