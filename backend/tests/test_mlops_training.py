import uuid
import time
import pytest
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app, base_url="https://testserver.local")


@pytest.fixture(scope="module")
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


def test_model_training_flow(user_tokens):
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

    # Commit the dataset so it represents a committed version in lakeFS
    commit_payload = {
        "message": "Initial dataset commit for training",
        "metadata": {"author": "ds_user"},
    }
    resp = client.post(
        f"/api/datasets/{dataset_name}/commit?branch=main",
        json=commit_payload,
        headers=ds_headers,
    )
    assert resp.status_code == 200

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
    assert resp.status_code == 202

    res_data = resp.json()
    assert res_data["status"] == "training"
    assert res_data["dataset_id"] == dataset_name
    assert "job_id" in res_data

    # 3. Wait for the training job to complete (asynchronous background execution)
    model_registered = False
    model_name = f"{dataset_name}-model"

    # Poll /api/models for up to 180 seconds (120 attempts with 1.5s sleep)
    for _ in range(120):
        time.sleep(1.5)
        resp_models = client.get("/api/models", headers=ds_headers)
        assert resp_models.status_code == 200
        models = resp_models.json()["models"]
        names = [m["name"] for m in models]
        if model_name in names:
            model_registered = True
            break


    assert (
        model_registered
    ), f"Model '{model_name}' was not registered in MLflow registry within the timeout."
