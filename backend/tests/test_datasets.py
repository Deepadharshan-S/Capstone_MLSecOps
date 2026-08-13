import socket

# Patch name resolution for mlsecops-minio when running on the host machine
_original_getaddrinfo = socket.getaddrinfo
def _patched_getaddrinfo(host, port, *args, **kwargs):
    if host == "mlsecops-minio":
        try:
            return _original_getaddrinfo(host, port, *args, **kwargs)
        except socket.gaierror:
            host = "127.0.0.1"
    return _original_getaddrinfo(host, port, *args, **kwargs)
socket.getaddrinfo = _patched_getaddrinfo

import uuid
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi import Depends

from app.main import app
from app.db.session import get_db
from app.db.base import Base
from app.models.user import User
from app.models.dataset import Dataset
from app.core.rate_limiter import RateLimiter
from app.core.logging_config import get_all_audit_logs

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


@pytest.fixture(scope="module")
def user_tokens():
    """
    Retrieves OAuth2 access tokens for different test users.
    """
    tokens = {}
    for username, password in [
        ("admin_user", "AdminPassword123!"),
        ("ds_user", "DataScientist123!"),
        ("viewer_user", "ViewerPassword123!"),
    ]:
        login_data = {"username": username, "password": password}
        resp = client.post("/api/auth/login", data=login_data)
        assert resp.status_code == 200
        tokens[username] = resp.json()["access_token"]
    return tokens


def test_dataset_lifecycle(user_tokens):
    # Use a unique dataset name to prevent conflicts in lakeFS
    dataset_name = f"test-dataset-{uuid.uuid4().hex[:8]}"
    ds_headers = {"Authorization": f"Bearer {user_tokens['ds_user']}"}
    admin_headers = {"Authorization": f"Bearer {user_tokens['admin_user']}"}
    viewer_headers = {"Authorization": f"Bearer {user_tokens['viewer_user']}"}

    # 1. Dataset Registration (RBAC: ds_user/admin can, viewer cannot)
    reg_payload = {
        "name": dataset_name,
        "description": "A classification dataset for integration testing"
    }

    dummy_file = ("data.csv", b"feature1,feature2,label\n5.1,3.5,0\n4.9,3.0,0")

    # Viewer should be blocked (403)
    resp = client.post("/api/datasets", data=reg_payload, files={"file": dummy_file}, headers=viewer_headers)
    assert resp.status_code == 403

    # Data Scientist should succeed (201)
    resp = client.post("/api/datasets", data=reg_payload, files={"file": dummy_file}, headers=ds_headers)
    assert resp.status_code == 201
    dataset_data = resp.json()
    assert dataset_data["name"] == dataset_name
    assert dataset_data["description"] == reg_payload["description"]
    assert "storage_namespace" in dataset_data

    # 2. List datasets
    # Viewer should be blocked (403)
    list_resp = client.get("/api/datasets", headers=viewer_headers)
    assert list_resp.status_code == 403

    # Data Scientist should succeed (200)
    list_resp = client.get("/api/datasets", headers=ds_headers)
    assert list_resp.status_code == 200
    dataset_names = [d["name"] for d in list_resp.json()]
    assert dataset_name in dataset_names

    # 3. Data upload (RBAC: ds_user/admin can, viewer cannot)
    file_content = b"feature1,feature2,label\n5.1,3.5,0\n4.9,3.0,0"
    file_path = "data.csv"

    # Viewer blocked from upload
    resp = client.post(
        f"/api/datasets/{dataset_name}/upload?branch=main",
        files={"file": ("data.csv", file_content)},
        headers=viewer_headers
    )
    assert resp.status_code == 403

    # Data Scientist uploads file
    resp = client.post(
        f"/api/datasets/{dataset_name}/upload?branch=main",
        files={"file": ("data.csv", file_content)},
        headers=ds_headers
    )
    assert resp.status_code == 200
    assert resp.json()["path"] == file_path

    # 4. Commit data (RBAC: ds_user/admin can, viewer cannot)
    commit_payload = {
        "message": "Initial dataset commit",
        "metadata": {"author": "ds_user", "framework": "pytest"}
    }

    # Viewer blocked
    resp = client.post(
        f"/api/datasets/{dataset_name}/commit?branch=main",
        json=commit_payload,
        headers=viewer_headers
    )
    assert resp.status_code == 403

    # DS commits
    resp = client.post(
        f"/api/datasets/{dataset_name}/commit?branch=main",
        json=commit_payload,
        headers=ds_headers
    )
    assert resp.status_code == 200
    commit1 = resp.json()
    assert commit1["message"] == commit_payload["message"]
    assert commit1["metadata"]["framework"] == "pytest"
    commit1_id = commit1["id"]

    # 5. Create branch & List branches
    branch_payload = {
        "name": "experiment-v1",
        "source_branch": "main"
    }

    # DS creates branch
    resp = client.post(
        f"/api/datasets/{dataset_name}/branches",
        json=branch_payload,
        headers=ds_headers
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "experiment-v1"

    # List branches: viewer should be blocked
    resp = client.get(f"/api/datasets/{dataset_name}/branches", headers=viewer_headers)
    assert resp.status_code == 403

    # DS List branches
    resp = client.get(f"/api/datasets/{dataset_name}/branches", headers=ds_headers)
    assert resp.status_code == 200
    branches = [b["name"] for b in resp.json()]
    assert "main" in branches
    assert "experiment-v1" in branches

    # 6. Upload new file on branch and commit to compare dataset versions
    new_file_content = b"feature1,feature2,label\n6.0,3.0,1\n5.8,2.7,1"
    resp = client.post(
        f"/api/datasets/{dataset_name}/upload?branch=experiment-v1",
        files={"file": ("new_data.csv", new_file_content)},
        headers=ds_headers
    )
    assert resp.status_code == 200

    resp = client.post(
        f"/api/datasets/{dataset_name}/commit?branch=experiment-v1",
        json={"message": "Added experiment data"},
        headers=ds_headers
    )
    assert resp.status_code == 200
    commit2_id = resp.json()["id"]

    # Compare versions: viewer blocked
    resp = client.get(
        f"/api/datasets/{dataset_name}/compare?left_ref=main&right_ref=experiment-v1",
        headers=viewer_headers
    )
    assert resp.status_code == 403

    # DS Compare versions
    resp = client.get(
        f"/api/datasets/{dataset_name}/compare?left_ref=main&right_ref=experiment-v1",
        headers=ds_headers
    )
    assert resp.status_code == 200
    changes = resp.json()
    assert len(changes) > 0
    # The file new_data.csv should be marked as added/changed on experiment-v1
    paths = [c["path"] for c in changes]
    assert "new_data.csv" in paths

    # 7. Dataset download (RBAC: write/read restricted to ds/admin)
    # Download from main branch: viewer blocked
    dl_resp = client.get(
        f"/api/datasets/{dataset_name}/download?path={file_path}&ref=main",
        headers=viewer_headers
    )
    assert dl_resp.status_code == 403

    # DS download
    dl_resp = client.get(
        f"/api/datasets/{dataset_name}/download?path={file_path}&ref=main",
        headers=ds_headers
    )
    assert dl_resp.status_code == 200
    assert dl_resp.content == file_content

    # 8. Create a tag (RBAC: ds_user/admin can, viewer cannot)
    tag_payload = {
        "name": "release-v1.0",
        "target_ref": "main"
    }

    # DS tags the main branch commit
    resp = client.post(
        f"/api/datasets/{dataset_name}/tags",
        json=tag_payload,
        headers=ds_headers
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "release-v1.0"

    # List tags: viewer blocked
    resp = client.get(f"/api/datasets/{dataset_name}/tags", headers=viewer_headers)
    assert resp.status_code == 403

    # DS List tags
    resp = client.get(f"/api/datasets/{dataset_name}/tags", headers=ds_headers)
    assert resp.status_code == 200
    tags = [t["name"] for t in resp.json()]
    assert "release-v1.0" in tags

    # Download file using tag reference: viewer blocked
    dl_tag_resp = client.get(
        f"/api/datasets/{dataset_name}/download?path={file_path}&ref=release-v1.0",
        headers=viewer_headers
    )
    assert dl_tag_resp.status_code == 403

    # DS Download file using tag reference
    dl_tag_resp = client.get(
        f"/api/datasets/{dataset_name}/download?path={file_path}&ref=release-v1.0",
        headers=ds_headers
    )
    assert dl_tag_resp.status_code == 200
    assert dl_tag_resp.content == file_content

    # 9. Get & Update metadata (RBAC: ds_user/admin)
    # Get metadata: viewer blocked
    resp = client.get(f"/api/datasets/{dataset_name}/metadata", headers=viewer_headers)
    assert resp.status_code == 403

    # DS Get metadata
    resp = client.get(f"/api/datasets/{dataset_name}/metadata", headers=ds_headers)
    assert resp.status_code == 200
    assert resp.json()["dataset_name"] == dataset_name

    # Update metadata
    meta_payload = {"metadata": {"license": "MIT", "owner": "MLSecOps Team"}}
    resp = client.put(f"/api/datasets/{dataset_name}/metadata", json=meta_payload, headers=ds_headers)
    assert resp.status_code == 200
    assert resp.json()["db_metadata"]["license"] == "MIT"

    # 10. Rollback / Revert a commit (RBAC: ds_user/admin can, viewer cannot)
    # On experiment-v1 branch, revert the second commit
    rollback_payload = {
        "branch": "experiment-v1",
        "commit_id": commit2_id
    }
    resp = client.post(
        f"/api/datasets/{dataset_name}/rollback",
        json=rollback_payload,
        headers=ds_headers
    )
    assert resp.status_code == 200
    assert "Successfully reverted" in resp.json()["message"]

    # 11. View commit history
    # Viewer blocked
    resp = client.get(f"/api/datasets/{dataset_name}/commits?ref=main", headers=viewer_headers)
    assert resp.status_code == 403

    # DS view commit history
    resp = client.get(f"/api/datasets/{dataset_name}/commits?ref=main", headers=ds_headers)
    assert resp.status_code == 200
    commits_list = resp.json()
    assert len(commits_list) > 0
    commit_msgs = [c["message"] for c in commits_list]
    assert "Initial dataset commit" in commit_msgs

    # 12. Deletions: tag, branch, dataset
    # Delete tag
    resp = client.delete(f"/api/datasets/{dataset_name}/tags/release-v1.0", headers=ds_headers)
    assert resp.status_code == 200

    # Delete branch
    resp = client.delete(f"/api/datasets/{dataset_name}/branches/experiment-v1", headers=ds_headers)
    assert resp.status_code == 200

    # Delete dataset (RBAC: admin/ds_user can)
    resp = client.delete(f"/api/datasets/{dataset_name}", headers=admin_headers)
    assert resp.status_code == 200

    # Verify dataset is deleted from DB list
    resp = client.get("/api/datasets", headers=admin_headers)
    assert resp.status_code == 200
    dataset_names = [d["name"] for d in resp.json()]
    assert dataset_name not in dataset_names

    # 13. Audit logs check
    audit_logs = get_all_audit_logs()
    actions = [log["action"] for log in audit_logs]
    
    assert "dataset_registration" in actions
    assert "dataset_file_upload" in actions
    assert "dataset_commit" in actions
    assert "dataset_branch_create" in actions
    assert "dataset_tag_create" in actions
    assert "dataset_rollback" in actions
    assert "dataset_branch_delete" in actions
    assert "dataset_tag_delete" in actions
    assert "dataset_delete" in actions
