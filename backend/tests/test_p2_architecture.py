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
