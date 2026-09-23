from typing import Optional
from app.services.interfaces import ObjectStorageService
from app.services.ml_ops.rayjob_service import RayJobService


class TrainingLogService:
    """
    Decouples log retrieval and archival from job management.
    Hides log storage locations (Kubernetes live pod vs. MinIO persistent archive)
    from higher-level business services and routers.
    """

    def __init__(
        self,
        storage_service: ObjectStorageService,
        rayjob_service: RayJobService,
    ) -> None:
        self.storage_service = storage_service
        self.rayjob_service = rayjob_service

    def get_logs(
        self,
        job_id: str,
        rayjob_name: str,
        status: str,
        tail_lines: Optional[int] = 1000,
        error_message: Optional[str] = None,
    ) -> dict:
        """
        Retrieves training execution logs according to the job lifecycle:
        - For active jobs (PENDING/RUNNING): attempts live read from Ray head pod.
        - For completed jobs: retrieves persistent archive from MinIO.
        - Fallback: retrieves from lingering head pod if MinIO archive not yet present.
        """
        clean_id = job_id.removeprefix("rayjob-")
        log_key = f"logs/{clean_id}/training.log"

        # 1. Live Kubernetes head-pod lookup for active jobs
        if status in ("PENDING", "RUNNING"):
            head_pod = self.rayjob_service.get_head_pod(clean_id)
            if head_pod:
                pod_name, _ = head_pod
                logs = self.rayjob_service.get_head_pod_logs(pod_name=pod_name, tail_lines=tail_lines)
                if logs is not None:
                    lines = logs.splitlines()
                    return {
                        "job_id": clean_id,
                        "rayjob_name": rayjob_name,
                        "status": status,
                        "source": "kubernetes",
                        "logs": logs,
                        "lines_count": len(lines),
                        "tail_lines": tail_lines,
                    }

        # 2. MinIO persistent archive lookup for completed jobs or if head pod is absent
        archived = self.storage_service.get_log_content("mlflow", log_key)
        if archived is not None:
            lines = archived.splitlines()
            if tail_lines and len(lines) > tail_lines:
                archived = "\n".join(lines[-tail_lines:])
                lines = archived.splitlines()
            return {
                "job_id": clean_id,
                "rayjob_name": rayjob_name,
                "status": status,
                "source": "minio",
                "logs": archived,
                "lines_count": len(lines),
                "tail_lines": tail_lines,
            }

        # 3. Fallback: if pod still lingers right after completion, extract and persist
        head_pod = self.rayjob_service.get_head_pod(clean_id)
        if head_pod:
            pod_name, _ = head_pod
            logs = self.rayjob_service.get_head_pod_logs(pod_name=pod_name, tail_lines=tail_lines)
            if logs:
                try:
                    self.storage_service.put_log_content("mlflow", log_key, logs)
                except Exception:
                    pass
                lines = logs.splitlines()
                return {
                    "job_id": clean_id,
                    "rayjob_name": rayjob_name,
                    "status": status,
                    "source": "kubernetes",
                    "logs": logs,
                    "lines_count": len(lines),
                    "tail_lines": tail_lines,
                }

        # 4. Logs are unavailable (pod removed and no MinIO archive)
        msg = error_message or "Logs are no longer available for this historical job."
        return {
            "job_id": clean_id,
            "rayjob_name": rayjob_name,
            "status": status,
            "source": "unavailable",
            "logs": msg,
            "lines_count": 1,
            "tail_lines": tail_lines,
        }

    def archive_pod_logs(self, job_id: str) -> bool:
        """
        Extracts logs from the live Ray head pod and persists them to MinIO.
        Idempotent: skips if already archived.
        """
        clean_id = job_id.removeprefix("rayjob-")
        log_key = f"logs/{clean_id}/training.log"

        if self.storage_service.get_log_content("mlflow", log_key) is not None:
            return True

        head_pod = self.rayjob_service.get_head_pod(clean_id)
        if not head_pod:
            return False

        pod_name, _ = head_pod
        logs = self.rayjob_service.get_head_pod_logs(pod_name=pod_name)
        if not logs:
            return False

        try:
            self.storage_service.put_log_content("mlflow", log_key, logs)
            return True
        except Exception as e:
            print(f"Notice: Failed to archive pod logs to MinIO ({e}).")
            return False

    def archive_log_content(self, job_id: str, content: str) -> None:
        """Persists raw log text content directly to MinIO under the job's log key."""
        clean_id = job_id.removeprefix("rayjob-")
        log_key = f"logs/{clean_id}/training.log"
        self.storage_service.put_log_content("mlflow", log_key, content)

    def has_archived_logs(self, job_id: str) -> bool:
        """Checks if persistent logs are available in MinIO for this job."""
        clean_id = job_id.removeprefix("rayjob-")
        log_key = f"logs/{clean_id}/training.log"
        return self.storage_service.get_log_content("mlflow", log_key) is not None
