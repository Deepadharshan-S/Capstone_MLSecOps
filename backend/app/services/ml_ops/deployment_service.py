import os
import uuid
import datetime
from typing import Optional
from sqlalchemy.orm import Session

from app.models.user import User
from app.core.logging_config import log_audit_event
from app.core.config import settings
from app.services.ml_ops.utils import get_scoped_training_credentials, to_k8s_endpoint
from app.services.dataset.utils import get_repo_name
from fastapi import HTTPException, status


class ModelDeploymentService:
    """
    Manages model deployment lifecycles using KubeRay RayService CRDs
    and MLflow Model Version metadata tracking (stages, aliases, and tags).
    Hides raw Kubernetes details from controllers using the official Kubernetes Python SDK.
    """

    def _get_k8s_apis(self):
        """Returns initialized Kubernetes CustomObjectsApi and CoreV1Api instances."""
        from kubernetes import client, config

        try:
            config.load_incluster_config()
        except Exception:
            config.load_kube_config()
        return client.CustomObjectsApi(), client.CoreV1Api()

    def perform_model_deploy(
        self,
        model_id: str,
        environment: str,
        user: User,
        version: Optional[str] = "latest",
        replicas: Optional[int] = 1,
        db: Optional[Session] = None,
    ) -> dict:
        """
        Deploys a registered MLflow model to a live Kubernetes RayService CRD using the Kubernetes SDK.
        Updates model version stages, aliases, and deployment metadata tags directly in MLflow.
        """
        from mlflow.tracking import MlflowClient

        mlflow_client = MlflowClient(tracking_uri=settings.MLFLOW_TRACKING_URI)
        deployment_id = uuid.uuid4().hex[:12]
        target_version = "1"
        model_uri = f"models:/{model_id}/1"
        registered = False

        try:
            # 1. Resolve model version from MLflow Model Registry
            try:
                mlflow_client.get_registered_model(model_id)
                registered = True
                if version and version != "latest":
                    mv = mlflow_client.get_model_version(model_id, str(version))
                    target_version = str(mv.version)
                else:
                    all_versions = mlflow_client.search_model_versions(
                        f"name = '{model_id}'", order_by=["version_number DESC"], max_results=1
                    )
                    if all_versions:
                        target_version = str(all_versions[0].version)
                    else:
                        latest_versions = mlflow_client.get_latest_versions(model_id)
                        if latest_versions:
                            target_version = str(latest_versions[-1].version)
                model_uri = f"models:/{model_id}/{target_version}"
            except Exception as ml_err:
                print(f"Notice: Model '{model_id}' not found in MLflow registry ({ml_err}). Using fallback version.")
                target_version = str(version) if (version and version != "latest") else "1"

            sanitized_name = get_repo_name(model_id)[:16].rstrip("-")
            rayservice_name = f"raysvc-{sanitized_name}-v{target_version}"
            internal_endpoint_url = f"http://{rayservice_name}-serve-svc.default.svc.cluster.local:8000/predict"
            endpoint_url = f"/api/deployments/{deployment_id}/predict"
            now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

            # 2. Update MLflow Model Version Tags, Stages & Aliases
            if registered:
                stage = "Production" if environment.lower() == "production" else "Staging"
                try:
                    import warnings
                    with warnings.catch_warnings():
                        warnings.simplefilter("ignore", category=FutureWarning)
                        mlflow_client.transition_model_version_stage(
                            name=model_id, version=target_version, stage=stage
                        )
                except Exception as st_err:
                    print(f"Notice: Could not transition stage in MLflow: {st_err}")

                try:
                    alias = "production" if environment.lower() == "production" else "staging"
                    mlflow_client.set_registered_model_alias(name=model_id, alias=alias, version=target_version)
                    if environment.lower() == "production":
                        mlflow_client.set_registered_model_alias(name=model_id, alias="champion", version=target_version)
                except Exception as al_err:
                    print(f"Notice: Could not set alias in MLflow: {al_err}")

                tags = {
                    "deployment.id": deployment_id,
                    "deployment.status": "running",
                    "deployment.environment": environment.lower(),
                    "deployment.rayservice_name": rayservice_name,
                    "deployment.endpoint_url": endpoint_url,
                    "deployment.internal_endpoint_url": internal_endpoint_url,
                    "deployment.deployed_by": user.username,
                    "deployment.deployed_at": now_iso,
                }
                for tk, tv in tags.items():
                    try:
                        mlflow_client.set_model_version_tag(model_id, target_version, tk, str(tv))
                    except Exception as tag_err:
                        print(f"Notice: Could not set tag {tk} on model version: {tag_err}")

            # 3. Render and apply KubeRay RayService Manifest using Kubernetes Python SDK
            template_path = os.path.join(os.path.dirname(__file__), "..", "..", "templates", "rayservice_template.yaml")
            serve_wrapper_path = os.path.join(os.path.dirname(__file__), "serve_wrapper.py")

            if os.path.exists(template_path) and os.path.exists(serve_wrapper_path):
                with open(serve_wrapper_path, "r") as f:
                    serve_code = f.read()
                serve_code_indented = "\n".join("    " + line for line in serve_code.splitlines())

                with open(template_path, "r") as f:
                    template_content = f.read()

                scoped_creds = get_scoped_training_credentials(deployment_id)

                replacements = {
                    "{{deployment_id}}": deployment_id,
                    "{{serve_wrapper_content_indented}}": serve_code_indented,
                    "{{rayservice_name}}": rayservice_name,
                    "{{model_name_label}}": get_repo_name(model_id)[:63],
                    "{{environment}}": environment.lower(),
                    "{{aws_access_key_id}}": scoped_creds["aws_access_key_id"],
                    "{{aws_secret_access_key}}": scoped_creds["aws_secret_access_key"],
                    "{{aws_session_token}}": scoped_creds["aws_session_token"],
                    "{{model_uri}}": model_uri,
                    "{{mlflow_tracking_uri}}": settings.MLFLOW_INTERNAL_ENDPOINT
                    or to_k8s_endpoint(settings.MLFLOW_TRACKING_URI),
                    "{{mlflow_s3_endpoint_url}}": settings.MINIO_INTERNAL_ENDPOINT
                    or to_k8s_endpoint(settings.MINIO_ENDPOINT),
                }
                rendered_yaml = template_content
                for placeholder, val in replacements.items():
                    rendered_yaml = rendered_yaml.replace(placeholder, str(val))

                try:
                    import yaml
                    from kubernetes import client

                    custom_api, core_api = self._get_k8s_apis()
                    docs = list(yaml.safe_load_all(rendered_yaml))
                    for doc in docs:
                        if not doc:
                            continue
                        kind = doc.get("kind")
                        ns = doc.get("metadata", {}).get("namespace", "default")
                        name = doc.get("metadata", {}).get("name")
                        if kind == "ConfigMap":
                            try:
                                core_api.create_namespaced_config_map(namespace=ns, body=doc)
                            except client.exceptions.ApiException as api_err:
                                if api_err.status == 409 and name:
                                    core_api.replace_namespaced_config_map(name=name, namespace=ns, body=doc)
                                else:
                                    raise
                        elif kind == "RayService":
                            try:
                                custom_api.create_namespaced_custom_object(
                                    group="ray.io",
                                    version="v1",
                                    namespace=ns,
                                    plural="rayservices",
                                    body=doc,
                                )
                            except client.exceptions.ApiException as api_err:
                                if api_err.status == 409 and name:
                                    existing = custom_api.get_namespaced_custom_object(
                                        group="ray.io", version="v1", namespace=ns, plural="rayservices", name=name
                                    )
                                    doc["metadata"]["resourceVersion"] = existing.get("metadata", {}).get("resourceVersion")
                                    custom_api.replace_namespaced_custom_object(
                                        group="ray.io", version="v1", namespace=ns, plural="rayservices", name=name, body=doc
                                    )
                                else:
                                    raise
                    print(f"Successfully applied RayService manifest via Kubernetes SDK: {rayservice_name}")
                except Exception as k8s_err:
                    print(f"Notice: Could not apply RayService manifest to Kubernetes via SDK: {k8s_err}")

            # Persist deployment record to PostgreSQL via DeploymentRepository
            try:
                from app.models.deployment import Deployment
                from app.repositories.deployment_repository import DeploymentRepository
                from app.db.session import SessionLocal

                dep_record = Deployment(
                    deployment_id=deployment_id,
                    model_name=model_id,
                    version=str(target_version),
                    environment=environment,
                    rayservice_name=rayservice_name,
                    endpoint_url=endpoint_url,
                    status="deployed",
                    created_by_id=getattr(user, "id", None) or uuid.uuid4(),
                    created_by_username=getattr(user, "username", "unknown"),
                )
                if db is not None:
                    DeploymentRepository(db).create(dep_record)
                else:
                    with SessionLocal() as db_session:
                        DeploymentRepository(db_session).create(dep_record)
            except Exception as db_err:
                print(f"Notice: Could not persist Deployment to PostgreSQL ({db_err}).")

            log_audit_event(
                "model_deploy",
                user.username,
                None,
                f"Deployed model '{model_id}' (v{target_version}) to '{environment}' as RayService '{rayservice_name}' (ID: {deployment_id}).",
            )

            return {
                "message": f"Model '{model_id}' (version {target_version}) deployed successfully to '{environment}'.",
                "model_id": model_id,
                "version": target_version,
                "environment": environment,
                "deployed_by": user.username,
                "status": "deployed",
                "endpoint_url": endpoint_url,
                "rayservice_name": rayservice_name,
                "deployment_id": deployment_id,
            }
        except Exception as e:
            print(f"Error during perform_model_deploy: {e}")
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))

    def retrieve_deployments(
        self,
        user: User,
        status: Optional[str] = None,
        environment: Optional[str] = None,
        active_only: bool = False,
        db: Optional[Session] = None,
    ) -> dict:
        """
        Retrieves model deployments tracked in PostgreSQL,
        reconciling them with live Kubernetes RayService status using a single batch query.
        Also scans legacy MLflow model version tags for backwards compatibility.
        """
        from mlflow.tracking import MlflowClient

        mlflow_client = MlflowClient(tracking_uri=settings.MLFLOW_TRACKING_URI)
        deployments = []

        # 1. Fetch live RayService statuses in a single bulk call
        k8s_status_map = {}
        try:
            custom_api, _ = self._get_k8s_apis()
            items = custom_api.list_namespaced_custom_object(
                group="ray.io", version="v1", namespace="default", plural="rayservices"
            ).get("items", [])
            for item in items:
                name = item.get("metadata", {}).get("name")
                svc_st = item.get("status", {}).get("serviceStatus")
                if name and svc_st:
                    k8s_status_map[name] = svc_st
        except Exception:
            pass

        seen_ids = set()

        # 2. Query PostgreSQL DeploymentRepository (Authoritative DB)
        try:
            from app.repositories.deployment_repository import DeploymentRepository
            from app.db.session import SessionLocal

            db_deployments = []
            if db is not None:
                repo = DeploymentRepository(db)
                db_deployments = list(repo.list(status=status, environment=environment))
            else:
                with SessionLocal() as db_session:
                    repo = DeploymentRepository(db_session)
                    db_deployments = list(repo.list(status=status, environment=environment))

            for dep in db_deployments:
                seen_ids.add(dep.deployment_id)
                k8s_status = "Unknown"
                if dep.rayservice_name:
                    if dep.rayservice_name in k8s_status_map:
                        k8s_status = k8s_status_map[dep.rayservice_name]
                    else:
                        k8s_status = "Not Found" if dep.status == "stopped" else "Initializing"

                deployments.append({
                    "deployment_id": dep.deployment_id,
                    "model_name": dep.model_name,
                    "version": str(dep.version),
                    "environment": dep.environment,
                    "status": dep.status,
                    "rayservice_name": dep.rayservice_name,
                    "endpoint_url": dep.endpoint_url or f"/api/deployments/{dep.deployment_id}/predict",
                    "deployed_by": dep.created_by_username,
                    "deployed_at": dep.created_at.isoformat() if dep.created_at else None,
                    "k8s_status": k8s_status,
                })
        except Exception as db_err:
            print(f"Notice: Failed to fetch deployments from DB ({db_err}).")

        # 3. Fallback scan of MLflow tags for legacy deployments
        try:
            all_versions = mlflow_client.search_model_versions("")
            for mv in all_versions:
                tags = mv.tags or {}
                if "deployment.status" in tags:
                    dep_id = tags.get("deployment.id", f"{mv.name}-v{mv.version}")
                    if dep_id in seen_ids:
                        continue
                    seen_ids.add(dep_id)
                    raysvc = tags.get("deployment.rayservice_name")
                    k8s_status = "Unknown"
                    if raysvc:
                        if raysvc in k8s_status_map:
                            k8s_status = k8s_status_map[raysvc]
                        else:
                            k8s_status = "Not Found" if tags.get("deployment.status") == "stopped" else "Initializing"

                    deployments.append({
                        "deployment_id": dep_id,
                        "model_name": mv.name,
                        "version": str(mv.version),
                        "environment": tags.get("deployment.environment", "staging"),
                        "status": tags.get("deployment.status", "unknown"),
                        "rayservice_name": raysvc,
                        "endpoint_url": tags.get("deployment.endpoint_url", f"/api/deployments/{dep_id}/predict"),
                        "deployed_by": tags.get("deployment.deployed_by"),
                        "deployed_at": tags.get("deployment.deployed_at"),
                        "k8s_status": k8s_status,
                    })
        except Exception as e:
            print(f"Notice: Error searching legacy deployments in MLflow: {e}")

        # Apply optional filtering
        if active_only:
            deployments = [d for d in deployments if d["status"].lower() not in ["stopped", "failed"]]
        if status:
            deployments = [d for d in deployments if d["status"].lower() == status.strip().lower()]
        if environment:
            deployments = [d for d in deployments if d["environment"].lower() == environment.strip().lower()]

        log_audit_event("deployments_view", user.username, None, "Viewed model deployments list.", db=db)
        return {"deployments": deployments}

    def retrieve_deployment_detail(
        self,
        deployment_id: str,
        user: User,
        db: Optional[Session] = None,
    ) -> dict:
        """
        Retrieves detailed RayService status, replica health, and internal endpoints
        for a specific deployment using native Kubernetes SDK and PostgreSQL.
        """
        log_audit_event(
            "deployment_detail_view",
            user.username,
            None,
            f"Viewed details for deployment '{deployment_id}'.",
            db=db,
        )

        db_dep = None
        try:
            from app.repositories.deployment_repository import DeploymentRepository
            from app.db.session import SessionLocal

            if db is not None:
                repo = DeploymentRepository(db)
                db_dep = repo.get_by_deployment_id(deployment_id)
            else:
                with SessionLocal() as db_session:
                    repo = DeploymentRepository(db_session)
                    db_dep = repo.get_by_deployment_id(deployment_id)
        except Exception as db_err:
            print(f"Notice: Failed to query deployment detail from DB: {db_err}")

        model_name = deployment_id
        version = "1"
        environment = "staging"
        dep_status = "unknown"
        raysvc = None
        endpoint_url = f"/api/deployments/{deployment_id}/predict"
        deployed_by = None
        deployed_at = None
        tags = {}
        dep_id = deployment_id

        if db_dep:
            dep_id = db_dep.deployment_id
            model_name = db_dep.model_name
            version = str(db_dep.version)
            environment = db_dep.environment
            dep_status = db_dep.status
            raysvc = db_dep.rayservice_name
            endpoint_url = db_dep.endpoint_url or f"/api/deployments/{dep_id}/predict"
            deployed_by = db_dep.created_by_username
            deployed_at = db_dep.created_at.isoformat() if db_dep.created_at else None
        else:
            from mlflow.tracking import MlflowClient

            mlflow_client = MlflowClient(tracking_uri=settings.MLFLOW_TRACKING_URI)
            target_item = None

            try:
                all_versions = mlflow_client.search_model_versions("")
                for mv in all_versions:
                    t = mv.tags or {}
                    if (
                        t.get("deployment.id") == deployment_id
                        or t.get("deployment.rayservice_name") == deployment_id
                        or deployment_id in [mv.name, f"{mv.name}-v{mv.version}"]
                    ):
                        target_item = (mv, t)
                        break
            except Exception as e:
                print(f"Error searching deployments in MLflow: {e}")

            if target_item:
                mv, t = target_item
                dep_id = t.get("deployment.id", deployment_id)
                model_name = mv.name
                version = str(mv.version)
                environment = t.get("deployment.environment", "staging")
                dep_status = t.get("deployment.status", "unknown")
                raysvc = t.get("deployment.rayservice_name")
                endpoint_url = t.get("deployment.endpoint_url", f"/api/deployments/{dep_id}/predict")
                deployed_by = t.get("deployment.deployed_by")
                deployed_at = t.get("deployment.deployed_at")
                tags = dict(t)
            elif deployment_id.startswith("raysvc-"):
                try:
                    custom_api, core_api = self._get_k8s_apis()
                    k8s_json = custom_api.get_namespaced_custom_object(
                        group="ray.io", version="v1", namespace="default", plural="rayservices", name=deployment_id
                    )
                    status_obj = k8s_json.get("status", {})
                    svc_status = status_obj.get("serviceStatus", "Unknown")
                    endpoints = status_obj.get("activeClusterStatus", {}).get("endpoints", {})
                    num_endpoints = status_obj.get("numServeEndpoints", 0)
                    return {
                        "deployment_id": deployment_id,
                        "model_name": deployment_id,
                        "version": "1",
                        "environment": "production",
                        "status": "active" if svc_status == "Running" else svc_status.lower(),
                        "rayservice_name": deployment_id,
                        "endpoint_url": f"/api/deployments/{deployment_id}/predict",
                        "internal_endpoints": {k: str(v) for k, v in endpoints.items()},
                        "deployed_by": "system",
                        "deployed_at": None,
                        "k8s_status": svc_status,
                        "service_status": svc_status,
                        "replica_health": {
                            "desired_replicas": 1,
                            "ready_replicas": num_endpoints,
                            "head_pod_status": svc_status,
                            "details": status_obj.get("applicationStatuses", {}),
                        },
                        "ray_cluster_status": status_obj.get("activeClusterStatus", {}),
                        "tags": {},
                    }
                except Exception:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail=f"Deployment '{deployment_id}' not found.",
                    )
            else:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Deployment '{deployment_id}' not found.",
                )

        internal_endpoints = {}
        if raysvc:
            internal_endpoints["serve"] = f"http://{raysvc}-serve-svc.default.svc.cluster.local:8000/predict"

        k8s_status = "Unknown"
        service_status = "Unknown"
        replica_health = {
            "desired_replicas": 1,
            "ready_replicas": 0,
            "active_worker_pods": 0,
            "total_worker_pods": 0,
            "head_pod_status": "Unknown",
            "details": {},
        }
        ray_cluster_status = {}

        if raysvc:
            try:
                custom_api, core_api = self._get_k8s_apis()
                k8s_json = custom_api.get_namespaced_custom_object(
                    group="ray.io", version="v1", namespace="default", plural="rayservices", name=raysvc
                )
                status_obj = k8s_json.get("status", {})
                service_status = status_obj.get("serviceStatus", "Unknown")
                k8s_status = service_status

                active_cluster = status_obj.get("activeClusterStatus", {})
                ray_cluster_status = active_cluster
                endpoints = active_cluster.get("endpoints", {})
                for ep_name, ep_val in endpoints.items():
                    internal_endpoints[ep_name] = str(ep_val)

                num_endpoints = status_obj.get("numServeEndpoints", 0)

                desired_reps = 1
                try:
                    spec = k8s_json.get("spec", {})
                    serve_cfg = spec.get("serveConfigV2", {})
                    apps = serve_cfg.get("applications", [])
                    if apps and "deployments" in apps[0] and apps[0]["deployments"]:
                        desired_reps = apps[0]["deployments"][0].get("num_replicas", 1)
                except Exception:
                    pass

                total_workers = 0
                head_status = service_status
                try:
                    pod_list = core_api.list_namespaced_pod(
                        namespace="default",
                        label_selector=f"ray.io/cluster={raysvc}",
                    )
                    items = getattr(pod_list, "items", [])
                    total_workers = len([p for p in items if getattr(getattr(p, "metadata", None), "deletion_timestamp", None) is None])
                    head_pod = next(
                        (p for p in items if getattr(getattr(p, "metadata", None), "labels", {}).get("ray.io/node-type") == "head"),
                        None,
                    )
                    if head_pod and head_pod.status:
                        head_status = head_pod.status.phase or service_status
                except Exception:
                    pass

                replica_health["desired_replicas"] = desired_reps
                replica_health["ready_replicas"] = num_endpoints
                replica_health["active_worker_pods"] = total_workers
                replica_health["total_worker_pods"] = total_workers
                replica_health["head_pod_status"] = "Running" if service_status == "Running" else head_status
                replica_health["details"] = {
                    "application_statuses": status_obj.get("applicationStatuses", {}),
                }
            except Exception as e:
                k8s_status = "Not Found" if dep_status == "stopped" else "Initializing"
                service_status = "Stopped" if dep_status == "stopped" else "Not Found"
                replica_health["head_pod_status"] = "Stopped" if dep_status == "stopped" else "Not Found"
                replica_health["desired_replicas"] = 0 if dep_status == "stopped" else 1

        return {
            "deployment_id": dep_id,
            "model_name": model_name,
            "version": str(version),
            "environment": environment,
            "status": dep_status,
            "rayservice_name": raysvc,
            "endpoint_url": endpoint_url,
            "internal_endpoints": internal_endpoints,
            "deployed_by": deployed_by,
            "deployed_at": deployed_at,
            "k8s_status": k8s_status,
            "service_status": service_status,
            "replica_health": replica_health,
            "ray_cluster_status": ray_cluster_status,
            "tags": dict(tags),
        }

    def perform_deployment_management(
        self,
        deployment_id: str,
        action: str,
        user: User,
        db: Optional[Session] = None,
    ) -> dict:
        """
        Executes lifecycle actions (restart, stop, rollback) on a model deployment.
        Updates PostgreSQL deployment status, MLflow tags, and deletes/restarts Kubernetes RayService resources using native SDK.
        """
        from mlflow.tracking import MlflowClient

        mlflow_client = MlflowClient(tracking_uri=settings.MLFLOW_TRACKING_URI)
        rayservice_name = None

        allowed_actions = ["stop", "restart", "rollback"]
        if action.lower() not in allowed_actions:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid action '{action}'. Supported actions are: {allowed_actions}."
            )

        if deployment_id.startswith("raysvc-"):
            rayservice_name = deployment_id

        # 1. Update PostgreSQL Deployment entity if present
        try:
            from app.repositories.deployment_repository import DeploymentRepository
            from app.db.session import SessionLocal

            db_conn = db
            should_close = False
            if db_conn is None:
                db_conn = SessionLocal()
                should_close = True

            repo = DeploymentRepository(db_conn)
            dep = repo.get_by_deployment_id(deployment_id)
            if dep:
                if action.lower() == "stop":
                    dep.status = "stopped"
                elif action.lower() == "restart":
                    dep.status = "active"
                elif action.lower() == "rollback":
                    dep.status = "rolled_back"
                repo.save(dep)
                if not rayservice_name and dep.rayservice_name:
                    rayservice_name = dep.rayservice_name

            if should_close:
                db_conn.close()
        except Exception as db_err:
            print(f"Notice: Failed to update deployment in DB ({db_err}).")

        # 2. Update MLflow Model Version tags for backward compatibility
        matched_items = []
        try:
            all_versions = mlflow_client.search_model_versions("")
            for mv in all_versions:
                tags = mv.tags or {}
                if (
                    tags.get("deployment.id") == deployment_id
                    or tags.get("deployment.rayservice_name") == deployment_id
                    or mv.name == deployment_id
                    or deployment_id in [mv.name, f"{mv.name}-v{mv.version}"]
                ):
                    matched_items.append((
                        mv.name,
                        str(mv.version),
                        tags.get("deployment.rayservice_name"),
                        tags.get("deployment.id", deployment_id),
                    ))
        except Exception as e:
            print(f"Notice: Could not search model versions in MLflow: {e}")

        if action.lower() == "stop":
            labels_to_clean = {f"deployment_id={deployment_id}"}
            raysvc_names_to_clean = set()
            if rayservice_name:
                raysvc_names_to_clean.add(rayservice_name)

            for m_name, m_ver, r_svc, d_id in matched_items:
                if d_id:
                    labels_to_clean.add(f"deployment_id={d_id}")
                if r_svc:
                    raysvc_names_to_clean.add(r_svc)
                try:
                    mlflow_client.set_model_version_tag(m_name, m_ver, "deployment.status", "stopped")
                except Exception:
                    pass

            try:
                custom_api, core_api = self._get_k8s_apis()
                for r_svc in raysvc_names_to_clean:
                    try:
                        custom_api.delete_namespaced_custom_object(
                            group="ray.io", version="v1", namespace="default", plural="rayservices", name=r_svc
                        )
                    except Exception:
                        pass
                    try:
                        custom_api.delete_namespaced_custom_object(
                            group="ray.io", version="v1", namespace="default", plural="rayclusters", name=r_svc
                        )
                    except Exception:
                        pass

                for lbl in labels_to_clean:
                    try:
                        core_api.delete_collection_namespaced_config_map(
                            namespace="default", label_selector=lbl
                        )
                    except Exception:
                        pass

                all_dep_ids = {deployment_id} | {item[3] for item in matched_items if item[3]}
                for d_id in all_dep_ids:
                    if d_id:
                        try:
                            core_api.delete_namespaced_config_map(
                                name=f"rayservice-code-{d_id}", namespace="default"
                            )
                        except Exception:
                            pass
            except Exception:
                pass

        elif action.lower() == "restart":
            targets = set()
            if rayservice_name:
                targets.add(rayservice_name)
            for _, _, r_svc, _ in matched_items:
                if r_svc:
                    targets.add(r_svc)

            try:
                _, core_api = self._get_k8s_apis()
                for target in targets:
                    try:
                        # Deleting the head pod triggers KubeRay to recreate it and reload configuration
                        core_api.delete_collection_namespaced_pod(
                            namespace="default",
                            label_selector=f"ray.io/cluster={target},ray.io/node-type=head",
                        )
                    except Exception:
                        pass
            except Exception:
                pass

        log_audit_event(
            "deployment_management",
            user.username,
            None,
            f"Triggered action '{action}' on deployment '{deployment_id}'.",
            db=db,
        )
        return {
            "message": f"Action '{action}' executed on deployment '{deployment_id}'.",
            "deployment_id": deployment_id,
            "action_taken": action,
            "triggered_by": user.username,
        }
