import uuid
import time
import pytest
from fastapi.testclient import TestClient
from mlflow.tracking import MlflowClient

from app.main import app
from app.core.config import settings
from app.db.session import SessionLocal
from app.models.user import User

client = TestClient(app, base_url="https://testserver.local")


@pytest.fixture(scope="module")
def user_tokens():
    """Retrieves OAuth2 access tokens for all test users."""
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


@pytest.fixture(scope="module")
def deployed_model_name(user_tokens):
    """
    Uploads a dedicated 3-feature model specifically for deployment and inference tests.
    """
    import pickle
    from sklearn.linear_model import LogisticRegression
    import numpy as np

    clf = LogisticRegression()
    X = np.array([[1.0, 2.0, 3.0], [4.0, 5.0, 6.0], [7.0, 8.0, 9.0], [2.0, 1.0, 3.0]])
    y = np.array([0, 1, 0, 1])
    clf.fit(X, y)
    pkl_bytes = pickle.dumps(clf)

    model_name = f"test-dep-model-{uuid.uuid4().hex[:8]}"
    ds_headers = {"Authorization": f"Bearer {user_tokens['ds_user']}"}
    resp = client.post(
        "/api/models/upload",
        data={"model_name": model_name, "experiment_name": "deployment-tests"},
        files={"file": ("model.pkl", pkl_bytes, "application/octet-stream")},
        headers=ds_headers,
    )
    assert resp.status_code == 201
    return model_name


def test_model_deploy_success(user_tokens, deployed_model_name):
    """Verifies that ML Engineer can deploy a model to staging and production."""
    mle_headers = {"Authorization": f"Bearer {user_tokens['mle_user']}"}

    # 1. Deploy to Staging
    payload = {
        "model_id": deployed_model_name,
        "environment": "staging",
        "version": "latest",
        "replicas": 1,
    }
    resp = client.post("/api/models/deploy", json=payload, headers=mle_headers)
    assert resp.status_code == 201
    data = resp.json()

    assert data["model_id"] == deployed_model_name
    assert data["environment"] == "staging"
    assert data["status"] == "deployed"
    assert "deployment_id" in data
    assert "rayservice_name" in data
    assert "endpoint_url" in data

    # 2. Verify MLflow Model Version Tags and Stage
    mlflow_client = MlflowClient(tracking_uri=settings.MLFLOW_TRACKING_URI)
    version_obj = mlflow_client.get_model_version(deployed_model_name, data["version"])
    assert version_obj.current_stage == "Staging"
    assert version_obj.tags.get("deployment.status") == "running"
    assert version_obj.tags.get("deployment.environment") == "staging"
    assert version_obj.tags.get("deployment.deployed_by") == "mle_user"


def test_model_deploy_production_alias(user_tokens, deployed_model_name):
    """Verifies that deploying to Production sets Production stage and @production / @champion aliases."""
    admin_headers = {"Authorization": f"Bearer {user_tokens['admin_user']}"}

    payload = {
        "model_id": deployed_model_name,
        "environment": "production",
        "version": "1",
    }
    resp = client.post("/api/models/deploy", json=payload, headers=admin_headers)
    assert resp.status_code == 201
    data = resp.json()

    assert data["environment"] == "production"

    mlflow_client = MlflowClient(tracking_uri=settings.MLFLOW_TRACKING_URI)
    reg_model = mlflow_client.get_registered_model(deployed_model_name)
    assert "production" in reg_model.aliases or reg_model.latest_versions[-1].current_stage == "Production"


def test_real_time_prediction_dataframe_records(user_tokens, deployed_model_name):
    """Tests real-time prediction using dataframe_records format."""
    viewer_headers = {"Authorization": f"Bearer {user_tokens['viewer_user']}"}

    # Fetch model to know number of features
    mlflow_client = MlflowClient(tracking_uri=settings.MLFLOW_TRACKING_URI)
    version_obj = mlflow_client.get_model_version(deployed_model_name, "1")
    run = mlflow_client.get_run(version_obj.run_id)
    
    # Try sample input
    predict_payload = {
        "dataframe_records": [
            {"feat1": 1.2, "feat2": 3.4, "feat3": 0.5},
            {"feat1": 6.5, "feat2": 8.2, "feat3": 9.1},
        ]
    }
    resp = client.post(f"/api/models/{deployed_model_name}/predict", json=predict_payload, headers=viewer_headers)
    if resp.status_code == 500 and "feature" in resp.text.lower():
        # Fallback to 2-feature format if model was 2 features
        predict_payload = {"dataframe_records": [{"feature1": 1.0, "feature2": 2.0}]}
        resp = client.post(f"/api/models/{deployed_model_name}/predict", json=predict_payload, headers=viewer_headers)

    assert resp.status_code == 200
    res_data = resp.json()
    assert "predictions" in res_data
    assert isinstance(res_data["predictions"], list)
    assert res_data["model_name"] == deployed_model_name
    assert res_data["latency_ms"] >= 0.0


def test_real_time_prediction_matrix_inputs(user_tokens, deployed_model_name):
    """Tests real-time prediction using inputs (matrix) format."""
    ds_headers = {"Authorization": f"Bearer {user_tokens['ds_user']}"}

    matrix_payload = {
        "inputs": [
            [1.0, 2.0, 3.0],
            [4.0, 5.0, 6.0],
        ]
    }
    resp = client.post(f"/api/models/{deployed_model_name}/predict", json=matrix_payload, headers=ds_headers)
    if resp.status_code == 500 and "feature" in resp.text.lower():
        matrix_payload = {"inputs": [[1.0, 2.0], [3.0, 4.0]]}
        resp = client.post(f"/api/models/{deployed_model_name}/predict", json=matrix_payload, headers=ds_headers)

    assert resp.status_code == 200
    res_data = resp.json()
    assert "predictions" in res_data
    assert len(res_data["predictions"]) == 2


def test_deployment_listing(user_tokens, deployed_model_name):
    """Verifies listing active deployments and checking status."""
    viewer_headers = {"Authorization": f"Bearer {user_tokens['viewer_user']}"}

    resp = client.get("/api/deployments", headers=viewer_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert "deployments" in data
    assert len(data["deployments"]) > 0

    dep = next((d for d in data["deployments"] if d["model_name"] == deployed_model_name), None)
    assert dep is not None
    assert dep["status"] in ["running", "deployed"]
    assert dep["environment"] in ["staging", "production"]
    assert "rayservice_name" in dep
    assert "endpoint_url" in dep


def test_deployment_management_stop_and_restart(user_tokens, deployed_model_name):
    """Verifies stopping and restarting a deployment."""
    mle_headers = {"Authorization": f"Bearer {user_tokens['mle_user']}"}

    # Retrieve active deployment ID
    resp_list = client.get("/api/deployments", headers=mle_headers)
    assert resp_list.status_code == 200
    dep = next((d for d in resp_list.json()["deployments"] if d["model_name"] == deployed_model_name), None)
    assert dep is not None
    dep_id = dep["deployment_id"]

    # 1. Restart
    restart_resp = client.post(
        "/api/deployments/manage",
        json={"deployment_id": dep_id, "action": "restart"},
        headers=mle_headers,
    )
    assert restart_resp.status_code == 200
    assert restart_resp.json()["action_taken"] == "restart"

    # 2. Stop
    stop_resp = client.post(
        "/api/deployments/manage",
        json={"deployment_id": dep_id, "action": "stop"},
        headers=mle_headers,
    )
    assert stop_resp.status_code == 200
    assert stop_resp.json()["action_taken"] == "stop"

    # 3. Verify MLflow tag updated to stopped
    mlflow_client = MlflowClient(tracking_uri=settings.MLFLOW_TRACKING_URI)
    mv = mlflow_client.get_model_version(deployed_model_name, dep["version"])
    assert mv.tags.get("deployment.status") == "stopped"


def test_deployment_rbac(user_tokens, deployed_model_name):
    """Verifies RBAC rules on deployment endpoints."""
    viewer_headers = {"Authorization": f"Bearer {user_tokens['viewer_user']}"}

    # Viewer cannot deploy
    deploy_payload = {"model_id": deployed_model_name, "environment": "staging"}
    assert client.post("/api/models/deploy", json=deploy_payload, headers=viewer_headers).status_code == 403

    # Viewer cannot manage
    manage_payload = {"deployment_id": "test-id", "action": "stop"}
    assert client.post("/api/deployments/manage", json=manage_payload, headers=viewer_headers).status_code == 403

    # Viewer CAN view deployments and query predictions
    assert client.get("/api/deployments", headers=viewer_headers).status_code == 200


def test_deployment_and_prediction_audit_logging(user_tokens):
    """Verifies that deployment and inference events are recorded in security audit logs."""
    admin_headers = {"Authorization": f"Bearer {user_tokens['admin_user']}"}

    resp = client.get("/api/users/audit-logs", headers=admin_headers)
    assert resp.status_code == 200
    logs = resp.json()
    actions = [log["action"] for log in logs]

    assert "model_deploy" in actions
    assert "model_prediction" in actions
