from typing import Optional

from app.models.user import User
from app.services.ml_ops.utils import get_scoped_training_credentials
from app.services.ml_ops.training_service import ModelTrainingService
from app.services.ml_ops.deployment_service import ModelDeploymentService
from app.services.ml_ops.serving_service import ModelServingService
from app.services.ml_ops.registry_service import ModelRegistryService


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
        db=None,
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
            db=db,
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
        db=None,
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
            db=db,
        )

    def retrieve_training_jobs(self, db, user: User, limit: int = 100) -> dict:
        return self.training.retrieve_training_jobs(db=db, user=user, limit=limit)

    def retrieve_training_job_detail(self, db, job_id: str, user: User) -> dict:
        return self.training.retrieve_training_job_detail(db=db, job_id=job_id, user=user)

    def retrieve_training_job_logs(self, db, job_id: str, user: User, tail_lines: Optional[int] = 1000) -> dict:
        return self.training.retrieve_training_job_logs(db=db, job_id=job_id, user=user, tail_lines=tail_lines)

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

    def retrieve_deployments(
        self,
        user: User,
        status: Optional[str] = None,
        environment: Optional[str] = None,
        active_only: bool = False,
    ) -> dict:
        return self.deployment.retrieve_deployments(
            user=user,
            status=status,
            environment=environment,
            active_only=active_only,
        )

    def perform_deployment_management(
        self, deployment_id: str, action: str, user: User
    ) -> dict:
        return self.deployment.perform_deployment_management(
            deployment_id=deployment_id, action=action, user=user
        )

    def retrieve_deployment_detail(self, deployment_id: str, user: User) -> dict:
        return self.deployment.retrieve_deployment_detail(deployment_id, user)

    def retrieve_model_detail(self, model_name: str, user: User) -> dict:
        return self.registry.retrieve_model_detail(model_name, user)


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

    # 5. Health Check Operations
    def check_health(self) -> tuple[bool, str]:
        """Checks connection/health of the underlying MLOps backend services (MLflow)."""
        return self.registry.check_health()


ml_ops_service = MLOpsService()

