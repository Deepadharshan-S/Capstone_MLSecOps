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
