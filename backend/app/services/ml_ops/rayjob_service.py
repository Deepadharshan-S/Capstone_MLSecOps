import logging
from datetime import datetime
from typing import Optional, Any
from app.services.ml_ops.utils import submit_rayjob_to_k8s

logger = logging.getLogger("rayjob_service")


class RayJobService:
    """
    Encapsulates all interactions with Kubernetes and KubeRay custom resources.
    Hides raw Kubernetes SDK details from business services.
    """

    def __init__(
        self,
        custom_api: Optional[Any] = None,
        core_api: Optional[Any] = None,
    ) -> None:
        self._custom_api = custom_api
        self._core_api = core_api

    def _get_apis(self):
        """Lazy-initialize Kubernetes client APIs if not injected."""
        if self._custom_api is not None and self._core_api is not None:
            return self._custom_api, self._core_api

        from app.services.ml_ops.training_service import ModelTrainingService
        try:
            return ModelTrainingService()._get_k8s_apis()
        except Exception as e:
            logger.debug(f"ModelTrainingService K8s API init fallback: {e}")

        from kubernetes import client, config
        try:
            config.load_incluster_config()
        except Exception as in_cluster_err:
            logger.debug(f"Could not load in-cluster K8s config, attempting kube_config: {in_cluster_err}")
            try:
                config.load_kube_config()
            except Exception as kube_cfg_err:
                logger.warning(f"Could not load local kube config: {kube_cfg_err}")

        custom_api = client.CustomObjectsApi()
        core_api = client.CoreV1Api()
        return custom_api, core_api

    @staticmethod
    def normalize_status(job_status: Optional[str], dep_status: Optional[str]) -> str:
        """
        Normalizes KubeRay status fields into standard SentinelML statuses:
        PENDING, RUNNING, SUCCEEDED, FAILED.
        """
        if not job_status and not dep_status:
            return "PENDING"

        j_st = (job_status or "").upper()
        d_st = (dep_status or "").upper()

        if "SUCCEEDED" in j_st or "COMPLETE" in j_st:
            return "SUCCEEDED"
        if "FAILED" in j_st or "ERROR" in j_st:
            return "FAILED"
        if "RUNNING" in j_st or "RUNNING" in d_st:
            return "RUNNING"
        if "INITIALIZING" in d_st or "PENDING" in j_st or "WAITING" in d_st:
            return "PENDING"

        return "PENDING"

    @staticmethod
    def parse_k8s_time(val: Optional[str]) -> Optional[datetime]:
        """Parses Kubernetes ISO timestamps into UTC datetimes."""
        if not val:
            return None
        try:
            return datetime.fromisoformat(val.replace("Z", "+00:00"))
        except Exception:
            return None

    def list_rayjobs(self, namespace: str = "default") -> list[dict]:
        """
        Fetches all RayJob custom resources in the namespace in a single batch query.
        Returns empty list if Kubernetes cluster is offline or unreachable.
        """
        try:
            custom_api, _ = self._get_apis()
            resp = custom_api.list_namespaced_custom_object(
                group="ray.io",
                version="v1",
                namespace=namespace,
                plural="rayjobs",
            )
            return resp.get("items", [])
        except Exception as e:
            print(f"Notice: Could not list RayJobs from Kubernetes ({e}).")
            return []

    def get_rayjob(self, rayjob_name: str, namespace: str = "default") -> Optional[dict]:
        """
        Fetches a single RayJob custom resource by name.
        Returns None if not found or if cluster is unreachable.
        """
        try:
            custom_api, _ = self._get_apis()
            return custom_api.get_namespaced_custom_object(
                group="ray.io",
                version="v1",
                namespace=namespace,
                plural="rayjobs",
                name=rayjob_name,
            )
        except Exception as e:
            print(f"Notice: Could not get RayJob '{rayjob_name}' from Kubernetes ({e}).")
            return None

    def get_head_pod(self, job_id: str, namespace: str = "default") -> Optional[tuple[str, str]]:
        """
        Finds the Ray head pod corresponding to a given job_id.
        Returns (pod_name, pod_phase) or None if not found.
        """
        try:
            _, core_api = self._get_apis()
            clean_id = job_id.removeprefix("rayjob-")
            pods = core_api.list_namespaced_pod(
                namespace=namespace,
                label_selector=f"ray.io/job-id={clean_id}",
            )
            if (
                hasattr(pods, "items")
                and isinstance(pods.items, (list, tuple))
                and len(pods.items) > 0
            ):
                pod = pods.items[0]
                name = getattr(getattr(pod, "metadata", None), "name", None)
                phase = getattr(getattr(pod, "status", None), "phase", None)
                if isinstance(name, str):
                    return name, str(phase) if phase is not None else "Unknown"
            return None
        except Exception as e:
            print(f"Notice: Could not inspect head pod for job {job_id} ({e}).")
            return None

    def get_head_pod_logs(
        self,
        pod_name: str,
        namespace: str = "default",
        tail_lines: Optional[int] = None,
        container: str = "ray-head",
    ) -> Optional[str]:
        """
        Reads live logs from the specified pod container via CoreV1Api.
        Returns log content string or None on failure.
        """
        try:
            _, core_api = self._get_apis()
            kwargs = {"name": pod_name, "namespace": namespace, "container": container}
            if tail_lines:
                kwargs["tail_lines"] = tail_lines
            return core_api.read_namespaced_pod_log(**kwargs)
        except Exception as e:
            print(f"Notice: Could not read pod logs from {pod_name} ({e}).")
            return None

    def submit_rayjob(
        self,
        rendered_yaml: str,
        job_id: str,
        username: str,
        mode_description: str = "custom code",
    ) -> bool:
        """Submits the rendered RayJob manifest to the Kubernetes cluster."""
        return submit_rayjob_to_k8s(
            rendered_yaml=rendered_yaml,
            job_id=job_id,
            username=username,
            mode_description=mode_description,
        )

    def cleanup_job_resources(self, job_id: str, namespace: str = "default") -> None:
        """
        Comprehensively cleans up all Kubernetes and local resources associated with a completed RayJob:
        - ConfigMap (rayjob-code-{job_id} and labeled collection)
        - NetworkPolicy (rayjob-netpol-{job_id})
        - RayJob custom resource
        - Associated RayCluster and pods
        - Associated services
        - Local temporary result files
        """
        import os
        import tempfile
        clean_id = job_id.removeprefix("rayjob-")
        rayjob_name = f"rayjob-{clean_id}"

        # 1. Delete ConfigMap(s)
        try:
            _, core_api = self._get_apis()
            core_api.delete_namespaced_config_map(
                name=f"rayjob-code-{clean_id}", namespace=namespace
            )
        except Exception as cm_err:
            logger.debug(f"ConfigMap rayjob-code-{clean_id} deletion note: {cm_err}")

        try:
            _, core_api = self._get_apis()
            core_api.delete_collection_namespaced_config_map(
                namespace=namespace, label_selector=f"ray.io/job-id={clean_id}"
            )
        except Exception as cm_lbl_err:
            logger.debug(f"ConfigMap collection cleanup note ({clean_id}): {cm_lbl_err}")

        # 2. Delete NetworkPolicy
        try:
            from kubernetes import client
            net_api = client.NetworkingV1Api()
            net_api.delete_namespaced_network_policy(
                name=f"rayjob-netpol-{clean_id}", namespace=namespace
            )
        except Exception as np_err:
            logger.debug(f"NetworkPolicy rayjob-netpol-{clean_id} deletion note: {np_err}")

        # 3. Delete RayJob Custom Resource
        try:
            custom_api, _ = self._get_apis()
            custom_api.delete_namespaced_custom_object(
                group="ray.io",
                version="v1",
                namespace=namespace,
                plural="rayjobs",
                name=rayjob_name,
            )
        except Exception as rj_err:
            logger.debug(f"RayJob {rayjob_name} deletion note: {rj_err}")

        # 4. Delete RayCluster Custom Resource (if any)
        try:
            custom_api, _ = self._get_apis()
            clusters = custom_api.list_namespaced_custom_object(
                group="ray.io",
                version="v1",
                namespace=namespace,
                plural="rayclusters",
                label_selector=f"ray.io/job-id={clean_id}",
            )
            for item in clusters.get("items", []):
                c_name = item.get("metadata", {}).get("name")
                if c_name:
                    custom_api.delete_namespaced_custom_object(
                        group="ray.io", version="v1", namespace=namespace, plural="rayclusters", name=c_name
                    )
        except Exception as rc_err:
            logger.debug(f"RayCluster cleanup note for {clean_id}: {rc_err}")

        # 5. Clean up any leftover pods/services for this job
        try:
            _, core_api = self._get_apis()
            core_api.delete_collection_namespaced_pod(
                namespace=namespace, label_selector=f"ray.io/job-id={clean_id}"
            )
        except Exception as pod_err:
            logger.debug(f"Pod cleanup note for {clean_id}: {pod_err}")

        try:
            _, core_api = self._get_apis()
            svcs = core_api.list_namespaced_service(
                namespace=namespace, label_selector=f"ray.io/job-id={clean_id}"
            )
            for s in svcs.items:
                core_api.delete_namespaced_service(name=s.metadata.name, namespace=namespace)
        except Exception as svc_err:
            logger.debug(f"Service cleanup note for {clean_id}: {svc_err}")

        # 6. Clean up temporary local files
        try:
            for sub in ["sentinelml_results", "ray_results"]:
                f_path = os.path.join(tempfile.gettempdir(), sub, f"{clean_id}.json")
                if os.path.exists(f_path):
                    os.remove(f_path)
        except Exception as tmp_err:
            logger.debug(f"Temp file cleanup note for {clean_id}: {tmp_err}")

    def sweep_orphaned_job_resources(self, namespace: str = "default") -> dict:
        """
        Sweeps and garbage-collects completed RayJobs, orphaned ConfigMaps,
        and NetworkPolicies from past training jobs.
        """
        cleaned_jobs = []
        cleaned_cms = []

        try:
            custom_api, core_api = self._get_apis()

            # 1. Sweep completed/failed RayJobs
            rayjobs = custom_api.list_namespaced_custom_object(
                group="ray.io", version="v1", namespace=namespace, plural="rayjobs"
            ).get("items", [])

            for rj in rayjobs:
                name = rj.get("metadata", {}).get("name", "")
                st = rj.get("status", {})
                job_status = (st.get("jobStatus") or "").upper()
                dep_status = (st.get("jobDeploymentStatus") or "").upper()

                if job_status in ("SUCCEEDED", "FAILED", "STOPPED") or dep_status in ("COMPLETE", "FAILED"):
                    job_id = rj.get("metadata", {}).get("labels", {}).get("ray.io/job-id") or name.removeprefix("rayjob-")
                    self.cleanup_job_resources(job_id, namespace=namespace)
                    cleaned_jobs.append(name)

            # 2. Sweep orphaned ConfigMaps
            cms = core_api.list_namespaced_config_map(namespace=namespace).items
            active_rayjobs = {
                rj.get("metadata", {}).get("name", "").removeprefix("rayjob-")
                for rj in custom_api.list_namespaced_custom_object(
                    group="ray.io", version="v1", namespace=namespace, plural="rayjobs"
                ).get("items", [])
            }

            for cm in cms:
                cm_name = cm.metadata.name
                if cm_name.startswith("rayjob-code-"):
                    cm_job_id = cm_name.removeprefix("rayjob-code-")
                    if cm_job_id not in active_rayjobs:
                        try:
                            core_api.delete_namespaced_config_map(name=cm_name, namespace=namespace)
                            cleaned_cms.append(cm_name)
                        except Exception as del_cm_err:
                            logger.debug(f"Orphan ConfigMap {cm_name} delete note: {del_cm_err}")

        except Exception as sweep_err:
            logger.debug(f"Resource sweep note: {sweep_err}")

        return {"cleaned_jobs": cleaned_jobs, "cleaned_configmaps": cleaned_cms}

