from pydantic import BaseModel
from typing import Optional


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


class DeployModelSchema(BaseModel):
    model_id: str
    environment: str = "staging"
    version: Optional[str] = "latest"
    replicas: Optional[int] = 1


class ManageDeploymentSchema(BaseModel):
    deployment_id: str
    action: str = "restart"  # restart, rollback, stop


class TrainModelResponse(BaseModel):
    message: str
    job_id: str
    dataset_id: str
    epochs: int
    started_by: str
    status: str
    model_name: Optional[str] = None


class ModelItem(BaseModel):
    id: str
    name: str
    accuracy: float
    precision: Optional[float] = 0.0
    recall: Optional[float] = 0.0
    f1_score: Optional[float] = 0.0
    created_at: str
    experiment_name: Optional[str] = "unknown"
    parameters: Optional[dict] = {}
    tags: Optional[dict] = {}


class ModelListResponse(BaseModel):
    models: list[ModelItem]


class DeployModelResponse(BaseModel):
    message: str
    model_id: str
    version: Optional[str] = "1"
    environment: str
    deployed_by: str
    status: str
    endpoint_url: Optional[str] = None
    rayservice_name: Optional[str] = None
    deployment_id: Optional[str] = None


class ManageDeploymentResponse(BaseModel):
    message: str
    deployment_id: str
    action_taken: str
    triggered_by: str


class PredictionRequestSchema(BaseModel):
    dataframe_records: Optional[list[dict]] = None
    inputs: Optional[list[list]] = None


class PredictionResponseSchema(BaseModel):
    predictions: list
    model_name: str
    model_version: str
    latency_ms: float


class DeploymentItem(BaseModel):
    deployment_id: str
    model_name: str
    version: str
    environment: str
    status: str
    rayservice_name: Optional[str] = None
    endpoint_url: Optional[str] = None
    deployed_by: Optional[str] = None
    deployed_at: Optional[str] = None
    k8s_status: Optional[str] = "Unknown"


class DeploymentListResponse(BaseModel):
    deployments: list[DeploymentItem]

class UploadModelResponse(BaseModel):
    message: str
    model_id: str
    model_name: str
    version: Optional[str] = None
    experiment_name: str
    uploaded_by: str
    status: str
