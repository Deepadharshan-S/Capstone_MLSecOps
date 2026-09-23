import uuid
import pytest
from fastapi.testclient import TestClient
from mlflow.tracking import MlflowClient

from app.main import app
from app.core.config import settings

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


def test_real_time_prediction_offline_returns_503(user_tokens, deployed_model_name):
    """Verifies that when RayService is offline or initializing, inference returns HTTP 503 with Retry-After header."""
    ds_headers = {"Authorization": f"Bearer {user_tokens['ds_user']}"}
    payload = {"inputs": [[1.0, 2.0, 3.0]]}
    resp = client.post(f"/api/models/{deployed_model_name}/predict", json=payload, headers=ds_headers)
    assert resp.status_code == 503
    assert "Retry-After" in resp.headers
    assert resp.headers["Retry-After"] == "10"


def test_real_time_prediction_dataframe_records(user_tokens, deployed_model_name, monkeypatch):
    """Tests real-time prediction using dataframe_records format routed via HTTP to RayService."""
    viewer_headers = {"Authorization": f"Bearer {user_tokens['viewer_user']}"}

    class MockResponse:
        status_code = 200
        text = '{"predictions": [0, 1]}'
        def json(self):
            return {"predictions": [0, 1]}

    import httpx
    orig_post = httpx.Client.post
    def mock_post(self, url, *args, **kwargs):
        if "raysvc" in str(url) or "8000" in str(url):
            return MockResponse()
        return orig_post(self, url, *args, **kwargs)

    monkeypatch.setattr("httpx.Client.post", mock_post)

    predict_payload = {
        "dataframe_records": [
            {"feat1": 1.2, "feat2": 3.4, "feat3": 0.5},
            {"feat1": 6.5, "feat2": 8.2, "feat3": 9.1},
        ]
    }
    resp = client.post(f"/api/models/{deployed_model_name}/predict", json=predict_payload, headers=viewer_headers)
    assert resp.status_code == 200
    res_data = resp.json()
    assert "predictions" in res_data
    assert isinstance(res_data["predictions"], list)
    assert res_data["model_name"] == deployed_model_name
    assert res_data["latency_ms"] >= 0.0


def test_real_time_prediction_matrix_inputs(user_tokens, deployed_model_name, monkeypatch):
    """Tests real-time prediction using inputs (matrix) format routed via HTTP to RayService."""
    ds_headers = {"Authorization": f"Bearer {user_tokens['ds_user']}"}

    class MockResponse:
        status_code = 200
        text = '{"predictions": [0, 1]}'
        def json(self):
            return {"predictions": [0, 1]}

    import httpx
    orig_post = httpx.Client.post
    def mock_post(self, url, *args, **kwargs):
        if "raysvc" in str(url) or "8000" in str(url):
            return MockResponse()
        return orig_post(self, url, *args, **kwargs)

    monkeypatch.setattr("httpx.Client.post", mock_post)

    matrix_payload = {
        "inputs": [
            [1.0, 2.0, 3.0],
            [4.0, 5.0, 6.0],
        ]
    }
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

    # Test filtering by active_only and environment
    resp_active = client.get("/api/deployments?active_only=true", headers=viewer_headers)
    assert resp_active.status_code == 200
    for d in resp_active.json()["deployments"]:
        assert d["status"] not in ["stopped", "failed"]

    resp_env = client.get("/api/deployments?environment=production", headers=viewer_headers)
    assert resp_env.status_code == 200
    for d in resp_env.json()["deployments"]:
        assert d["environment"] == "production"


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


def test_get_model_detail(user_tokens, deployed_model_name):
    """Verifies retrieving detailed model metadata, version history, tags, metrics, and production alias."""
    viewer_headers = {"Authorization": f"Bearer {user_tokens['viewer_user']}"}
    ds_headers = {"Authorization": f"Bearer {user_tokens['ds_user']}"}

    # 1. Successful model detail retrieval
    resp = client.get(f"/api/models/{deployed_model_name}", headers=viewer_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == deployed_model_name
    assert "versions" in data
    assert len(data["versions"]) >= 1

    v1 = data["versions"][0]
    assert v1["version"] == "1"
    assert v1["status"] == "READY"
    assert "tags" in v1
    assert "metrics" in v1
    assert "parameters" in v1

    # 2. Verify 404 for non-existent model
    resp_404 = client.get("/api/models/non-existent-model-xyz", headers=ds_headers)
    assert resp_404.status_code == 404
    assert "not found" in resp_404.json()["detail"].lower()


def test_get_deployment_detail(user_tokens, deployed_model_name):
    """Verifies retrieving detailed RayService status, replica health, and internal endpoints for a deployment."""
    viewer_headers = {"Authorization": f"Bearer {user_tokens['viewer_user']}"}
    mle_headers = {"Authorization": f"Bearer {user_tokens['mle_user']}"}

    # 1. Fetch available deployments
    dep_list_resp = client.get("/api/deployments", headers=viewer_headers)
    assert dep_list_resp.status_code == 200
    deployments = dep_list_resp.json()["deployments"]
    assert len(deployments) >= 1
    target_dep = deployments[0]
    dep_id = target_dep["deployment_id"]

    # 2. Retrieve detailed deployment info
    resp = client.get(f"/api/deployments/{dep_id}", headers=viewer_headers)
    assert resp.status_code == 200
    detail = resp.json()
    assert detail["deployment_id"] == dep_id
    assert detail["model_name"] == target_dep["model_name"]
    assert "internal_endpoints" in detail
    assert "replica_health" in detail
    assert "service_status" in detail
    assert "k8s_status" in detail
    assert isinstance(detail["replica_health"]["desired_replicas"], int)
    assert isinstance(detail["replica_health"]["ready_replicas"], int)

    # 3. Verify 404 for non-existent deployment
    resp_404 = client.get("/api/deployments/non-existent-dep-xyz", headers=mle_headers)
    assert resp_404.status_code == 404
    assert "not found" in resp_404.json()["detail"].lower()


def test_deployment_repository_isolated_crud():
    """Verifies DeploymentRepository CRUD operations in isolation against PostgreSQL."""
    import uuid
    from app.db.session import SessionLocal
    from app.models.user import User
    from app.models.deployment import Deployment
    from app.repositories.deployment_repository import DeploymentRepository

    dep_id = f"test-repodep-{uuid.uuid4().hex[:8]}"

    with SessionLocal() as db:
        user = db.query(User).filter(User.username == "mle_user").first()
        user_id = user.id if user else uuid.uuid4()
        username = user.username if user else "mle_user"

        repo = DeploymentRepository(db)

        # 1. Create
        dep = Deployment(
            deployment_id=dep_id,
            model_name="isolated-test-model",
            version="1",
            environment="staging",
            rayservice_name=f"raysvc-{dep_id}",
            endpoint_url=f"/api/deployments/{dep_id}/predict",
            status="deployed",
            created_by_id=user_id,
            created_by_username=username,
        )
        saved = repo.create(dep)
        assert saved.id is not None
        assert saved.deployment_id == dep_id

        # 2. Get by deployment_id
        found = repo.get_by_deployment_id(dep_id)
        assert found is not None
        assert found.model_name == "isolated-test-model"

        # 3. Get by rayservice_name
        found_svc = repo.get_by_rayservice_name(f"raysvc-{dep_id}")
        assert found_svc is not None
        assert found_svc.deployment_id == dep_id

        # 4. List by model_name
        by_model = repo.get_by_model_name("isolated-test-model")
        assert any(d.deployment_id == dep_id for d in by_model)

        # 5. List with filters
        filtered = repo.list(status="deployed", environment="staging")
        assert any(d.deployment_id == dep_id for d in filtered)

        # 6. Update
        found.status = "stopped"
        updated = repo.save(found)
        assert updated.status == "stopped"

        # 7. Delete
        assert repo.delete_by_deployment_id(dep_id) is True
        assert repo.get_by_deployment_id(dep_id) is None


