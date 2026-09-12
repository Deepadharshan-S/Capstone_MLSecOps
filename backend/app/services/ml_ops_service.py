from typing import Optional

from app.models.user import User
from app.services.ml_ops_utils import get_scoped_training_credentials, to_k8s_endpoint
from app.services.model_training_service import ModelTrainingService
from app.services.model_deployment_service import ModelDeploymentService
from app.services.model_serving_service import ModelServingService
from app.services.model_registry_service import ModelRegistryService


class MLOpsService:
    """
    Unified MLOps Facade coordinating model training, deployment, serving,
    and registry operations. Preserves 100% backwards compatibility with
    existing FastAPI routes and tests while delegating to specialized domain services.
    """

    def __init__(
        self,
        training_service: Optional[ModelTrainingService] = None,
        deployment_service: Optional[ModelDeploymentService] = None,
        serving_service: Optional[ModelServingService] = None,
        registry_service: Optional[ModelRegistryService] = None,
    ):
        self.training = training_service or ModelTrainingService()
        self.deployment = deployment_service or ModelDeploymentService()
        self.serving = serving_service or ModelServingService()
        self.registry = registry_service or ModelRegistryService()

    def _get_scoped_training_credentials(self, job_id: str) -> dict:
        return get_scoped_training_credentials(job_id)

    # 1. Training Operations
    def perform_model_training(
        self,
        dataset_id: str,
        ref: str,
        epochs: int,
        hyperparameters: dict,
        code: str,
        user: User,
        experiment_name: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> dict:
        return self.training.perform_model_training(
            dataset_id=dataset_id,
            ref=ref,
            epochs=epochs,
            hyperparameters=hyperparameters,
            code=code,
            user=user,
            experiment_name=experiment_name,
            model_name=model_name,
        )

    def perform_pipeline_training(
        self,
        dataset_id: str,
        ref: str,
        target_column: str,
        model_type: str,
        hyperparameters: dict,
        user: User,
        experiment_name: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> dict:
        return self.training.perform_pipeline_training(
            dataset_id=dataset_id,
            ref=ref,
            target_column=target_column,
            model_type=model_type,
            hyperparameters=hyperparameters,
            user=user,
            experiment_name=experiment_name,
            model_name=model_name,
        )

    # 2. Registry Operations
    def retrieve_models(self, user: User) -> dict:
        return self.registry.retrieve_models(user=user)

    def perform_model_upload(
        self,
        file,
        user: User,
        model_name: Optional[str] = None,
        experiment_name: Optional[str] = None,
        metadata: Optional[str] = None,
        metrics: Optional[str] = None,
    ) -> dict:
        return self.registry.perform_model_upload(
            file=file,
            user=user,
            model_name=model_name,
            experiment_name=experiment_name,
            metadata=metadata,
            metrics=metrics,
        )

    # 3. Deployment Operations
    def perform_model_deploy(
        self,
        model_id: str,
        environment: str,
        user: User,
        version: Optional[str] = "latest",
        replicas: Optional[int] = 1,
    ) -> dict:
        return self.deployment.perform_model_deploy(
            model_id=model_id,
            environment=environment,
            user=user,
            version=version,
            replicas=replicas,
        )

    def retrieve_deployments(self, user: User) -> dict:
        return self.deployment.retrieve_deployments(user=user)

    def perform_deployment_management(
        self, deployment_id: str, action: str, user: User
    ) -> dict:
        return self.deployment.perform_deployment_management(
            deployment_id=deployment_id, action=action, user=user
        )

    # 4. Serving / Prediction Operations
    def perform_model_prediction(
        self,
        model_name_or_id: str,
        data: dict,
        user: User,
        version: Optional[str] = None,
    ) -> dict:
        return self.serving.perform_model_prediction(
            model_name_or_id=model_name_or_id,
            data=data,
            user=user,
            version=version,
        )


ml_ops_service = MLOpsService()
