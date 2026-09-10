from fastapi import APIRouter, Security, status, Depends, File, UploadFile, Form, HTTPException
from typing import Optional
from app.core.rate_limiter import RateLimiter

from app.api.permissions import get_current_active_user
from app.models.user import User
from app.schemas import (
    TrainModelSchema,
    TrainPipelineSchema,
    DeployModelSchema,
    ManageDeploymentSchema,
    TrainModelResponse,
    ModelListResponse,
    DeployModelResponse,
    ManageDeploymentResponse,
    UploadModelResponse,
)
from app.services.dependencies import get_ml_ops_service
from app.services.ml_ops_service import MLOpsService

router = APIRouter(prefix="", tags=["mlops"])


@router.post(
    "/models/train",
    response_model=TrainModelResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(RateLimiter(times=2, seconds=60))],
)
def train_model(
    train_info: TrainModelSchema,
    user: User = Security(get_current_active_user, scopes=["models:train"]),
    ml_ops_service: MLOpsService = Depends(get_ml_ops_service),
):
    """
    Start model training. Accessible to Data Scientists and Admins.
    """
    return ml_ops_service.perform_model_training(
        dataset_id=train_info.dataset_id,
        ref=train_info.ref,
        epochs=train_info.epochs,
        hyperparameters=train_info.hyperparameters,
        code=train_info.code,
        user=user,
        experiment_name=train_info.experiment_name,
        model_name=train_info.model_name,
    )


@router.post(
    "/models/train-pipeline",
    response_model=TrainModelResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(RateLimiter(times=2, seconds=60))],
)
def train_pipeline(
    train_info: TrainPipelineSchema,
    user: User = Security(get_current_active_user, scopes=["models:train"]),
    ml_ops_service: MLOpsService = Depends(get_ml_ops_service),
):
    """
    Start automated pipeline training. Accessible to Data Scientists and Admins.
    """
    return ml_ops_service.perform_pipeline_training(
        dataset_id=train_info.dataset_id,
        ref=train_info.ref,
        target_column=train_info.target_column,
        model_type=train_info.model_type,
        hyperparameters=train_info.hyperparameters,
        user=user,
        experiment_name=train_info.experiment_name,
        model_name=train_info.model_name,
    )


@router.get("/models", response_model=ModelListResponse)
def view_models(
    user: User = Security(get_current_active_user, scopes=["models:view"]),
    ml_ops_service: MLOpsService = Depends(get_ml_ops_service),
):
    """
    View models. Accessible to all roles (Viewer, ML Engineer, Data Scientist, Admin).
    """
    return ml_ops_service.retrieve_models(user)


@router.post(
    "/models/upload",
    response_model=UploadModelResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(RateLimiter(times=5, seconds=60))],
)
def upload_model(
    file: UploadFile = File(...),
    model_name: Optional[str] = Form(None),
    experiment_name: Optional[str] = Form(None),
    metadata: Optional[str] = Form(None),
    metrics: Optional[str] = Form(None),
    user: User = Security(get_current_active_user, scopes=["models:train"]),
    ml_ops_service: MLOpsService = Depends(get_ml_ops_service),
):
    """
    Upload a .pkl model file. Accessible to Data Scientists and Admins.
    Registers the model in MLflow Model Registry and enforces upload size limits.
    """
    if not file.filename or not file.filename.endswith(".pkl"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only .pkl files are allowed.",
        )
        
    return ml_ops_service.perform_model_upload(
        file=file,
        user=user,
        model_name=model_name,
        experiment_name=experiment_name,
        metadata=metadata,
        metrics=metrics,
    )


@router.post(
    "/models/deploy",
    response_model=DeployModelResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(RateLimiter(times=2, seconds=60))],
)
def deploy_model(
    deploy_info: DeployModelSchema,
    user: User = Security(get_current_active_user, scopes=["models:deploy"]),
    ml_ops_service: MLOpsService = Depends(get_ml_ops_service),
):
    """
    Deploy models. Accessible to ML Engineers and Admins.
    """
    return ml_ops_service.perform_model_deploy(
        deploy_info.model_id, deploy_info.environment, user
    )


@router.post(
    "/deployments/manage",
    response_model=ManageDeploymentResponse,
    dependencies=[Depends(RateLimiter(times=10, seconds=60))],
)
def manage_deployment(
    manage_info: ManageDeploymentSchema,
    user: User = Security(get_current_active_user, scopes=["deployments:manage"]),
    ml_ops_service: MLOpsService = Depends(get_ml_ops_service),
):
    """
    Manage deployments. Accessible to ML Engineers and Admins.
    """
    return ml_ops_service.perform_deployment_management(
        manage_info.deployment_id, manage_info.action, user
    )
