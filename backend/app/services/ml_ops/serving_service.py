import os
import json
import time
from typing import Optional

import pandas as pd
from app.models.user import User
from app.core.logging_config import log_audit_event
from app.core.config import settings
from fastapi import HTTPException, status


class ModelServingService:
    """
    Handles real-time model inference and prediction requests.
    Enforces strict process isolation: routes predictions exclusively via HTTP
    to live Kubernetes RayService endpoints. Never executes model code inside
    the web server process.
    """

    def perform_model_prediction(
        self,
        model_name_or_id: str,
        data: dict,
        user: User,
        version: Optional[str] = None,
    ) -> dict:
        """
        Executes real-time inference against the deployed model.
        Routes request directly to the active live Kubernetes RayService endpoint.
        Returns 503 with Retry-After header if the RayService is initializing or offline.
        """
        from mlflow.tracking import MlflowClient

        start_time = time.time()
        mlflow_client = MlflowClient(tracking_uri=settings.MLFLOW_TRACKING_URI)

        # 1. Parse and validate input payload format
        try:
            if "dataframe_records" in data and data["dataframe_records"] is not None:
                df = pd.DataFrame(data["dataframe_records"])
            elif "inputs" in data and data["inputs"] is not None:
                df = pd.DataFrame(data["inputs"])
            elif isinstance(data, list):
                df = pd.DataFrame(data)
            elif isinstance(data, dict):
                df = pd.DataFrame([data])
            else:
                raise ValueError("Payload must contain 'dataframe_records' or 'inputs'.")
        except Exception as parse_err:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid prediction input format: {str(parse_err)}",
            )

        # 2. Resolve model metadata, version, and deployment tags
        resolved_version = str(version) if version else "latest"
        matched_model = model_name_or_id
        matched_dep_id = None
        matched_raysvc = None
        internal_url_from_tags = None

        try:
            all_versions = mlflow_client.search_model_versions("")
            for mv in all_versions:
                tags = mv.tags or {}
                if (
                    tags.get("deployment.id") == model_name_or_id
                    or tags.get("deployment.rayservice_name") == model_name_or_id
                ):
                    matched_model = mv.name
                    resolved_version = str(mv.version)
                    matched_dep_id = tags.get("deployment.id")
                    matched_raysvc = tags.get("deployment.rayservice_name")
                    internal_url_from_tags = tags.get("deployment.internal_endpoint_url")
                    break
                elif mv.name == model_name_or_id:
                    if tags.get("deployment.status") == "running":
                        matched_dep_id = tags.get("deployment.id")
                        matched_raysvc = tags.get("deployment.rayservice_name")
                        resolved_version = str(mv.version)
                        internal_url_from_tags = tags.get("deployment.internal_endpoint_url")
        except Exception:
            pass

        # 3. Route inference request via HTTP to the RayService endpoint
        # Enforce strict isolation: NEVER execute model code in the FastAPI process
        target_service = matched_raysvc or (
            model_name_or_id if model_name_or_id.startswith("raysvc-") else None
        )

        if not target_service and not matched_dep_id:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    f"Model '{matched_model}' does not have an active RayService deployment. "
                    f"Please deploy the model via POST /api/models/deploy to serve it."
                ),
                headers={"Retry-After": "10"},
            )

        endpoints = []
        if os.getenv("RAY_SERVE_HTTP_ENDPOINT"):
            endpoints.append(os.getenv("RAY_SERVE_HTTP_ENDPOINT"))
        if internal_url_from_tags:
            endpoints.append(internal_url_from_tags)
        if target_service:
            endpoints.append(f"http://{target_service}-serve-svc.default.svc.cluster.local:8000/predict")
            endpoints.append(f"http://{target_service}-serve-svc:8000/predict")

        # Deduplicate while preserving order
        unique_endpoints = list(dict.fromkeys(endpoints))

        preds = None
        last_err = None
        infer_successful = False

        for endpoint in unique_endpoints:
            try:
                import httpx
                with httpx.Client(timeout=15.0) as http_client:
                    resp = http_client.post(endpoint, json=data)
                    if resp.status_code == 200:
                        res_obj = resp.json()
                        preds = res_obj.get("predictions", [])
                        infer_successful = True
                        break
                    elif resp.status_code == 400:
                        err_detail = resp.text
                        try:
                            err_json = resp.json()
                            err_detail = err_json.get("detail", resp.text)
                        except Exception:
                            pass
                        raise HTTPException(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            detail=f"Inference error: {err_detail}",
                        )
                    else:
                        last_err = f"Endpoint {endpoint} returned status {resp.status_code}: {resp.text}"
            except HTTPException:
                raise
            except Exception as conn_err:
                last_err = str(conn_err)

        if not infer_successful:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    f"RayService inference endpoint for model '{matched_model}' is initializing or unreachable. "
                    f"Please retry shortly. (Details: {last_err})"
                ),
                headers={"Retry-After": "10"},
            )

        latency_ms = round((time.time() - start_time) * 1000, 2)

        # 4. Security Audit Log
        log_audit_event(
            "model_prediction",
            user.username,
            None,
            f"Prediction on model '{matched_model}' (version {resolved_version}, samples {len(df)}, latency {latency_ms}ms, routed_to_rayservice=True).",
        )

        return {
            "predictions": preds if preds is not None else [],
            "model_name": matched_model,
            "model_version": str(resolved_version),
            "latency_ms": latency_ms,
        }
