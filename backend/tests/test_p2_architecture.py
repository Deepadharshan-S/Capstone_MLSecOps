import os
import uuid
from datetime import datetime, timezone, timedelta
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.main import app
from app.db.base import Base
from app.models.user import User
from app.models.dataset import Dataset
from app.models.refresh_token import RefreshToken
from app.models.blacklisted_token import BlacklistedToken
from app.repositories import (
    BaseRepository,
    UserRepository,
    DatasetRepository,
    RefreshTokenRepository,
    BlacklistedTokenRepository,
)
from app.services.auth import (
    AuthService,
    WeakPasswordError,
    UserAlreadyExistsError,
    InvalidCredentialsError,
    AccountLockedError,
    InvalidTokenError,
    TokenReuseError,
    UserNotFoundError,
    SelfRoleModificationError,
)
from app.core.config import settings
from app.services.ml_ops.utils import render_rayjob_manifest, get_scoped_training_credentials


TEST_DB_URL = "sqlite:///:memory:"
engine = create_engine(TEST_DB_URL, connect_args={"check_same_thread": False}, poolclass=StaticPool)
TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base.metadata.create_all(bind=engine)
client = TestClient(app)


def test_db_pool_configuration():
    """Verify that PostgreSQL engine configuration includes connection pool resilience parameters."""
    from app.db.session import engine as app_engine
    # In PostgreSQL (or configured default), engine pool has pre-ping enabled
    assert hasattr(app_engine.pool, "_pre_ping")
    assert app_engine.pool._pre_ping is True


def test_user_repository_crud():
    """Test UserRepository operations."""
    db = TestingSession()
    repo = UserRepository(db)

    test_user = User(
        username="repo_test_user",
        email="repo_test@example.com",
        password_hash="hash123",
        role="viewer",
        is_active=True,
    )
    created = repo.create(test_user)
    assert created.id is not None

    by_id = repo.get_by_id(created.id)
    assert by_id is not None
    assert by_id.username == "repo_test_user"

    by_username = repo.get_by_username("repo_test_user")
    assert by_username is not None

    by_email = repo.get_by_email("repo_test@example.com")
    assert by_email is not None

    by_either = repo.get_by_username_or_email("repo_test_user")
    assert by_either is not None

    by_either_email = repo.get_by_username_or_email("repo_test@example.com")
    assert by_either_email is not None

    assert repo.count() >= 1

    created.role = "data_scientist"
    saved = repo.save(created)
    assert saved.role == "data_scientist"

    repo.delete(saved)
    assert repo.get_by_id(created.id) is None
    db.close()


def test_dataset_repository_crud():
    """Test DatasetRepository operations."""
    db = TestingSession()
    repo = DatasetRepository(db)

    user_repo = UserRepository(db)
    owner = user_repo.create(
        User(
            username="dataset_owner",
            email="owner@example.com",
            password_hash="hash",
            role="data_scientist",
        )
    )

    ds = Dataset(
        name="test-repo-dataset",
        description="Dataset for repo test",
        storage_namespace="s3://lakefs/test-repo-dataset",
        default_branch="main",
        created_by_id=owner.id,
        metadata_info={"framework": "pytorch"},
    )
    created = repo.create(ds)
    assert created.id is not None

    by_name = repo.get_by_name("test-repo-dataset")
    assert by_name is not None
    assert by_name.metadata_info == {"framework": "pytorch"}

    created.description = "Updated description"
    saved = repo.save(created)
    assert saved.description == "Updated description"

    repo.delete(saved)
    assert repo.get_by_name("test-repo-dataset") is None
    user_repo.delete(owner)
    db.close()


def test_token_repositories_crud():
    """Test RefreshTokenRepository and BlacklistedTokenRepository operations."""
    db = TestingSession()
    user_repo = UserRepository(db)
    refresh_repo = RefreshTokenRepository(db)
    blacklist_repo = BlacklistedTokenRepository(db)

    user = user_repo.create(
        User(
            username="token_test_user",
            email="token_user@example.com",
            password_hash="hash",
            role="viewer",
        )
    )

    jti = str(uuid.uuid4())
    token = RefreshToken(
        token_hash=jti,
        user_id=user.id,
        expires_at=datetime.now(timezone.utc) + timedelta(days=7),
        is_revoked=False,
    )
    refresh_repo.create(token)

    found = refresh_repo.get_by_jti(jti)
    assert found is not None
    assert found.is_revoked is False

    refresh_repo.revoke_all_for_user(user.id)
    revoked = refresh_repo.get_by_jti(jti)
    assert revoked.is_revoked is True

    # Blacklisted token
    bl_jti = str(uuid.uuid4())
    bl_token = BlacklistedToken(
        jti=bl_jti,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    blacklist_repo.create(bl_token)
    assert blacklist_repo.get_by_jti(bl_jti) is not None

    # Cleanup
    refresh_repo.delete(token)
    blacklist_repo.delete(bl_token)
    user_repo.delete(user)
    db.close()


def test_domain_exception_handlers():
    """Verify that domain exceptions are cleanly mapped to expected HTTP status codes and payloads."""
    # Test registered exception handlers directly via TestClient on dynamic test routes
    from fastapi import APIRouter
    test_router = APIRouter()

    @test_router.get("/test-err-weak-pwd")
    def r_weak():
        raise WeakPasswordError("Password lacks symbols.")

    @test_router.get("/test-err-collision")
    def r_coll():
        raise UserAlreadyExistsError()

    @test_router.get("/test-err-credentials")
    def r_cred():
        raise InvalidCredentialsError()

    @test_router.get("/test-err-locked")
    def r_lock():
        raise AccountLockedError()

    @test_router.get("/test-err-token")
    def r_tok():
        raise InvalidTokenError("Custom invalid token detail")

    @test_router.get("/test-err-reuse")
    def r_reuse():
        raise TokenReuseError()

    @test_router.get("/test-err-not-found")
    def r_nf():
        raise UserNotFoundError()

    @test_router.get("/test-err-self-role")
    def r_self():
        raise SelfRoleModificationError()

    app.include_router(test_router, prefix="/api/test-exceptions")

    resp = client.get("/api/test-exceptions/test-err-weak-pwd")
    assert resp.status_code == 400
    assert "symbols" in resp.json()["detail"]

    resp = client.get("/api/test-exceptions/test-err-collision")
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Username or email is unavailable."

    resp = client.get("/api/test-exceptions/test-err-credentials")
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Incorrect username or password."

    resp = client.get("/api/test-exceptions/test-err-locked")
    assert resp.status_code == 403
    assert "temporarily locked" in resp.json()["detail"]

    resp = client.get("/api/test-exceptions/test-err-token")
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Custom invalid token detail"

    resp = client.get("/api/test-exceptions/test-err-reuse")
    assert resp.status_code == 401
    assert "compromised" in resp.json()["detail"]

    resp = client.get("/api/test-exceptions/test-err-not-found")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "User not found."

    resp = client.get("/api/test-exceptions/test-err-self-role")
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Admins cannot change their own roles."


def test_internal_networking_configuration(monkeypatch):
    """Verify that configured cluster-internal endpoints override default networking in template rendering."""
    monkeypatch.setattr(settings, "MLFLOW_INTERNAL_ENDPOINT", "http://sentinelml-mlflow.default.svc.cluster.local:5000")
    monkeypatch.setattr(settings, "MINIO_INTERNAL_ENDPOINT", "http://sentinelml-minio.default.svc.cluster.local:9000")

    creds = get_scoped_training_credentials("test-job-p2")
    rendered = render_rayjob_manifest(
        job_id="test-job-p2",
        entrypoint_cmd="python3 train.py",
        user_code="",
        dataset_id="iris",
        ref="main",
        hyperparameters={"lr": 0.01},
        experiment_name="p2-experiment",
        scoped_creds=creds,
        epochs=5,
        target_column="target",
        model_type="random_forest",
    )

    assert "http://sentinelml-mlflow.default.svc.cluster.local:5000" in rendered
    assert "http://sentinelml-minio.default.svc.cluster.local:9000" in rendered


def test_kubernetes_namespace_and_image_configuration(monkeypatch):
    """Verify that KUBERNETES_NAMESPACE and RAY_IMAGE settings are respected across rendered manifests and services."""
    monkeypatch.setattr(settings, "KUBERNETES_NAMESPACE", "custom-mlsecops")
    monkeypatch.setattr(settings, "RAY_IMAGE", "registry.internal/sentinelml-ray:v2.0.0")

    creds = get_scoped_training_credentials("test-job-custom-ns")
    rendered_job = render_rayjob_manifest(
        job_id="test-job-custom-ns",
        entrypoint_cmd="python3 train.py",
        user_code="",
        dataset_id="iris",
        ref="main",
        hyperparameters={"lr": 0.01},
        experiment_name="custom-ns-experiment",
        scoped_creds=creds,
        epochs=5,
        target_column="target",
        model_type="random_forest",
    )

    assert "namespace: custom-mlsecops" in rendered_job
    assert "image: registry.internal/sentinelml-ray:v2.0.0" in rendered_job

    # Verify RayService template rendering
    import os
    template_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "app", "templates", "rayservice_template.yaml",
    )
    with open(template_path, "r", encoding="utf-8") as f:
        template_content = f.read()

    rendered_svc = template_content.replace("{{namespace}}", settings.KUBERNETES_NAMESPACE).replace("{{ray_image}}", settings.RAY_IMAGE)
    assert "namespace: custom-mlsecops" in rendered_svc
    assert "image: registry.internal/sentinelml-ray:v2.0.0" in rendered_svc


def test_lakefs_blockstore_bucket_configuration(monkeypatch):
    """Verify that LAKEFS_BLOCKSTORE_BUCKET is configurable and respected."""
    monkeypatch.setattr(settings, "LAKEFS_BLOCKSTORE_BUCKET", "custom-lakefs-bucket")
    from app.services.dataset.catalog_service import DatasetCatalogService
    from unittest.mock import MagicMock
    from uuid import uuid4

    mock_vc = MagicMock()
    mock_storage = MagicMock()
    mock_repo = MagicMock()
    mock_repo.get_by_name.return_value = None
    mock_db = MagicMock()
    mock_repo.db = mock_db

    catalog = DatasetCatalogService(version_control_service=mock_vc, storage_service=mock_storage, repository=mock_repo)

    created_ds = catalog.register_dataset(mock_db, "test-custom-bucket-ds", "desc", uuid4(), "testuser")
    assert "s3://custom-lakefs-bucket/test-custom-bucket-ds" == mock_vc.create_repository.call_args[0][1]


def test_production_security_validations():
    """Verify production security validation rules enforce non-default secrets and forbid insecure fallbacks."""
    from app.core.config import Settings

    valid_prod_kwargs = {
        "ENVIRONMENT": "production",
        "SECRET_KEY": "a" * 32,
        "MINIO_WEBHOOK_SECRET": "prod-minio-secret-at-least-32-chars-long",
        "LAKEFS_WEBHOOK_SECRET": "prod-lakefs-secret-at-least-32-chars-long",
        "LAKEFS_ENDPOINT": "http://lakefs.prod:8000",
        "LAKEFS_ACCESS_KEY_ID": "prod-key",
        "LAKEFS_SECRET_ACCESS_KEY": "prod-secret",
        "MINIO_ROOT_USER": "prodadmin",
        "MINIO_ROOT_PASSWORD": "StrongProdRootPassword!2026",
        "MINIO_ACCESS_KEY": "prod-minio-app-user",
        "MINIO_SECRET_KEY": "StrongProdAppPassword!2026",
        "POSTGRES_USER": "produser",
        "POSTGRES_PASSWORD": "StrongPostgresPassword!2026",
        "POSTGRES_DB": "sentinelml_prod",
        "POSTGRES_HOST": "postgres.prod",
        "POSTGRES_PORT": 5432,
        "DATABASE_URL": "postgresql://produser:StrongPostgresPassword!2026@postgres.prod:5432/sentinelml_prod",
    }

    # 1. Valid production settings load successfully
    prod_settings = Settings(**valid_prod_kwargs)
    assert prod_settings.ENVIRONMENT == "production"

    # 2. Insecure default SECRET_KEY fails
    with pytest.raises(ValueError, match="SECRET_KEY"):
        bad = dict(valid_prod_kwargs)
        bad["SECRET_KEY"] = "your-super-secure-jwt-secret-key-change-this-in-production"
        Settings(**bad)

    # 3. Default MINIO_ROOT_PASSWORD fails
    with pytest.raises(ValueError, match="MINIO_ROOT_PASSWORD"):
        bad = dict(valid_prod_kwargs)
        bad["MINIO_ROOT_PASSWORD"] = "minioadmin123"
        Settings(**bad)

    # 4. Default database credentials fail
    with pytest.raises(ValueError, match="default database credentials"):
        bad = dict(valid_prod_kwargs)
        bad["DATABASE_URL"] = "postgresql://sentinelml:sentinelml123@postgres.prod:5432/sentinelml_prod"
        Settings(**bad)

    # 5. Query param auth forbidden in production
    with pytest.raises(ValueError, match="POISONING_ALLOW_QUERY_PARAM_AUTH"):
        bad = dict(valid_prod_kwargs)
        bad["POISONING_ALLOW_QUERY_PARAM_AUTH"] = True
        Settings(**bad)

    # 6. Fallback to SQLite forbidden in production
    with pytest.raises(ValueError, match="fallback to local SQLite is forbidden"):
        bad = dict(valid_prod_kwargs)
        bad["DATABASE_URL"] = None
        bad["POSTGRES_HOST"] = None
        Settings(**bad)

    # 7. Explicit SQLite URLs forbidden in production and prod (D-1)
    for env in ("production", "prod"):
        for sqlite_url in ("sqlite:///./prod.db", "sqlite:///:memory:", "sqlite://", "sqlite:///test.db"):
            with pytest.raises(ValueError, match="fallback to local SQLite is forbidden"):
                bad = dict(valid_prod_kwargs)
                bad["ENVIRONMENT"] = env
                bad["DATABASE_URL"] = sqlite_url
                Settings(**bad)

    # 8. Missing MINIO_ACCESS_KEY or MINIO_SECRET_KEY forbidden in production and prod (D-2)
    for env in ("production", "prod"):
        with pytest.raises(ValueError, match="MINIO_ACCESS_KEY"):
            bad = dict(valid_prod_kwargs)
            bad["ENVIRONMENT"] = env
            bad["MINIO_ACCESS_KEY"] = None
            Settings(**bad)

        with pytest.raises(ValueError, match="MINIO_SECRET_KEY"):
            bad = dict(valid_prod_kwargs)
            bad["ENVIRONMENT"] = env
            bad["MINIO_SECRET_KEY"] = None
            Settings(**bad)

    # 9. In development, omitted MinIO keys and explicit SQLite DATABASE_URL are permitted
    dev_settings = Settings(ENVIRONMENT="development", MINIO_ACCESS_KEY=None, MINIO_SECRET_KEY=None, DATABASE_URL="sqlite:///./dev.db")
    assert dev_settings.ENVIRONMENT == "development"
    assert dev_settings.DATABASE_URL.startswith("sqlite")


def test_derive_storage_deletion_target():
    """Verify derive_storage_deletion_target parses S3 namespaces safely and prevents broad/empty deletions (D-5)."""
    from app.services.dataset.catalog_service import derive_storage_deletion_target

    # 1. Bucket only without prefix defaults to sanitized repo name
    b, p = derive_storage_deletion_target("s3://test-bucket", "repo-alpha", "fallback-bucket")
    assert b == "test-bucket"
    assert p == "repo-alpha/"

    # 2. Bucket with prefix
    b, p = derive_storage_deletion_target("s3://test-bucket/custom_prefix", "repo-alpha", "fallback-bucket")
    assert b == "test-bucket"
    assert p == "custom_prefix/"

    # 3. Bucket with prefix and repository subfolder
    b, p = derive_storage_deletion_target("s3://test-bucket/datasets/repo-alpha", "repo-alpha", "fallback-bucket")
    assert b == "test-bucket"
    assert p == "datasets/repo-alpha/"

    # 4. None or empty storage namespace falls back to default bucket and repo prefix
    b, p = derive_storage_deletion_target(None, "repo-alpha", "lakefs-default")
    assert b == "lakefs-default"
    assert p == "repo-alpha/"

    b, p = derive_storage_deletion_target("", "repo-alpha", "lakefs-default")
    assert b == "lakefs-default"
    assert p == "repo-alpha/"

    # 5. Empty or whitespace-only repository name is rejected
    with pytest.raises(ValueError, match="repository name cannot be empty"):
        derive_storage_deletion_target("s3://test-bucket", "", "fallback-bucket")

    with pytest.raises(ValueError, match="repository name cannot be empty"):
        derive_storage_deletion_target("s3://test-bucket", "   ///   ", "fallback-bucket")

    # 6. S3 URI with no bucket is rejected
    with pytest.raises(ValueError, match="S3 URI has no bucket"):
        derive_storage_deletion_target("s3://", "repo-alpha", "fallback-bucket")

    with pytest.raises(ValueError, match="S3 URI has no bucket"):
        derive_storage_deletion_target("s3:///", "repo-alpha", "fallback-bucket")


def test_lakefs_webhook_dynamic_baseline_branch(monkeypatch):
    """Verify lakeFS pre-merge webhook enforces data validation on the dataset's configured baseline branch (D-3)."""
    from unittest.mock import MagicMock
    from app.db.session import get_db
    from app.services.dataset.lakefs_service import lakefs_service

    # Create an isolated database session and populate test datasets
    db = TestingSession()
    try:
        def override_get_db():
            yield db

        app.dependency_overrides[get_db] = override_get_db

        user = User(
            username=f"webhook_user_{uuid.uuid4().hex[:8]}",
            email=f"webhook_{uuid.uuid4().hex[:8]}@example.com",
            password_hash="hash",
            role="data_scientist",
        )
        db.add(user)
        db.commit()

        # Dataset A: Custom baseline branch "production"
        ds_custom = Dataset(
            name="custom-baseline-dataset",
            description="Dataset with production as baseline",
            storage_namespace="s3://lakefs/custom-baseline-dataset",
            default_branch="production",
            created_by_id=user.id,
        )
        # Dataset B: Default baseline branch "main"
        ds_default = Dataset(
            name="default-baseline-dataset",
            description="Dataset with default main baseline",
            storage_namespace="s3://lakefs/default-baseline-dataset",
            default_branch="main",
            created_by_id=user.id,
        )
        db.add_all([ds_custom, ds_default])
        db.commit()

        # Mock lakefs_service.list_files to return empty list (no candidate files)
        monkeypatch.setattr(lakefs_service, "list_files", MagicMock(return_value=[]))
        headers = {"X-Webhook-Secret": settings.LAKEFS_WEBHOOK_SECRET}

        # 1. ds_custom targeting "main" (not its baseline): MUST BE SKIPPED
        payload_main = {
            "event_type": "pre-merge",
            "repository_id": "custom-baseline-dataset",
            "branch_id": "main",
            "source_ref": "feature-branch",
        }
        res = client.post("/api/webhooks/lakefs/data-validation", json=payload_main, headers=headers)
        assert res.status_code == 200
        assert res.json() == {"status": "skipped", "reason": "not_baseline_branch_main"}

        # 2. ds_custom targeting "production" (its baseline): MUST PROCEED TO VALIDATION
        payload_prod = {
            "event_type": "pre-merge",
            "repository_id": "custom-baseline-dataset",
            "branch_id": "production",
            "source_ref": "feature-branch",
        }
        res = client.post("/api/webhooks/lakefs/data-validation", json=payload_prod, headers=headers)
        assert res.status_code == 200
        # Proceeded past baseline branch check and was evaluated by validation gate (no candidate files)
        assert res.json() == {"status": "approved", "reason": "no_data_files"}

        # 3. ds_default targeting "production" (not its baseline): MUST BE SKIPPED
        payload_default_prod = {
            "event_type": "pre-merge",
            "repository_id": "default-baseline-dataset",
            "branch_id": "production",
            "source_ref": "feature-branch",
        }
        res = client.post("/api/webhooks/lakefs/data-validation", json=payload_default_prod, headers=headers)
        assert res.status_code == 200
        assert res.json() == {"status": "skipped", "reason": "not_baseline_branch_production"}

        # 4. ds_default targeting "main" (its baseline): MUST PROCEED TO VALIDATION
        payload_default_main = {
            "event_type": "pre-merge",
            "repository_id": "default-baseline-dataset",
            "branch_id": "main",
            "source_ref": "feature-branch",
        }
        res = client.post("/api/webhooks/lakefs/data-validation", json=payload_default_main, headers=headers)
        assert res.status_code == 200
        assert res.json() == {"status": "approved", "reason": "no_data_files"}

    finally:
        app.dependency_overrides.pop(get_db, None)
        db.close()
