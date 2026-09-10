from enum import Enum


class Role(str, Enum):
    ADMIN = "admin"
    DATA_SCIENTIST = "data_scientist"
    ML_ENGINEER = "ml_engineer"
    VIEWER = "viewer"


class Permission(str, Enum):
    # Dataset operations
    DATASETS_VIEW = "datasets:view"
    DATASETS_UPLOAD = "datasets:upload"
    DATASETS_DELETE = "datasets:delete"

    # Model operations
    MODELS_TRAIN = "models:train"
    MODELS_VIEW = "models:view"
    MODELS_DEPLOY = "models:deploy"

    # Deployment operations
    DEPLOYMENTS_MANAGE = "deployments:manage"

    # User and role administration
    USERS_MANAGE = "users:manage"


# Map each role to a set of allowed permissions
ROLE_PERMISSIONS: dict[Role, set[Permission]] = {
    Role.ADMIN: set(Permission),  # Admin gets full access (all permissions)
    Role.DATA_SCIENTIST: {
        Permission.DATASETS_VIEW,
        Permission.DATASETS_UPLOAD,
        Permission.DATASETS_DELETE,
        Permission.MODELS_TRAIN,
        Permission.MODELS_VIEW,
        Permission.MODELS_DEPLOY,
        Permission.DEPLOYMENTS_MANAGE,
    },
    Role.ML_ENGINEER: {
        Permission.DATASETS_VIEW,
        Permission.MODELS_TRAIN,
        Permission.MODELS_VIEW,
        Permission.MODELS_DEPLOY,
        Permission.DEPLOYMENTS_MANAGE,
    },
    Role.VIEWER: {
        Permission.MODELS_VIEW,
    },
}

