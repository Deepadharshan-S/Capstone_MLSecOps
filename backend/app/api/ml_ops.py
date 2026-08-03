from fastapi import APIRouter, Security, status, Depends
from app.core.rate_limiter import RateLimiter

from app.api.permissions import get_current_active_user
from app.models.user import User
from app.schemas import (
    DatasetUploadSchema,
    TrainModelSchema,
    DeployModelSchema,
    ManageDeploymentSchema,
)
from app.services.ml_ops_service import ml_ops_service

router = APIRouter(prefix="", tags=["mlops"])


@router.post(
    "/datasets/upload",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(RateLimiter(times=5, seconds=60))],
)
def upload_dataset(
    dataset: DatasetUploadSchema,
    user: User = Security(get_current_active_user, scopes=["datasets:upload"]),
):
    """
    Upload datasets. Accessible to Data Scientists and Admins.
    """
    return ml_ops_service.perform_dataset_upload(dataset.name, user)


@router.post(
    "/models/train",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(RateLimiter(times=2, seconds=60))],
)
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


@router.post(
    "/models/deploy",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(RateLimiter(times=2, seconds=60))],
)
def deploy_model(
    deploy_info: DeployModelSchema,
    user: User = Security(get_current_active_user, scopes=["models:deploy"]),
):
    """
    Deploy models. Accessible to ML Engineers and Admins.
    """
    return ml_ops_service.perform_model_deploy(deploy_info.model_id, deploy_info.environment, user)


@router.post(
    "/deployments/manage",
    dependencies=[Depends(RateLimiter(times=10, seconds=60))],
)
def manage_deployment(
    manage_info: ManageDeploymentSchema,
    user: User = Security(get_current_active_user, scopes=["deployments:manage"]),
):
    """
    Manage deployments. Accessible to ML Engineers and Admins.
    """
    return ml_ops_service.perform_deployment_management(manage_info.deployment_id, manage_info.action, user)
