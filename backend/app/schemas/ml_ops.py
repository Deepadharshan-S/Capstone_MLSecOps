from pydantic import BaseModel


class TrainModelSchema(BaseModel):
    dataset_id: str
    epochs: int = 10
    hyperparameters: dict = {}


class DeployModelSchema(BaseModel):
    model_id: str
    environment: str = "staging"


class ManageDeploymentSchema(BaseModel):
    deployment_id: str
    action: str = "restart"  # restart, rollback, stop


class TrainModelResponse(BaseModel):
    message: str
    dataset_id: str
    epochs: int
    started_by: str
    status: str


class ModelItem(BaseModel):
    id: str
    name: str
    accuracy: float
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
