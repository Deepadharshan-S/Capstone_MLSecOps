import logging
from typing import Optional, List, Dict, Set, Any
from sqlalchemy.orm import Session

from app.core.logging_config import log_audit_event
from app.services.threat_hunting.event_service import event_service
from app.schemas.threat_hunting import SecurityEventCreate

logger = logging.getLogger("rayserve_monitor")


class RayServeSecurityMonitor:
    """
    Monitors and audits active RayService deployments on Kubernetes / RayServe controller.
    Detects unapproved, rogue, or out-of-band injected model deployments that bypass
    the SentinelML API or serve unsigned/tampered models.
    (MITRE ATLAS AML.T0038 - Execution via Workload Container / MITRE ATT&CK T1584)
    """

    def scan_live_rayservices(self, k8s_custom_api=None, namespace: str = "default") -> List[Dict[str, Any]]:
        """
        Queries Kubernetes custom object API for all active RayService CRDs in the namespace.
        """
        results = []
        if k8s_custom_api is None:
            try:
                from kubernetes import client, config
                try:
                    config.load_incluster_config()
                except Exception:
                    config.load_kube_config()
                k8s_custom_api = client.CustomObjectsApi()
            except Exception as k8s_init_err:
                logger.debug(f"Could not initialize Kubernetes SDK for RayService scan: {k8s_init_err}")
                return results

        try:
            resp = k8s_custom_api.list_namespaced_custom_object(
                group="ray.io",
                version="v1",
                namespace=namespace,
                plural="rayservices",
            )
            for item in resp.get("items", []):
                metadata = item.get("metadata", {})
                spec = item.get("spec", {})
                name = metadata.get("name", "unknown")
                labels = metadata.get("labels", {})
                annotations = metadata.get("annotations", {})

                # Extract model_uri or serve config if present
                model_uri = None
                serve_cfg = spec.get("serveConfigV2", "")
                if "models:/" in serve_cfg:
                    for line in serve_cfg.splitlines():
                        if "models:/" in line:
                            model_uri = line.strip().split("models:/")[-1].strip("\"'")

                results.append({
                    "rayservice_name": name,
                    "namespace": metadata.get("namespace", namespace),
                    "created_at": metadata.get("creationTimestamp"),
                    "labels": labels,
                    "model_uri": model_uri,
                    "model_name_label": labels.get("app.kubernetes.io/name") or labels.get("model_name"),
                    "deployment_id": labels.get("deployment_id"),
                })
        except Exception as scan_err:
            logger.warning(f"Error scanning live RayServices: {scan_err}")

        return results

    def reconcile_deployments(
        self,
        live_rayservices: List[Dict[str, Any]],
        authorized_rayservice_names: Set[str],
        authorized_model_names: Optional[Set[str]] = None,
        db: Optional[Session] = None,
    ) -> Dict[str, Any]:
        """
        Reconciles live running RayServices against authorized SentinelML PostgreSQL deployments
        and approved models. Flags rogue serving endpoints injected out-of-band.
        """
        authorized_models = authorized_model_names or set()
        rogues = []

        for svc in live_rayservices:
            name = svc["rayservice_name"]
            dep_id = svc.get("deployment_id")
            model_label = svc.get("model_name_label")

            # Check if this service is registered in PostgreSQL
            is_authorized = (
                name in authorized_rayservice_names
                or (dep_id and dep_id in authorized_rayservice_names)
                or (model_label and model_label in authorized_models)
            )

            if not is_authorized:
                rogues.append(svc)

                # 1. Log security audit event
                log_audit_event(
                    action="rayserve_rogue_deployment_detected",
                    username="rayserve_monitor",
                    ip_address=None,
                    details=(
                        f"Rogue RayServe deployment detected on Kubernetes: '{name}' "
                        f"(namespace='{svc.get('namespace')}', model_uri='{svc.get('model_uri')}'). "
                        f"No matching registered deployment found in PostgreSQL."
                    ),
                    db=db,
                )

                # 2. Ingest structured Threat Hunting event
                if db:
                    try:
                        sec_event = event_service.ingest_event(
                            db,
                            SecurityEventCreate(
                                event_type="rayserve.rogue_deployment_detected",
                                severity="CRITICAL",
                                source="rayserve_monitor",
                                actor_id="unauthorized_cluster_actor",
                                resource_type="rayservice",
                                resource_id=name,
                                description=(
                                    f"Direct RayServe deployment injection detected: RayService '{name}' "
                                    f"is actively running on the cluster without an authorized registry record."
                                ),
                                details={
                                    "rayservice_name": name,
                                    "namespace": svc.get("namespace"),
                                    "model_uri": svc.get("model_uri"),
                                    "created_at": svc.get("created_at"),
                                    "mitre_atlas_id": "AML.T0038",
                                    "mitre_attack_id": "T1584",
                                },
                            ),
                        )

                        # Correlate immediately to generate alert
                        from app.services.threat_hunting.alert_service import alert_service
                        from app.services.threat_hunting.detection_engine import detection_engine

                        detections = detection_engine.evaluate_all_rules(db)
                        for d in detections:
                            if d.resource_id == name:
                                alert_service.process_detection(db, d)
                    except Exception as ev_err:
                        logger.error(f"Failed to record threat hunting event for rogue RayService: {ev_err}")

        return {
            "total_scanned": len(live_rayservices),
            "authorized_count": len(authorized_rayservice_names),
            "rogue_count": len(rogues),
            "rogue_deployments": rogues,
        }


rayserve_security_monitor = RayServeSecurityMonitor()
