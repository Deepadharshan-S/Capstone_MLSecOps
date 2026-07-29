from fastapi import APIRouter, Security, status
from pydantic import BaseModel

from app.api.permissions import get_current_active_user
from app.models.user import User
from app.services import ml_ops_service

router = APIRouter(prefix="", tags=["mlops"])


class DatasetUploadSchema(BaseModel):
    name: str
    description: str


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


@router.post("/datasets/upload", status_code=status.HTTP_201_CREATED)
def upload_dataset(
    dataset: DatasetUploadSchema,
    user: User = Security(get_current_active_user, scopes=["datasets:upload"]),
):
    """
    Upload datasets. Accessible to Data Scientists and Admins.
    """
    return ml_ops_service.perform_dataset_upload(dataset.name, user)


@router.post("/models/train", status_code=status.HTTP_202_ACCEPTED)
def train_model(
    train_info: TrainModelSchema,
    user: User = Security(get_current_active_user, scopes=["models:train"]),
):
    """
    Start model training. Accessible to Data Scientists and Admins.
    """
    return ml_ops_service.perform_model_training(train_info.dataset_id, train_info.epochs, user)


@router.get("/models")
def view_models(
    user: User = Security(get_current_active_user, scopes=["models:view"]),
):
    """
    View models. Accessible to all roles (Viewer, ML Engineer, Data Scientist, Admin).
    """
    return ml_ops_service.retrieve_models(user)


@router.post("/models/deploy", status_code=status.HTTP_201_CREATED)
def deploy_model(
    deploy_info: DeployModelSchema,
    user: User = Security(get_current_active_user, scopes=["models:deploy"]),
):
    """
    Deploy models. Accessible to ML Engineers and Admins.
    """
    return ml_ops_service.perform_model_deploy(deploy_info.model_id, deploy_info.environment, user)


@router.post("/deployments/manage")
def manage_deployment(
    manage_info: ManageDeploymentSchema,
    user: User = Security(get_current_active_user, scopes=["deployments:manage"]),
):
    """
    Manage deployments. Accessible to ML Engineers and Admins.
    """
    return ml_ops_service.perform_deployment_management(manage_info.deployment_id, manage_info.action, user)
