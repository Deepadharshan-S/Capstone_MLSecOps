import os
import json
import subprocess
import threading
import shutil
from app.core.config import settings
from app.core.logging_config import log_audit_event


def to_k8s_endpoint(url: str) -> str:
    """
    Translates localhost / 127.0.0.1 URLs to host.docker.internal
    so that Kubernetes pods can communicate with host Docker services.
    """
    return url.replace("localhost", "host.docker.internal").replace(
        "127.0.0.1", "host.docker.internal"
    )


def get_scoped_training_credentials(job_id: str) -> dict:
    """
    Generates least-privilege credentials for Ray training and serving jobs.
    Attempts to generate temporary, 1-hour MinIO STS session credentials
    scoped strictly to the MLflow artifact storage bucket (s3://mlflow/*).
    Falls back to dedicated training credentials (MINIO_TRAINING_ACCESS_KEY_ID /
    LAKEFS_TRAINING_ACCESS_KEY_ID) or default configured credentials.
    """
    creds = {
        "aws_access_key_id": settings.MINIO_TRAINING_ACCESS_KEY_ID
        or os.getenv("AWS_ACCESS_KEY_ID", settings.MINIO_ROOT_USER),
        "aws_secret_access_key": settings.MINIO_TRAINING_SECRET_ACCESS_KEY
        or os.getenv("AWS_SECRET_ACCESS_KEY", settings.MINIO_ROOT_PASSWORD),
        "aws_session_token": "",
        "lakefs_access_key_id": settings.LAKEFS_TRAINING_ACCESS_KEY_ID
        or settings.LAKEFS_ACCESS_KEY_ID,
        "lakefs_secret_access_key": settings.LAKEFS_TRAINING_SECRET_ACCESS_KEY
        or settings.LAKEFS_SECRET_ACCESS_KEY,
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
                config=Config(
                    signature_version="s3v4",
                    connect_timeout=1,
                    read_timeout=1,
                    retries={"max_attempts": 1},
                ),
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


def render_rayjob_manifest(
    job_id: str,
    entrypoint_cmd: str,
    user_code: str,
    dataset_id: str,
    ref: str,
    hyperparameters: dict,
    experiment_name: str,
    scoped_creds: dict,
    epochs: int = 0,
    target_column: str = "",
    model_type: str = "",
) -> str:
    """
    Renders the KubeRay RayJob manifest from YAML template with injected user code,
    Ray wrapper, environment configuration, and scoped MinIO/lakeFS credentials.
    """
    template_path = os.path.join(os.path.dirname(__file__), "..", "..", "templates", "rayjob_template.yaml")
    with open(template_path, "r") as f:
        template_content = f.read()

    wrapper_path = os.path.join(os.path.dirname(__file__), "ray_wrapper.py")
    with open(wrapper_path, "r") as f:
        wrapper_content = f.read()

    user_code_content_indented = (
        "\n".join("    " + line for line in user_code.splitlines())
        if user_code.strip()
        else "    # No custom code. Running in pipeline mode."
    )
    ray_wrapper_content_indented = "\n".join("    " + line for line in wrapper_content.splitlines())

    replacements = {
        "{{job_id}}": job_id,
        "{{user_code_content_indented}}": user_code_content_indented,
        "{{ray_wrapper_content_indented}}": ray_wrapper_content_indented,
        "{{entrypoint_cmd}}": entrypoint_cmd,
        "{{dataset_id}}": dataset_id,
        "{{ref}}": ref,
        "{{epochs}}": str(epochs),
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

    # Write for debugging/auditing
    try:
        logs_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "logs"))
        os.makedirs(logs_dir, exist_ok=True)
        with open(os.path.join(logs_dir, "last_rendered_yaml.yaml"), "w") as f:
            f.write(rendered_yaml)
    except Exception:
        pass

    return rendered_yaml


def submit_rayjob_to_k8s(
    rendered_yaml: str,
    job_id: str,
    username: str,
    mode_description: str = "",
) -> bool:
    """
    Submits a RayJob CRD manifest directly to the Kubernetes cluster using the official Kubernetes SDK.
    Attaches ownerReferences so ConfigMap and NetworkPolicy are automatically garbage-collected
    when the RayJob is deleted.
    Returns True if submission succeeded, False if cluster is unreachable.
    """
    try:
        import yaml
        from kubernetes import client, config

        try:
            config.load_incluster_config()
        except Exception:
            config.load_kube_config()

        custom_api = client.CustomObjectsApi()
        core_api = client.CoreV1Api()
        net_api = client.NetworkingV1Api()

        docs = list(yaml.safe_load_all(rendered_yaml))
        configmap_doc = None
        rayjob_doc = None
        netpol_doc = None

        for doc in docs:
            if not doc:
                continue
            kind = doc.get("kind")
            if kind == "ConfigMap":
                configmap_doc = doc
            elif kind == "RayJob":
                rayjob_doc = doc
            elif kind == "NetworkPolicy":
                netpol_doc = doc

        if not rayjob_doc:
            raise ValueError("No RayJob manifest found in rendered YAML.")

        # 1. Submit RayJob CRD
        rayjob_name = rayjob_doc.get("metadata", {}).get("name", f"rayjob-{job_id}")
        namespace = rayjob_doc.get("metadata", {}).get("namespace", "default")

        rayjob_res = custom_api.create_namespaced_custom_object(
            group="ray.io",
            version="v1",
            namespace=namespace,
            plural="rayjobs",
            body=rayjob_doc,
        )
        rayjob_uid = rayjob_res.get("metadata", {}).get("uid")

        # 2. Attach ownerReferences for automatic cascading cleanup
        owner_refs = []
        if rayjob_uid:
            owner_refs = [
                {
                    "apiVersion": "ray.io/v1",
                    "kind": "RayJob",
                    "name": rayjob_name,
                    "uid": rayjob_uid,
                    "blockOwnerDeletion": False,
                }
            ]

        # 3. Submit ConfigMap
        if configmap_doc:
            if owner_refs:
                if "metadata" not in configmap_doc:
                    configmap_doc["metadata"] = {}
                configmap_doc["metadata"]["ownerReferences"] = owner_refs
            cm_name = configmap_doc.get("metadata", {}).get("name")
            try:
                core_api.create_namespaced_config_map(namespace=namespace, body=configmap_doc)
            except client.exceptions.ApiException as api_err:
                if api_err.status == 409 and cm_name:
                    core_api.replace_namespaced_config_map(name=cm_name, namespace=namespace, body=configmap_doc)
                else:
                    raise

        # 4. Submit NetworkPolicy
        if netpol_doc:
            if owner_refs:
                if "metadata" not in netpol_doc:
                    netpol_doc["metadata"] = {}
                netpol_doc["metadata"]["ownerReferences"] = owner_refs
            np_name = netpol_doc.get("metadata", {}).get("name")
            try:
                net_api.create_namespaced_network_policy(namespace=namespace, body=netpol_doc)
            except client.exceptions.ApiException as api_err:
                if api_err.status == 409 and np_name:
                    net_api.replace_namespaced_network_policy(name=np_name, namespace=namespace, body=netpol_doc)
                else:
                    raise

        desc = f" for {mode_description}" if mode_description else ""
        log_audit_event(
            "model_training_k8s_submit",
            username,
            None,
            f"Submitted RayJob CRD to Kubernetes cluster{desc}: {rayjob_name}",
        )
        return True
    except Exception as e:
        log_audit_event(
            "model_training_submit_warning",
            username,
            None,
            f"Kubernetes cluster offline. Using local Ray fallback: {str(e)}",
        )
        return False


def spawn_local_ray_subprocess(
    cmd: list[str],
    temp_job_dir: str,
    scoped_creds: dict,
    username: str,
    job_id: str,
    success_description: str,
    error_description: str,
) -> None:
    """
    Spawns an asynchronous background thread executing Ray training locally as a fallback.
    Ensures environment variable propagation and temporary directory cleanup.
    """
    backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))

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
            res = subprocess.run(cmd, capture_output=True, text=True, env=env)
            output_log = f"{res.stdout}\n{res.stderr}".strip()
            try:
                from app.services.dataset.s3_storage_service import S3StorageService
                s3_svc = S3StorageService()
                s3_svc.put_log_content("mlflow", f"logs/{job_id}/training.log", output_log)
            except Exception:
                pass

            try:
                from app.db.session import SessionLocal
                from app.repositories import TrainingJobRepository
                from datetime import datetime, timezone
                now = datetime.now(timezone.utc)
                with SessionLocal() as db_session:
                    repo = TrainingJobRepository(db_session)
                    tj = repo.get_by_job_id(job_id)
                    if tj:
                        if res.returncode == 0:
                            tj.status = "SUCCEEDED"
                        else:
                            tj.status = "FAILED"
                            tj.error_message = res.stderr[:500] if res.stderr else "Local training subprocess failed"
                        tj.completed_at = now
                        if tj.started_at:
                            tj.duration_seconds = round((now - tj.started_at).total_seconds(), 2)
                        repo.save(tj)
            except Exception:
                pass

            if res.returncode == 0:
                log_audit_event(
                    "model_training_completed",
                    username,
                    None,
                    success_description,
                )
            else:
                log_audit_event(
                    "model_training_error",
                    username,
                    None,
                    f"{error_description}: return code {res.returncode}",
                )
        except Exception as subprocess_err:
            try:
                from app.db.session import SessionLocal
                from app.repositories import TrainingJobRepository
                from datetime import datetime, timezone
                with SessionLocal() as db_session:
                    repo = TrainingJobRepository(db_session)
                    tj = repo.get_by_job_id(job_id)
                    if tj:
                        tj.status = "FAILED"
                        tj.completed_at = datetime.now(timezone.utc)
                        tj.error_message = str(subprocess_err)
                        repo.save(tj)
            except Exception:
                pass
            log_audit_event(
                "model_training_error",
                username,
                None,
                f"{error_description}: {str(subprocess_err)}",
            )
        finally:
            shutil.rmtree(temp_job_dir, ignore_errors=True)

    thread = threading.Thread(target=run_training_subprocess)
    thread.start()
