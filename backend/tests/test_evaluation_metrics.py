import os
import tempfile
import pandas as pd
import numpy as np
import pytest
from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.neighbors import KNeighborsClassifier
from sklearn.pipeline import Pipeline
from sklearn.compose import ColumnTransformer
from sklearn.preprocessing import StandardScaler, OneHotEncoder

from app.services.ml_ops.ray_wrapper import calculate_metrics
from app.schemas.ml_ops.training import JobStatusResponse


@pytest.fixture
def sample_csv():
    """Creates a temporary CSV file with mixed numerical and categorical features."""
    df = pd.DataFrame({
        "age": [25, 30, 45, 50, 22, 60, 35, 40],
        "income": [50000, 60000, 80000, 110000, 30000, 120000, 75000, 90000],
        "category": ["A", "B", "A", "C", "B", "C", "A", "B"],
        "target": [0, 0, 1, 1, 0, 1, 0, 1],
    })
    with tempfile.NamedTemporaryFile(suffix=".csv", mode="w", delete=False) as f:
        df.to_csv(f.name, index=False)
        temp_path = f.name
    yield temp_path
    if os.path.exists(temp_path):
        os.remove(temp_path)


def test_feature_importance_and_roc_random_forest(sample_csv):
    """Verifies feature importance and ROC curve for a tree-based model."""
    df = pd.read_csv(sample_csv)
    X = df[["age", "income"]]
    y = df["target"]

    model = RandomForestClassifier(n_estimators=10, random_state=42)
    model.fit(X, y)

    metrics, X_out, y_pred = calculate_metrics(model, sample_csv, target_col="target")

    # 1. Feature importance assertions
    assert "feature_importance" in metrics
    fi = metrics["feature_importance"]
    assert isinstance(fi, list)
    assert len(fi) == 2
    for item in fi:
        assert "name" in item
        assert "value" in item
        assert isinstance(item["value"], float)
        assert item["value"] >= 0.0

    # Ensure sorted descending
    assert fi[0]["value"] >= fi[1]["value"]

    # 2. ROC curve assertions
    assert "roc_curve" in metrics
    roc = metrics["roc_curve"]
    assert isinstance(roc, list)
    assert len(roc) == 51

    # Boundary conditions
    assert roc[0]["fpr"] == 0.0
    assert roc[0]["tpr"] == 0.0
    assert roc[-1]["fpr"] == 1.0
    assert roc[-1]["tpr"] == 1.0

    # Strictly monotonic FPRs (no duplicate zeros)
    fpr_values = [p["fpr"] for p in roc]
    assert len(set(fpr_values)) == len(fpr_values)
    assert sorted(fpr_values) == fpr_values

    # Monotonically non-decreasing TPRs
    tpr_values = [p["tpr"] for p in roc]
    for i in range(len(tpr_values) - 1):
        assert tpr_values[i] <= tpr_values[i + 1]

    # ROC AUC
    assert "roc_auc" in metrics
    assert 0.0 <= metrics["roc_auc"] <= 1.0


def test_feature_importance_pipeline_with_onehot(sample_csv):
    """
    Verifies feature importance extraction from a Pipeline with ColumnTransformer,
    even when OneHotEncoder expands the feature space beyond X.columns length.
    """
    df = pd.read_csv(sample_csv)
    X = df[["age", "income", "category"]]
    y = df["target"]

    preprocessor = ColumnTransformer(
        transformers=[
            ("num", StandardScaler(), ["age", "income"]),
            ("cat", OneHotEncoder(handle_unknown="ignore", sparse_output=False), ["category"]),
        ]
    )
    pipeline = Pipeline([
        ("preprocessor", preprocessor),
        ("classifier", RandomForestClassifier(n_estimators=10, random_state=42)),
    ])
    pipeline.fit(X, y)

    metrics, _, _ = calculate_metrics(pipeline, sample_csv, target_col="target")

    assert "feature_importance" in metrics
    fi = metrics["feature_importance"]
    assert isinstance(fi, list)
    assert len(fi) > 0

    # Check feature names are extracted and cleaned (e.g. category_A, age, etc.)
    names = [item["name"] for item in fi]
    assert any("category" in n or "age" in n or "income" in n for n in names)


def test_feature_importance_multiclass_linear_model():
    """
    Verifies feature importance for multi-class linear models where coef_ is 2D.
    """
    df = pd.DataFrame({
        "f1": [1.0, 2.0, 5.0, 6.0, 9.0, 10.0],
        "f2": [1.1, 1.9, 5.2, 5.8, 8.9, 10.1],
        "target": [0, 0, 1, 1, 2, 2],
    })
    with tempfile.NamedTemporaryFile(suffix=".csv", mode="w", delete=False) as f:
        df.to_csv(f.name, index=False)
        temp_path = f.name

    try:
        model = LogisticRegression(max_iter=100)
        model.fit(df[["f1", "f2"]], df["target"])

        metrics, _, _ = calculate_metrics(model, temp_path, target_col="target")
        assert "feature_importance" in metrics
        fi = metrics["feature_importance"]
        assert len(fi) == 2
        assert fi[0]["name"] in ["f1", "f2"]
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


def test_feature_importance_permutation_fallback():
    """
    Verifies permutation importance fallback for non-linear / non-tree models (e.g. KNN).
    """
    df = pd.DataFrame({
        "feature_a": [1.0, 2.0, 3.0, 8.0, 9.0, 10.0],
        "feature_b": [0.1, 0.2, 0.1, 0.9, 0.8, 0.9],
        "target": [0, 0, 0, 1, 1, 1],
    })
    with tempfile.NamedTemporaryFile(suffix=".csv", mode="w", delete=False) as f:
        df.to_csv(f.name, index=False)
        temp_path = f.name

    try:
        model = KNeighborsClassifier(n_neighbors=2)
        model.fit(df[["feature_a", "feature_b"]], df["target"])

        metrics, _, _ = calculate_metrics(model, temp_path, target_col="target")
        assert "feature_importance" in metrics
        fi = metrics["feature_importance"]
        assert isinstance(fi, list)
        assert len(fi) == 2
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


def test_job_status_response_schema_roc_auc():
    """Verifies JobStatusResponse includes roc_auc and artifact fields."""
    resp = JobStatusResponse(
        job_id="test-job-123",
        status="completed",
        progress=100,
        roc_auc=0.9542,
        feature_importance=[{"name": "f1", "value": 0.8}],
        roc_curve=[{"fpr": 0.0, "tpr": 0.0}, {"fpr": 1.0, "tpr": 1.0}],
    )
    assert resp.roc_auc == 0.9542
    assert len(resp.feature_importance) == 1
    assert len(resp.roc_curve) == 2


def test_learning_curve_random_forest(sample_csv):
    """Verifies that non-iterative models produce sample-size based learning curves."""
    df = pd.read_csv(sample_csv)
    # Expand dataset slightly to allow 3-fold cross validation
    df_expanded = pd.concat([df] * 3, ignore_index=True)
    with tempfile.NamedTemporaryFile(suffix=".csv", mode="w", delete=False) as f:
        df_expanded.to_csv(f.name, index=False)
        temp_path = f.name

    try:
        model = RandomForestClassifier(n_estimators=10, random_state=42)
        model.fit(df_expanded[["age", "income"]], df_expanded["target"])

        metrics, _, _ = calculate_metrics(model, temp_path, target_col="target")
        assert "history" in metrics
        hist = metrics["history"]
        assert isinstance(hist, list)
        assert len(hist) > 0
        for pt in hist:
            assert "epoch" in pt
            assert "train" in pt
            assert "val" in pt
            assert 0.0 <= pt["train"] <= 1.0
            assert 0.0 <= pt["val"] <= 1.0
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


def test_learning_curve_mlp(sample_csv):
    """Verifies that MLPClassifier produces epoch-by-epoch loss & validation history."""
    from sklearn.neural_network import MLPClassifier
    df = pd.read_csv(sample_csv)
    df_expanded = pd.concat([df] * 4, ignore_index=True)
    with tempfile.NamedTemporaryFile(suffix=".csv", mode="w", delete=False) as f:
        df_expanded.to_csv(f.name, index=False)
        temp_path = f.name

    try:
        model = MLPClassifier(max_iter=15, early_stopping=True, random_state=42)
        model.fit(df_expanded[["age", "income"]], df_expanded["target"])

        metrics, _, _ = calculate_metrics(model, temp_path, target_col="target")
        assert "history" in metrics
        hist = metrics["history"]
        assert isinstance(hist, list)
        assert len(hist) > 0
        for pt in hist:
            assert "epoch" in pt
            assert isinstance(pt["epoch"], int)
            assert "train" in pt
            assert isinstance(pt["train"], float)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


def test_learning_curve_custom_trainer(sample_csv):
    """Verifies that custom trainer objects exposing history are captured cleanly."""
    class DummyTrainerModel:
        def __init__(self):
            self.history = [
                {"epoch": 1, "train": 0.85, "val": 0.80},
                {"epoch": 2, "train": 0.90, "val": 0.86},
                {"epoch": 3, "train": 0.95, "val": 0.91},
            ]
        def predict(self, X):
            return np.zeros(len(X), dtype=int)

    model = DummyTrainerModel()
    metrics, _, _ = calculate_metrics(model, sample_csv, target_col="target")
    assert "history" in metrics
    assert metrics["history"] == model.history


def test_job_status_response_schema_history():
    """Verifies JobStatusResponse includes history field."""
    hist = [{"epoch": 1, "train": 0.9, "val": 0.85}]
    resp = JobStatusResponse(
        job_id="test-job-456",
        status="completed",
        progress=100,
        history=hist,
    )
    assert resp.history == hist

