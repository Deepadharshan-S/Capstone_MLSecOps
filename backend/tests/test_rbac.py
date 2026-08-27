import pytest
import uuid
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.main import app
from app.db.session import get_db
from app.db.base import Base
from app.models.user import User
from app.models.refresh_token import RefreshToken  # noqa: F401
from app.models.audit_log import AuditLog  # noqa: F401
from app.models.blacklisted_token import BlacklistedToken  # noqa: F401

from sqlalchemy.pool import StaticPool
from fastapi import Depends
from app.core.rate_limiter import RateLimiter

# Dynamically add a temporary route to test rate limiting in isolation
@app.get("/api/test-rate-limiting-endpoint", dependencies=[Depends(RateLimiter(times=3, seconds=5, force_enable=True))])
def rate_limiting_test_endpoint():
    return {"message": "success"}

# Isolated Test Database setup (using in-memory SQLite)
TEST_DATABASE_URL = "sqlite:///:memory:"
engine = create_engine(
    TEST_DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool
)

# Enable foreign keys support in SQLite
@event.listens_for(engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()

TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture(scope="module", autouse=True)
def setup_test_database():
    """
    Creates tables in the SQLite database and seeds default users before running tests,
    then tears down the database after tests complete.
    """
    Base.metadata.create_all(bind=engine)
    
    db = TestingSessionLocal()
    try:
        from app.core.security import hash_password
        seed_data = [
            {
                "username": "admin_user",
                "email": "admin@mlsecops.com",
                "password": "AdminPassword123!",
                "role": "admin",
            },
            {
                "username": "ds_user",
                "email": "ds@mlsecops.com",
                "password": "DataScientist123!",
                "role": "data_scientist",
            },
            {
                "username": "mle_user",
                "email": "mle@mlsecops.com",
                "password": "MLEngineerPassword123!",
                "role": "ml_engineer",
            },
            {
                "username": "viewer_user",
                "email": "viewer@mlsecops.com",
                "password": "ViewerPassword123!",
                "role": "viewer",
            },
        ]
        for u in seed_data:
            user = User(
                username=u["username"],
                email=u["email"],
                password_hash=hash_password(u["password"]),
                role=u["role"],
                is_active=True,
            )
            db.add(user)
        db.commit()
    finally:
        db.close()

    # Apply dependency overrides to the app
    app.dependency_overrides[get_db] = override_get_db

    yield

    # Clean up dependency overrides
    if get_db in app.dependency_overrides:
        del app.dependency_overrides[get_db]

    Base.metadata.drop_all(bind=engine)


def override_get_db():
    try:
        db = TestingSessionLocal()
        yield db
    finally:
        db.close()


client = TestClient(app, base_url="https://testserver.local")


def test_public_endpoints():
    """Verify that root and health endpoints do not require authentication."""
    r_root = client.get("/")
    assert r_root.status_code == 200
    assert r_root.json() == {"message": "Welcome to MLSecOps"}

    r_health = client.get("/health")
    assert r_health.status_code == 200
    res = r_health.json()
    assert res["status"] == "healthy"
    assert res["database"] == "connected"
    assert res["lakefs"] == "connected"


def test_password_complexity():
    """Verify that register enforces password policies."""
    # Case 1: Fails Pydantic schema validation (length < 8) -> returns 422
    payload_short = {
        "username": "short_pwd_user",
        "email": "short@mlsecops.com",
        "password": "123",
        "role": "viewer",
    }
    response_short = client.post("/api/auth/register", json=payload_short)
    assert response_short.status_code == 422
    assert "at least 8 characters" in response_short.json()["detail"][0]["msg"]

    # Case 2: Passes schema check but fails complexity rules -> returns 400
    payload_weak = {
        "username": "weak_pwd_user",
        "email": "weak@mlsecops.com",
        "password": "weakpassword123",  # 15 chars, but no uppercase or special chars
        "role": "viewer",
    }
    response_weak = client.post("/api/auth/register", json=payload_weak)
    assert response_weak.status_code == 400
    assert "uppercase letter" in response_weak.json()["detail"]

    # Case 3: Register with a valid complex password
    payload_ok = {
        "username": "complex_pwd_user",
        "email": "complex@mlsecops.com",
        "password": "StrongPassword123!",
        "role": "viewer",
    }
    response_ok = client.post("/api/auth/register", json=payload_ok)
    assert response_ok.status_code == 201
    assert response_ok.json()["username"] == "complex_pwd_user"
    assert response_ok.json()["role"] == "viewer"


def test_authentication_workflow():
    """Verify login, JWT issuance, and refresh token cookie settings."""
    # 1. Login with ds_user
    login_data = {
        "username": "ds_user",
        "password": "DataScientist123!",
    }
    response = client.post("/api/auth/login", data=login_data)
    assert response.status_code == 200
    assert "access_token" in response.json()
    
    # 2. Check for HttpOnly refresh token cookie
    assert "refresh_token" in response.cookies
    # Verify that we can retrieve profile using the access token
    headers = {"Authorization": f"Bearer {response.json()['access_token']}"}
    profile_response = client.get("/api/users/me", headers=headers)
    assert profile_response.status_code == 200
    assert profile_response.json()["username"] == "ds_user"
    assert profile_response.json()["role"] == "data_scientist"


def test_token_refresh_rotation_and_revocation():
    """Verify Refresh Token Rotation (RTR) and token compromise reuse protection."""
    login_data = {
        "username": "viewer_user",
        "password": "ViewerPassword123!",
    }
    login_response = client.post("/api/auth/login", data=login_data)
    assert login_response.status_code == 200
    token1 = login_response.json()["access_token"]
    cookie1 = client.cookies.get("refresh_token")
    assert cookie1 is not None

    # 1. First refresh - Should succeed and return a new access token
    refresh_response = client.post("/api/auth/refresh")
    assert refresh_response.status_code == 200
    token2 = refresh_response.json()["access_token"]
    assert token1 != token2
    cookie2 = client.cookies.get("refresh_token")
    assert cookie2 is not None
    assert cookie1 != cookie2

    # 2. Reuse check (Security Breach Containment)
    # If cookie1 is used again (simulating token theft), it should trigger reuse detection,
    # revoke all sessions, and return a 401 status.
    client.cookies.set("refresh_token", cookie1)
    abuse_response = client.post("/api/auth/refresh")
    assert abuse_response.status_code == 401
    assert "compromised" in abuse_response.json()["detail"]

    # 3. Verify that the user's active session is also invalidated
    client.cookies.set("refresh_token", cookie2)
    invalidated_response = client.post("/api/auth/refresh")
    assert invalidated_response.status_code == 401


def test_brute_force_lockout():
    """Verify that an account is locked out after 5 consecutive failed login attempts."""
    # We will register a fresh user to test lockout without affecting standard seeds
    register_payload = {
        "username": "lockout_user",
        "email": "lockout@mlsecops.com",
        "password": "LockoutPassword123!",
        "role": "viewer",
    }
    reg_resp = client.post("/api/auth/register", json=register_payload)
    assert reg_resp.status_code == 201

    login_data_bad = {
        "username": "lockout_user",
        "password": "WrongPassword!",
    }

    # Attempt 1 to 4: Should return 400
    for i in range(4):
        resp = client.post("/api/auth/login", data=login_data_bad)
        assert resp.status_code == 400
        assert "Incorrect username" in resp.json()["detail"]

    # Attempt 5: Should trigger account lockout and return 403
    resp_5 = client.post("/api/auth/login", data=login_data_bad)
    assert resp_5.status_code == 403
    assert "temporarily locked" in resp_5.json()["detail"]

    # Attempt 6 (Immediate subsequent attempt): Should block immediately
    resp_6 = client.post("/api/auth/login", data=login_data_bad)
    assert resp_6.status_code == 403
    assert "temporarily locked" in resp_6.json()["detail"]

    # Trying to login with correct password now: should still fail since account is locked
    login_data_good = {
        "username": "lockout_user",
        "password": "LockoutPassword123!",
    }
    resp_good_but_locked = client.post("/api/auth/login", data=login_data_good)
    assert resp_good_but_locked.status_code == 403
    assert "temporarily locked" in resp_good_but_locked.json()["detail"]


def test_rbac_permissions_matrix():
    """
    Test the full permission matrix for all roles:
    - Admin: Full access to all endpoints.
    - Data Scientist: Upload datasets, start training, view models.
    - ML Engineer: Deploy models, manage deployments, view models.
    - Viewer: Read-only access to view models.
    """
    # Helper to get headers for a user
    def get_user_headers(username, password):
        login_data = {"username": username, "password": password}
        resp = client.post("/api/auth/login", data=login_data)
        assert resp.status_code == 200
        return {"Authorization": f"Bearer {resp.json()['access_token']}"}

    admin_headers = get_user_headers("admin_user", "AdminPassword123!")
    ds_headers = get_user_headers("ds_user", "DataScientist123!")
    mle_headers = get_user_headers("mle_user", "MLEngineerPassword123!")
    viewer_headers = get_user_headers("viewer_user", "ViewerPassword123!")

    # 1. Dataset Upload (Admin and Data Scientist only)
    suffix = uuid.uuid4().hex[:8]
    dataset_payload_admin = {"name": f"test-dataset-admin-{suffix}", "description": "Classification dataset"}
    dataset_payload_ds = {"name": f"test-dataset-ds-{suffix}", "description": "Classification dataset"}
    
    assert client.post("/api/datasets", data=dataset_payload_admin, files={"file": ("data.csv", b"content")}, headers=admin_headers).status_code == 201
    assert client.post("/api/datasets", data=dataset_payload_ds, files={"file": ("data.csv", b"content")}, headers=ds_headers).status_code == 201
    assert client.post("/api/datasets", data=dataset_payload_ds, files={"file": ("data.csv", b"content")}, headers=mle_headers).status_code == 403
    assert client.post("/api/datasets", data=dataset_payload_ds, files={"file": ("data.csv", b"content")}, headers=viewer_headers).status_code == 403

    # 2. Model Training (Admin and Data Scientist only)
    train_payload = {"dataset_id": "iris-uuid", "epochs": 5}
    assert client.post("/api/models/train", json=train_payload, headers=admin_headers).status_code == 202
    assert client.post("/api/models/train", json=train_payload, headers=ds_headers).status_code == 202
    assert client.post("/api/models/train", json=train_payload, headers=mle_headers).status_code == 403
    assert client.post("/api/models/train", json=train_payload, headers=viewer_headers).status_code == 403

    # 3. Model Viewing (All roles)
    assert client.get("/api/models", headers=admin_headers).status_code == 200
    assert client.get("/api/models", headers=ds_headers).status_code == 200
    assert client.get("/api/models", headers=mle_headers).status_code == 200
    assert client.get("/api/models", headers=viewer_headers).status_code == 200

    # 4. Model Deployment (Admin and ML Engineer only)
    deploy_payload = {"model_id": "model-uuid-1", "environment": "production"}
    assert client.post("/api/models/deploy", json=deploy_payload, headers=admin_headers).status_code == 201
    assert client.post("/api/models/deploy", json=deploy_payload, headers=mle_headers).status_code == 201
    assert client.post("/api/models/deploy", json=deploy_payload, headers=ds_headers).status_code == 403
    assert client.post("/api/models/deploy", json=deploy_payload, headers=viewer_headers).status_code == 403

    # 5. Deployment Management (Admin and ML Engineer only)
    manage_payload = {"deployment_id": "deploy-uuid-1", "action": "restart"}
    assert client.post("/api/deployments/manage", json=manage_payload, headers=admin_headers).status_code == 200
    assert client.post("/api/deployments/manage", json=manage_payload, headers=mle_headers).status_code == 200
    assert client.post("/api/deployments/manage", json=manage_payload, headers=ds_headers).status_code == 403
    assert client.post("/api/deployments/manage", json=manage_payload, headers=viewer_headers).status_code == 403


def test_admin_user_management():
    """Verify that Admin can view users list and alter user roles, and non-admins are blocked."""
    def get_user_headers(username, password):
        login_data = {"username": username, "password": password}
        resp = client.post("/api/auth/login", data=login_data)
        return {"Authorization": f"Bearer {resp.json()['access_token']}"}

    admin_headers = get_user_headers("admin_user", "AdminPassword123!")
    viewer_headers = get_user_headers("viewer_user", "ViewerPassword123!")

    # 1. Admin lists all users
    list_resp = client.get("/api/users/", headers=admin_headers)
    assert list_resp.status_code == 200
    users_list = list_resp.json()
    assert len(users_list) >= 4

    # 2. Non-admin fails to list users
    assert client.get("/api/users/", headers=viewer_headers).status_code == 403

    # 3. Find the viewer user ID to update its role
    viewer_id = None
    for u in users_list:
        if u["username"] == "viewer_user":
            viewer_id = u["id"]
            break
    assert viewer_id is not None

    # 4. Non-admin fails to update role
    role_payload = {"role": "admin"}
    assert client.put(f"/api/users/{viewer_id}/role", json=role_payload, headers=viewer_headers).status_code == 403

    # 5. Admin updates viewer's role to data_scientist
    role_payload_ok = {"role": "data_scientist"}
    update_resp = client.put(f"/api/users/{viewer_id}/role", json=role_payload_ok, headers=admin_headers)
    assert update_resp.status_code == 200
    assert update_resp.json()["role"] == "data_scientist"

    # 6. Verify the updated user now has Data Scientist permissions (can upload datasets)
    # Login again to get new token reflecting the new role
    new_headers = get_user_headers("viewer_user", "ViewerPassword123!")
    dataset_payload = {"name": f"test-dataset-2-{uuid.uuid4().hex[:8]}", "description": "Classification dataset 2"}
    assert client.post("/api/datasets", data=dataset_payload, files={"file": ("data.csv", b"content")}, headers=new_headers).status_code == 201


def test_access_token_blacklisting():
    """Verify that logging out blacklists the access token immediately."""
    # 1. Login to get token
    login_data = {
        "username": "viewer_user",
        "password": "ViewerPassword123!",
    }
    login_resp = client.post("/api/auth/login", data=login_data)
    assert login_resp.status_code == 200
    token = login_resp.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    
    # 2. Access endpoint - should succeed
    resp_before = client.get("/api/users/me", headers=headers)
    assert resp_before.status_code == 200
    
    # 3. Logout
    logout_resp = client.post("/api/auth/logout", headers=headers)
    assert logout_resp.status_code == 200
    
    # 4. Access endpoint again - should be rejected with 401
    resp_after = client.get("/api/users/me", headers=headers)
    assert resp_after.status_code == 401


def test_audit_logging_and_retrieval():
    """Verify that security events generate audit logs and Admins can query them."""
    # 1. Non-admin cannot access audit logs
    viewer_login = client.post("/api/auth/login", data={"username": "viewer_user", "password": "ViewerPassword123!"})
    viewer_headers = {"Authorization": f"Bearer {viewer_login.json()['access_token']}"}
    assert client.get("/api/users/audit-logs", headers=viewer_headers).status_code == 403
    
    # 2. Admin can access audit logs
    admin_login = client.post("/api/auth/login", data={"username": "admin_user", "password": "AdminPassword123!"})
    admin_headers = {"Authorization": f"Bearer {admin_login.json()['access_token']}"}
    logs_resp = client.get("/api/users/audit-logs", headers=admin_headers)
    assert logs_resp.status_code == 200
    logs = logs_resp.json()
    assert len(logs) > 0
    
    # 3. Check that actions like login_success are recorded
    actions = [log["action"] for log in logs]
    assert "login_success" in actions


def test_rate_limiting_enforcement():
    """Verify that exceeding the rate limit triggers HTTP 429 Too Many Requests."""
    # First 3 requests must succeed
    for _ in range(3):
        response = client.get("/api/test-rate-limiting-endpoint")
        assert response.status_code == 200
        assert response.json() == {"message": "success"}

    # 4th request must fail with 429
    response = client.get("/api/test-rate-limiting-endpoint")
    assert response.status_code == 429
    assert "Too many requests" in response.json()["detail"]


def test_registration_always_assigns_viewer_role():
    """Verify that registering a new user always assigns the 'viewer' role, even if another role is passed."""
    # Register requesting 'admin' role
    payload = {
        "username": "attacker_admin",
        "email": "attacker@mlsecops.com",
        "password": "StrongPassword123!",
        "role": "admin"
    }
    response = client.post("/api/auth/register", json=payload)
    assert response.status_code == 201
    
    # Assert that role returned in response is viewer, not admin
    user_data = response.json()
    assert user_data["username"] == "attacker_admin"
    assert user_data["role"] == "viewer"

    # Verify directly in the DB
    db = TestingSessionLocal()
    db_user = db.query(User).filter(User.username == "attacker_admin").first()
    assert db_user is not None
    assert db_user.role == "viewer"
    db.close()


def test_registration_integrity_error_handling():
    """Verify that registering a user with duplicate username or email returns a clean 400 Bad Request."""
    # Register first user
    payload1 = {
        "username": "duplicate_user",
        "email": "duplicate@mlsecops.com",
        "password": "StrongPassword123!"
    }
    response1 = client.post("/api/auth/register", json=payload1)
    assert response1.status_code == 201

    # Register second user with same username and email
    response2 = client.post("/api/auth/register", json=payload1)
    assert response2.status_code == 400
    assert response2.json()["detail"] == "Username or email is unavailable."


def test_health_endpoint_failure_modes(monkeypatch):
    """Verify that /health returns 503 if database or lakeFS is unhealthy."""
    from unittest.mock import MagicMock
    from app.services.data_service import data_service

    # Case 1: Database failure
    original_get_db = app.dependency_overrides.get(get_db)
    
    mock_db = MagicMock()
    mock_db.execute.side_effect = Exception("DB Connection Lost")
    
    def override_get_db_fail():
        yield mock_db
        
    app.dependency_overrides[get_db] = override_get_db_fail
    try:
        r_health = client.get("/health")
        assert r_health.status_code == 503
        res = r_health.json()
        assert res["status"] == "unhealthy"
        assert "error: DB Connection Lost" in res["database"]
        assert res["lakefs"] == "connected"
    finally:
        if original_get_db:
            app.dependency_overrides[get_db] = original_get_db
        else:
            del app.dependency_overrides[get_db]

    # Case 2: lakeFS failure
    original_client = data_service.client
    class MockLakefsClient:
        class sdk_client:
            class config_api:
                @staticmethod
                def get_config():
                    raise Exception("lakeFS Offline")
            
    data_service.client = MockLakefsClient()
    try:
        r_health = client.get("/health")
        assert r_health.status_code == 503
        res = r_health.json()
        assert res["status"] == "unhealthy"
        assert res["database"] == "connected"
        assert "error: lakeFS Offline" in res["lakefs"]
    finally:
        data_service.client = original_client
