import os
import sys
import uuid
import subprocess
import threading
import json
import datetime
from typing import Optional

from app.models.user import User
from app.core.logging_config import log_audit_event
from app.core.config import settings



class MLOpsService:
    """
    Service class managing MLOps metadata activities (dataset uploads, training configurations,
    deployment states) and writing activity records to security audit files.
    """

    def perform_model_training(
        self,
        dataset_id: str,
        ref: str,
        epochs: int,
        hyperparameters: dict,
        code: str,
        user: User,
    ) -> dict:
        """
        Submits a custom training job to Ray by generating RayJobs CRD manifests
        and running a local Ray runner background process fallback.
        """
        job_id = str(uuid.uuid4())
        
        if not code:
            log_audit_event(
                "model_training_start",
                user.username,
                None,
                f"Started training on dataset '{dataset_id}' for {epochs} epochs.",
            )
            return {
                "message": "Training job started successfully.",
                "job_id": job_id,
                "dataset_id": dataset_id,
                "epochs": epochs,
                "started_by": user.username,
                "status": "training",
            }
            
        # Get workspace root directory path
        project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))

        # 1. Load the RayJob YAML template
        template_path = os.path.join(os.path.dirname(__file__), "..", "templates", "rayjob_template.yaml")
        with open(template_path, "r") as f:
            template_content = f.read()

        # Helper to convert localhost endpoints to host.docker.internal for K8s environment
        def to_k8s_endpoint(url: str) -> str:
            return url.replace("localhost", "host.docker.internal").replace("127.0.0.1", "host.docker.internal")

        # Indent user code content for YAML data block (4 spaces)
        user_code_content_indented = "\n".join("    " + line for line in code.splitlines())

        # Load and indent ray_wrapper.py content for YAML data block
        wrapper_path = os.path.join(os.path.dirname(__file__), "ray_wrapper.py")
        with open(wrapper_path, "r") as f:
            wrapper_content = f.read()
        ray_wrapper_content_indented = "\n".join("    " + line for line in wrapper_content.splitlines())

        # Render the template in-memory
        rendered_yaml = template_content
        replacements = {
            "{{job_id}}": job_id,
            "{{user_code_content_indented}}": user_code_content_indented,
            "{{ray_wrapper_content_indented}}": ray_wrapper_content_indented,
            "{{project_root}}": project_root,
            "{{dataset_id}}": dataset_id,
            "{{ref}}": ref,
            "{{epochs}}": str(epochs),
            "{{hyperparameters}}": json.dumps(hyperparameters),
            "{{mlflow_tracking_uri}}": to_k8s_endpoint(settings.MLFLOW_TRACKING_URI),
            "{{mlflow_s3_endpoint_url}}": to_k8s_endpoint(os.getenv("MLFLOW_S3_ENDPOINT_URL", settings.MINIO_ENDPOINT)),
            "{{aws_access_key_id}}": os.getenv("AWS_ACCESS_KEY_ID", settings.MINIO_ROOT_USER),
            "{{aws_secret_access_key}}": os.getenv("AWS_SECRET_ACCESS_KEY", settings.MINIO_ROOT_PASSWORD),
            "{{mlflow_s3_ignore_tls}}": "true",
            "{{lakefs_endpoint}}": to_k8s_endpoint(settings.LAKEFS_ENDPOINT),
            "{{lakefs_access_key_id}}": settings.LAKEFS_ACCESS_KEY_ID,
            "{{lakefs_secret_access_key}}": settings.LAKEFS_SECRET_ACCESS_KEY,
            "{{lakefs_default_branch}}": settings.LAKEFS_DEFAULT_BRANCH,
            "{{minio_endpoint}}": to_k8s_endpoint(settings.MINIO_ENDPOINT),
        }
        for key, val in replacements.items():
            rendered_yaml = rendered_yaml.replace(key, val)

        # Write for debugging
        try:
            with open("/home/deepadharshan/Desktop/Capstone_MLSecOps/backend/logs/last_rendered_yaml.yaml", "w") as f:
                f.write(rendered_yaml)
        except Exception:
            pass

        # 2. Attempt Kubernetes RayJob CRD submission (writing manifest directly to stdin)
        k8s_submitted = False
        try:
            kubeconfig_path = os.path.expanduser("~/.kube/config")
            env = os.environ.copy()
            env["KUBECONFIG"] = kubeconfig_path
            
            res = subprocess.run(
                ["kubectl", "apply", "-f", "-"],
                input=rendered_yaml,
                capture_output=True,
                text=True,
                check=True,
                env=env,
            )
            k8s_submitted = True
            log_audit_event(
                "model_training_k8s_submit",
                user.username,
                None,
                f"Submitted RayJob CRD to Kubernetes cluster: rayjob-{job_id}",
            )
        except Exception as e:
            # Kubectl or cluster offline - log warning and fallback to local Ray runner
            stderr_msg = ""
            if isinstance(e, subprocess.CalledProcessError):
                stderr_msg = f" | stderr: {e.stderr} | stdout: {e.stdout}"
            log_audit_event(
                "model_training_submit_warning",
                user.username,
                None,
                f"Kubernetes cluster offline. Using local Ray fallback: {str(e)}{stderr_msg}",
            )

        # 3. Trigger training run locally if Kubernetes submission failed
        if not k8s_submitted:
            import tempfile
            import shutil
            temp_job_dir = tempfile.mkdtemp(prefix=f"rayjob-{job_id}-")
            code_file = os.path.join(temp_job_dir, "user_code.py")
            with open(code_file, "w") as f:
                f.write(code)

            cmd = [
                sys.executable,
                "app/services/ray_wrapper.py",
                "--dataset_id",
                dataset_id,
                "--ref",
                ref,
                "--epochs",
                str(epochs),
                "--hyperparameters",
                json.dumps(hyperparameters),
                "--code_file",
                code_file,
                "--output_model_name",
                f"{dataset_id}-model",
                "--job_dir",
                temp_job_dir,
            ]

            def run_training_subprocess():
                try:
                    env = os.environ.copy()
                    env["PYTHONPATH"] = os.path.abspath(".")
                    env["MLFLOW_TRACKING_URI"] = settings.MLFLOW_TRACKING_URI
                    env["AWS_ACCESS_KEY_ID"] = settings.MINIO_ROOT_USER
                    env["AWS_SECRET_ACCESS_KEY"] = settings.MINIO_ROOT_PASSWORD
                    env["MLFLOW_S3_ENDPOINT_URL"] = settings.MINIO_ENDPOINT
                    env["MLFLOW_S3_IGNORE_TLS"] = "true"
                    env["LAKEFS_ENDPOINT"] = settings.LAKEFS_ENDPOINT
                    env["LAKEFS_ACCESS_KEY_ID"] = settings.LAKEFS_ACCESS_KEY_ID
                    env["LAKEFS_SECRET_ACCESS_KEY"] = settings.LAKEFS_SECRET_ACCESS_KEY
                    subprocess.run(cmd, check=True, env=env)
                    log_audit_event(
                        "model_training_completed",
                        user.username,
                        None,
                        f"Successfully trained custom model for dataset '{dataset_id}' (Job: {job_id}).",
                    )
                except Exception as subprocess_err:
                    log_audit_event(
                        "model_training_error",
                        user.username,
                        None,
                        f"Error in Ray training background process: {str(subprocess_err)}",
                    )
                finally:
                    shutil.rmtree(temp_job_dir, ignore_errors=True)

            thread = threading.Thread(target=run_training_subprocess)
            thread.start()

        log_audit_event(
            "model_training_start",
            user.username,
            None,
            f"Started training on dataset '{dataset_id}' for {epochs} epochs. Job ID: {job_id}",
        )

        return {
            "message": "Training job started successfully.",
            "job_id": job_id,
            "dataset_id": dataset_id,
            "epochs": epochs,
            "started_by": user.username,
            "status": "training",
        }

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

        from app.core.config import settings
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
                
                if rm.latest_versions:
                    latest_v = rm.latest_versions[-1]
                    # creation_timestamp is in milliseconds since epoch
                    dt = datetime.datetime.fromtimestamp(
                        latest_v.creation_timestamp / 1000.0, datetime.timezone.utc
                    )
                    created_at_str = dt.isoformat()
                    run_id = latest_v.run_id
                    
                    try:
                        run = client.get_run(run_id)
                        run_metrics = run.data.metrics
                        accuracy = run_metrics.get("accuracy", 0.0)
                        precision = run_metrics.get("precision", 0.0)
                        recall = run_metrics.get("recall", 0.0)
                        f1_score = run_metrics.get("f1_score", 0.0)
                    except Exception as e:
                        print(f"Error retrieving run metrics from MLflow: {str(e)}")

                models_list.append(
                    {
                        "id": run_id,
                        "name": rm.name,
                        "accuracy": accuracy,
                        "precision": precision,
                        "recall": recall,
                        "f1_score": f1_score,
                        "created_at": created_at_str,
                    }
                )
        except Exception as e:
            print(f"Error fetching from MLflow registry: {str(e)}")

        return {"models": models_list}

    def perform_model_deploy(self, model_id: str, environment: str, user: User) -> dict:
        """Logs model deployment audit event and returns status payload."""
        log_audit_event(
            "model_deploy",
            user.username,
            None,
            f"Deployed model '{model_id}' to '{environment}' environment.",
        )
        return {
            "message": f"Model '{model_id}' deployed to '{environment}'.",
            "model_id": model_id,
            "environment": environment,
            "deployed_by": user.username,
            "status": "deployed",
        }

    def perform_deployment_management(
        self, deployment_id: str, action: str, user: User
    ) -> dict:
        """Logs deployment management audit event and returns action status payload."""
        log_audit_event(
            "deployment_management",
            user.username,
            None,
            f"Triggered action '{action}' on deployment '{deployment_id}'.",
        )
        return {
            "message": f"Action '{action}' executed on deployment '{deployment_id}'.",
            "deployment_id": deployment_id,
            "action_taken": action,
            "triggered_by": user.username,
        }


ml_ops_service = MLOpsService()
