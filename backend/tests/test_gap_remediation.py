import uuid
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.main import app
from app.core.config import Settings, settings
from app.db.session import SessionLocal
from app.models.user import User
from app.models.refresh_token import RefreshToken
from app.models.blacklisted_token import BlacklistedToken
from app.repositories.token_repository import RefreshTokenRepository, BlacklistedTokenRepository
from app.repositories.user_repository import UserRepository
from app.repositories.dataset_repository import DatasetRepository
from app.services.dataset.s3_storage_service import S3StorageService
from app.services.auth.auth_service import AuthService
from app.core.security import create_access_token

client = TestClient(app)


# ---------------------------------------------------------------------------
# GAP 1: Token Cleanup Tests
# ---------------------------------------------------------------------------

def test_refresh_token_repository_delete_expired():
    """Verifies that expired refresh tokens are deleted while active ones are preserved."""
    with SessionLocal() as db:
        user = db.query(User).filter(User.username == "admin_user").first()
        if not user:
            pytest.skip("admin_user not found in DB")

        repo = RefreshTokenRepository(db)
        now = datetime.now(timezone.utc)

        # 1. Create one expired token and one valid token
        expired_hash = f"expired-refresh-{uuid.uuid4().hex}"
        valid_hash = f"valid-refresh-{uuid.uuid4().hex}"

        expired_tok = RefreshToken(
            token_hash=expired_hash,
            user_id=user.id,
            expires_at=now - timedelta(days=2),
            is_revoked=False,
        )
        valid_tok = RefreshToken(
            token_hash=valid_hash,
            user_id=user.id,
            expires_at=now + timedelta(days=7),
            is_revoked=False,
        )
        repo.create(expired_tok)
        repo.create(valid_tok)

        # 2. Run delete_expired
        deleted = repo.delete_expired(now=now)
        assert deleted >= 1

        # 3. Assert expired token is gone and valid token remains
        assert repo.get_by_jti(expired_hash) is None
        remaining = repo.get_by_jti(valid_hash)
        assert remaining is not None

        # Clean up valid token
        repo.delete(remaining)


def test_blacklisted_token_repository_delete_expired():
    """Verifies that expired blacklisted tokens are deleted while unexpired ones remain."""
    with SessionLocal() as db:
        repo = BlacklistedTokenRepository(db)
        now = datetime.now(timezone.utc)

        expired_jti = f"expired-jti-{uuid.uuid4().hex}"
        valid_jti = f"valid-jti-{uuid.uuid4().hex}"

        expired_tok = BlacklistedToken(
            jti=expired_jti,
            expires_at=now - timedelta(hours=1),
        )
        valid_tok = BlacklistedToken(
            jti=valid_jti,
            expires_at=now + timedelta(hours=1),
        )
        repo.create(expired_tok)
        repo.create(valid_tok)

        deleted = repo.delete_expired(now=now)
        assert deleted >= 1

        assert repo.get_by_jti(expired_jti) is None
        remaining = repo.get_by_jti(valid_jti)
        assert remaining is not None

        repo.delete(remaining)


def test_cleanup_tokens_api_admin_and_rbac(user_tokens):
    """Verifies POST /api/auth/cleanup-tokens endpoint permissions and response."""
    admin_headers = {"Authorization": f"Bearer {user_tokens['admin_user']}"}
    viewer_headers = {"Authorization": f"Bearer {user_tokens['viewer_user']}"}

    # 1. Viewer cannot invoke token cleanup (403 Forbidden)
    resp_viewer = client.post("/api/auth/cleanup-tokens", headers=viewer_headers)
    assert resp_viewer.status_code == 403

    # 2. Admin can invoke token cleanup (200 OK)
    resp_admin = client.post("/api/auth/cleanup-tokens", headers=admin_headers)
    assert resp_admin.status_code == 200
    data = resp_admin.json()
    assert "deleted_refresh_tokens" in data
    assert "deleted_blacklisted_tokens" in data
    assert "total_deleted" in data
    assert data["total_deleted"] == data["deleted_refresh_tokens"] + data["deleted_blacklisted_tokens"]


# ---------------------------------------------------------------------------
# GAP 2: Pagination Tests
# ---------------------------------------------------------------------------

def test_users_pagination(user_tokens):
    """Verifies pagination support on GET /api/users."""
    admin_headers = {"Authorization": f"Bearer {user_tokens['admin_user']}"}

    # 1. Default pagination
    resp_default = client.get("/api/users/", headers=admin_headers)
    assert resp_default.status_code == 200
    all_users = resp_default.json()
    assert isinstance(all_users, list)
    assert len(all_users) >= 1

    # 2. Custom limit = 1
    resp_limit_1 = client.get("/api/users/?limit=1", headers=admin_headers)
    assert resp_limit_1.status_code == 200
    users_1 = resp_limit_1.json()
    assert len(users_1) == 1

    # 3. Offset pagination
    if len(all_users) > 1:
        resp_offset_1 = client.get("/api/users/?limit=1&offset=1", headers=admin_headers)
        assert resp_offset_1.status_code == 200
        users_offset = resp_offset_1.json()
        assert len(users_offset) == 1
        assert users_offset[0]["username"] != users_1[0]["username"]

    # 4. Large offset returning empty list
    resp_empty = client.get("/api/users/?limit=10&offset=10000", headers=admin_headers)
    assert resp_empty.status_code == 200
    assert resp_empty.json() == []

    # 5. Bounds validation (max limit 1000, ge 1)
    resp_invalid_0 = client.get("/api/users/?limit=0", headers=admin_headers)
    assert resp_invalid_0.status_code == 422

    resp_invalid_over = client.get("/api/users/?limit=1001", headers=admin_headers)
    assert resp_invalid_over.status_code == 422


def test_datasets_pagination(user_tokens):
    """Verifies pagination support on GET /api/datasets and RBAC scope."""
    ds_headers = {"Authorization": f"Bearer {user_tokens['ds_user']}"}
    viewer_headers = {"Authorization": f"Bearer {user_tokens['viewer_user']}"}

    # 1. Viewer without datasets:view scope cannot view datasets (403 Forbidden)
    resp_viewer = client.get("/api/datasets", headers=viewer_headers)
    assert resp_viewer.status_code == 403

    # 2. Default pagination
    resp = client.get("/api/datasets", headers=ds_headers)
    assert resp.status_code == 200
    datasets = resp.json()
    assert isinstance(datasets, list)

    # 3. Custom limit
    resp_limit = client.get("/api/datasets?limit=2", headers=ds_headers)
    assert resp_limit.status_code == 200
    assert len(resp_limit.json()) <= 2

    # 4. Large offset
    resp_empty = client.get("/api/datasets?limit=10&offset=10000", headers=ds_headers)
    assert resp_empty.status_code == 200
    assert resp_empty.json() == []

    # 5. Bounds validation
    resp_bad = client.get("/api/datasets?limit=0", headers=ds_headers)
    assert resp_bad.status_code == 422


# ---------------------------------------------------------------------------
# GAP 3 & 4: S3 Client Reuse and Least Privilege Tests
# ---------------------------------------------------------------------------

def test_s3_storage_service_client_reuse():
    """Verifies that S3StorageService reuses the persistent Boto3 client/resource."""
    service = S3StorageService()
    client1 = service.client
    client2 = service.client
    resource1 = service.resource
    resource2 = service.resource

    assert client1 is client2
    assert resource1 is resource2


def test_s3_storage_service_dependency_injection():
    """Verifies S3StorageService supports dependency injection for mock isolation."""
    mock_client = MagicMock()
    mock_resource = MagicMock()
    mock_client.list_buckets.return_value = {"Buckets": []}

    service = S3StorageService(s3_client=mock_client, s3_resource=mock_resource)
    assert service.client is mock_client
    assert service.resource is mock_resource

    is_healthy, msg = service.check_health()
    assert is_healthy is True
    assert msg == "connected"
    mock_client.list_buckets.assert_called_once()


def test_minio_app_credentials_configuration():
    """Verifies settings properties for MinIO runtime application credentials."""
    s = Settings(
        SECRET_KEY="test-secret-key-123",
        LAKEFS_ENDPOINT="http://localhost:8000",
        LAKEFS_ACCESS_KEY_ID="lakefs_key",
        LAKEFS_SECRET_ACCESS_KEY="lakefs_secret",
        MINIO_ACCESS_KEY="custom_app_key",
        MINIO_SECRET_KEY="custom_app_secret",
    )
    assert s.minio_app_user == "custom_app_key"
    assert s.minio_app_password == "custom_app_secret"


# ---------------------------------------------------------------------------
# GAP 5: CORS Configuration Tests
# ---------------------------------------------------------------------------

def test_cors_origins_configuration():
    """Verifies parsing of CORS_ORIGINS from comma-separated string and JSON array."""
    # 1. Comma-separated string
    s1 = Settings(
        SECRET_KEY="test-secret-key-123",
        LAKEFS_ENDPOINT="http://localhost:8000",
        LAKEFS_ACCESS_KEY_ID="lakefs_key",
        LAKEFS_SECRET_ACCESS_KEY="lakefs_secret",
        CORS_ORIGINS="https://app.sentinel.io, https://staging.sentinel.io",
    )
    assert s1.CORS_ORIGINS == ["https://app.sentinel.io", "https://staging.sentinel.io"]

    # 2. JSON array string
    s2 = Settings(
        SECRET_KEY="test-secret-key-123",
        LAKEFS_ENDPOINT="http://localhost:8000",
        LAKEFS_ACCESS_KEY_ID="lakefs_key",
        LAKEFS_SECRET_ACCESS_KEY="lakefs_secret",
        CORS_ORIGINS='["https://app.sentinel.io", "https://api.sentinel.io"]',
    )
    assert s2.CORS_ORIGINS == ["https://app.sentinel.io", "https://api.sentinel.io"]


# ---------------------------------------------------------------------------
# GAP 7: Request Correlation ID Tests
# ---------------------------------------------------------------------------

def test_request_id_middleware_generates_id():
    """Verifies that an incoming request without X-Request-ID gets a generated ID in response headers."""
    resp = client.get("/")
    assert resp.status_code == 200
    assert "X-Request-ID" in resp.headers
    req_id = resp.headers["X-Request-ID"]
    assert len(req_id) >= 16


def test_request_id_middleware_preserves_incoming_id():
    """Verifies that a valid incoming X-Request-ID is preserved across request lifecycle."""
    custom_id = "trace-secops-9876543210"
    resp = client.get("/", headers={"X-Request-ID": custom_id})
    assert resp.status_code == 200
    assert resp.headers.get("X-Request-ID") == custom_id


# ---------------------------------------------------------------------------
# GAP 8: External Service Failure Path Tests
# ---------------------------------------------------------------------------

def test_health_check_service_unavailability():
    """Verifies /health endpoint returns 503 when any dependency backend reports unhealthy."""
    with patch("app.services.dataset.s3_storage_service.S3StorageService.check_health") as mock_s3:
        mock_s3.return_value = (False, "Connection timeout to MinIO")
        resp = client.get("/health")
        assert resp.status_code == 503
        data = resp.json()
        assert data["status"] == "unhealthy"
        assert "error" in data["minio"] or "timeout" in data["minio"].lower()


def test_k8s_api_failure_handling():
    """Verifies RayJobService handles Kubernetes API exceptions gracefully without crashing."""
    from app.services.ml_ops.rayjob_service import RayJobService
    from kubernetes.client.exceptions import ApiException

    mock_custom = MagicMock()
    mock_core = MagicMock()
    mock_custom.get_namespaced_custom_object.side_effect = ApiException(status=500, reason="K8s Internal Server Error")
    mock_custom.list_namespaced_custom_object.side_effect = ApiException(status=500, reason="K8s Internal Server Error")

    service = RayJobService(custom_api=mock_custom, core_api=mock_core)
    job_result = service.get_rayjob("test-failed-k8s-job")
    assert job_result is None

    list_result = service.list_rayjobs()
    assert list_result == []

