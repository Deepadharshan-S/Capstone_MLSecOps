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
from fastapi import HTTPException, status



class MLOpsService:
    """
    Service class managing MLOps metadata activities (dataset uploads, training configurations,
    deployment states) and writing activity records to security audit files.
    """

    def _get_scoped_training_credentials(self, job_id: str) -> dict:
        """
        Generates least-privilege credentials for Ray training jobs.
        Attempts to generate temporary, 1-hour MinIO STS session credentials
        scoped strictly to the MLflow artifact storage bucket (s3://mlflow/*).
        Falls back to dedicated training credentials (MINIO_TRAINING_ACCESS_KEY_ID /
        LAKEFS_TRAINING_ACCESS_KEY_ID) or default configured credentials.
        """
        creds = {
            "aws_access_key_id": settings.MINIO_TRAINING_ACCESS_KEY_ID or os.getenv("AWS_ACCESS_KEY_ID", settings.MINIO_ROOT_USER),
            "aws_secret_access_key": settings.MINIO_TRAINING_SECRET_ACCESS_KEY or os.getenv("AWS_SECRET_ACCESS_KEY", settings.MINIO_ROOT_PASSWORD),
            "aws_session_token": "",
            "lakefs_access_key_id": settings.LAKEFS_TRAINING_ACCESS_KEY_ID or settings.LAKEFS_ACCESS_KEY_ID,
            "lakefs_secret_access_key": settings.LAKEFS_TRAINING_SECRET_ACCESS_KEY or settings.LAKEFS_SECRET_ACCESS_KEY,
            "scoped_sts": False,
        }

        if not settings.MINIO_TRAINING_ACCESS_KEY_ID:
            try:
                import boto3
                from botocore.client import Config

                sts_client = boto3.client(
                    "sts",
                    endpoint_url=settings.MINIO_ENDPOINT,
                    aws_access_key_id=settings.MINIO_ROOT_USER,
                    aws_secret_access_key=settings.MINIO_ROOT_PASSWORD,
                    config=Config(signature_version="s3v4", connect_timeout=1, read_timeout=1, retries={"max_attempts": 1}),
                    region_name="us-east-1",
                )
                policy_doc = json.dumps(
                    {
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Effect": "Allow",
                                "Action": [
                                    "s3:GetBucketLocation",
                                    "s3:ListBucket",
                                    "s3:GetObject",
                                    "s3:PutObject",
                                ],
                                "Resource": [
                                    "arn:aws:s3:::mlflow",
                                    "arn:aws:s3:::mlflow/*",
                                ],
                            }
                        ],
                    }
                )
                response = sts_client.assume_role(
                    RoleArn="arn:aws:iam:::role/RayTrainingJobRole",
                    RoleSessionName=f"rayjob-{job_id[:16]}",
                    Policy=policy_doc,
                    DurationSeconds=3600,
                )
                sts_creds = response.get("Credentials", {})
                if sts_creds.get("AccessKeyId"):
                    creds["aws_access_key_id"] = sts_creds["AccessKeyId"]
                    creds["aws_secret_access_key"] = sts_creds["SecretAccessKey"]
                    creds["aws_session_token"] = sts_creds.get("SessionToken", "")
                    creds["scoped_sts"] = True
            except Exception:
                pass

        return creds

    def perform_model_training(
        self,
        dataset_id: str,
        ref: str,
        epochs: int,
        hyperparameters: dict,
        code: str,
        user: User,
        experiment_name: Optional[str] = None,
    ) -> dict:
        """
        Submits a custom training job to Ray by generating RayJobs CRD manifests
        and running a local Ray runner background process fallback.
        """
        job_id = uuid.uuid4().hex[:12]
        experiment_name = experiment_name or f"dataset-{dataset_id}-experiment"
        
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

        entrypoint_cmd = (
            f"python /app/user_code/ray_wrapper.py --dataset_id {dataset_id} --ref {ref} "
            f"--epochs {epochs} --hyperparameters '{json.dumps(hyperparameters)}' "
            f"--code_file /app/user_code/user_code.py --output_model_name {dataset_id}-model "
            f"--job_dir /tmp/rayjob-{job_id} --experiment_name '{experiment_name}'"
        )

        scoped_creds = self._get_scoped_training_credentials(job_id)
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
                f"{dataset_id}-model",
                "--job_dir",
                temp_job_dir,
                "--experiment_name",
                experiment_name,
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
                experiment_name = "unknown"
                
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
                    }
                )
        except Exception as e:
            print(f"Error fetching from MLflow registry: {str(e)}")

        return {"models": models_list}

    def perform_pipeline_training(
        self,
        dataset_id: str,
        ref: str,
        target_column: str,
        model_type: str,
        hyperparameters: dict,
        user: User,
        experiment_name: Optional[str] = None,
    ) -> dict:
        """Submits an automated pipeline training job either via Kubernetes RayJob CRD or local fallback process."""
        # Role checking (viewer cannot train)
        if user.role == "viewer":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Role 'viewer' is not authorized to train models.",
            )

        job_id = uuid.uuid4().hex[:12]
        experiment_name = experiment_name or f"dataset-{dataset_id}-experiment"
        log_audit_event(
            "model_training_initiated",
            user.username,
            None,
            f"Initiated automated pipeline training ({model_type}) on dataset '{dataset_id}' at ref '{ref}' (Job ID: {job_id}).",
        )

        from app.core.config import settings

        # 1. Load the RayJob YAML template
        template_path = os.path.join(os.path.dirname(__file__), "..", "templates", "rayjob_template.yaml")
        with open(template_path, "r") as f:
            template_content = f.read()

        # Helper to convert localhost endpoints to host.docker.internal for K8s environment
        def to_k8s_endpoint(url: str) -> str:
            return url.replace("localhost", "host.docker.internal").replace("127.0.0.1", "host.docker.internal")

        # Indent empty user code content for YAML data block (4 spaces)
        user_code_content_indented = "    # No custom code. Running in pipeline mode."

        # Load and indent ray_wrapper.py content for YAML data block
        wrapper_path = os.path.join(os.path.dirname(__file__), "ray_wrapper.py")
        with open(wrapper_path, "r") as f:
            wrapper_content = f.read()
        ray_wrapper_content_indented = "\n".join("    " + line for line in wrapper_content.splitlines())

        entrypoint_cmd = (
            f"python /app/user_code/ray_wrapper.py --dataset_id {dataset_id} --ref {ref} "
            f"--pipeline_mode --target_column '{target_column}' --model_type '{model_type}' "
            f"--hyperparameters '{json.dumps(hyperparameters)}' --output_model_name {dataset_id}-model "
            f"--job_dir /tmp/rayjob-{job_id} --experiment_name '{experiment_name}'"
        )

        scoped_creds = self._get_scoped_training_credentials(job_id)
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
                f"{dataset_id}-model",
                "--job_dir",
                temp_job_dir,
                "--experiment_name",
                experiment_name,
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
        }

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
        import tempfile
        import shutil
        import re
        import cloudpickle
        import pickle
        import mlflow
        from mlflow.tracking import MlflowClient
        from fastapi import HTTPException, status
        from app.services.ray_wrapper import ModelWrapper
        from app.core.config import settings

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

            wrapped_model = ModelWrapper(model)

            mlflow.set_tracking_uri(settings.MLFLOW_TRACKING_URI)
            exp_name = experiment_name or "uploaded-models"
            mlflow.set_experiment(exp_name)

            # 6. Log and register model in MLflow
            with mlflow.start_run() as run:
                run_id = run.info.run_id

                if parsed_metadata:
                    mlflow.set_tags(parsed_metadata)

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

ml_ops_service = MLOpsService()

