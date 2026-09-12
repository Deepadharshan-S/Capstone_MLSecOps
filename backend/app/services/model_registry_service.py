import os
import uuid
import json
import datetime
import tempfile
import shutil
import re
from typing import Optional

from app.models.user import User
from app.core.logging_config import log_audit_event
from app.core.config import settings
from fastapi import HTTPException, status


class ModelRegistryService:
    """
    Manages querying MLflow registered models and secure model artifact uploads (.pkl).
    """

    def retrieve_models(self, user: User) -> dict:
        """Logs model viewing request and returns list of registered models in MLflow."""
        log_audit_event(
            "models_view",
            user.username,
            None,
            "Viewed models list.",
        )
        import mlflow
        from mlflow.tracking import MlflowClient

        mlflow.set_tracking_uri(settings.MLFLOW_TRACKING_URI)
        client = MlflowClient()

        models_list = []
        try:
            registered_models = client.search_registered_models()
            for rm in registered_models:
                created_at_str = "unknown"
                run_id = "unknown"
                accuracy = 0.0
                precision = 0.0
                recall = 0.0
                f1_score = 0.0
                experiment_name = "unknown"

                if rm.latest_versions:
                    latest_v = rm.latest_versions[-1]
                    # creation_timestamp is in milliseconds since epoch
                    dt = datetime.datetime.fromtimestamp(
                        latest_v.creation_timestamp / 1000.0, datetime.timezone.utc
                    )
                    created_at_str = dt.isoformat()
                    run_id = latest_v.run_id
                    run_params = {}
                    run_tags = {}

                    try:
                        run = client.get_run(run_id)
                        run_metrics = run.data.metrics
                        accuracy = run_metrics.get("accuracy", 0.0)
                        precision = run_metrics.get("precision", 0.0)
                        recall = run_metrics.get("recall", 0.0)
                        f1_score = run_metrics.get("f1_score", 0.0)
                        run_params = run.data.params or {}
                        run_tags = run.data.tags or {}
                        try:
                            exp = client.get_experiment(run.info.experiment_id)
                            experiment_name = exp.name
                        except Exception as ee:
                            print(f"Error retrieving experiment details: {str(ee)}")
                    except Exception as e:
                        print(f"Error retrieving run details from MLflow: {str(e)}")

                models_list.append(
                    {
                        "id": run_id,
                        "name": rm.name,
                        "accuracy": accuracy,
                        "precision": precision,
                        "recall": recall,
                        "f1_score": f1_score,
                        "created_at": created_at_str,
                        "experiment_name": experiment_name,
                        "parameters": run_params,
                        "tags": run_tags,
                    }
                )
        except Exception as e:
            print(f"Error fetching from MLflow registry: {str(e)}")

        return {"models": models_list}

    def perform_model_upload(
        self,
        file,
        user: User,
        model_name: Optional[str] = None,
        experiment_name: Optional[str] = None,
        metadata: Optional[str] = None,
        metrics: Optional[str] = None,
    ) -> dict:
        """Uploads and registers a .pkl model to MLflow, optionally with metadata and metrics."""
        import cloudpickle
        import pickle
        import mlflow
        from mlflow.tracking import MlflowClient
        from app.services.ray_wrapper import ModelWrapper

        log_audit_event(
            "model_upload_initiated",
            user.username,
            None,
            f"Initiated .pkl model upload (Model: {model_name}, Experiment: {experiment_name}).",
        )

        # 1. Determine target model name
        raw_name = (
            model_name.strip()
            if model_name and model_name.strip()
            else os.path.splitext(os.path.basename(file.filename))[0]
        )
        final_model_name = re.sub(r"[^a-zA-Z0-9_\-]", "-", raw_name).strip("-")
        if not final_model_name:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Model name must contain at least one valid alphanumeric character.",
            )

        # 2. Parse and validate metadata
        parsed_metadata = {}
        if metadata:
            try:
                parsed_metadata = json.loads(metadata)
                if not isinstance(parsed_metadata, dict):
                    raise ValueError("Metadata must be a JSON object.")
            except Exception as json_err:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid metadata JSON: {str(json_err)}",
                )

        # 3. Parse and validate metrics
        parsed_metrics = {}
        if metrics:
            try:
                raw_metrics = json.loads(metrics)
                if not isinstance(raw_metrics, dict):
                    raise ValueError("Metrics must be a JSON object.")
                for k, v in raw_metrics.items():
                    parsed_metrics[str(k)] = float(v)
            except Exception as metric_err:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid metrics JSON (must be float key-value pairs): {str(metric_err)}",
                )

        # 4. Stream and write file enforcing size limits
        max_size = settings.MAX_MODEL_UPLOAD_SIZE_BYTES
        temp_dir = tempfile.mkdtemp()
        safe_filename = os.path.basename(file.filename)
        temp_file_path = os.path.join(temp_dir, safe_filename)

        total_bytes = 0
        try:
            try:
                file.file.seek(0)
            except Exception:
                pass

            with open(temp_file_path, "wb") as buffer:
                while True:
                    chunk = file.file.read(1024 * 1024)
                    if not chunk:
                        break
                    total_bytes += len(chunk)
                    if total_bytes > max_size:
                        raise HTTPException(
                            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                            detail=f"Model file size exceeds maximum limit of {max_size} bytes ({max_size // (1024 * 1024)}MB).",
                        )
                    buffer.write(chunk)

            if total_bytes == 0:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Uploaded model file is empty.",
                )

            # 5. Load the model using cloudpickle or pickle
            try:
                with open(temp_file_path, "rb") as f:
                    model = cloudpickle.load(f)
            except Exception:
                try:
                    with open(temp_file_path, "rb") as f:
                        model = pickle.load(f)
                except Exception as load_err:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Failed to deserialize model file as a valid pickle object: {str(load_err)}",
                    )

            # Configure S3/MinIO credentials for artifact storage
            os.environ["AWS_ACCESS_KEY_ID"] = os.getenv("AWS_ACCESS_KEY_ID", settings.MINIO_ROOT_USER)
            os.environ["AWS_SECRET_ACCESS_KEY"] = os.getenv("AWS_SECRET_ACCESS_KEY", settings.MINIO_ROOT_PASSWORD)
            os.environ["MLFLOW_S3_ENDPOINT_URL"] = os.getenv("MLFLOW_S3_ENDPOINT_URL", settings.MINIO_ENDPOINT)
            os.environ["MLFLOW_S3_IGNORE_TLS"] = "true"

            wrapped_model = ModelWrapper(model)

            mlflow.set_tracking_uri(settings.MLFLOW_TRACKING_URI)
            exp_name = experiment_name or "uploaded-models"
            mlflow.set_experiment(exp_name)

            # 6. Log and register model in MLflow
            clean_name = re.sub(r"[^a-zA-Z0-9_-]", "-", final_model_name).strip("-_")
            upload_run_name = f"upload_{clean_name[:20]}_{uuid.uuid4().hex[:8]}"
            with mlflow.start_run(run_name=upload_run_name) as run:
                run_id = run.info.run_id

                tags_to_set = {"uploaded_by": user.username, "upload_filename": file.filename}
                if parsed_metadata:
                    tags_to_set.update(parsed_metadata)
                mlflow.set_tags(tags_to_set)

                if parsed_metrics:
                    for k, v in parsed_metrics.items():
                        mlflow.log_metric(k, float(v))

                mlflow.pyfunc.log_model(
                    artifact_path="model",
                    python_model=wrapped_model,
                    registered_model_name=final_model_name,
                    pip_requirements=["mlflow", "scikit-learn", "pandas", "cloudpickle"],
                )

            client = MlflowClient()
            model_version = None
            try:
                latest_versions = client.get_latest_versions(final_model_name)
                if latest_versions:
                    model_version = str(latest_versions[-1].version)
            except Exception:
                pass

            log_audit_event(
                "model_upload_success",
                user.username,
                None,
                f"Successfully uploaded and registered model '{final_model_name}' (version: {model_version}) to experiment '{exp_name}'.",
            )
            return {
                "message": "Model uploaded and registered successfully.",
                "model_id": run_id,
                "model_name": final_model_name,
                "version": model_version,
                "experiment_name": exp_name,
                "uploaded_by": user.username,
                "status": "uploaded",
            }
        except HTTPException:
            raise
        except Exception as e:
            log_audit_event(
                "model_upload_error",
                user.username,
                None,
                f"Error uploading model '{file.filename}': {str(e)}",
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Model upload failed: {str(e)}",
            )
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
