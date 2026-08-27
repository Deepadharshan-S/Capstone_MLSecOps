from app.services.interfaces import VersionControlService, ObjectStorageService
from app.services.lakefs_service import lakefs_service
from app.services.s3_storage_service import S3StorageService
from app.services.data_service import data_service, DataService
from app.services.auth_service import auth_service, AuthService
from app.services.user_service import user_service, UserService
from app.services.ml_ops_service import ml_ops_service, MLOpsService

# Shared Singletons
_storage_service = S3StorageService()
_version_control_service = lakefs_service
_data_service = data_service
_auth_service = auth_service
_user_service = user_service
_ml_ops_service = ml_ops_service


def get_storage_service() -> ObjectStorageService:
    """Returns the singleton ObjectStorageService instance."""
    return _storage_service


def get_version_control_service() -> VersionControlService:
    """Returns the singleton VersionControlService instance."""
    return _version_control_service


def get_data_service() -> DataService:
    """Returns the singleton DataService instance."""
    return _data_service


def get_auth_service() -> AuthService:
    """Returns the singleton AuthService instance."""
    return _auth_service


def get_user_service() -> UserService:
    """Returns the singleton UserService instance."""
    return _user_service


def get_ml_ops_service() -> MLOpsService:
    """Returns the singleton MLOpsService instance."""
    return _ml_ops_service
