from typing import Optional
from datetime import datetime
from pydantic import BaseModel, field_validator


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

    @field_validator("model_type")
    @classmethod
    def validate_model_type(cls, v: str) -> str:
        from app.services.ml_ops.supported_models import ALL_ACCEPTED_TYPES, parse_model_list

        requested = [m.strip() for m in (v or "").split(",") if m.strip()]
        if not requested:
            raise ValueError("model_type cannot be empty.")
        bad = [m for m in requested if m.strip().lower() not in ALL_ACCEPTED_TYPES]
        if bad:
            raise ValueError(
                f"Unsupported model type: {', '.join(bad)}. "
                f"Supported types: {', '.join(ALL_ACCEPTED_TYPES)}"
            )
        # Normalize aliases (svc -> svm, xgb -> xgboost) so downstream
        # code and job records always use canonical names.
        return ",".join(parse_model_list(v))

    @field_validator("hyperparameters", mode="before")
    @classmethod
    def normalize_hyperparameters(cls, v) -> dict:
        if v is None:
            return {}
        if not isinstance(v, dict):
            raise ValueError("hyperparameters must be an object.")
        out: dict = {}
        for k, val in v.items():
            key = str(k).strip()
            if not key:
                continue
            # The old frontend lower-cased every key ("C" -> "c"), which
            # silently broke LogisticRegression/SVC. Recover the canonical
            # sklearn casing for known single-letter params.
            if key == "c" and "C" not in v:
                key = "C"
            # MLP hidden_layer_sizes arrives from the UI as "128,64" or
            # 128 — sklearn needs a tuple of ints.
            if key == "hidden_layer_sizes" and isinstance(val, str):
                parts = [p.strip() for p in val.split(",") if p.strip()]
                try:
                    nums = tuple(int(float(p)) for p in parts)
                    val = nums[0] if len(nums) == 1 else nums
                except ValueError:
                    pass
            # Drop params no sklearn estimator accepts (legacy UI sent
            # "dropout" for MLP, which only caused a fallback to defaults).
            if key.lower() == "dropout":
                continue
            out[key] = val
        return out


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


class JobStatusResponse(BaseModel):
    job_id: str
    status: str
    progress: int
    model_name: Optional[str] = None
    model_type: Optional[str] = None
    dataset_id: Optional[str] = None
    accuracy: Optional[float] = None
    precision_score: Optional[float] = None
    recall_score: Optional[float] = None
    f1_score: Optional[float] = None
    training_duration: Optional[float] = None
    confusion_matrix: Optional[dict] = None
    feature_importance: Optional[list] = None
    roc_curve: Optional[list] = None
    history: Optional[list] = None
    error_message: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    logs: Optional[list[dict]] = None
