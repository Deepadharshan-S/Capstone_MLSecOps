from typing import Optional
from pydantic import BaseModel


class TrainModelSchema(BaseModel):
    dataset_id: str
    ref: str = "main"  # The committed dataset version (branch, commit ID, or tag)
    epochs: int = 10
    hyperparameters: dict = {}
    code: str = ""  # The custom python code containing the training class
    experiment_name: Optional[str] = None
    model_name: Optional[str] = None  # Optional registered model name in MLflow


class TrainPipelineSchema(BaseModel):
    dataset_id: str
    ref: str = "main"  # The committed dataset version (branch, commit ID, or tag)
    target_column: str
    model_type: str  # e.g. logistic_regression, random_forest, or comma-separated for multi-model sweep
    hyperparameters: dict = {}
    experiment_name: Optional[str] = None
    model_name: Optional[str] = None  # Optional registered model name in MLflow


class TrainModelResponse(BaseModel):
    message: str
    job_id: str
    dataset_id: str
    epochs: Optional[int] = 0
    started_by: str
    status: str
    model_name: Optional[str] = None


class RayJobSummary(BaseModel):
    job_id: str
    rayjob_name: str
    status: str  # PENDING, RUNNING, SUCCEEDED, FAILED
    dataset_id: str
    ref: Optional[str] = "main"
    model_name: Optional[str] = None
    experiment_name: Optional[str] = None
    epochs: Optional[int] = 0
    started_by: str
    created_at: str
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    duration_seconds: Optional[float] = None
    error_message: Optional[str] = None


class RayJobListResponse(BaseModel):
    jobs: list[RayJobSummary]
    total: int


class RayJobDetailResponse(BaseModel):
    job_id: str
    rayjob_name: str
    status: str  # PENDING, RUNNING, SUCCEEDED, FAILED
    dataset_id: str
    ref: Optional[str] = "main"
    model_name: Optional[str] = None
    experiment_name: Optional[str] = None
    epochs: Optional[int] = 0
    hyperparameters: Optional[dict] = {}
    entrypoint: Optional[str] = None
    started_by: str
    created_at: str
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    duration_seconds: Optional[float] = None
    error_message: Optional[str] = None
    log_path: Optional[str] = None
    head_pod_name: Optional[str] = None
    head_pod_status: Optional[str] = None
    k8s_status: Optional[str] = None


class RayJobLogsResponse(BaseModel):
    job_id: str
    rayjob_name: str
    status: str
    source: str  # "kubernetes" or "minio" or "unavailable"
    logs: str
    lines_count: int
    tail_lines: Optional[int] = None
