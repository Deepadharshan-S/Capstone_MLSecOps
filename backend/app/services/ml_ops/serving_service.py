import os
import json
import time
import logging
import socket
import subprocess
import atexit
import threading
import re
from typing import Optional

import pandas as pd
from app.models.user import User
from app.core.logging_config import log_audit_event
from app.core.config import settings
from app.core.telemetry import (
    trace_ml_operation,
    record_inference_request,
    record_inference_duration,
)
from app.core.mlflow_loader import load_mlflow
from fastapi import HTTPException, status

logger = logging.getLogger("serving_service")


class PortForwardManager:
    """
    Manages on-demand kubectl port-forward processes for local host-to-Kubernetes connectivity.
    Caches active port-forwards and cleans them up on exit.
    """
    _instance = None
    _lock = threading.Lock()

    def __init__(self):
        self._forwards: dict[str, dict] = {}
        self._proc_lock = threading.Lock()

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
                    atexit.register(cls._instance.stop_all)
        return cls._instance

    def _is_port_open(self, host: str, port: int) -> bool:
        try:
            with socket.create_connection((host, port), timeout=0.3):
                return True
        except Exception:
            return False

    def get_service_url(self, service_name: str, target_port: int = 8000, namespace: str = "default") -> Optional[str]:
        # In Kubernetes pods, internal DNS and ClusterIP route directly without port-forwarding
        if os.path.exists("/var/run/secrets/kubernetes.io/serviceaccount"):
            return None

        key = f"{namespace}/{service_name}:{target_port}"
        with self._proc_lock:
            if key in self._forwards:
                info = self._forwards[key]
                proc = info.get("proc")
                local_port = info.get("local_port")
                if proc and proc.poll() is None and self._is_port_open("127.0.0.1", local_port):
                    return f"http://127.0.0.1:{local_port}"
                self._stop_forward(key)

            try:
                proc = subprocess.Popen(
                    ["kubectl", "port-forward", "-n", namespace, f"svc/{service_name}", f":{target_port}"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                local_port = None
                start_t = time.time()
                while time.time() - start_t < 3.0:
                    if proc.poll() is not None:
                        break
                    line = proc.stdout.readline() if proc.stdout else ""
                    m = re.search(r"127\.0\.0\.1:(\d+)", line)
                    if m:
                        local_port = int(m.group(1))
                        break
                    time.sleep(0.05)

                if local_port:
                    self._forwards[key] = {"proc": proc, "local_port": local_port}
                    return f"http://127.0.0.1:{local_port}"
                else:
                    if proc.poll() is None:
                        proc.terminate()
                    return None
            except Exception as e:
                logger.debug(f"Could not establish port-forward for {service_name}: {e}")
                return None

    def _stop_forward(self, key: str):
        if key in self._forwards:
            info = self._forwards.pop(key)
            proc = info.get("proc")
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=1.0)
                except Exception:
                    proc.kill()

    def stop_all(self):
        with self._proc_lock:
            for key in list(self._forwards.keys()):
                self._stop_forward(key)


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
        batch_size = 1
        if isinstance(data, dict):
            if "dataframe_records" in data and data["dataframe_records"] is not None:
                batch_size = len(data["dataframe_records"])
            elif "inputs" in data and data["inputs"] is not None:
                batch_size = len(data["inputs"])
        elif isinstance(data, list):
            batch_size = len(data)

        span_attrs = {
            "ml.operation": "model.inference",
            "ml.serving.target": "rayservice",
            "ml.model.name": str(model_name_or_id),
            "ml.inference.batch_size": batch_size,
        }
        with trace_ml_operation("model.inference", attributes=span_attrs):
            try:
                return self._execute_model_prediction(
                    model_name_or_id=model_name_or_id,
                    data=data,
                    user=user,
                    version=version,
                )
            except Exception:
                record_inference_request(result="error")
                raise

    def _execute_model_prediction(
        self,
        model_name_or_id: str,
        data: dict,
        user: User,
        version: Optional[str] = None,
    ) -> dict:
        mlflow, MlflowClient = load_mlflow()

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

        # 2a. Check authoritative PostgreSQL Deployments table
        try:
            from app.db.session import SessionLocal
            from app.models.deployment import Deployment
            with SessionLocal() as db_session:
                db_dep = db_session.query(Deployment).filter(
                    (Deployment.deployment_id == model_name_or_id)
                    | (Deployment.rayservice_name == model_name_or_id)
                ).first()
                if not db_dep:
                    q = db_session.query(Deployment).filter(Deployment.model_name == model_name_or_id)
                    if version and version != "latest":
                        q = q.filter(Deployment.version == str(version))
                    all_matches = q.order_by(Deployment.created_at.desc()).all()
                    for d in all_matches:
                        if d.status in ("deployed", "running"):
                            db_dep = d
                            break
                    if not db_dep and all_matches:
                        db_dep = all_matches[0]

                if db_dep:
                    matched_model = db_dep.model_name
                    resolved_version = str(db_dep.version)
                    matched_dep_id = db_dep.deployment_id
                    matched_raysvc = db_dep.rayservice_name
                    internal_url_from_tags = f"http://{db_dep.rayservice_name}-serve-svc.default.svc.cluster.local:8000/predict"
        except Exception as db_err:
            logger.debug(f"Could not resolve deployment from DB: {db_err}")

        # 2b. Check MLflow Model Versions
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
                    internal_url_from_tags = tags.get("deployment.internal_endpoint_url") or internal_url_from_tags
                    break
                elif mv.name == model_name_or_id:
                    if tags.get("deployment.status") == "running":
                        matched_dep_id = tags.get("deployment.id")
                        matched_raysvc = tags.get("deployment.rayservice_name")
                        resolved_version = str(mv.version)
                        internal_url_from_tags = tags.get("deployment.internal_endpoint_url") or internal_url_from_tags
        except Exception as search_err:
            logger.debug(f"Could not search MLflow model versions for serving: {search_err}")

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

        # When running on host (outside K8s), port-forward to local ephemeral port for host-to-cluster connectivity
        if target_service and not os.path.exists("/var/run/secrets/kubernetes.io/serviceaccount"):
            port_fwd_url = PortForwardManager.get_instance().get_service_url(
                f"{target_service}-serve-svc", target_port=8000
            )
            if port_fwd_url:
                endpoints.append(f"{port_fwd_url}/predict")

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
                        except Exception as decode_err:
                            logger.debug(f"Could not parse inference error JSON: {decode_err}")
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

        latency_s = time.time() - start_time
        latency_ms = round(latency_s * 1000, 2)
        record_inference_request(result="success")
        record_inference_duration(latency_s)

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
