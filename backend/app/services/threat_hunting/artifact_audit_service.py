import re
import logging
from typing import Optional, List, Dict, Set, Any
from sqlalchemy.orm import Session
from botocore.client import Config
import boto3

from app.core.config import settings
from app.core.logging_config import log_audit_event
from app.services.threat_hunting.event_service import event_service
from app.schemas.threat_hunting import SecurityEventCreate

logger = logging.getLogger("artifact_audit")


class ArtifactAuditService:
    """
    Reconciles S3 object storage (MinIO) against registered MLflow / PostgreSQL runs.
    Detects orphan or shadow model artifacts (e.g., untracked weights planted out-of-band
    or left behind by unvetted training runs).
    (MITRE ATLAS AML.T0010 - ML Supply Chain Compromise / MITRE ATT&CK T1584)
    """

    def get_s3_client(self):
        """Constructs an S3 client configured for the MinIO storage backend."""
        return boto3.client(
            "s3",
            endpoint_url=settings.MINIO_ENDPOINT,
            aws_access_key_id=settings.MINIO_ROOT_USER,
            aws_secret_access_key=settings.MINIO_ROOT_PASSWORD,
            config=Config(signature_version="s3v4"),
            region_name="us-east-1",
        )

    def extract_run_id_from_key(self, key: str) -> Optional[str]:
        """
        Parses MLflow artifact key structure:
        Formats:
        - <experiment_id>/<run_id>/artifacts/...
        - <run_id>/artifacts/...
        - artifacts/<run_id>/...
        """
        parts = key.strip("/").split("/")
        # Check standard MLflow pattern: <exp_id>/<run_id>/...
        if len(parts) >= 2 and len(parts[1]) in (32, 36) and re.match(r"^[a-f0-9\-]+$", parts[1]):
            return parts[1]
        # Check direct run_id prefix
        if len(parts) >= 1 and len(parts[0]) in (32, 36) and re.match(r"^[a-f0-9\-]+$", parts[0]):
            return parts[0]
        # Check artifacts/<run_id>/...
        if len(parts) >= 2 and parts[0] == "artifacts" and len(parts[1]) in (32, 36):
            return parts[1]
        return None

    def scan_storage_prefixes(self, s3_client=None, bucket_name: str = "mlflow") -> List[Dict[str, Any]]:
        """
        Scans all top-level / run-level object keys in the specified MinIO bucket.
        """
        client = s3_client or self.get_s3_client()
        detected_runs = {}

        paginator = client.get_paginator("list_objects_v2")
        try:
            for page in paginator.paginate(Bucket=bucket_name):
                for obj in page.get("Contents", []):
                    key = obj.get("Key", "")
                    run_id = self.extract_run_id_from_key(key)
                    if run_id:
                        if run_id not in detected_runs:
                            detected_runs[run_id] = {
                                "run_id": run_id,
                                "sample_key": key,
                                "total_size_bytes": 0,
                                "object_count": 0,
                            }
                        detected_runs[run_id]["total_size_bytes"] += obj.get("Size", 0)
                        detected_runs[run_id]["object_count"] += 1
        except Exception as scan_err:
            logger.warning(f"Failed to scan S3 bucket '{bucket_name}': {scan_err}")

        return list(detected_runs.values())

    def reconcile_artifacts(
        self,
        valid_run_ids: Set[str],
        storage_prefixes: List[Dict[str, Any]],
        bucket_name: str = "mlflow",
        db: Optional[Session] = None,
    ) -> Dict[str, Any]:
        """
        Compares storage run artifacts against known, valid run IDs in PostgreSQL / MLflow.
        Emits Threat Hunting events and alerts for any untracked / shadow artifacts.
        """
        orphans = []

        for item in storage_prefixes:
            run_id = item["run_id"]
            if run_id not in valid_run_ids:
                orphans.append(item)
                # 1. Log security audit event
                log_audit_event(
                    action="orphan_artifact_detected",
                    username="artifact_auditor",
                    ip_address=None,
                    details=(
                        f"Unregistered shadow artifact detected in bucket '{bucket_name}': "
                        f"run_id='{run_id}', sample_key='{item['sample_key']}', "
                        f"objects={item['object_count']}, size={item['total_size_bytes']} bytes."
                    ),
                    db=db,
                )

                # 2. Ingest structured Threat Hunting event
                if db:
                    try:
                        event_service.ingest_event(
                            db,
                            SecurityEventCreate(
                                event_type="artifact.orphan_detected",
                                severity="HIGH",
                                source="artifact_auditor",
                                actor_id="unknown_or_out_of_band",
                                resource_type="artifact",
                                resource_id=run_id,
                                description=(
                                    f"Shadow / orphan model artifact detected in S3 storage ({item['sample_key']}) "
                                    f"without matching run entry in MLflow or PostgreSQL registry."
                                ),
                                details={
                                    "bucket": bucket_name,
                                    "run_id": run_id,
                                    "sample_key": item["sample_key"],
                                    "object_count": item["object_count"],
                                    "total_size_bytes": item["total_size_bytes"],
                                    "mitre_atlas_id": "AML.T0010",
                                    "mitre_attack_id": "T1584",
                                },
                            ),
                        )

                        # Correlate immediately to generate alert
                        from app.services.threat_hunting.alert_service import alert_service
                        from app.services.threat_hunting.detection_engine import detection_engine

                        detections = detection_engine.evaluate_all_rules(db)
                        for d in detections:
                            if d.resource_id == run_id:
                                alert_service.process_detection(db, d)
                    except Exception as ev_err:
                        logger.error(f"Failed to record threat hunting event for orphan artifact: {ev_err}")

        return {
            "bucket": bucket_name,
            "total_prefixes_scanned": len(storage_prefixes),
            "valid_runs_count": len(valid_run_ids),
            "orphan_count": len(orphans),
            "orphan_artifacts": orphans,
        }


artifact_audit_service = ArtifactAuditService()
