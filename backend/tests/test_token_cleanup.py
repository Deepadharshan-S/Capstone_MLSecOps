import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import SessionLocal
from app.main import app, lifespan
from app.models.blacklisted_token import BlacklistedToken
from app.models.refresh_token import RefreshToken
from app.models.user import User
from app.repositories.token_repository import (
    BlacklistedTokenRepository,
    RefreshTokenRepository,
)
from app.services.auth.token_cleanup import (
    run_token_cleanup_once,
    token_cleanup_loop,
)

client = TestClient(app)


# ---------------------------------------------------------------------------
# Requirement 1 & 2: Expired tokens deleted, non-expired tokens remain
# ---------------------------------------------------------------------------

def test_expired_tokens_deleted_and_active_tokens_remain():
    """
    Verifies that run_token_cleanup_once deletes expired refresh and blacklisted
    tokens while leaving non-expired tokens untouched in PostgreSQL.
    """
    db: Session = SessionLocal()
    try:
        # Find or create a test user
        user = db.query(User).filter(User.username == "admin_user").first()
        assert user is not None, "admin_user must exist in database"

        now = datetime.now(timezone.utc)
        expired_refresh_hash = f"exp-ref-{uuid.uuid4().hex}"
        active_refresh_hash = f"act-ref-{uuid.uuid4().hex}"
        expired_blacklist_jti = f"exp-bl-{uuid.uuid4().hex}"
        active_blacklist_jti = f"act-bl-{uuid.uuid4().hex}"

        # 1. Seed tokens
        exp_ref = RefreshToken(
            user_id=user.id,
            token_hash=expired_refresh_hash,
            expires_at=now - timedelta(hours=2),
            is_revoked=False,
        )
        act_ref = RefreshToken(
            user_id=user.id,
            token_hash=active_refresh_hash,
            expires_at=now + timedelta(hours=24),
            is_revoked=False,
        )
        exp_bl = BlacklistedToken(
            jti=expired_blacklist_jti,
            expires_at=now - timedelta(hours=2),
        )
        act_bl = BlacklistedToken(
            jti=active_blacklist_jti,
            expires_at=now + timedelta(hours=24),
        )

        db.add_all([exp_ref, act_ref, exp_bl, act_bl])
        db.commit()

        # 2. Execute single cleanup iteration
        result = run_token_cleanup_once(db_session_factory=SessionLocal)

        assert result["deleted_refresh_tokens"] >= 1
        assert result["deleted_blacklisted_tokens"] >= 1
        assert result["total_deleted"] >= 2

        # 3. Assert expired tokens are deleted
        ref_repo = RefreshTokenRepository(db)
        bl_repo = BlacklistedTokenRepository(db)

        assert ref_repo.get_by_jti(expired_refresh_hash) is None
        assert bl_repo.get_by_jti(expired_blacklist_jti) is None

        # 4. Assert active tokens remain intact
        assert ref_repo.get_by_jti(active_refresh_hash) is not None
        assert bl_repo.get_by_jti(active_blacklist_jti) is not None

        # Clean up active test tokens
        remaining_ref = ref_repo.get_by_jti(active_refresh_hash)
        remaining_bl = bl_repo.get_by_jti(active_blacklist_jti)
        if remaining_ref:
            db.delete(remaining_ref)
        if remaining_bl:
            db.delete(remaining_bl)
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Requirement 3: Background cleanup invokes existing cleanup logic
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_background_cleanup_invokes_cleanup_service():
    """
    Verifies that token_cleanup_loop periodically calls run_token_cleanup_once
    and executes without error.
    """
    invocation_count = 0

    def mock_run_cleanup(db_session_factory=SessionLocal):
        nonlocal invocation_count
        invocation_count += 1
        return {"deleted_refresh_tokens": 0, "deleted_blacklisted_tokens": 0, "total_deleted": 0}

    with patch("app.services.auth.token_cleanup.run_token_cleanup_once", side_effect=mock_run_cleanup):
        # Run with short 0.02s interval
        task = asyncio.create_task(token_cleanup_loop(interval_seconds=0.02))
        await asyncio.sleep(0.08)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert invocation_count >= 2, f"Expected at least 2 cleanup iterations, got {invocation_count}"


# ---------------------------------------------------------------------------
# Requirement 4: Cleanup failure does not crash the loop or FastAPI
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_cleanup_failure_resilience():
    """
    Verifies that when database errors occur during cleanup, the exception is
    logged, resources are cleaned up, and the loop continues running without crashing.
    """
    iteration = 0

    def failing_cleanup(db_session_factory=SessionLocal):
        nonlocal iteration
        iteration += 1
        if iteration == 1:
            raise RuntimeError("Simulated transient PostgreSQL connection failure")
        return {"deleted_refresh_tokens": 0, "deleted_blacklisted_tokens": 0, "total_deleted": 0}

    with patch("app.services.auth.token_cleanup.run_token_cleanup_once", side_effect=failing_cleanup):
        task = asyncio.create_task(token_cleanup_loop(interval_seconds=0.02))
        await asyncio.sleep(0.08)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # The loop should have survived iteration 1's failure and successfully proceeded to iteration 2+
    assert iteration >= 2, f"Loop did not recover from error; total iterations: {iteration}"


# ---------------------------------------------------------------------------
# Requirement 5: Background task shuts down cleanly
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_background_task_clean_shutdown():
    """
    Verifies that cancelling the background task terminates it gracefully
    handling asyncio.CancelledError without leaving unhandled exceptions.
    """
    task = asyncio.create_task(token_cleanup_loop(interval_seconds=10.0))
    await asyncio.sleep(0.02)
    assert not task.done()

    # Cancel the task
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert task.done()


# ---------------------------------------------------------------------------
# Requirement 6: Database sessions are not leaked
# ---------------------------------------------------------------------------

def test_database_sessions_not_leaked():
    """
    Verifies that database sessions are always closed in run_token_cleanup_once,
    both on successful execution and when an unhandled error occurs.
    """
    opened_sessions = []
    closed_sessions = []

    class MockSessionWrapper:
        def __init__(self, real_session: Session):
            self._real = real_session
            opened_sessions.append(self)

        def close(self):
            closed_sessions.append(self)
            return self._real.close()

        def rollback(self):
            return self._real.rollback()

        def commit(self):
            return self._real.commit()

        def __getattr__(self, name):
            return getattr(self._real, name)

    def tracking_factory():
        return MockSessionWrapper(SessionLocal())

    # 1. Normal execution
    run_token_cleanup_once(db_session_factory=tracking_factory)
    assert len(opened_sessions) == 1
    assert len(closed_sessions) == 1

    # 2. Failing execution
    with patch("app.services.auth.token_cleanup.auth_service.cleanup_expired_tokens", side_effect=ValueError("DB fail")):
        with pytest.raises(ValueError):
            run_token_cleanup_once(db_session_factory=tracking_factory)

    assert len(opened_sessions) == 2
    assert len(closed_sessions) == 2, "Session was leaked during exception path!"


# ---------------------------------------------------------------------------
# Requirement 7: FastAPI lifespan integration and manual endpoint parity
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_fastapi_lifespan_lifecycle():
    """
    Verifies that FastAPI lifespan context manager starts the background task
    on startup and cancels it on shutdown.
    """
    async with lifespan(app):
        # Look for the background task in current event loop
        tasks = [t for t in asyncio.all_tasks() if t.get_name() == "sentinelml_token_cleanup"]
        assert len(tasks) == 1, "Lifespan failed to spawn sentinelml_token_cleanup task"
        cleanup_task = tasks[0]
        assert not cleanup_task.done()

    # After exiting lifespan, the task should be terminated
    assert cleanup_task.done()


def test_manual_cleanup_endpoint_still_works_with_rbac(user_tokens):
    """
    Verifies that POST /api/auth/cleanup-tokens remains functional and shares
    identical underlying cleanup logic with the background task.
    """
    admin_headers = {"Authorization": f"Bearer {user_tokens['admin_user']}"}
    viewer_headers = {"Authorization": f"Bearer {user_tokens['viewer_user']}"}

    # 1. Viewer is rejected (403 Forbidden)
    resp_viewer = client.post("/api/auth/cleanup-tokens", headers=viewer_headers)
    assert resp_viewer.status_code == 403

    # 2. Admin succeeds (200 OK)
    resp_admin = client.post("/api/auth/cleanup-tokens", headers=admin_headers)
    assert resp_admin.status_code == 200
    data = resp_admin.json()
    assert "deleted_refresh_tokens" in data
    assert "deleted_blacklisted_tokens" in data
    assert "total_deleted" in data
    assert isinstance(data["total_deleted"], int)
