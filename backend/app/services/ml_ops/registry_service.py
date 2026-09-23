import os
import uuid
import json
import datetime
import tempfile
import shutil
import re
import logging
from typing import Optional

from app.models.user import User
from app.core.logging_config import log_audit_event
from app.core.config import settings
from fastapi import HTTPException, status

logger = logging.getLogger("registry_service")


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

    def retrieve_model_detail(self, model_name: str, user: User) -> dict:
        """
        Retrieves detailed version history, tags, metrics, and production alias
        for a specific model registered in MLflow.
        """
        log_audit_event(
            "model_detail_view",
            user.username,
            None,
            f"Viewed details for model '{model_name}'.",
        )
        from mlflow.tracking import MlflowClient

        client = MlflowClient(tracking_uri=settings.MLFLOW_TRACKING_URI)

        try:
            rm = client.get_registered_model(model_name)
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Model '{model_name}' not found.",
            )

        # Model timestamps
        created_at_str = None
        if getattr(rm, "creation_timestamp", None):
            created_at_str = datetime.datetime.fromtimestamp(
                rm.creation_timestamp / 1000.0, datetime.timezone.utc
            ).isoformat()

        updated_at_str = None
        if getattr(rm, "last_updated_timestamp", None):
            updated_at_str = datetime.datetime.fromtimestamp(
                rm.last_updated_timestamp / 1000.0, datetime.timezone.utc
            ).isoformat()

        aliases = dict(getattr(rm, "aliases", {}) or {})
        production_alias = aliases.get("production") or aliases.get("Production")

        # Retrieve all versions for this model
        try:
            versions = client.search_model_versions(f"name = '{model_name}'")
        except Exception:
            versions = getattr(rm, "latest_versions", []) or []

        version_details = []
        for v in versions:
            v_created = None
            if getattr(v, "creation_timestamp", None):
                v_created = datetime.datetime.fromtimestamp(
                    v.creation_timestamp / 1000.0, datetime.timezone.utc
                ).isoformat()

            v_updated = None
            if getattr(v, "last_updated_timestamp", None):
                v_updated = datetime.datetime.fromtimestamp(
                    v.last_updated_timestamp / 1000.0, datetime.timezone.utc
                ).isoformat()

            v_aliases = list(getattr(v, "aliases", []) or [])
            for a_name, a_ver in aliases.items():
                if str(a_ver) == str(v.version) and a_name not in v_aliases:
                    v_aliases.append(a_name)

            if not production_alias and getattr(v, "current_stage", "") == "Production":
                production_alias = str(v.version)

            v_metrics = {}
            v_params = {}
            if getattr(v, "run_id", None):
                try:
                    run = client.get_run(v.run_id)
                    v_metrics = dict(run.data.metrics or {})
                    v_params = dict(run.data.params or {})
                except Exception as run_err:
                    logger.debug(f"Could not fetch run data for {v.run_id}: {run_err}")

            version_details.append(
                {
                    "version": str(v.version),
                    "current_stage": getattr(v, "current_stage", "None"),
                    "status": getattr(v, "status", "READY"),
                    "run_id": getattr(v, "run_id", None),
                    "source": getattr(v, "source", None),
                    "created_at": v_created,
                    "last_updated_at": v_updated,
                    "description": getattr(v, "description", None),
                    "tags": dict(getattr(v, "tags", {}) or {}),
                    "aliases": v_aliases,
                    "metrics": v_metrics,
                    "parameters": v_params,
                }
            )

        try:
            version_details.sort(key=lambda x: int(x["version"]), reverse=True)
        except Exception as sort_err:
            logger.debug(f"Version sort fallback for model {rm.name}: {sort_err}")

        return {
            "name": rm.name,
            "description": getattr(rm, "description", None),
            "created_at": created_at_str,
            "last_updated_at": updated_at_str,
            "tags": dict(getattr(rm, "tags", {}) or {}),
            "aliases": aliases,
            "production_alias": str(production_alias) if production_alias else None,
            "versions": version_details,
        }


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
        from app.services.ml_ops.ray_wrapper import ModelWrapper

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
            except Exception as seek_err:
                logger.debug(f"Seek error on upload file: {seek_err}")

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
            os.environ["AWS_ACCESS_KEY_ID"] = os.getenv("AWS_ACCESS_KEY_ID", settings.minio_app_user)
            os.environ["AWS_SECRET_ACCESS_KEY"] = os.getenv("AWS_SECRET_ACCESS_KEY", settings.minio_app_password)
            os.environ["MLFLOW_S3_ENDPOINT_URL"] = os.getenv("MLFLOW_S3_ENDPOINT_URL", settings.MINIO_ENDPOINT)
            os.environ["MLFLOW_S3_IGNORE_TLS"] = "true"

            wrapped_model = ModelWrapper(model)

            mlflow.set_tracking_uri(settings.MLFLOW_TRACKING_URI)
            exp_name = experiment_name or "uploaded-models"
            try:
                temp_client = MlflowClient()
                existing_exp = temp_client.get_experiment_by_name(exp_name)
                if existing_exp and getattr(existing_exp, "lifecycle_stage", "") == "deleted":
                    temp_client.restore_experiment(existing_exp.experiment_id)
            except Exception as exp_err:
                logger.debug(f"Experiment restore check note: {exp_err}")
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
                all_versions = client.search_model_versions(
                    f"name = '{final_model_name}'", order_by=["version_number DESC"], max_results=1
                )
                if all_versions:
                    model_version = str(all_versions[0].version)
                else:
                    latest_versions = client.get_latest_versions(final_model_name)
                    if latest_versions:
                        model_version = str(latest_versions[-1].version)
            except Exception as search_ver_err:
                logger.debug(f"Search model version note: {search_ver_err}")
                try:
                    latest_versions = client.get_latest_versions(final_model_name)
                    if latest_versions:
                        model_version = str(latest_versions[-1].version)
                except Exception as ver_err:
                    logger.debug(f"Latest version fetch note: {ver_err}")

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

    def check_health(self) -> tuple[bool, str]:
        """Checks connection/health of the MLflow tracking server."""
        try:
            from mlflow.tracking import MlflowClient

            client = MlflowClient(tracking_uri=settings.MLFLOW_TRACKING_URI)
            client.search_experiments(max_results=1)
            return True, "connected"
        except Exception as e:
            return False, f"error: {str(e)}"

