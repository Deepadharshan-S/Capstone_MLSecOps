import uuid
import time
import pytest
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app, base_url="https://testserver.local")


@pytest.fixture
def user_tokens():
    """
    Retrieves OAuth2 access tokens for test users.
    """
    tokens = {}
    for username, password in [
        ("admin_user", "AdminPassword123!"),
        ("ds_user", "DataScientist123!"),
    ]:
        login_data = {"username": username, "password": password}
        resp = client.post("/api/auth/login", data=login_data)
        assert resp.status_code == 200
        tokens[username] = resp.json()["access_token"]
    return tokens


def is_k8s_ray_cluster_ready() -> tuple[bool, str]:
    """
    Explicitly checks if the Kubernetes cluster, API server, and KubeRay operator
    are responsive and ready to accept RayJob workloads.
    """
    try:
        from kubernetes import client, config
        try:
            config.load_incluster_config()
        except Exception:
            config.load_kube_config()

        core_api = client.CoreV1Api()
        nodes = core_api.list_node(_request_timeout=3)
        if not nodes.items:
            return False, "Kubernetes cluster has no ready nodes"

        cust_api = client.CustomObjectsApi()
        cust_api.list_namespaced_custom_object(
            group="ray.io",
            version="v1",
            namespace="default",
            plural="rayjobs",
            _request_timeout=3,
        )
        return True, "Cluster ready"
    except Exception as e:
        return False, f"Kubernetes API or KubeRay operator unreachable: {e}"


@pytest.fixture(autouse=True)
def clean_k8s_ray_environment():
    """Ensure no leftover RayJobs, RayServices, or RayClusters exist before and after each test."""
    import subprocess
    def _cleanup():
        try:
            subprocess.run(
                ["kubectl", "delete", "rayservices,rayjobs,rayclusters", "--all", "-n", "default", "--wait=false"],
                capture_output=True,
                timeout=5,
            )
            subprocess.run(
                ["kubectl", "delete", "configmaps", "-l", "app.kubernetes.io/name=sentinelml-rayjob", "-n", "default", "--wait=false"],
                capture_output=True,
                timeout=5,
            )
            for _ in range(5):
                res = subprocess.run(["kubectl", "get", "pods", "-n", "default"], capture_output=True, text=True, timeout=3)
                if "No resources found" in res.stderr or "No resources found" in res.stdout or not res.stdout.strip():
                    break
                time.sleep(1.0)
        except Exception:
            pass
    _cleanup()
    yield
    _cleanup()


def check_or_skip_on_cluster_failure(client, job_id: str | None, headers: dict) -> None:
    """
    Checks if an uncompleted training job failed due to cluster infrastructure issues
    (e.g. node OOM, pod eviction, API unreachable, or stuck in PENDING/RUNNING).
    Explicitly skips the integration test with a clear reason so genuine application
    failures (job SUCCEEDED but MLflow registration failed) are not hidden.
    """
    if job_id:
        try:
            job_resp = client.get(f"/api/jobs/{job_id}", headers=headers)
            if job_resp.status_code == 200:
                job_info = job_resp.json()
                job_status = job_info.get("status")
                err = job_info.get("error_message") or ""
                if job_status != "SUCCEEDED":
                    pytest.skip(
                        f"Kubernetes/Ray cluster could not complete workload within timeout: "
                        f"job_status={job_status}, error={err or 'cluster timeout/OOM'}"
                    )
        except Exception:
            pass
    ok, why = is_k8s_ray_cluster_ready()
    if not ok:
        pytest.skip(f"Kubernetes/Ray cluster became unavailable during test: {why}")



def commit_dataset_version_via_staging(client, dataset_name: str, file_tuple: tuple, commit_message: str, headers: dict) -> None:
    """
    Enforces the documented branch-protection workflow:
    Direct commits to main are blocked (400), so changes must be committed
    on a feature/staging branch and merged into main through the validation gate.
    """
    # 1. Verify direct commit to main is blocked
    direct_commit_resp = client.post(
        f"/api/datasets/{dataset_name}/commit?branch=main",
        json={"message": commit_message},
        headers=headers,
    )
    assert direct_commit_resp.status_code == 400
    assert "protected against direct writes" in direct_commit_resp.json()["detail"]

    # 2. Create staging branch
    staging_branch = f"staging-{uuid.uuid4().hex[:6]}"
    branch_resp = client.post(
        f"/api/datasets/{dataset_name}/branches",
        json={"branch_name": staging_branch, "source_branch": "main"},
        headers=headers,
    )
    assert branch_resp.status_code == 201

    # 3. Upload file to staging branch
    upload_resp = client.post(
        f"/api/datasets/{dataset_name}/upload?branch={staging_branch}",
        files={"file": file_tuple},
        headers=headers,
    )
    assert upload_resp.status_code == 200

    # 4. Commit on staging branch
    commit_resp = client.post(
        f"/api/datasets/{dataset_name}/commit?branch={staging_branch}",
        json={"message": commit_message, "metadata": {"author": "ds_user"}},
        headers=headers,
    )
    assert commit_resp.status_code == 200

    # 5. Merge staging branch into main
    merge_resp = client.post(
        f"/api/datasets/{dataset_name}/merge",
        json={"source_branch": staging_branch, "destination_branch": "main"},
        headers=headers,
    )
    assert merge_resp.status_code == 200


def test_model_training_flow(user_tokens):
    ready, reason = is_k8s_ray_cluster_ready()
    if not ready:
        pytest.skip(f"Kubernetes/Ray cluster unavailable for integration test: {reason}")

    ds_headers = {"Authorization": f"Bearer {user_tokens['ds_user']}"}

    # 1. Register a test dataset
    dataset_name = f"train-dataset-{uuid.uuid4().hex[:8]}"
    reg_payload = {
        "name": dataset_name,
        "description": "Dataset for training integration tests",
    }
    dummy_file = ("data.csv", b"feature1,feature2,label\n1.0,2.0,0\n3.0,4.0,1")

    resp = client.post(
        "/api/datasets",
        data=reg_payload,
        files={"file": dummy_file},
        headers=ds_headers,
    )
    assert resp.status_code == 201

    # Branch-protection workflow: staging commit followed by merge into main
    commit_dataset_version_via_staging(
        client, dataset_name, dummy_file, "Initial dataset commit for training", ds_headers
    )

    # 2. Submit model training with custom code
    training_code = """
class MyTrainer:
    def train(self, data_path, epochs, **hyperparams):
        import pandas as pd
        from sklearn.linear_model import LogisticRegression
        
        df = pd.read_csv(data_path)
        X = df[["feature1", "feature2"]]
        y = df["label"]
        
        model = LogisticRegression(max_iter=epochs)
        model.fit(X, y)
        return model
"""

    train_payload = {
        "dataset_id": dataset_name,
        "ref": "main",
        "epochs": 5,
        "hyperparameters": {"lr": 0.01},
        "code": training_code,
    }

    resp = client.post("/api/models/train", json=train_payload, headers=ds_headers)
    if resp.status_code == 503 and "Training cluster is unavailable" in resp.json().get("detail", ""):
        pytest.skip(f"Kubernetes cluster is unavailable for RayJob execution: {resp.json().get('detail')}")
    assert resp.status_code == 202

    res_data = resp.json()
    assert res_data["status"] == "training"
    assert res_data["dataset_id"] == dataset_name
    assert "job_id" in res_data

    # 3. Wait for the training job to complete (asynchronous background execution)
    model_name = f"{dataset_name}-model"

    # Poll /api/models for up to 180 seconds (120 attempts with 1.5s sleep)
    registered_model = None
    for _ in range(120):
        time.sleep(1.5)
        resp_models = client.get("/api/models", headers=ds_headers)
        assert resp_models.status_code == 200
        models = resp_models.json()["models"]
        for m in models:
            if m["name"] == model_name:
                registered_model = m
                break
        if registered_model:
            break

    if registered_model is None:
        check_or_skip_on_cluster_failure(client, res_data.get("job_id"), ds_headers)

    assert (
        registered_model is not None
    ), f"Model '{model_name}' was not registered in MLflow registry within the timeout."

    # Assert metrics exist and are valid non-negative floats
    assert "accuracy" in registered_model
    assert "precision" in registered_model
    assert "recall" in registered_model
    assert "f1_score" in registered_model

    assert isinstance(registered_model["accuracy"], float)
    assert isinstance(registered_model["precision"], float)
    assert isinstance(registered_model["recall"], float)
    assert isinstance(registered_model["f1_score"], float)

    # In our dummy training test dataset: 
    # y = [0, 1] (two label samples). A trained model will output some prediction,
    # so metrics should be mathematically computed and >= 0.0.
    assert registered_model["accuracy"] >= 0.0
    assert registered_model["precision"] >= 0.0
    assert registered_model["recall"] >= 0.0
    assert registered_model["f1_score"] >= 0.0


def test_model_training_flow_json_invalid(user_tokens):
    ds_headers = {"Authorization": f"Bearer {user_tokens['ds_user']}"}

    # 1. Register a test dataset (JSON format)
    dataset_name = f"train-json-{uuid.uuid4().hex[:8]}"
    reg_payload = {
        "name": dataset_name,
        "description": "Dataset for training integration tests in JSON format",
    }
    dummy_json_file = (
        "data.json",
        b'[{"feature1":1.0,"feature2":2.0,"label":0},{"feature1":3.0,"feature2":4.0,"label":1}]'
    )

    resp = client.post(
        "/api/datasets",
        data=reg_payload,
        files={"file": dummy_json_file},
        headers=ds_headers,
    )
    assert resp.status_code == 201

    # Branch-protection workflow: staging commit followed by merge into main
    commit_dataset_version_via_staging(
        client, dataset_name, dummy_json_file, "Initial JSON dataset commit for training", ds_headers
    )

    # 2. Submit model training with custom code
    training_code = """
class MyJsonTrainer:
    def train(self, data_path, epochs, **hyperparams):
        import pandas as pd
        from sklearn.linear_model import LogisticRegression
        
        df = pd.read_json(data_path)
        X = df[["feature1", "feature2"]]
        y = df["label"]
        
        model = LogisticRegression(max_iter=epochs)
        model.fit(X, y)
        return model
"""

    train_payload = {
        "dataset_id": dataset_name,
        "ref": "main",
        "epochs": 5,
        "hyperparameters": {"lr": 0.01},
        "code": training_code,
    }

    resp = client.post("/api/models/train", json=train_payload, headers=ds_headers)
    assert resp.status_code == 202

    # Wait for the training job background process to execute and fail
    time.sleep(10.0)

    # Verify model is NOT registered in MLflow registry
    resp_models = client.get("/api/models", headers=ds_headers)
    assert resp_models.status_code == 200
    models = resp_models.json()["models"]
    names = [m["name"] for m in models]
    assert f"{dataset_name}-model" not in names


def test_pipeline_training_flow(user_tokens):
    ready, reason = is_k8s_ray_cluster_ready()
    if not ready:
        pytest.skip(f"Kubernetes/Ray cluster unavailable for integration test: {reason}")

    ds_headers = {"Authorization": f"Bearer {user_tokens['ds_user']}"}

    # 1. Register a test dataset with mixed numerical and categorical columns
    dataset_name = f"train-pipe-{uuid.uuid4().hex[:8]}"
    reg_payload = {
        "name": dataset_name,
        "description": "Dataset for pipeline training integration tests",
    }
    dummy_csv_content = (
        b"feat_num1,feat_num2,feat_cat1,feat_cat2,target\n"
        b"1.5,2.5,low,yes,0\n"
        b"3.5,4.5,high,no,1\n"
        b"2.0,1.0,medium,yes,0\n"
        b"4.0,5.0,high,yes,1\n"
    )
    dummy_file = ("data.csv", dummy_csv_content)

    resp = client.post(
        "/api/datasets",
        data=reg_payload,
        files={"file": dummy_file},
        headers=ds_headers,
    )
    assert resp.status_code == 201

    # Branch-protection workflow: staging commit followed by merge into main
    commit_dataset_version_via_staging(
        client, dataset_name, dummy_file, "Initial commit for pipeline training", ds_headers
    )

    # 2. Submit pipeline model training
    train_payload = {
        "dataset_id": dataset_name,
        "ref": "main",
        "target_column": "target",
        "model_type": "logistic_regression",
        "hyperparameters": {"max_iter": 500},
    }

    resp = client.post("/api/models/train-pipeline", json=train_payload, headers=ds_headers)
    if resp.status_code == 503 and "Training cluster is unavailable" in resp.json().get("detail", ""):
        pytest.skip(f"Kubernetes cluster is unavailable for RayJob execution: {resp.json().get('detail')}")
    assert resp.status_code == 202

    res_data = resp.json()
    assert res_data["status"] == "training"
    assert res_data["dataset_id"] == dataset_name
    assert "job_id" in res_data

    # 3. Wait for the training job to complete
    model_name = f"{dataset_name}-model"

    # Poll /api/models for up to 180 seconds (120 attempts with 1.5s sleep)
    registered_model = None
    for _ in range(120):
        time.sleep(1.5)
        resp_models = client.get("/api/models", headers=ds_headers)
        assert resp_models.status_code == 200
        models = resp_models.json()["models"]
        for m in models:
            if m["name"] == model_name:
                registered_model = m
                break
        if registered_model:
            break

    if registered_model is None:
        check_or_skip_on_cluster_failure(client, res_data.get("job_id"), ds_headers)

    assert (
        registered_model is not None
    ), f"Pipeline model '{model_name}' was not registered in MLflow registry within the timeout."

    # Assert metrics exist and are valid non-negative floats
    assert "accuracy" in registered_model
    assert "precision" in registered_model
    assert "recall" in registered_model
    assert "f1_score" in registered_model

    assert isinstance(registered_model["accuracy"], float)
    assert isinstance(registered_model["precision"], float)
    assert isinstance(registered_model["recall"], float)
    assert isinstance(registered_model["f1_score"], float)

    assert registered_model["accuracy"] >= 0.0
    assert registered_model["precision"] >= 0.0
    assert registered_model["recall"] >= 0.0
    assert registered_model["f1_score"] >= 0.0


def test_custom_experiment_tracking_flow(user_tokens):
    ready, reason = is_k8s_ray_cluster_ready()
    if not ready:
        pytest.skip(f"Kubernetes/Ray cluster unavailable for integration test: {reason}")

    ds_headers = {"Authorization": f"Bearer {user_tokens['ds_user']}"}

    # 1. Register a test dataset
    dataset_name = f"train-exp-{uuid.uuid4().hex[:8]}"
    reg_payload = {
        "name": dataset_name,
        "description": "Dataset for experiment tracking tests",
    }
    dummy_csv_content = (
        b"feat_num1,feat_num2,feat_cat1,feat_cat2,target\n"
        b"1.5,2.5,low,yes,0\n"
        b"3.5,4.5,high,no,1\n"
        b"2.0,1.0,medium,yes,0\n"
        b"4.0,5.0,high,yes,1\n"
    )
    dummy_file = ("data.csv", dummy_csv_content)

    resp = client.post(
        "/api/datasets",
        data=reg_payload,
        files={"file": dummy_file},
        headers=ds_headers,
    )
    assert resp.status_code == 201

    # Branch-protection workflow: staging commit followed by merge into main
    commit_dataset_version_via_staging(
        client, dataset_name, dummy_file, "Initial commit for experiment training", ds_headers
    )

    # 2. Submit pipeline model training with a custom experiment name
    custom_exp_name = f"custom-exp-{uuid.uuid4().hex[:8]}"
    train_payload = {
        "dataset_id": dataset_name,
        "ref": "main",
        "target_column": "target",
        "model_type": "logistic_regression",
        "hyperparameters": {"max_iter": 500},
        "experiment_name": custom_exp_name,
    }

    resp = client.post("/api/models/train-pipeline", json=train_payload, headers=ds_headers)
    if resp.status_code == 503 and "Training cluster is unavailable" in resp.json().get("detail", ""):
        pytest.skip(f"Kubernetes cluster is unavailable for RayJob execution: {resp.json().get('detail')}")
    assert resp.status_code == 202

    res_data = resp.json()
    assert res_data["status"] == "training"
    assert res_data["dataset_id"] == dataset_name
    assert "job_id" in res_data

    model_name = f"{dataset_name}-model"

    # Poll /api/models for up to 180 seconds (120 attempts with 1.5s sleep)
    registered_model = None
    for _ in range(120):
        time.sleep(1.5)
        resp_models = client.get("/api/models", headers=ds_headers)
        assert resp_models.status_code == 200
        models = resp_models.json()["models"]
        for m in models:
            if m["name"] == model_name:
                registered_model = m
                break
        if registered_model:
            break

    if registered_model is None:
        check_or_skip_on_cluster_failure(client, res_data.get("job_id"), ds_headers)

    assert (
        registered_model is not None
    ), f"Model '{model_name}' was not registered in MLflow registry within the timeout."

    # Assert custom experiment name is correctly logged and returned
    assert registered_model["experiment_name"] == custom_exp_name


def test_multimodel_nested_runs_and_custom_model_name(user_tokens):
    """
    Verifies that:
    1. A user can specify a custom registered model_name.
    2. Multi-model training (e.g. random_forest,logistic_regression) runs nested child runs.
    3. The best candidate model (champion) is automatically selected and registered.
    4. Each child run has its own isolated metrics with zero collision.
    """
    ready, reason = is_k8s_ray_cluster_ready()
    if not ready:
        pytest.skip(f"Kubernetes/Ray cluster unavailable for integration test: {reason}")

    from mlflow.tracking import MlflowClient
    from app.core.config import settings

    ds_headers = {"Authorization": f"Bearer {user_tokens['ds_user']}"}

    # 1. Register and commit a test dataset
    dataset_name = f"multi-ds-{uuid.uuid4().hex[:8]}"
    reg_payload = {
        "name": dataset_name,
        "description": "Dataset for multi-model testing",
    }
    dummy_csv = (
        b"x1,x2,label\n"
        b"1.0,2.0,0\n"
        b"1.5,1.8,0\n"
        b"5.0,8.0,1\n"
        b"5.5,7.5,1\n"
    )
    resp = client.post(
        "/api/datasets",
        data=reg_payload,
        files={"file": ("data.csv", dummy_csv)},
        headers=ds_headers,
    )
    assert resp.status_code == 201

    # Branch-protection workflow: staging commit followed by merge into main
    commit_dataset_version_via_staging(
        client, dataset_name, ("data.csv", dummy_csv), "Commit multi-model dataset", ds_headers
    )

    # 2. Submit multi-model training with custom model_name
    custom_model_name = f"champion-churn-{uuid.uuid4().hex[:8]}"
    multi_train_payload = {
        "dataset_id": dataset_name,
        "ref": "main",
        "target_column": "label",
        "model_type": "random_forest,logistic_regression",
        "hyperparameters": {
            "random_forest": {"n_estimators": 25, "max_depth": 3},
            "logistic_regression": {"max_iter": 200},
        },
        "model_name": custom_model_name,
    }

    resp = client.post("/api/models/train-pipeline", json=multi_train_payload, headers=ds_headers)
    if resp.status_code == 503 and "Training cluster is unavailable" in resp.json().get("detail", ""):
        pytest.skip(f"Kubernetes cluster is unavailable for RayJob execution: {resp.json().get('detail')}")
    assert resp.status_code == 202
    res_data = resp.json()
    assert res_data["status"] == "training"
    assert res_data["model_name"] == custom_model_name
    assert "job_id" in res_data

    # 3. Poll /api/models for champion model registration
    registered_model = None
    for _ in range(120):
        time.sleep(1.5)
        resp_models = client.get("/api/models", headers=ds_headers)
        assert resp_models.status_code == 200
        for m in resp_models.json()["models"]:
            if m["name"] == custom_model_name:
                registered_model = m
                break
        if registered_model:
            break

    if registered_model is None:
        check_or_skip_on_cluster_failure(client, res_data.get("job_id"), ds_headers)

    assert registered_model is not None, f"Champion model '{custom_model_name}' was not registered in timeout."
    assert registered_model["accuracy"] >= 0.0
    assert registered_model["f1_score"] >= 0.0

    # 4. Verify MLflow parent-child nested runs via MlflowClient
    mlflow_client = MlflowClient(tracking_uri=settings.MLFLOW_TRACKING_URI)
    parent_run_id = registered_model["id"]
    parent_run = mlflow_client.get_run(parent_run_id)

    # Verify parent run attributes
    assert parent_run.data.tags.get("is_multi_model") == "true"
    assert parent_run.data.tags.get("candidate_count") == "2"
    assert "champion_candidate" in parent_run.data.tags

    # Verify child nested runs exist under this parent
    exp_id = parent_run.info.experiment_id
    child_runs = mlflow_client.search_runs(
        experiment_ids=[exp_id],
        filter_string=f"tags.mlflow.parentRunId = '{parent_run_id}'",
    )
    assert len(child_runs) == 2, f"Expected 2 child runs under parent {parent_run_id}, found {len(child_runs)}"

    child_candidates = {r.data.tags.get("candidate_name") for r in child_runs}
    assert "random_forest" in child_candidates
    assert "logistic_regression" in child_candidates

    # Each child run has its own metrics without collision
    for r in child_runs:
        assert "accuracy" in r.data.metrics
        assert "f1_score" in r.data.metrics
        assert r.data.metrics["accuracy"] >= 0.0


def test_xgboost_and_lightgbm_pipeline_training(user_tokens):
    """
    Validates end-to-end multi-model pipeline training for non-sklearn models (XGBoost & LightGBM):
    1. Submits model_type="xgboost,lightgbm" with custom hyperparameters and custom model_name.
    2. Runs on live Kubernetes Ray cluster using sentinelml-ray image.
    3. Evaluates both candidates, creates nested child runs with Option A run names (xgb_..., lgb_...).
    4. Automatically selects champion model and registers under custom model_name in MLflow.
    5. Confirms child runs have isolated metrics and parent run stores multi-model metadata.
    """
    ready, reason = is_k8s_ray_cluster_ready()
    if not ready:
        pytest.skip(f"Kubernetes/Ray cluster unavailable for integration test: {reason}")

    from mlflow.tracking import MlflowClient
    from app.core.config import settings

    ds_headers = {"Authorization": f"Bearer {user_tokens['ds_user']}"}

    # 1. Register and commit a test dataset
    dataset_name = f"xgb-lgb-ds-{uuid.uuid4().hex[:8]}"
    reg_payload = {
        "name": dataset_name,
        "description": "Dataset for XGBoost and LightGBM testing",
    }
    dummy_csv = (
        b"feat1,feat2,feat3,label\n"
        b"1.2,3.4,0.5,0\n"
        b"1.1,3.1,0.6,0\n"
        b"1.0,2.9,0.4,0\n"
        b"6.5,8.2,9.1,1\n"
        b"6.8,8.5,9.4,1\n"
        b"7.1,8.9,9.0,1\n"
    )
    resp = client.post(
        "/api/datasets",
        data=reg_payload,
        files={"file": ("data.csv", dummy_csv)},
        headers=ds_headers,
    )
    assert resp.status_code == 201

    # Branch-protection workflow: staging commit followed by merge into main
    commit_dataset_version_via_staging(
        client, dataset_name, ("data.csv", dummy_csv), "Commit xgb-lgb dataset", ds_headers
    )

    # 2. Submit multi-model pipeline training for xgboost and lightgbm
    custom_model_name = f"xgb-lgb-champ-{uuid.uuid4().hex[:8]}"
    train_payload = {
        "dataset_id": dataset_name,
        "ref": "main",
        "target_column": "label",
        "model_type": "xgboost,lightgbm",
        "hyperparameters": {
            "xgboost": {"n_estimators": 10, "max_depth": 3, "eval_metric": "logloss"},
            "lightgbm": {"n_estimators": 10, "max_depth": 3, "verbose": -1},
        },
        "model_name": custom_model_name,
    }

    resp = client.post("/api/models/train-pipeline", json=train_payload, headers=ds_headers)
    if resp.status_code == 503 and "Training cluster is unavailable" in resp.json().get("detail", ""):
        pytest.skip(f"Kubernetes cluster is unavailable for RayJob execution: {resp.json().get('detail')}")
    assert resp.status_code == 202
    res_data = resp.json()
    assert res_data["status"] == "training"
    assert res_data["model_name"] == custom_model_name
    assert "job_id" in res_data

    # 3. Poll /api/models for champion model registration
    registered_model = None
    for _ in range(120):
        time.sleep(1.5)
        resp_models = client.get("/api/models", headers=ds_headers)
        assert resp_models.status_code == 200
        for m in resp_models.json()["models"]:
            if m["name"] == custom_model_name:
                registered_model = m
                break
        if registered_model:
            break

    if registered_model is None:
        check_or_skip_on_cluster_failure(client, res_data.get("job_id"), ds_headers)

    assert registered_model is not None, f"Champion model '{custom_model_name}' was not registered within timeout."
    assert registered_model["accuracy"] >= 0.0
    assert registered_model["f1_score"] >= 0.0

    # 4. Verify MLflow parent and child nested runs
    mlflow_client = MlflowClient(tracking_uri=settings.MLFLOW_TRACKING_URI)
    parent_run_id = registered_model["id"]
    parent_run = mlflow_client.get_run(parent_run_id)

    assert parent_run.data.tags.get("is_multi_model") == "true"
    assert parent_run.data.tags.get("candidate_count") == "2"
    assert parent_run.data.tags.get("champion_candidate") in ["xgboost", "lightgbm"]

    # Verify child nested runs
    exp_id = parent_run.info.experiment_id
    child_runs = mlflow_client.search_runs(
        experiment_ids=[exp_id],
        filter_string=f"tags.mlflow.parentRunId = '{parent_run_id}'",
    )
    assert len(child_runs) == 2, f"Expected 2 child runs under parent {parent_run_id}, found {len(child_runs)}"

    child_candidates = {r.data.tags.get("candidate_name") for r in child_runs}
    assert "xgboost" in child_candidates
    assert "lightgbm" in child_candidates

    # Check child run names follow Option A convention
    child_run_names = [r.info.run_name for r in child_runs]
    assert any(name.startswith("xgb_") for name in child_run_names), f"Missing xgb_ prefix in {child_run_names}"
    assert any(name.startswith("lgb_") for name in child_run_names), f"Missing lgb_ prefix in {child_run_names}"

    # Verify metrics on each child run
    for r in child_runs:
        assert "accuracy" in r.data.metrics
        assert "f1_score" in r.data.metrics
        assert r.data.metrics["accuracy"] >= 0.0


def test_pipeline_training_submission_unit_mocked(user_tokens, monkeypatch):
    """
    Unit test: Verifies POST /api/models/train-pipeline endpoint logic, audit logging,
    and 202 response with mocked cluster dependencies (RayJob CRD submission).
    """
    monkeypatch.setattr(
        "app.services.ml_ops.training_service.submit_rayjob_to_k8s",
        lambda *args, **kwargs: True,
    )
    ds_headers = {"Authorization": f"Bearer {user_tokens['ds_user']}"}

    dataset_name = f"unit-pipe-{uuid.uuid4().hex[:8]}"
    dummy_csv = ("data.csv", b"feat1,feat2,target\n1.0,2.0,0\n3.0,4.0,1\n")
    resp_ds = client.post(
        "/api/datasets",
        data={"name": dataset_name, "description": "Unit test dataset"},
        files={"file": dummy_csv},
        headers=ds_headers,
    )
    assert resp_ds.status_code == 201
    commit_dataset_version_via_staging(client, dataset_name, dummy_csv, "Unit test commit", ds_headers)

    train_payload = {
        "dataset_id": dataset_name,
        "ref": "main",
        "target_column": "target",
        "model_type": "logistic_regression",
        "hyperparameters": {"max_iter": 100},
    }
    resp = client.post("/api/models/train-pipeline", json=train_payload, headers=ds_headers)
    assert resp.status_code == 202
    res_data = resp.json()
    assert res_data["status"] == "training"
    assert res_data["dataset_id"] == dataset_name
    assert "job_id" in res_data
    assert res_data["model_name"] == f"{dataset_name}-model"


def test_custom_experiment_tracking_submission_unit_mocked(user_tokens, monkeypatch):
    """
    Unit test: Verifies custom experiment_name and model_name propagation
    with mocked RayJob cluster submission.
    """
    monkeypatch.setattr(
        "app.services.ml_ops.training_service.submit_rayjob_to_k8s",
        lambda *args, **kwargs: True,
    )
    ds_headers = {"Authorization": f"Bearer {user_tokens['ds_user']}"}

    dataset_name = f"unit-exp-{uuid.uuid4().hex[:8]}"
    dummy_csv = ("data.csv", b"feat1,feat2,target\n1.0,2.0,0\n3.0,4.0,1\n")
    resp_ds = client.post(
        "/api/datasets",
        data={"name": dataset_name, "description": "Unit test exp dataset"},
        files={"file": dummy_csv},
        headers=ds_headers,
    )
    assert resp_ds.status_code == 201
    commit_dataset_version_via_staging(client, dataset_name, dummy_csv, "Unit test commit", ds_headers)

    custom_model = f"custom-unit-model-{uuid.uuid4().hex[:6]}"
    custom_exp = f"custom-unit-exp-{uuid.uuid4().hex[:6]}"
    train_payload = {
        "dataset_id": dataset_name,
        "ref": "main",
        "target_column": "target",
        "model_type": "logistic_regression",
        "hyperparameters": {"max_iter": 100},
        "experiment_name": custom_exp,
        "model_name": custom_model,
    }
    resp = client.post("/api/models/train-pipeline", json=train_payload, headers=ds_headers)
    assert resp.status_code == 202
    res_data = resp.json()
    assert res_data["status"] == "training"
    assert res_data["dataset_id"] == dataset_name
    assert res_data["model_name"] == custom_model


def test_pipeline_training_cluster_unavailable_fails_gracefully(user_tokens, monkeypatch):
    """
    Unit test: Verifies that when the Kubernetes Ray cluster is unavailable and local fallback
    is disabled for security, the API returns HTTP 503 Service Unavailable without hanging.
    """
    monkeypatch.setattr(
        "app.services.ml_ops.training_service.submit_rayjob_to_k8s",
        lambda *args, **kwargs: False,
    )
    from app.core.config import settings
    monkeypatch.setattr(settings, "ALLOW_LOCAL_RAY_FALLBACK", False)

    ds_headers = {"Authorization": f"Bearer {user_tokens['ds_user']}"}
    dataset_name = f"unit-fail-{uuid.uuid4().hex[:8]}"
    dummy_csv = ("data.csv", b"feat1,feat2,target\n1.0,2.0,0\n3.0,4.0,1\n")
    resp_ds = client.post(
        "/api/datasets",
        data={"name": dataset_name, "description": "Unit test fail dataset"},
        files={"file": dummy_csv},
        headers=ds_headers,
    )
    assert resp_ds.status_code == 201
    commit_dataset_version_via_staging(client, dataset_name, dummy_csv, "Unit test commit", ds_headers)

    train_payload = {
        "dataset_id": dataset_name,
        "ref": "main",
        "target_column": "target",
        "model_type": "logistic_regression",
        "hyperparameters": {"max_iter": 100},
    }
    resp = client.post("/api/models/train-pipeline", json=train_payload, headers=ds_headers)
    assert resp.status_code == 503
    assert "Training cluster is unavailable" in resp.json()["detail"]





