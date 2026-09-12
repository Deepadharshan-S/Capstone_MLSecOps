import os
import subprocess
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
    Dynamically routes predictions to live Kubernetes RayService pods,
    handles model startup warm-up retries, and falls back to local pyfunc execution.
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
        Routes request directly to the active live Kubernetes RayService pod when present,
        or loads model from MLflow / MinIO via pyfunc fallback.
        """
        import mlflow.pyfunc
        from mlflow.tracking import MlflowClient

        start_time = time.time()
        mlflow_client = MlflowClient(tracking_uri=settings.MLFLOW_TRACKING_URI)

        # 1. Parse payload to DataFrame
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
            raise HTTPException(status_code=400, detail=f"Invalid prediction input format: {str(parse_err)}")

        # 2. Resolve model metadata, version, and deployment tags
        resolved_version = str(version) if version else "latest"
        matched_model = model_name_or_id
        matched_dep_id = None
        matched_raysvc = None

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
                    break
                elif mv.name == model_name_or_id:
                    if tags.get("deployment.status") == "running":
                        matched_dep_id = tags.get("deployment.id")
                        matched_raysvc = tags.get("deployment.rayservice_name")
                        resolved_version = str(mv.version)
        except Exception:
            pass

        try:
            reg_model = mlflow_client.get_registered_model(matched_model)
            if not version or version == "latest":
                aliases = reg_model.aliases or {}
                if "production" in aliases:
                    resolved_version = str(aliases["production"])
                elif "champion" in aliases:
                    resolved_version = str(aliases["champion"])
                elif "staging" in aliases:
                    resolved_version = str(aliases["staging"])
                elif reg_model.latest_versions:
                    resolved_version = str(reg_model.latest_versions[-1].version)
                else:
                    resolved_version = "1"
            model_uri = f"models:/{matched_model}/{resolved_version}"
        except Exception:
            model_uri = f"models:/{matched_model}/{resolved_version}"

        # 3. Try routing directly to active Kubernetes RayService pod
        k8s_prediction_done = False
        preds = None

        pod_selectors = []
        if matched_dep_id:
            pod_selectors.append(f"deployment_id={matched_dep_id},ray.io/node-type=head")
        if matched_raysvc:
            pod_selectors.append(f"ray.io/cluster={matched_raysvc},ray.io/node-type=head")
        if model_name_or_id.startswith("raysvc-"):
            pod_selectors.append(f"ray.io/cluster={model_name_or_id},ray.io/node-type=head")

        for selector in pod_selectors:
            try:
                chk = subprocess.run(
                    [
                        "kubectl", "get", "pod",
                        "-l", selector,
                        "--field-selector=status.phase=Running",
                        "-n", "default",
                        "-o", "json",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=4,
                )
                if chk.returncode == 0 and chk.stdout.strip():
                    pod_list = json.loads(chk.stdout.strip()).get("items", [])
                    active_pods = [
                        p for p in pod_list
                        if not p.get("metadata", {}).get("deletionTimestamp")
                        and any(c.get("ready") for c in p.get("status", {}).get("containerStatuses", []))
                    ]
                    if not active_pods and pod_list:
                        active_pods = [p for p in pod_list if not p.get("metadata", {}).get("deletionTimestamp")]
                    if active_pods:
                        k8s_pod_name = active_pods[0]["metadata"]["name"]
                        payload_json = json.dumps(data)
                        exec_cmd = [
                            "kubectl", "exec", k8s_pod_name, "-n", "default", "-i", "--",
                            "python", "-c",
                            (
                                "import urllib.request, sys, time\n"
                                "payload = sys.stdin.read().encode('utf-8')\n"
                                "for attempt in range(5):\n"
                                "    try:\n"
                                "        req = urllib.request.Request('http://localhost:8000/predict', data=payload, headers={'Content-Type': 'application/json'})\n"
                                "        with urllib.request.urlopen(req, timeout=10) as resp:\n"
                                "            sys.stdout.write(resp.read().decode('utf-8'))\n"
                                "            sys.exit(0)\n"
                                "    except urllib.error.HTTPError as e:\n"
                                "        if e.code == 400:\n"
                                "            sys.stderr.write(e.read().decode('utf-8'))\n"
                                "            sys.exit(400)\n"
                                "        time.sleep(2)\n"
                                "    except Exception:\n"
                                "        time.sleep(2)\n"
                                "sys.exit(1)\n"
                            )
                        ]
                        exec_res = subprocess.run(exec_cmd, input=payload_json, capture_output=True, text=True, timeout=25)
                        if exec_res.returncode == 0 and exec_res.stdout.strip():
                            res_obj = json.loads(exec_res.stdout.strip())
                            preds = res_obj.get("predictions", [])
                            k8s_prediction_done = True
                            break
                        elif exec_res.returncode == 400:
                            err_detail = exec_res.stderr.strip() or exec_res.stdout.strip()
                            raise HTTPException(status_code=400, detail=f"Inference error: {err_detail}")
                        else:
                            raise HTTPException(
                                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                                detail=f"RayService deployment for '{matched_model}' is initializing or not ready to accept traffic. Please try again shortly."
                            )
            except HTTPException:
                raise
            except Exception as k8s_err:
                print(f"Notice: Kubernetes pod inference check failed ({k8s_err}). Falling back to local loader.")

        # 4. Fallback to local pyfunc execution if not handled by Kubernetes RayService
        if not k8s_prediction_done:
            os.environ["AWS_ACCESS_KEY_ID"] = os.getenv("AWS_ACCESS_KEY_ID", settings.MINIO_ROOT_USER)
            os.environ["AWS_SECRET_ACCESS_KEY"] = os.getenv("AWS_SECRET_ACCESS_KEY", settings.MINIO_ROOT_PASSWORD)
            os.environ["MLFLOW_S3_ENDPOINT_URL"] = os.getenv("MLFLOW_S3_ENDPOINT_URL", settings.MINIO_ENDPOINT)
            os.environ["MLFLOW_S3_IGNORE_TLS"] = "true"
            mlflow.set_tracking_uri(settings.MLFLOW_TRACKING_URI)

            try:
                loaded_model = mlflow.pyfunc.load_model(model_uri)
                try:
                    preds = loaded_model.predict(df)
                except Exception as first_infer_err:
                    try:
                        preds = loaded_model.predict(df.values)
                    except Exception:
                        raise first_infer_err

                if hasattr(preds, "tolist"):
                    preds = preds.tolist()
                elif hasattr(preds, "to_dict"):
                    preds = preds.to_dict()
            except Exception as infer_err:
                err_str = str(infer_err)
                if "code() argument 13" in err_str:
                    raise HTTPException(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail=(
                            f"Model '{matched_model}' was trained in Python 3.10 and requires an active RayService "
                            f"deployment. Please deploy the model via POST /api/models/deploy to serve it."
                        ),
                    )
                raise HTTPException(
                    status_code=500,
                    detail=f"Inference execution failed for model '{matched_model}' from {model_uri}: {err_str}",
                )

        latency_ms = round((time.time() - start_time) * 1000, 2)

        # 5. Security Audit Log
        log_audit_event(
            "model_prediction",
            user.username,
            None,
            f"Prediction on model '{matched_model}' (version {resolved_version}, samples {len(df)}, latency {latency_ms}ms, k8s_served={k8s_prediction_done}).",
        )

        return {
            "predictions": preds if preds is not None else [],
            "model_name": matched_model,
            "model_version": str(resolved_version),
            "latency_ms": latency_ms,
        }
