from pydantic import BaseModel
from typing import Optional


class TrainModelSchema(BaseModel):
    dataset_id: str
    ref: str = "main"  # The committed dataset version (branch, commit ID, or tag)
    epochs: int = 10
    hyperparameters: dict = {}
    code: str = ""  # The custom python code containing the training class


class TrainPipelineSchema(BaseModel):
    dataset_id: str
    ref: str = "main"  # The committed dataset version (branch, commit ID, or tag)
    target_column: str
    model_type: str  # e.g. logistic_regression, random_forest, decision_tree, etc.
    hyperparameters: dict = {}


class DeployModelSchema(BaseModel):
    model_id: str
    environment: str = "staging"


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


class ModelItem(BaseModel):
    id: str
    name: str
    accuracy: float
    precision: Optional[float] = 0.0
    recall: Optional[float] = 0.0
    f1_score: Optional[float] = 0.0
    created_at: str


class ModelListResponse(BaseModel):
    models: list[ModelItem]


class DeployModelResponse(BaseModel):
    message: str
    model_id: str
    environment: str
    deployed_by: str
    status: str


class ManageDeploymentResponse(BaseModel):
    message: str
    deployment_id: str
    action_taken: str
    triggered_by: str
