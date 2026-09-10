import os
import pytest
from unittest.mock import MagicMock
from fastapi import HTTPException

from app.core.config import settings
from app.models.user import User
from app.services.ml_ops_service import ml_ops_service


@pytest.fixture(scope="session", autouse=True)
def cleanup_after_tests():
    """Override session teardown to prevent connection stalls when lakeFS is offline."""
    yield


@pytest.fixture
def mock_user():
    return User(username="security_tester", role="admin")


def test_local_fallback_disabled_raises_http_503(mock_user, monkeypatch):
    """
    Verify that when ALLOW_LOCAL_RAY_FALLBACK is False and Kubernetes is offline,
    perform_model_training immediately raises HTTP 503 instead of running locally.
    """
    monkeypatch.setattr(settings, "ALLOW_LOCAL_RAY_FALLBACK", False)

    import subprocess
    monkeypatch.setattr("subprocess.run", MagicMock(side_effect=subprocess.CalledProcessError(1, ["kubectl"])))

    sample_code = "class Trainer:\n    def train(self, *args, **kwargs):\n        pass"

    with pytest.raises(HTTPException) as exc_info:
        ml_ops_service.perform_model_training(
            dataset_id="sec-dataset",
            ref="main",
            epochs=1,
            hyperparameters={},
            code=sample_code,
            user=mock_user,
        )

    assert exc_info.value.status_code == 503
    assert "local fallback execution is disabled for security" in exc_info.value.detail


def test_pipeline_training_local_fallback_disabled_raises_http_503(mock_user, monkeypatch):
    """
    Verify that perform_pipeline_training also enforces ALLOW_LOCAL_RAY_FALLBACK.
    """
    monkeypatch.setattr(settings, "ALLOW_LOCAL_RAY_FALLBACK", False)

    import subprocess
    monkeypatch.setattr("subprocess.run", MagicMock(side_effect=subprocess.CalledProcessError(1, ["kubectl"])))

    with pytest.raises(HTTPException) as exc_info:
        ml_ops_service.perform_pipeline_training(
            dataset_id="sec-pipeline-dataset",
            ref="main",
            target_column="label",
            model_type="logistic_regression",
            hyperparameters={},
            user=mock_user,
        )

    assert exc_info.value.status_code == 503
    assert "local fallback execution is disabled for security" in exc_info.value.detail


def test_local_fallback_enabled_permits_execution(mock_user, monkeypatch):
    """
    Verify that when ALLOW_LOCAL_RAY_FALLBACK is True, execution proceeds.
    """
    monkeypatch.setattr(settings, "ALLOW_LOCAL_RAY_FALLBACK", True)

    # Mock subprocess.run inside run_training_subprocess to avoid running actual heavy jobs
    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: MagicMock(returncode=0))

    sample_code = "class Trainer:\n    def train(self, *args, **kwargs):\n        pass"

    res = ml_ops_service.perform_model_training(
        dataset_id="sec-dataset-allowed",
        ref="main",
        epochs=1,
        hyperparameters={},
        code=sample_code,
        user=mock_user,
    )

    assert res["status"] == "training"
    assert "job_id" in res
    assert res["started_by"] == mock_user.username


def test_rendered_yaml_hardened_security_context(mock_user, monkeypatch):
    """
    Verify that the rendered RayJob YAML template contains:
    - automountServiceAccountToken: false
    - runAsNonRoot: true
    - runAsUser: 1000
    - capabilities drop ALL
    - NO hostPath mount
    - NO workspace-volume
    """
    monkeypatch.setattr(settings, "ALLOW_LOCAL_RAY_FALLBACK", True)
    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: MagicMock(returncode=0))

    sample_code = "class Trainer:\n    def train(self, *args, **kwargs):\n        pass"
    ml_ops_service.perform_model_training(
        dataset_id="sec-dataset-yaml",
        ref="main",
        epochs=1,
        hyperparameters={},
        code=sample_code,
        user=mock_user,
    )

    log_path = os.path.join(os.path.dirname(__file__), "..", "logs", "last_rendered_yaml.yaml")
    assert os.path.exists(log_path), "Rendered YAML log file was not generated"

    with open(log_path, "r") as f:
        yaml_content = f.read()

    # SecurityContext assertions
    assert "automountServiceAccountToken: false" in yaml_content
    assert "runAsNonRoot: true" in yaml_content
    assert "runAsUser: 1000" in yaml_content
    assert "allowPrivilegeEscalation: false" in yaml_content
    assert "drop:" in yaml_content
    assert "- ALL" in yaml_content

    # Absence of insecure hostPath volume
    assert "hostPath:" not in yaml_content
    assert "workspace-volume" not in yaml_content


def test_rendered_yaml_contains_network_policy(mock_user, monkeypatch):
    """
    Verify that the rendered YAML includes a NetworkPolicy resource restricting egress.
    """
    monkeypatch.setattr(settings, "ALLOW_LOCAL_RAY_FALLBACK", True)
    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: MagicMock(returncode=0))

    sample_code = "class Trainer:\n    def train(self, *args, **kwargs):\n        pass"
    ml_ops_service.perform_model_training(
        dataset_id="sec-dataset-netpol",
        ref="main",
        epochs=1,
        hyperparameters={},
        code=sample_code,
        user=mock_user,
    )

    log_path = os.path.join(os.path.dirname(__file__), "..", "logs", "last_rendered_yaml.yaml")
    with open(log_path, "r") as f:
        yaml_content = f.read()

    # NetworkPolicy assertions
    assert "kind: NetworkPolicy" in yaml_content
    assert "rayjob-netpol-" in yaml_content
    assert "port: 8000" in yaml_content   # lakeFS
    assert "port: 9000" in yaml_content   # MinIO
    assert "port: 5000" in yaml_content   # MLflow
    assert "port: 53" in yaml_content     # DNS


def test_scoped_credentials_generation(monkeypatch):
    """
    Verify _get_scoped_training_credentials returns correctly structured credentials
    and respects explicit training credential overrides.
    """
    # 1. Test fallback / defaults
    creds = ml_ops_service._get_scoped_training_credentials("test-job-id")
    assert "aws_access_key_id" in creds
    assert "aws_secret_access_key" in creds
    assert "lakefs_access_key_id" in creds
    assert "lakefs_secret_access_key" in creds
    assert "aws_session_token" in creds

    # 2. Test explicit training keys override
    monkeypatch.setattr(settings, "MINIO_TRAINING_ACCESS_KEY_ID", "scoped-minio-key")
    monkeypatch.setattr(settings, "MINIO_TRAINING_SECRET_ACCESS_KEY", "scoped-minio-secret")
    monkeypatch.setattr(settings, "LAKEFS_TRAINING_ACCESS_KEY_ID", "scoped-lakefs-key")
    monkeypatch.setattr(settings, "LAKEFS_TRAINING_SECRET_ACCESS_KEY", "scoped-lakefs-secret")

    overridden = ml_ops_service._get_scoped_training_credentials("test-job-id")
    assert overridden["aws_access_key_id"] == "scoped-minio-key"
    assert overridden["aws_secret_access_key"] == "scoped-minio-secret"
    assert overridden["lakefs_access_key_id"] == "scoped-lakefs-key"
    assert overridden["lakefs_secret_access_key"] == "scoped-lakefs-secret"


def test_scoped_credentials_sts_assume_role(monkeypatch):
    """
    Verify that when STS assume_role succeeds, temporary credentials and session token are populated.
    """
    fake_sts_client = MagicMock()
    fake_sts_client.assume_role.return_value = {
        "Credentials": {
            "AccessKeyId": "ASIA_TEST_ACCESS_KEY",
            "SecretAccessKey": "test_secret_key_123",
            "SessionToken": "session_token_xyz_456",
        }
    }

    import boto3
    monkeypatch.setattr(boto3, "client", lambda service, **kwargs: fake_sts_client if service == "sts" else MagicMock())

    creds = ml_ops_service._get_scoped_training_credentials("test-job-sts")
    assert creds["scoped_sts"] is True
    assert creds["aws_access_key_id"] == "ASIA_TEST_ACCESS_KEY"
    assert creds["aws_secret_access_key"] == "test_secret_key_123"
    assert creds["aws_session_token"] == "session_token_xyz_456"
