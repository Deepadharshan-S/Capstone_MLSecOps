class JobNotFoundError(Exception):
    """Raised when a requested training job does not exist in the platform."""

    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        super().__init__(f"Training job '{job_id}' not found.")


class JobAccessDeniedError(Exception):
    """Raised when an authenticated user attempts to access a job they do not own."""

    def __init__(self, detail: str = "Access denied: You do not have permission to access this training job.") -> None:
        self.detail = detail
        super().__init__(detail)


class JobLogsNotFoundError(Exception):
    """Raised when logs for a training job cannot be retrieved from any source."""

    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        super().__init__(f"Logs for training job '{job_id}' not found.")


class ClusterUnavailableError(Exception):
    """Raised when the Kubernetes cluster is offline and local fallback is not allowed."""

    def __init__(self, detail: str = "Training cluster is unavailable.") -> None:
        self.detail = detail
        super().__init__(detail)
