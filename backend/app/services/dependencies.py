from fastapi import Depends
from sqlalchemy.orm import Session
from app.db.session import get_db
from app.repositories import (
    TrainingJobRepository,
    DeploymentRepository,
    AuditLogRepository,
)
from app.services.interfaces import VersionControlService, ObjectStorageService
from app.services.dataset import (
    lakefs_service,
    S3StorageService,
    data_service,
    DataService,
    DatasetCatalogService,
    DatasetVersioningService,
    DatasetStorageService,
    DatasetDiffService,
)
from app.services.auth import auth_service, AuthService, user_service, UserService
from app.services.ml_ops import (
    ml_ops_service,
    MLOpsService,
    ModelTrainingService,
    ModelDeploymentService,
    ModelServingService,
    ModelRegistryService,
    RayJobService,
    TrainingLogService,
    TrainingJobService,
)

# Shared Singletons
_storage_service = S3StorageService()
_version_control_service = lakefs_service
_data_service = data_service
_auth_service = auth_service
_user_service = user_service
_ml_ops_service = ml_ops_service
_rayjob_service = RayJobService()


def get_storage_service() -> ObjectStorageService:
    """Returns the singleton ObjectStorageService instance."""
    return _storage_service


def get_version_control_service() -> VersionControlService:
    """Returns the singleton VersionControlService instance."""
    return _version_control_service


def get_data_service() -> DataService:
    """Returns the singleton DataService facade instance."""
    return _data_service


def get_dataset_catalog_service() -> DatasetCatalogService:
    """Returns the singleton DatasetCatalogService instance."""
    return _data_service.catalog


def get_dataset_versioning_service() -> DatasetVersioningService:
    """Returns the singleton DatasetVersioningService instance."""
    return _data_service.versioning


def get_dataset_storage_service() -> DatasetStorageService:
    """Returns the singleton DatasetStorageService instance."""
    return _data_service.storage


def get_dataset_diff_service() -> DatasetDiffService:
    """Returns the singleton DatasetDiffService instance."""
    return _data_service.diff


def get_auth_service() -> AuthService:
    """Returns the singleton AuthService instance."""
    return _auth_service


def get_user_service() -> UserService:
    """Returns the singleton UserService instance."""
    return _user_service


def get_ml_ops_service() -> MLOpsService:
    """Returns the singleton MLOpsService facade instance."""
    return _ml_ops_service


def get_model_training_service() -> ModelTrainingService:
    """Returns the singleton ModelTrainingService instance."""
    return _ml_ops_service.training


def get_model_deployment_service() -> ModelDeploymentService:
    """Returns the singleton ModelDeploymentService instance."""
    return _ml_ops_service.deployment


def get_model_serving_service() -> ModelServingService:
    """Returns the singleton ModelServingService instance."""
    return _ml_ops_service.serving


def get_model_registry_service() -> ModelRegistryService:
    """Returns the singleton ModelRegistryService instance."""
    return _ml_ops_service.registry


def get_training_job_repository(db: Session = Depends(get_db)) -> TrainingJobRepository:
    """Returns a TrainingJobRepository bound to the current DB session."""
    return TrainingJobRepository(db)


def get_rayjob_service() -> RayJobService:
    """Returns the singleton RayJobService instance."""
    return _rayjob_service


def get_training_log_service(
    storage_service: ObjectStorageService = Depends(get_storage_service),
    rayjob_service: RayJobService = Depends(get_rayjob_service),
) -> TrainingLogService:
    """Returns a TrainingLogService instance."""
    return TrainingLogService(storage_service=storage_service, rayjob_service=rayjob_service)


def get_training_job_service(
    repository: TrainingJobRepository = Depends(get_training_job_repository),
    rayjob_service: RayJobService = Depends(get_rayjob_service),
    log_service: TrainingLogService = Depends(get_training_log_service),
) -> TrainingJobService:
    """Returns a TrainingJobService orchestrator instance."""
    return TrainingJobService(
        repository=repository,
        rayjob_service=rayjob_service,
        log_service=log_service,
    )


def get_deployment_repository(db: Session = Depends(get_db)) -> DeploymentRepository:
    """Returns a DeploymentRepository bound to the current DB session."""
    return DeploymentRepository(db)


def get_audit_log_repository(db: Session = Depends(get_db)) -> AuditLogRepository:
    """Returns an AuditLogRepository bound to the current DB session."""
    return AuditLogRepository(db)


