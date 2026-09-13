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
