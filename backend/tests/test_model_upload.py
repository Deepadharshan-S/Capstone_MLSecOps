import json
import pickle
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.core.security import hash_password
from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models.audit_log import AuditLog  # noqa: F401
from app.models.blacklisted_token import BlacklistedToken  # noqa: F401
from app.models.refresh_token import RefreshToken  # noqa: F401
from app.models.user import User

# Isolated Test Database setup (using in-memory SQLite)
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


@pytest.fixture(scope="session", autouse=True)
def cleanup_after_tests():
    yield


def override_get_db():
    try:
        db = TestingSessionLocal()
        yield db
    finally:
        db.close()


@pytest.fixture(scope="module", autouse=True)
def setup_test_database():
    Base.metadata.create_all(bind=engine)
    db = TestingSessionLocal()
    try:
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

    app.dependency_overrides[get_db] = override_get_db
    yield
    if get_db in app.dependency_overrides:
        del app.dependency_overrides[get_db]
    Base.metadata.drop_all(bind=engine)


client = TestClient(app, base_url="https://testserver.local")


class DummyModel:
    def predict(self, x):
        return [1] * len(x)


@pytest.fixture(scope="module")
def user_tokens():
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


@patch("mlflow.set_tracking_uri")
@patch("mlflow.set_experiment")
@patch("mlflow.start_run")
@patch("mlflow.pyfunc.log_model")
@patch("mlflow.tracking.MlflowClient")
def test_upload_model_success_with_explicit_name(
    mock_mlflow_client_cls,
    mock_log_model,
    mock_start_run,
    mock_set_experiment,
    mock_set_tracking_uri,
    user_tokens,
):
    """Verify successful .pkl model upload and registration with an explicit model name."""
    mock_run = MagicMock()
    mock_run.info.run_id = "test-run-id-12345"
    mock_start_run.return_value.__enter__.return_value = mock_run

    mock_client = MagicMock()
    mock_version = MagicMock()
    mock_version.version = "1"
    mock_client.get_latest_versions.return_value = [mock_version]
    mock_mlflow_client_cls.return_value = mock_client

    model_bytes = pickle.dumps(DummyModel())
    ds_headers = {"Authorization": f"Bearer {user_tokens['ds_user']}"}

    payload = {
        "model_name": "custom_iris_model",
        "experiment_name": "iris-exp",
        "metadata": json.dumps({"framework": "sklearn", "author": "ds_user"}),
        "metrics": json.dumps({"accuracy": 0.96, "f1_score": 0.94}),
    }
    files = {"file": ("my_model.pkl", model_bytes, "application/octet-stream")}

    response = client.post(
        "/api/models/upload",
        data=payload,
        files=files,
        headers=ds_headers,
    )
    assert response.status_code == 201
    res = response.json()
    assert res["model_id"] == "test-run-id-12345"
    assert res["model_name"] == "custom_iris_model"
    assert res["version"] == "1"
    assert res["status"] == "uploaded"
    assert res["uploaded_by"] == "ds_user"

    # Verify MLflow pyfunc.log_model was called with registered_model_name
    mock_log_model.assert_called_once()
    _, kwargs = mock_log_model.call_args
    assert kwargs["registered_model_name"] == "custom_iris_model"


@patch("mlflow.set_tracking_uri")
@patch("mlflow.set_experiment")
@patch("mlflow.start_run")
@patch("mlflow.pyfunc.log_model")
@patch("mlflow.tracking.MlflowClient")
def test_upload_model_defaults_name_from_filename(
    mock_mlflow_client_cls,
    mock_log_model,
    mock_start_run,
    mock_set_experiment,
    mock_set_tracking_uri,
    user_tokens,
):
    """Verify that omitting model_name defaults to the sanitized filename without .pkl."""
    mock_run = MagicMock()
    mock_run.info.run_id = "test-run-default-name"
    mock_start_run.return_value.__enter__.return_value = mock_run

    mock_client = MagicMock()
    mock_version = MagicMock()
    mock_version.version = "2"
    mock_client.get_latest_versions.return_value = [mock_version]
    mock_mlflow_client_cls.return_value = mock_client

    model_bytes = pickle.dumps(DummyModel())
    ds_headers = {"Authorization": f"Bearer {user_tokens['ds_user']}"}

    files = {"file": ("churn_detector_v1.pkl", model_bytes, "application/octet-stream")}

    response = client.post(
        "/api/models/upload",
        files=files,
        headers=ds_headers,
    )
    assert response.status_code == 201
    res = response.json()
    assert res["model_name"] == "churn_detector_v1"
    assert res["version"] == "2"

    _, kwargs = mock_log_model.call_args
    assert kwargs["registered_model_name"] == "churn_detector_v1"


def test_upload_model_non_pkl_rejected(user_tokens):
    """Verify that uploading non-.pkl files is rejected with HTTP 400."""
    ds_headers = {"Authorization": f"Bearer {user_tokens['ds_user']}"}
    files = {"file": ("model.h5", b"fake h5 bytes", "application/octet-stream")}

    response = client.post(
        "/api/models/upload",
        files=files,
        headers=ds_headers,
    )
    assert response.status_code == 400
    assert "Only .pkl files are allowed." in response.json()["detail"]


def test_upload_model_size_limit_exceeded(user_tokens, monkeypatch):
    """Verify that files exceeding MAX_MODEL_UPLOAD_SIZE_BYTES return HTTP 413."""
    # Set limit to 20 bytes for this test
    monkeypatch.setattr(settings, "MAX_MODEL_UPLOAD_SIZE_BYTES", 20)

    model_bytes = pickle.dumps(DummyModel())
    assert len(model_bytes) > 20

    ds_headers = {"Authorization": f"Bearer {user_tokens['ds_user']}"}
    files = {"file": ("oversized_model.pkl", model_bytes, "application/octet-stream")}

    response = client.post(
        "/api/models/upload",
        files=files,
        headers=ds_headers,
    )
    assert response.status_code == 413
    assert "exceeds maximum limit" in response.json()["detail"]


def test_upload_model_empty_file_rejected(user_tokens):
    """Verify that uploading an empty 0-byte file returns HTTP 400."""
    ds_headers = {"Authorization": f"Bearer {user_tokens['ds_user']}"}
    files = {"file": ("empty_model.pkl", b"", "application/octet-stream")}

    response = client.post(
        "/api/models/upload",
        files=files,
        headers=ds_headers,
    )
    assert response.status_code == 400
    assert "empty" in response.json()["detail"]


def test_upload_model_invalid_json_metadata(user_tokens):
    """Verify that malformed JSON in metadata returns HTTP 400."""
    ds_headers = {"Authorization": f"Bearer {user_tokens['ds_user']}"}
    model_bytes = pickle.dumps(DummyModel())
    files = {"file": ("model.pkl", model_bytes, "application/octet-stream")}
    payload = {"metadata": "{not_valid_json}"}

    response = client.post(
        "/api/models/upload",
        data=payload,
        files=files,
        headers=ds_headers,
    )
    assert response.status_code == 400
    assert "Invalid metadata JSON" in response.json()["detail"]


def test_upload_model_invalid_json_metrics(user_tokens):
    """Verify that non-numeric metrics values return HTTP 400."""
    ds_headers = {"Authorization": f"Bearer {user_tokens['ds_user']}"}
    model_bytes = pickle.dumps(DummyModel())
    files = {"file": ("model.pkl", model_bytes, "application/octet-stream")}
    payload = {"metrics": json.dumps({"accuracy": "not-a-number"})}

    response = client.post(
        "/api/models/upload",
        data=payload,
        files=files,
        headers=ds_headers,
    )
    assert response.status_code == 400
    assert "Invalid metrics JSON" in response.json()["detail"]


def test_upload_model_corrupt_pickle(user_tokens):
    """Verify that corrupt pickle data returns HTTP 400."""
    ds_headers = {"Authorization": f"Bearer {user_tokens['ds_user']}"}
    files = {"file": ("corrupt.pkl", b"invalid pickle bytes", "application/octet-stream")}

    response = client.post(
        "/api/models/upload",
        files=files,
        headers=ds_headers,
    )
    assert response.status_code == 400
    assert "Failed to deserialize" in response.json()["detail"]


def test_upload_model_viewer_forbidden(user_tokens):
    """Verify that users without models:train scope (e.g. viewer) cannot upload models."""
    viewer_headers = {"Authorization": f"Bearer {user_tokens['viewer_user']}"}
    model_bytes = pickle.dumps(DummyModel())
    files = {"file": ("model.pkl", model_bytes, "application/octet-stream")}

    response = client.post(
        "/api/models/upload",
        files=files,
        headers=viewer_headers,
    )
    assert response.status_code == 403
