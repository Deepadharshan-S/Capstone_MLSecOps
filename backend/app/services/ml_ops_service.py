import logging

from app.models.user import User

logger = logging.getLogger("security_audit")


def perform_dataset_upload(dataset_name: str, user: User) -> dict:
    """Logs dataset upload audit event and returns status payload."""
    logger.info(f"AUDIT: User '{user.username}' (role: {user.role}) uploaded dataset '{dataset_name}'.")
    return {
        "message": f"Dataset '{dataset_name}' uploaded successfully by {user.username}.",
        "dataset_name": dataset_name,
        "uploaded_by": user.username,
    }


def perform_model_training(dataset_id: str, epochs: int, user: User) -> dict:
    """Logs model training start event and returns execution status payload."""
    logger.info(
        f"AUDIT: User '{user.username}' (role: {user.role}) started training job"
        f" on dataset '{dataset_id}' for {epochs} epochs."
    )
    return {
        "message": "Training job started successfully.",
        "dataset_id": dataset_id,
        "epochs": epochs,
        "started_by": user.username,
        "status": "training",
    }


def retrieve_models(user: User) -> dict:
    """Logs model viewing request and returns list of accessible models."""
    logger.info(f"AUDIT: User '{user.username}' (role: {user.role}) viewed models list.")
    return {
        "models": [
            {
                "id": "model-uuid-1",
                "name": "SpamDetector-v1",
                "accuracy": 0.985,
                "created_at": "2026-07-25T12:00:00Z",
            },
            {
                "id": "model-uuid-2",
                "name": "FraudDetection-LSTM",
                "accuracy": 0.991,
                "created_at": "2026-07-26T15:30:00Z",
            },
        ]
    }


def perform_model_deploy(model_id: str, environment: str, user: User) -> dict:
    """Logs model deployment audit event and returns status payload."""
    logger.info(
        f"AUDIT: User '{user.username}' (role: {user.role}) deployed model '{model_id}'"
        f" to '{environment}' environment."
    )
    return {
        "message": f"Model '{model_id}' deployed to '{environment}'.",
        "model_id": model_id,
        "environment": environment,
        "deployed_by": user.username,
        "status": "deployed",
    }


def perform_deployment_management(deployment_id: str, action: str, user: User) -> dict:
    """Logs deployment management audit event and returns action status payload."""
    logger.info(
        f"AUDIT: User '{user.username}' (role: {user.role}) triggered action '{action}'"
        f" on deployment '{deployment_id}'."
    )
    return {
        "message": f"Action '{action}' executed on deployment '{deployment_id}'.",
        "deployment_id": deployment_id,
        "action_taken": action,
        "triggered_by": user.username,
    }
