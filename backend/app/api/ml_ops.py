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
    PredictionRequestSchema,
    PredictionResponseSchema,
    DeploymentListResponse,
)
from app.services.dependencies import (
    get_model_training_service,
    get_model_deployment_service,
    get_model_serving_service,
    get_model_registry_service,
)
from app.services.model_training_service import ModelTrainingService
from app.services.model_deployment_service import ModelDeploymentService
from app.services.model_serving_service import ModelServingService
from app.services.model_registry_service import ModelRegistryService

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
    training_service: ModelTrainingService = Depends(get_model_training_service),
):
    """
    Start model training. Accessible to Data Scientists and Admins.
    """
    return training_service.perform_model_training(
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
    training_service: ModelTrainingService = Depends(get_model_training_service),
):
    """
    Start automated pipeline training. Accessible to Data Scientists and Admins.
    """
    return training_service.perform_pipeline_training(
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
    registry_service: ModelRegistryService = Depends(get_model_registry_service),
):
    """
    View models. Accessible to all roles (Viewer, ML Engineer, Data Scientist, Admin).
    """
    return registry_service.retrieve_models(user)


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
    registry_service: ModelRegistryService = Depends(get_model_registry_service),
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

    return registry_service.perform_model_upload(
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
    dependencies=[Depends(RateLimiter(times=5, seconds=60))],
)
def deploy_model(
    deploy_info: DeployModelSchema,
    user: User = Security(get_current_active_user, scopes=["models:deploy"]),
    deployment_service: ModelDeploymentService = Depends(get_model_deployment_service),
):
    """
    Deploy models using KubeRay RayService CRD and MLflow-native tracking.
    Accessible to ML Engineers and Admins.
    """
    return deployment_service.perform_model_deploy(
        model_id=deploy_info.model_id,
        environment=deploy_info.environment,
        user=user,
        version=deploy_info.version,
        replicas=deploy_info.replicas,
    )


@router.get(
    "/deployments",
    response_model=DeploymentListResponse,
    dependencies=[Depends(RateLimiter(times=30, seconds=60))],
)
def list_deployments(
    user: User = Security(get_current_active_user, scopes=["models:view"]),
    deployment_service: ModelDeploymentService = Depends(get_model_deployment_service),
):
    """
    List all model deployments with their MLflow metadata and live Kubernetes RayService status.
    Accessible to all authenticated roles.
    """
    return deployment_service.retrieve_deployments(user)


@router.post(
    "/models/{model_name}/predict",
    response_model=PredictionResponseSchema,
    dependencies=[Depends(RateLimiter(times=60, seconds=60))],
)
def predict_model(
    model_name: str,
    payload: PredictionRequestSchema,
    version: Optional[str] = None,
    user: User = Security(get_current_active_user, scopes=["models:view"]),
    serving_service: ModelServingService = Depends(get_model_serving_service),
):
    """
    Executes real-time inference on a registered/deployed model.
    Accepts feature matrices or dataframe records.
    Accessible to all authenticated roles.
    """
    data = payload.model_dump()
    return serving_service.perform_model_prediction(
        model_name_or_id=model_name,
        data=data,
        user=user,
        version=version,
    )


@router.post(
    "/deployments/{deployment_id}/predict",
    response_model=PredictionResponseSchema,
    dependencies=[Depends(RateLimiter(times=60, seconds=60))],
)
def predict_deployment(
    deployment_id: str,
    payload: PredictionRequestSchema,
    user: User = Security(get_current_active_user, scopes=["models:view"]),
    serving_service: ModelServingService = Depends(get_model_serving_service),
):
    """
    Executes real-time inference against a specific deployment ID.
    Accessible to all authenticated roles.
    """
    data = payload.model_dump()
    return serving_service.perform_model_prediction(
        model_name_or_id=deployment_id,
        data=data,
        user=user,
    )


@router.post(
    "/deployments/manage",
    response_model=ManageDeploymentResponse,
    dependencies=[Depends(RateLimiter(times=10, seconds=60))],
)
def manage_deployment(
    manage_info: ManageDeploymentSchema,
    user: User = Security(get_current_active_user, scopes=["deployments:manage"]),
    deployment_service: ModelDeploymentService = Depends(get_model_deployment_service),
):
    """
    Manage deployments (restart, stop, rollback).
    Accessible to ML Engineers and Admins.
    """
    return deployment_service.perform_deployment_management(
        manage_info.deployment_id, manage_info.action, user
    )
