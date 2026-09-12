import os
import sys
import uuid
import subprocess
import threading
import json
from typing import Optional

from app.models.user import User
from app.core.logging_config import log_audit_event
from app.core.config import settings
from app.services.ml_ops_utils import get_scoped_training_credentials, to_k8s_endpoint
from fastapi import HTTPException, status


class ModelTrainingService:
    """
    Handles model training jobs, generating Kubernetes RayJob CRD manifests,
    and executing background local Ray training fallbacks.
    """

    def perform_model_training(
        self,
        dataset_id: str,
        ref: str,
        epochs: int,
        hyperparameters: dict,
        code: str,
        user: User,
        experiment_name: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> dict:
        """
        Submits a custom training job to Ray by generating RayJobs CRD manifests
        and running a local Ray runner background process fallback.
        """
        job_id = uuid.uuid4().hex[:12]
        experiment_name = experiment_name or f"dataset-{dataset_id}-experiment"
        output_model_name = model_name.strip() if (model_name and model_name.strip()) else f"{dataset_id}-model"

        if not code or not code.strip():
            log_audit_event(
                "model_training_error",
                user.username,
                None,
                f"Attempted training on dataset '{dataset_id}' with empty code.",
            )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Custom training code cannot be empty. Please provide Python training code containing your trainer class.",
            )

        # 1. Load the RayJob YAML template
        template_path = os.path.join(os.path.dirname(__file__), "..", "templates", "rayjob_template.yaml")
        with open(template_path, "r") as f:
            template_content = f.read()

        # Indent user code content for YAML data block (4 spaces)
        user_code_content_indented = "\n".join("    " + line for line in code.splitlines())

        # Load and indent ray_wrapper.py content for YAML data block
        wrapper_path = os.path.join(os.path.dirname(__file__), "ray_wrapper.py")
        with open(wrapper_path, "r") as f:
            wrapper_content = f.read()
        ray_wrapper_content_indented = "\n".join("    " + line for line in wrapper_content.splitlines())

        entrypoint_cmd = (
            f"python /app/user_code/ray_wrapper.py --dataset_id {dataset_id} --ref {ref} "
            f"--epochs {epochs} --hyperparameters '{json.dumps(hyperparameters)}' "
            f"--code_file /app/user_code/user_code.py --output_model_name '{output_model_name}' "
            f"--job_dir /tmp/rayjob-{job_id} --experiment_name '{experiment_name}' "
            f"--user '{user.username}' --job_id '{job_id}'"
        )

        scoped_creds = get_scoped_training_credentials(job_id)
        if scoped_creds["scoped_sts"]:
            log_audit_event(
                "training_credentials_scoped",
                user.username,
                None,
                f"Generated temporary MinIO STS session token for RayJob {job_id} scoped to s3://mlflow/*",
            )

        replacements = {
            "{{job_id}}": job_id,
            "{{user_code_content_indented}}": user_code_content_indented,
            "{{ray_wrapper_content_indented}}": ray_wrapper_content_indented,
            "{{entrypoint_cmd}}": entrypoint_cmd,
            "{{dataset_id}}": dataset_id,
            "{{ref}}": ref,
            "{{epochs}}": str(epochs),
            "{{hyperparameters}}": json.dumps(hyperparameters),
            "{{experiment_name}}": experiment_name,
            "{{mlflow_tracking_uri}}": to_k8s_endpoint(settings.MLFLOW_TRACKING_URI),
            "{{mlflow_s3_endpoint_url}}": to_k8s_endpoint(os.getenv("MLFLOW_S3_ENDPOINT_URL", settings.MINIO_ENDPOINT)),
            "{{aws_access_key_id}}": scoped_creds["aws_access_key_id"],
            "{{aws_secret_access_key}}": scoped_creds["aws_secret_access_key"],
            "{{aws_session_token}}": scoped_creds["aws_session_token"],
            "{{mlflow_s3_ignore_tls}}": "true",
            "{{lakefs_endpoint}}": to_k8s_endpoint(settings.LAKEFS_ENDPOINT),
            "{{lakefs_access_key_id}}": scoped_creds["lakefs_access_key_id"],
            "{{lakefs_secret_access_key}}": scoped_creds["lakefs_secret_access_key"],
            "{{lakefs_default_branch}}": settings.LAKEFS_DEFAULT_BRANCH,
            "{{minio_endpoint}}": to_k8s_endpoint(settings.MINIO_ENDPOINT),
        }
        rendered_yaml = template_content
        for key, val in replacements.items():
            rendered_yaml = rendered_yaml.replace(key, val)

        # Write for debugging
        try:
            logs_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "logs"))
            os.makedirs(logs_dir, exist_ok=True)
            with open(os.path.join(logs_dir, "last_rendered_yaml.yaml"), "w") as f:
                f.write(rendered_yaml)
        except Exception:
            pass

        # 2. Attempt Kubernetes RayJob CRD submission (writing manifest directly to stdin)
        k8s_submitted = False
        try:
            kubeconfig_path = os.path.expanduser("~/.kube/config")
            env = os.environ.copy()
            env["KUBECONFIG"] = kubeconfig_path

            subprocess.run(
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
            if not settings.ALLOW_LOCAL_RAY_FALLBACK:
                log_audit_event(
                    "model_training_rejected",
                    user.username,
                    None,
                    f"Kubernetes cluster offline and local fallback execution is disabled for security (Job: {job_id}).",
                )
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Training cluster is unavailable and local fallback execution is disabled for security.",
                )

            import tempfile
            import shutil

            temp_job_dir = tempfile.mkdtemp(prefix=f"rayjob-{job_id}-")
            code_file = os.path.join(temp_job_dir, "user_code.py")
            with open(code_file, "w") as f:
                f.write(code)

            backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
            ray_wrapper_script = os.path.abspath(os.path.join(os.path.dirname(__file__), "ray_wrapper.py"))

            cmd = [
                sys.executable,
                ray_wrapper_script,
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
                output_model_name,
                "--job_dir",
                temp_job_dir,
                "--experiment_name",
                experiment_name,
                "--user",
                user.username,
                "--job_id",
                job_id,
            ]

            def run_training_subprocess():
                try:
                    env = os.environ.copy()
                    env["PYTHONPATH"] = f"{backend_dir}:{os.environ.get('PYTHONPATH', '')}".rstrip(":")
                    env["MLFLOW_TRACKING_URI"] = settings.MLFLOW_TRACKING_URI
                    env["AWS_ACCESS_KEY_ID"] = scoped_creds["aws_access_key_id"]
                    env["AWS_SECRET_ACCESS_KEY"] = scoped_creds["aws_secret_access_key"]
                    if scoped_creds["aws_session_token"]:
                        env["AWS_SESSION_TOKEN"] = scoped_creds["aws_session_token"]
                        env["AWS_SECURITY_TOKEN"] = scoped_creds["aws_session_token"]
                    env["MLFLOW_S3_ENDPOINT_URL"] = settings.MINIO_ENDPOINT
                    env["MLFLOW_S3_IGNORE_TLS"] = "true"
                    env["LAKEFS_ENDPOINT"] = settings.LAKEFS_ENDPOINT
                    env["LAKEFS_ACCESS_KEY_ID"] = scoped_creds["lakefs_access_key_id"]
                    env["LAKEFS_SECRET_ACCESS_KEY"] = scoped_creds["lakefs_secret_access_key"]
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
            "model_name": output_model_name,
        }

    def perform_pipeline_training(
        self,
        dataset_id: str,
        ref: str,
        target_column: str,
        model_type: str,
        hyperparameters: dict,
        user: User,
        experiment_name: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> dict:
        """Submits an automated pipeline training job either via Kubernetes RayJob CRD or local fallback process."""
        if user.role == "viewer":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Role 'viewer' is not authorized to train models.",
            )

        job_id = uuid.uuid4().hex[:12]
        experiment_name = experiment_name or f"dataset-{dataset_id}-experiment"
        output_model_name = model_name.strip() if (model_name and model_name.strip()) else f"{dataset_id}-model"
        log_audit_event(
            "model_training_initiated",
            user.username,
            None,
            f"Initiated automated pipeline training ({model_type}) on dataset '{dataset_id}' at ref '{ref}' (Job ID: {job_id}).",
        )

        # 1. Load the RayJob YAML template
        template_path = os.path.join(os.path.dirname(__file__), "..", "templates", "rayjob_template.yaml")
        with open(template_path, "r") as f:
            template_content = f.read()

        user_code_content_indented = "    # No custom code. Running in pipeline mode."

        wrapper_path = os.path.join(os.path.dirname(__file__), "ray_wrapper.py")
        with open(wrapper_path, "r") as f:
            wrapper_content = f.read()
        ray_wrapper_content_indented = "\n".join("    " + line for line in wrapper_content.splitlines())

        entrypoint_cmd = (
            f"python /app/user_code/ray_wrapper.py --dataset_id {dataset_id} --ref {ref} "
            f"--pipeline_mode --target_column '{target_column}' --model_type '{model_type}' "
            f"--hyperparameters '{json.dumps(hyperparameters)}' --output_model_name '{output_model_name}' "
            f"--job_dir /tmp/rayjob-{job_id} --experiment_name '{experiment_name}' "
            f"--user '{user.username}' --job_id '{job_id}'"
        )

        scoped_creds = get_scoped_training_credentials(job_id)
        if scoped_creds["scoped_sts"]:
            log_audit_event(
                "training_credentials_scoped",
                user.username,
                None,
                f"Generated temporary MinIO STS session token for automated pipeline RayJob {job_id} scoped to s3://mlflow/*",
            )

        replacements = {
            "{{job_id}}": job_id,
            "{{user_code_content_indented}}": user_code_content_indented,
            "{{ray_wrapper_content_indented}}": ray_wrapper_content_indented,
            "{{entrypoint_cmd}}": entrypoint_cmd,
            "{{dataset_id}}": dataset_id,
            "{{ref}}": ref,
            "{{epochs}}": "0",  # Not used in pipeline mode
            "{{hyperparameters}}": json.dumps(hyperparameters),
            "{{target_column}}": target_column,
            "{{model_type}}": model_type,
            "{{experiment_name}}": experiment_name,
            "{{mlflow_tracking_uri}}": to_k8s_endpoint(settings.MLFLOW_TRACKING_URI),
            "{{mlflow_s3_endpoint_url}}": to_k8s_endpoint(os.getenv("MLFLOW_S3_ENDPOINT_URL", settings.MINIO_ENDPOINT)),
            "{{aws_access_key_id}}": scoped_creds["aws_access_key_id"],
            "{{aws_secret_access_key}}": scoped_creds["aws_secret_access_key"],
            "{{aws_session_token}}": scoped_creds["aws_session_token"],
            "{{mlflow_s3_ignore_tls}}": "true",
            "{{lakefs_endpoint}}": to_k8s_endpoint(settings.LAKEFS_ENDPOINT),
            "{{lakefs_access_key_id}}": scoped_creds["lakefs_access_key_id"],
            "{{lakefs_secret_access_key}}": scoped_creds["lakefs_secret_access_key"],
            "{{lakefs_default_branch}}": settings.LAKEFS_DEFAULT_BRANCH,
            "{{minio_endpoint}}": to_k8s_endpoint(settings.MINIO_ENDPOINT),
        }
        rendered_yaml = template_content
        for key, val in replacements.items():
            rendered_yaml = rendered_yaml.replace(key, val)

        # Write for debugging
        try:
            logs_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "logs"))
            os.makedirs(logs_dir, exist_ok=True)
            with open(os.path.join(logs_dir, "last_rendered_yaml.yaml"), "w") as f:
                f.write(rendered_yaml)
        except Exception:
            pass

        # 2. Attempt Kubernetes RayJob CRD submission
        k8s_submitted = False
        try:
            kubeconfig_path = os.path.expanduser("~/.kube/config")
            env = os.environ.copy()
            env["KUBECONFIG"] = kubeconfig_path

            subprocess.run(
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
                f"Submitted RayJob CRD to Kubernetes cluster for automated pipeline: rayjob-{job_id}",
            )
        except Exception as e:
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
            if not settings.ALLOW_LOCAL_RAY_FALLBACK:
                log_audit_event(
                    "model_training_rejected",
                    user.username,
                    None,
                    f"Kubernetes cluster offline and local fallback execution is disabled for security (Job: {job_id}).",
                )
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Training cluster is unavailable and local fallback execution is disabled for security.",
                )

            import tempfile
            import shutil

            temp_job_dir = tempfile.mkdtemp(prefix=f"rayjob-{job_id}-")

            backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
            ray_wrapper_script = os.path.abspath(os.path.join(os.path.dirname(__file__), "ray_wrapper.py"))

            cmd = [
                sys.executable,
                ray_wrapper_script,
                "--dataset_id",
                dataset_id,
                "--ref",
                ref,
                "--pipeline_mode",
                "--target_column",
                target_column,
                "--model_type",
                model_type,
                "--hyperparameters",
                json.dumps(hyperparameters),
                "--output_model_name",
                output_model_name,
                "--job_dir",
                temp_job_dir,
                "--experiment_name",
                experiment_name,
                "--user",
                user.username,
                "--job_id",
                job_id,
            ]

            def run_training_subprocess():
                try:
                    env = os.environ.copy()
                    env["PYTHONPATH"] = f"{backend_dir}:{os.environ.get('PYTHONPATH', '')}".rstrip(":")
                    env["MLFLOW_TRACKING_URI"] = settings.MLFLOW_TRACKING_URI
                    env["AWS_ACCESS_KEY_ID"] = scoped_creds["aws_access_key_id"]
                    env["AWS_SECRET_ACCESS_KEY"] = scoped_creds["aws_secret_access_key"]
                    if scoped_creds["aws_session_token"]:
                        env["AWS_SESSION_TOKEN"] = scoped_creds["aws_session_token"]
                        env["AWS_SECURITY_TOKEN"] = scoped_creds["aws_session_token"]
                    env["MLFLOW_S3_ENDPOINT_URL"] = settings.MINIO_ENDPOINT
                    env["MLFLOW_S3_IGNORE_TLS"] = "true"
                    env["LAKEFS_ENDPOINT"] = settings.LAKEFS_ENDPOINT
                    env["LAKEFS_ACCESS_KEY_ID"] = scoped_creds["lakefs_access_key_id"]
                    env["LAKEFS_SECRET_ACCESS_KEY"] = scoped_creds["lakefs_secret_access_key"]
                    subprocess.run(cmd, check=True, env=env)
                    log_audit_event(
                        "model_training_completed",
                        user.username,
                        None,
                        f"Successfully trained automated pipeline model ({model_type}) for dataset '{dataset_id}' (Job: {job_id}).",
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

        return {
            "message": f"Model training job '{job_id}' started on Ray cluster.",
            "job_id": job_id,
            "dataset_id": dataset_id,
            "epochs": 0,
            "started_by": user.username,
            "status": "training",
            "model_name": output_model_name,
        }
