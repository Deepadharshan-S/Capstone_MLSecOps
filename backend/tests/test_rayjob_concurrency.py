import os
import unittest.mock as mock
import pytest
from unittest.mock import MagicMock

from app.core.config import settings
from app.services.ml_ops.utils import (
    get_active_rayjobs_count,
    wait_for_rayjob_capacity,
    submit_rayjob_to_k8s,
    _rayjob_submission_lock,
)


def test_get_active_rayjobs_count_filtering():
    """
    Verifies that get_active_rayjobs_count correctly distinguishes between
    active jobs (PENDING, RUNNING) and finished jobs (SUCCEEDED, FAILED, STOPPED).
    """
    mock_custom = MagicMock()
    mock_core = MagicMock()

    # Mock 4 RayJobs: 1 PENDING, 1 RUNNING, 1 SUCCEEDED, 1 FAILED
    mock_custom.list_namespaced_custom_object.return_value = {
        "items": [
            {
                "metadata": {"name": "rayjob-1"},
                "status": {"jobStatus": "PENDING", "jobDeploymentStatus": "Initializing"},
            },
            {
                "metadata": {"name": "rayjob-2"},
                "status": {"jobStatus": "RUNNING", "jobDeploymentStatus": "Running"},
            },
            {
                "metadata": {"name": "rayjob-3"},
                "status": {"jobStatus": "SUCCEEDED", "jobDeploymentStatus": "Complete"},
            },
            {
                "metadata": {"name": "rayjob-4"},
                "status": {"jobStatus": "FAILED", "jobDeploymentStatus": "Failed"},
            },
        ]
    }

    mock_core.list_namespaced_pod.return_value.items = []

    count = get_active_rayjobs_count(mock_custom, mock_core, namespace="default")
    # Only rayjob-1 and rayjob-2 are active
    assert count == 2


def test_get_active_rayjobs_count_with_active_pods():
    """
    Verifies that active/terminating head pods are taken into account
    even if the RayJob custom resource was already removed.
    """
    mock_custom = MagicMock()
    mock_core = MagicMock()

    # 1 RayJob in CRD list
    mock_custom.list_namespaced_custom_object.return_value = {
        "items": [
            {
                "metadata": {"name": "rayjob-1"},
                "status": {"jobStatus": "RUNNING", "jobDeploymentStatus": "Running"},
            }
        ]
    }

    # 2 pods: 1 Running pod, 1 Terminating pod (has deletion_timestamp)
    pod1 = MagicMock()
    pod1.status.phase = "Running"
    pod1.metadata.deletion_timestamp = None

    pod2 = MagicMock()
    pod2.status.phase = "Terminating"
    pod2.metadata.deletion_timestamp = "2026-10-10T02:00:00Z"

    mock_core.list_namespaced_pod.return_value.items = [pod1, pod2]

    count = get_active_rayjobs_count(mock_custom, mock_core, namespace="default")
    # max(1 CR, 2 pods) -> 2
    assert count == 2


def test_wait_for_rayjob_capacity_immediate_when_under_limit(monkeypatch):
    """
    Verifies wait_for_rayjob_capacity immediately returns True when active count < 2.
    """
    mock_custom = MagicMock()
    mock_core = MagicMock()

    mock_custom.list_namespaced_custom_object.return_value = {
        "items": [
            {
                "metadata": {"name": "rayjob-1"},
                "status": {"jobStatus": "RUNNING", "jobDeploymentStatus": "Running"},
            }
        ]
    }
    mock_core.list_namespaced_pod.return_value.items = []

    # Mock RayJobService.sweep_orphaned_job_resources
    with mock.patch("app.services.ml_ops.rayjob_service.RayJobService.sweep_orphaned_job_resources") as mock_sweep:
        mock_sweep.return_value = {}
        allowed = wait_for_rayjob_capacity(
            mock_custom, mock_core, namespace="default", max_concurrent=2, timeout_seconds=5.0
        )
        assert allowed is True
        mock_sweep.assert_called_once_with(namespace="default")


def test_wait_for_rayjob_capacity_blocks_and_resumes(monkeypatch):
    """
    Verifies wait_for_rayjob_capacity waits when at limit (2 active) and
    proceeds once an active job completes and active count drops below 2.
    """
    mock_custom = MagicMock()
    mock_core = MagicMock()

    call_count = [0]

    def mock_list_custom(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] <= 1:
            # First check: 2 active jobs
            return {
                "items": [
                    {"metadata": {"name": "job-1"}, "status": {"jobStatus": "RUNNING"}},
                    {"metadata": {"name": "job-2"}, "status": {"jobStatus": "RUNNING"}},
                ]
            }
        else:
            # Subsequent check: 1 job finished, only 1 active job remains
            return {
                "items": [
                    {"metadata": {"name": "job-2"}, "status": {"jobStatus": "RUNNING"}},
                ]
            }

    mock_custom.list_namespaced_custom_object.side_effect = mock_list_custom
    mock_core.list_namespaced_pod.return_value.items = []

    with mock.patch("app.services.ml_ops.rayjob_service.RayJobService.sweep_orphaned_job_resources") as mock_sweep:
        mock_sweep.return_value = {}
        allowed = wait_for_rayjob_capacity(
            mock_custom,
            mock_core,
            namespace="default",
            max_concurrent=2,
            timeout_seconds=5.0,
            poll_interval=0.01,
        )
        assert allowed is True
        assert call_count[0] >= 2


def test_wait_for_rayjob_capacity_respects_custom_env_limit(monkeypatch):
    """
    Verifies MAX_CONCURRENT_RAYJOBS env var is respected.
    """
    mock_custom = MagicMock()
    mock_core = MagicMock()

    # 1 active job
    mock_custom.list_namespaced_custom_object.return_value = {
        "items": [
            {"metadata": {"name": "job-1"}, "status": {"jobStatus": "RUNNING"}},
        ]
    }
    mock_core.list_namespaced_pod.return_value.items = []

    # If MAX_CONCURRENT_RAYJOBS=1, 1 active job should hit the limit and timeout
    monkeypatch.setenv("MAX_CONCURRENT_RAYJOBS", "1")

    with mock.patch("app.services.ml_ops.rayjob_service.RayJobService.sweep_orphaned_job_resources"):
        allowed = wait_for_rayjob_capacity(
            mock_custom,
            mock_core,
            namespace="default",
            max_concurrent=None,  # reads from env
            timeout_seconds=0.05,
            poll_interval=0.01,
        )
        assert allowed is False  # Timed out waiting


def test_submit_rayjob_to_k8s_invokes_capacity_check(monkeypatch):
    """
    Verifies submit_rayjob_to_k8s invokes wait_for_rayjob_capacity before creating K8s object.
    """
    test_yaml = """
apiVersion: ray.io/v1
kind: RayJob
metadata:
  name: rayjob-test-concurrency-1
  namespace: default
spec:
  entrypoint: python3 test.py
"""

    mock_custom = MagicMock()
    mock_core = MagicMock()
    mock_net = MagicMock()

    monkeypatch.setattr("kubernetes.config.load_incluster_config", MagicMock())
    monkeypatch.setattr("kubernetes.client.CustomObjectsApi", lambda: mock_custom)
    monkeypatch.setattr("kubernetes.client.CoreV1Api", lambda: mock_core)
    monkeypatch.setattr("kubernetes.client.NetworkingV1Api", lambda: mock_net)

    capacity_called = []

    def fake_wait(c_api, k_api, namespace="default", **kwargs):
        capacity_called.append(True)
        return True

    monkeypatch.setattr("app.services.ml_ops.utils.wait_for_rayjob_capacity", fake_wait)

    mock_custom.create_namespaced_custom_object.return_value = {"metadata": {"uid": "dummy-uid"}}

    res = submit_rayjob_to_k8s(
        rendered_yaml=test_yaml,
        job_id="test-concurrency-1",
        username="admin_user",
        mode_description="test",
    )

    assert res is True
    assert len(capacity_called) == 1
    mock_custom.create_namespaced_custom_object.assert_called_once()
