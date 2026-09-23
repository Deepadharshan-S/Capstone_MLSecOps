import uuid
from datetime import datetime, timezone
import pytest
from unittest.mock import MagicMock
from fastapi.testclient import TestClient

from app.main import app
from app.db.session import SessionLocal
from app.models.training_job import TrainingJob
from app.models.user import User
from app.services.dataset.s3_storage_service import S3StorageService
from app.services.ml_ops.training_service import ModelTrainingService

client = TestClient(app, base_url="https://testserver.local")


@pytest.fixture(scope="module")
def user_tokens():
    """Retrieves OAuth2 access tokens for all test roles."""
    tokens = {}
    for username, password in [
        ("admin_user", "AdminPassword123!"),
        ("mle_user", "MLEngineerPassword123!"),
        ("ds_user", "DataScientist123!"),
        ("viewer_user", "ViewerPassword123!"),
    ]:
        resp = client.post("/api/auth/login", data={"username": username, "password": password})
        assert resp.status_code == 200
        tokens[username] = resp.json()["access_token"]
    return tokens


@pytest.fixture
def test_users():
    """Fetches user records from DB for association in fixtures."""
    users = {}
    with SessionLocal() as db:
        for u in db.query(User).all():
            users[u.username] = u
    return users


@pytest.fixture
def cleanup_test_jobs():
    """Cleans up any created test jobs after test runs."""
    created_job_ids = []
    yield created_job_ids
    if created_job_ids:
        with SessionLocal() as db:
            db.query(TrainingJob).filter(TrainingJob.job_id.in_(created_job_ids)).delete(synchronize_session=False)
            db.commit()


# =========================================================================
# 1. Tests for GET /api/models/jobs
# =========================================================================

def test_list_jobs_empty(user_tokens, test_users, cleanup_test_jobs):
    """Verifies listing jobs when no jobs are present for a user."""
    # Create a unique test user with no jobs
    unique_username = f"ds_empty_{uuid.uuid4().hex[:6]}"
    with SessionLocal() as db:
        new_user = User(
            username=unique_username,
            email=f"{unique_username}@test.com",
            password_hash="testhash",
            role="data_scientist",
        )
        db.add(new_user)
        db.commit()
        new_user_id = new_user.id

    from app.core.security import create_access_token
    token = create_access_token(subject=str(new_user_id))
    resp = client.get("/api/models/jobs", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["jobs"] == []
    assert data["total"] == 0

    # Cleanup user
    with SessionLocal() as db:
        db.query(User).filter(User.id == new_user_id).delete()
        db.commit()


def test_list_jobs_pending(user_tokens, test_users, cleanup_test_jobs):
    """Verifies that a newly created PENDING job appears in /api/models/jobs."""
    job_id = uuid.uuid4().hex[:12]
    cleanup_test_jobs.append(job_id)
    ds_user = test_users["ds_user"]

    with SessionLocal() as db:
        job = TrainingJob(
            job_id=job_id,
            rayjob_name=f"rayjob-{job_id}",
            status="PENDING",
            dataset_id="test-dataset",
            ref="main",
            model_name="test-model",
            created_by_id=ds_user.id,
            created_by_username=ds_user.username,
        )
        db.add(job)
        db.commit()

    resp = client.get("/api/models/jobs", headers={"Authorization": f"Bearer {user_tokens['ds_user']}"})
    assert resp.status_code == 200
    jobs = resp.json()["jobs"]
    matching = [j for j in jobs if j["job_id"] == job_id]
    assert len(matching) == 1
    assert matching[0]["status"] == "PENDING"
    assert matching[0]["dataset_id"] == "test-dataset"


def test_list_jobs_running_reconciliation(user_tokens, test_users, cleanup_test_jobs, monkeypatch):
    """Verifies that active jobs are reconciled to RUNNING when Kubernetes reports Running status."""
    job_id = uuid.uuid4().hex[:12]
    cleanup_test_jobs.append(job_id)
    ds_user = test_users["ds_user"]

    with SessionLocal() as db:
        job = TrainingJob(
            job_id=job_id,
            rayjob_name=f"rayjob-{job_id}",
            status="PENDING",
            dataset_id="test-dataset",
            ref="main",
            model_name="test-model",
            created_by_id=ds_user.id,
            created_by_username=ds_user.username,
        )
        db.add(job)
        db.commit()

    # Mock Kubernetes CustomObjectsApi returning jobStatus="RUNNING"
    mock_custom = MagicMock()
    mock_custom.list_namespaced_custom_object.return_value = {
        "items": [
            {
                "metadata": {"name": f"rayjob-{job_id}"},
                "status": {
                    "jobStatus": "RUNNING",
                    "startTime": "2026-09-21T09:00:00Z",
                },
            }
        ]
    }
    mock_core = MagicMock()
    monkeypatch.setattr(ModelTrainingService, "_get_k8s_apis", lambda self: (mock_custom, mock_core))

    resp = client.get("/api/models/jobs", headers={"Authorization": f"Bearer {user_tokens['ds_user']}"})
    assert resp.status_code == 200
    jobs = resp.json()["jobs"]
    matching = [j for j in jobs if j["job_id"] == job_id]
    assert len(matching) == 1
    assert matching[0]["status"] == "RUNNING"
    assert matching[0]["started_at"] is not None

    # Verify updated in DB as well
    with SessionLocal() as db:
        db_job = db.query(TrainingJob).filter(TrainingJob.job_id == job_id).first()
        assert db_job.status == "RUNNING"


def test_list_jobs_succeeded_and_archived(user_tokens, test_users, cleanup_test_jobs, monkeypatch):
    """Verifies reconciliation transitions an active job to SUCCEEDED and triggers pod log archival."""
    job_id = uuid.uuid4().hex[:12]
    cleanup_test_jobs.append(job_id)
    ds_user = test_users["ds_user"]

    with SessionLocal() as db:
        job = TrainingJob(
            job_id=job_id,
            rayjob_name=f"rayjob-{job_id}",
            status="RUNNING",
            dataset_id="test-dataset",
            ref="main",
            model_name="test-model",
            created_by_id=ds_user.id,
            created_by_username=ds_user.username,
            started_at=datetime.now(timezone.utc),
        )
        db.add(job)
        db.commit()

    # Mock Kubernetes returning SUCCEEDED status and a head pod
    mock_custom = MagicMock()
    mock_custom.list_namespaced_custom_object.return_value = {
        "items": [
            {
                "metadata": {"name": f"rayjob-{job_id}"},
                "status": {
                    "jobStatus": "SUCCEEDED",
                    "startTime": "2026-09-21T09:00:00Z",
                    "endTime": "2026-09-21T09:01:00Z",
                },
            }
        ]
    }
    mock_core = MagicMock()
    mock_pod = MagicMock()
    mock_pod.metadata.name = f"rayjob-{job_id}-head-abc"
    mock_core.list_namespaced_pod.return_value.items = [mock_pod]
    mock_core.read_namespaced_pod_log.return_value = "Epoch 1/1 - loss: 0.12 - acc: 0.98\nTraining completed."
    monkeypatch.setattr(ModelTrainingService, "_get_k8s_apis", lambda self: (mock_custom, mock_core))

    # Mock S3 storage put_log_content
    archived_logs = {}
    monkeypatch.setattr(S3StorageService, "get_log_content", lambda self, b, k: archived_logs.get(k))
    monkeypatch.setattr(S3StorageService, "put_log_content", lambda self, b, k, content: archived_logs.update({k: content}))

    resp = client.get("/api/models/jobs", headers={"Authorization": f"Bearer {user_tokens['ds_user']}"})
    assert resp.status_code == 200
    jobs = resp.json()["jobs"]
    matching = [j for j in jobs if j["job_id"] == job_id]
    assert len(matching) == 1
    assert matching[0]["status"] == "SUCCEEDED"
    assert matching[0]["completed_at"] is not None

    # Verify log was archived to MinIO
    assert f"logs/{job_id}/training.log" in archived_logs
    assert "Training completed." in archived_logs[f"logs/{job_id}/training.log"]


def test_list_jobs_failed(user_tokens, test_users, cleanup_test_jobs, monkeypatch):
    """Verifies reconciliation transitions an active job to FAILED when reported by Kubernetes."""
    job_id = uuid.uuid4().hex[:12]
    cleanup_test_jobs.append(job_id)
    ds_user = test_users["ds_user"]

    with SessionLocal() as db:
        job = TrainingJob(
            job_id=job_id,
            rayjob_name=f"rayjob-{job_id}",
            status="RUNNING",
            dataset_id="test-dataset",
            ref="main",
            model_name="test-model",
            created_by_id=ds_user.id,
            created_by_username=ds_user.username,
        )
        db.add(job)
        db.commit()

    mock_custom = MagicMock()
    mock_custom.list_namespaced_custom_object.return_value = {
        "items": [
            {
                "metadata": {"name": f"rayjob-{job_id}"},
                "status": {
                    "jobStatus": "FAILED",
                    "message": "Out of memory in Ray worker actor",
                },
            }
        ]
    }
    mock_core = MagicMock()
    mock_core.list_namespaced_pod.return_value.items = []
    monkeypatch.setattr(ModelTrainingService, "_get_k8s_apis", lambda self: (mock_custom, mock_core))

    resp = client.get("/api/models/jobs", headers={"Authorization": f"Bearer {user_tokens['ds_user']}"})
    assert resp.status_code == 200
    jobs = resp.json()["jobs"]
    matching = [j for j in jobs if j["job_id"] == job_id]
    assert len(matching) == 1
    assert matching[0]["status"] == "FAILED"
    assert "Out of memory" in matching[0]["error_message"]


def test_list_jobs_historical_after_k8s_deletion(user_tokens, test_users, cleanup_test_jobs, monkeypatch):
    """
    CRITICAL: Verifies that after a RayJob has completed and its K8s CRD/pods are
    permanently removed by TTL, it remains 100% visible in GET /api/models/jobs from PostgreSQL.
    """
    job_id = uuid.uuid4().hex[:12]
    cleanup_test_jobs.append(job_id)
    ds_user = test_users["ds_user"]

    with SessionLocal() as db:
        job = TrainingJob(
            job_id=job_id,
            rayjob_name=f"rayjob-{job_id}",
            status="SUCCEEDED",
            dataset_id="historical-dataset",
            ref="main",
            model_name="historical-model",
            duration_seconds=42.5,
            created_by_id=ds_user.id,
            created_by_username=ds_user.username,
            completed_at=datetime.now(timezone.utc),
        )
        db.add(job)
        db.commit()

    # Kubernetes returns NO items (cluster has 0 RayJobs)
    mock_custom = MagicMock()
    mock_custom.list_namespaced_custom_object.return_value = {"items": []}
    mock_core = MagicMock()
    monkeypatch.setattr(ModelTrainingService, "_get_k8s_apis", lambda self: (mock_custom, mock_core))

    resp = client.get("/api/models/jobs", headers={"Authorization": f"Bearer {user_tokens['ds_user']}"})
    assert resp.status_code == 200
    jobs = resp.json()["jobs"]
    matching = [j for j in jobs if j["job_id"] == job_id]
    assert len(matching) == 1
    assert matching[0]["status"] == "SUCCEEDED"
    assert matching[0]["dataset_id"] == "historical-dataset"
    assert matching[0]["duration_seconds"] == 42.5


# =========================================================================
# 2. Tests for GET /api/models/jobs/{job_id}
# =========================================================================

def test_get_job_detail_active(user_tokens, test_users, cleanup_test_jobs, monkeypatch):
    """Verifies inspecting an active job blends DB metadata with live Kubernetes head pod status."""
    job_id = uuid.uuid4().hex[:12]
    cleanup_test_jobs.append(job_id)
    ds_user = test_users["ds_user"]

    with SessionLocal() as db:
        job = TrainingJob(
            job_id=job_id,
            rayjob_name=f"rayjob-{job_id}",
            status="RUNNING",
            dataset_id="live-dataset",
            ref="feature-branch",
            model_name="live-model",
            epochs=20,
            hyperparameters={"lr": 0.005},
            created_by_id=ds_user.id,
            created_by_username=ds_user.username,
        )
        db.add(job)
        db.commit()

    mock_custom = MagicMock()
    mock_custom.get_namespaced_custom_object.return_value = {
        "status": {
            "jobStatus": "RUNNING",
            "jobDeploymentStatus": "Running",
        }
    }
    mock_core = MagicMock()
    mock_pod = MagicMock()
    mock_pod.metadata.name = f"rayjob-{job_id}-head-pod-xyz"
    mock_pod.status.phase = "Running"
    mock_core.list_namespaced_pod.return_value.items = [mock_pod]
    monkeypatch.setattr(ModelTrainingService, "_get_k8s_apis", lambda self: (mock_custom, mock_core))

    resp = client.get(f"/api/models/jobs/{job_id}", headers={"Authorization": f"Bearer {user_tokens['ds_user']}"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["job_id"] == job_id
    assert data["status"] == "RUNNING"
    assert data["head_pod_name"] == f"rayjob-{job_id}-head-pod-xyz"
    assert data["head_pod_status"] == "Running"
    assert data["hyperparameters"] == {"lr": 0.005}


def test_get_job_detail_completed_without_k8s(user_tokens, test_users, cleanup_test_jobs):
    """
    CRITICAL: Verifies that GET /api/models/jobs/{job_id} works seamlessly
    even after RayJob, RayCluster, and pods are 100% deleted from Kubernetes.
    """
    job_id = uuid.uuid4().hex[:12]
    cleanup_test_jobs.append(job_id)
    ds_user = test_users["ds_user"]

    with SessionLocal() as db:
        job = TrainingJob(
            job_id=job_id,
            rayjob_name=f"rayjob-{job_id}",
            status="SUCCEEDED",
            dataset_id="done-dataset",
            ref="v1.0",
            model_name="done-model",
            epochs=5,
            duration_seconds=18.3,
            created_by_id=ds_user.id,
            created_by_username=ds_user.username,
            completed_at=datetime.now(timezone.utc),
        )
        db.add(job)
        db.commit()

    resp = client.get(f"/api/models/jobs/{job_id}", headers={"Authorization": f"Bearer {user_tokens['ds_user']}"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["job_id"] == job_id
    assert data["status"] == "SUCCEEDED"
    assert data["duration_seconds"] == 18.3
    # Kubernetes was not called for this completed job
    assert data["head_pod_name"] is None


def test_get_job_detail_failed(user_tokens, test_users, cleanup_test_jobs):
    """Verifies inspecting a failed job returns the failure error message."""
    job_id = uuid.uuid4().hex[:12]
    cleanup_test_jobs.append(job_id)
    ds_user = test_users["ds_user"]

    with SessionLocal() as db:
        job = TrainingJob(
            job_id=job_id,
            rayjob_name=f"rayjob-{job_id}",
            status="FAILED",
            dataset_id="failed-dataset",
            ref="main",
            error_message="ValueError: Invalid label column in dataset",
            created_by_id=ds_user.id,
            created_by_username=ds_user.username,
        )
        db.add(job)
        db.commit()

    resp = client.get(f"/api/models/jobs/{job_id}", headers={"Authorization": f"Bearer {user_tokens['ds_user']}"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "FAILED"
    assert "ValueError: Invalid label column" in data["error_message"]


def test_get_job_detail_unknown(user_tokens):
    """Verifies that querying a nonexistent job returns 404 Not Found."""
    resp = client.get("/api/models/jobs/nonexistent-job-99999", headers={"Authorization": f"Bearer {user_tokens['ds_user']}"})
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"].lower()


def test_get_job_detail_k8s_unavailable(user_tokens, test_users, cleanup_test_jobs, monkeypatch):
    """Verifies that if Kubernetes is unreachable for an active job, it is NOT erroneously marked as FAILED."""
    job_id = uuid.uuid4().hex[:12]
    cleanup_test_jobs.append(job_id)
    ds_user = test_users["ds_user"]

    with SessionLocal() as db:
        job = TrainingJob(
            job_id=job_id,
            rayjob_name=f"rayjob-{job_id}",
            status="PENDING",
            dataset_id="offline-dataset",
            ref="main",
            created_by_id=ds_user.id,
            created_by_username=ds_user.username,
        )
        db.add(job)
        db.commit()

    mock_custom = MagicMock()
    mock_custom.get_namespaced_custom_object.side_effect = Exception("Connection refused to k8s API")
    monkeypatch.setattr(ModelTrainingService, "_get_k8s_apis", lambda self: (mock_custom, MagicMock()))

    resp = client.get(f"/api/models/jobs/{job_id}", headers={"Authorization": f"Bearer {user_tokens['ds_user']}"})
    assert resp.status_code == 200
    data = resp.json()
    # Must remain PENDING in DB, not marked as FAILED
    assert data["status"] == "PENDING"


# =========================================================================
# 3. Tests for GET /api/models/jobs/{job_id}/logs
# =========================================================================

def test_get_logs_active_job_from_head_pod(user_tokens, test_users, cleanup_test_jobs, monkeypatch):
    """Verifies retrieving live logs through Kubernetes API for an active job."""
    job_id = uuid.uuid4().hex[:12]
    cleanup_test_jobs.append(job_id)
    ds_user = test_users["ds_user"]

    with SessionLocal() as db:
        job = TrainingJob(
            job_id=job_id,
            rayjob_name=f"rayjob-{job_id}",
            status="RUNNING",
            dataset_id="log-dataset",
            ref="main",
            created_by_id=ds_user.id,
            created_by_username=ds_user.username,
        )
        db.add(job)
        db.commit()

    mock_core = MagicMock()
    mock_pod = MagicMock()
    mock_pod.metadata.name = f"rayjob-{job_id}-head"
    mock_core.list_namespaced_pod.return_value.items = [mock_pod]
    mock_core.read_namespaced_pod_log.return_value = "Starting Ray runtime...\nEpoch 1: loss 0.45\nEpoch 2: loss 0.32"
    monkeypatch.setattr(ModelTrainingService, "_get_k8s_apis", lambda self: (MagicMock(), mock_core))

    resp = client.get(f"/api/models/jobs/{job_id}/logs", headers={"Authorization": f"Bearer {user_tokens['ds_user']}"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["job_id"] == job_id
    assert data["source"] == "kubernetes"
    assert "Epoch 2: loss 0.32" in data["logs"]
    assert data["lines_count"] == 3


def test_get_logs_completed_job_from_minio(user_tokens, test_users, cleanup_test_jobs, monkeypatch):
    """Verifies retrieving archived logs from MinIO for completed jobs."""
    job_id = uuid.uuid4().hex[:12]
    cleanup_test_jobs.append(job_id)
    ds_user = test_users["ds_user"]

    with SessionLocal() as db:
        job = TrainingJob(
            job_id=job_id,
            rayjob_name=f"rayjob-{job_id}",
            status="SUCCEEDED",
            dataset_id="archived-dataset",
            ref="main",
            created_by_id=ds_user.id,
            created_by_username=ds_user.username,
        )
        db.add(job)
        db.commit()

    # Mock MinIO log content
    archived_content = "Dataset downloaded from lakeFS.\nModel training complete.\nMetrics logged to MLflow.\nExit code: 0"
    monkeypatch.setattr(S3StorageService, "get_log_content", lambda self, b, k: archived_content if k == f"logs/{job_id}/training.log" else None)

    resp = client.get(f"/api/models/jobs/{job_id}/logs", headers={"Authorization": f"Bearer {user_tokens['ds_user']}"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["source"] == "minio"
    assert "Metrics logged to MLflow." in data["logs"]


def test_get_logs_tail_lines(user_tokens, test_users, cleanup_test_jobs, monkeypatch):
    """Verifies that ?tail_lines query parameter limits the returned log lines."""
    job_id = uuid.uuid4().hex[:12]
    cleanup_test_jobs.append(job_id)
    ds_user = test_users["ds_user"]

    with SessionLocal() as db:
        job = TrainingJob(
            job_id=job_id,
            rayjob_name=f"rayjob-{job_id}",
            status="SUCCEEDED",
            dataset_id="tail-dataset",
            ref="main",
            created_by_id=ds_user.id,
            created_by_username=ds_user.username,
        )
        db.add(job)
        db.commit()

    long_logs = "\n".join([f"Line {i}" for i in range(1, 101)])
    monkeypatch.setattr(S3StorageService, "get_log_content", lambda self, b, k: long_logs)

    resp = client.get(f"/api/models/jobs/{job_id}/logs?tail_lines=5", headers={"Authorization": f"Bearer {user_tokens['ds_user']}"})
    assert resp.status_code == 200
    data = resp.json()
    lines = data["logs"].splitlines()
    assert len(lines) == 5
    assert lines[-1] == "Line 100"
    assert lines[0] == "Line 96"


def test_get_logs_deleted_head_pod_missing_minio(user_tokens, test_users, cleanup_test_jobs, monkeypatch):
    """Verifies clean message when head pod is deleted and MinIO log is unavailable."""
    job_id = uuid.uuid4().hex[:12]
    cleanup_test_jobs.append(job_id)
    ds_user = test_users["ds_user"]

    with SessionLocal() as db:
        job = TrainingJob(
            job_id=job_id,
            rayjob_name=f"rayjob-{job_id}",
            status="FAILED",
            dataset_id="unavail-dataset",
            ref="main",
            error_message="Pod was terminated prematurely",
            created_by_id=ds_user.id,
            created_by_username=ds_user.username,
        )
        db.add(job)
        db.commit()

    monkeypatch.setattr(S3StorageService, "get_log_content", lambda self, b, k: None)
    mock_core = MagicMock()
    mock_core.list_namespaced_pod.return_value.items = []
    monkeypatch.setattr(ModelTrainingService, "_get_k8s_apis", lambda self: (MagicMock(), mock_core))

    resp = client.get(f"/api/models/jobs/{job_id}/logs", headers={"Authorization": f"Bearer {user_tokens['ds_user']}"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["source"] == "unavailable"
    assert "Pod was terminated prematurely" in data["logs"]


def test_get_logs_unknown_job(user_tokens):
    """Verifies 404 for requesting logs of an unknown job."""
    resp = client.get("/api/models/jobs/unknown-job-8888/logs", headers={"Authorization": f"Bearer {user_tokens['ds_user']}"})
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"].lower()


# =========================================================================
# 4. Security & RBAC tests
# =========================================================================

def test_rbac_unauthenticated_requests():
    """Verifies unauthenticated requests receive 401 Unauthorized."""
    assert client.get("/api/models/jobs").status_code == 401
    assert client.get("/api/models/jobs/test-id").status_code == 401
    assert client.get("/api/models/jobs/test-id/logs").status_code == 401


def test_rbac_job_isolation_and_admin_override(user_tokens, test_users, cleanup_test_jobs):
    """
    Verifies that a Data Scientist cannot access another Data Scientist's job or logs (403),
    while an Admin or ML Engineer or Viewer can view all platform jobs.
    """
    job_id = uuid.uuid4().hex[:12]
    cleanup_test_jobs.append(job_id)
    admin_user = test_users["admin_user"]

    # Job created by admin_user
    with SessionLocal() as db:
        job = TrainingJob(
            job_id=job_id,
            rayjob_name=f"rayjob-{job_id}",
            status="SUCCEEDED",
            dataset_id="admin-dataset",
            ref="main",
            created_by_id=admin_user.id,
            created_by_username=admin_user.username,
        )
        db.add(job)
        db.commit()

    # ds_user attempts to access admin_user's private job detail -> 403 Forbidden
    resp = client.get(f"/api/models/jobs/{job_id}", headers={"Authorization": f"Bearer {user_tokens['ds_user']}"})
    assert resp.status_code == 403
    assert "access denied" in resp.json()["detail"].lower()

    # ds_user attempts to access admin_user's logs -> 403 Forbidden
    resp_logs = client.get(f"/api/models/jobs/{job_id}/logs", headers={"Authorization": f"Bearer {user_tokens['ds_user']}"})
    assert resp_logs.status_code == 403
    assert "access denied" in resp_logs.json()["detail"].lower()

    # admin_user accesses -> 200 OK
    resp_admin = client.get(f"/api/models/jobs/{job_id}", headers={"Authorization": f"Bearer {user_tokens['admin_user']}"})
    assert resp_admin.status_code == 200

    # mle_user (ML Engineer) accesses -> 200 OK
    resp_mle = client.get(f"/api/models/jobs/{job_id}", headers={"Authorization": f"Bearer {user_tokens['mle_user']}"})
    assert resp_mle.status_code == 200

    # viewer_user (Auditor/Viewer) accesses -> 200 OK
    resp_viewer = client.get(f"/api/models/jobs/{job_id}", headers={"Authorization": f"Bearer {user_tokens['viewer_user']}"})
    assert resp_viewer.status_code == 200


# =========================================================================
# 5. Layer-Specific Modular Unit Tests
# =========================================================================

def test_repository_isolated_crud(test_users, cleanup_test_jobs):
    """Verifies TrainingJobRepository operations without API or external dependencies."""
    from app.repositories import TrainingJobRepository
    job_id = uuid.uuid4().hex[:12]
    cleanup_test_jobs.append(job_id)
    ds_user = test_users["ds_user"]

    with SessionLocal() as db:
        repo = TrainingJobRepository(db)
        # Create
        job = TrainingJob(
            job_id=job_id,
            rayjob_name=f"rayjob-{job_id}",
            status="PENDING",
            dataset_id="repo-dataset",
            ref="main",
            created_by_id=ds_user.id,
            created_by_username=ds_user.username,
        )
        saved = repo.create(job)
        assert saved.id is not None

        # Get by exact job_id and by prefixed rayjob_name
        found1 = repo.get_by_job_id(job_id)
        found2 = repo.get_by_job_id(f"rayjob-{job_id}")
        assert found1 is not None and found2 is not None
        assert found1.job_id == job_id

        # List active
        active = repo.list_active()
        assert any(j.job_id == job_id for j in active)

        # List filtered by user
        user_jobs = repo.list(user_id=ds_user.id)
        assert any(j.job_id == job_id for j in user_jobs)

        # Delete
        assert repo.delete_by_job_id(job_id) is True
        assert repo.get_by_job_id(job_id) is None


def test_rayjob_service_status_normalization():
    """Verifies RayJobService.normalize_status logic across various KubeRay CR states."""
    from app.services.ml_ops.rayjob_service import RayJobService
    assert RayJobService.normalize_status("JobStatusSucceeded", "Complete") == "SUCCEEDED"
    assert RayJobService.normalize_status("JobStatusFailed", "Failed") == "FAILED"
    assert RayJobService.normalize_status("JobStatusRunning", "Running") == "RUNNING"
    assert RayJobService.normalize_status("JobStatusPending", "Initializing") == "PENDING"
    assert RayJobService.normalize_status(None, None) == "PENDING"


def test_rayjob_service_mocked_k8s_client():
    """Verifies RayJobService calls custom_api and core_api cleanly."""
    from app.services.ml_ops.rayjob_service import RayJobService

    mock_custom = MagicMock()
    mock_custom.list_namespaced_custom_object.return_value = {
        "items": [{"metadata": {"name": "test-job"}, "status": {"jobStatus": "RUNNING"}}]
    }
    mock_core = MagicMock()
    mock_pod = MagicMock()
    mock_pod.metadata.name = "rayjob-head-123"
    mock_pod.status.phase = "Running"
    mock_core.list_namespaced_pod.return_value.items = [mock_pod]
    mock_core.read_namespaced_pod_log.return_value = "Epoch 1/10 loss: 0.25"

    svc = RayJobService(custom_api=mock_custom, core_api=mock_core)

    # 1. list_rayjobs
    items = svc.list_rayjobs()
    assert len(items) == 1

    # 2. get_head_pod
    pod_info = svc.get_head_pod("test-job")
    assert pod_info == ("rayjob-head-123", "Running")

    # 3. get_head_pod_logs
    logs = svc.get_head_pod_logs("rayjob-head-123", tail_lines=50)
    assert "Epoch 1/10" in logs

    # 4. Graceful error handling on offline cluster
    mock_custom.list_namespaced_custom_object.side_effect = Exception("K8s API down")
    assert svc.list_rayjobs() == []


def test_training_log_service_routing():
    """Verifies TrainingLogService correctly routes between live Kubernetes and MinIO storage."""
    from app.services.ml_ops.training_log_service import TrainingLogService

    mock_storage = MagicMock()
    mock_rayjob = MagicMock()

    log_svc = TrainingLogService(storage_service=mock_storage, rayjob_service=mock_rayjob)

    # Case 1: Active job with running head pod -> routes to Kubernetes
    mock_rayjob.get_head_pod.return_value = ("head-pod-1", "Running")
    mock_rayjob.get_head_pod_logs.return_value = "Live cluster training log line 1\nLine 2"
    res1 = log_svc.get_logs("job-1", "rayjob-job-1", status="RUNNING")
    assert res1["source"] == "kubernetes"
    assert "Live cluster" in res1["logs"]
    assert res1["lines_count"] == 2

    # Case 2: Completed job -> routes to MinIO persistent archive
    mock_storage.get_log_content.return_value = "Archived S3 log line 1\nLine 2\nLine 3"
    res2 = log_svc.get_logs("job-2", "rayjob-job-2", status="SUCCEEDED", tail_lines=2)
    assert res2["source"] == "minio"
    assert res2["lines_count"] == 2
    assert "Line 3" in res2["logs"]

    # Case 3: Pod gone and MinIO log absent -> returns unavailable
    mock_storage.get_log_content.return_value = None
    mock_rayjob.get_head_pod.return_value = None
    res3 = log_svc.get_logs("job-3", "rayjob-job-3", status="FAILED", error_message="Pod evicted")
    assert res3["source"] == "unavailable"
    assert "Pod evicted" in res3["logs"]

