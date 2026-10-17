import logging
import time
from typing import Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.threat_hunting import DetectionRule
from app.schemas.threat_hunting import (
    DetectionRuleCreate,
    DetectionRuleUpdate,
    DetectionRuleTestResponse,
    RuleConfigThreshold,
)

logger = logging.getLogger("threat_hunting.rules")


DEFAULT_RULES = [
    {
        "name": "Repeated Authentication Failures (Brute Force)",
        "description": "Detects multiple consecutive authentication failures for a single actor/account within a short time window.",
        "rule_type": "threshold",
        "severity": "HIGH",
        "is_enabled": True,
        "mitre_attack_id": "T1110.001",
        "mitre_atlas_id": None,
        "config": {
            "event_type": "auth.login_failed",
            "threshold": 5,
            "window_seconds": 300,
            "group_by": "actor_id",
            "match_attributes": {},
        },
    },
    {
        "name": "Excessive Unauthorized Requests (Privilege Abuse / Enumeration)",
        "description": "Detects repeated 401/403 forbidden or unauthorized requests indicating privilege enumeration or token tampering.",
        "rule_type": "threshold",
        "severity": "MEDIUM",
        "is_enabled": True,
        "mitre_attack_id": "T1078",
        "mitre_atlas_id": None,
        "config": {
            "event_type": "auth.unauthorized_access",
            "threshold": 10,
            "window_seconds": 300,
            "group_by": "actor_id",
            "match_attributes": {},
        },
    },
    {
        "name": "Revoked Token Reuse Attempt",
        "description": "Detects any attempt to authenticate using a blacklisted or revoked JWT token.",
        "rule_type": "threshold",
        "severity": "HIGH",
        "is_enabled": True,
        "mitre_attack_id": "T1550",
        "mitre_atlas_id": None,
        "config": {
            "event_type": "auth.token_blacklisted_used",
            "threshold": 1,
            "window_seconds": 600,
            "group_by": "actor_id",
            "match_attributes": {},
        },
    },
    {
        "name": "Anomalous Inference Request Rate (Model Inversion / Extraction)",
        "description": "Detects an unexpected burst of inference predictions against a deployed model exceeding standard baseline (50 requests/min).",
        "rule_type": "threshold",
        "severity": "MEDIUM",
        "is_enabled": True,
        "mitre_attack_id": None,
        "mitre_atlas_id": "AML.T0024",
        "config": {
            "event_type": "inference.request_burst",
            "threshold": 1,
            "window_seconds": 60,
            "group_by": "resource_id",
            "match_attributes": {},
        },
    },
    {
        "name": "Unauthorized Dataset Branch Mutation",
        "description": "Detects unauthorized deletion or forced commit modification on a lakeFS dataset repository.",
        "rule_type": "threshold",
        "severity": "HIGH",
        "is_enabled": True,
        "mitre_attack_id": "T1485",
        "mitre_atlas_id": "AML.T0031",
        "config": {
            "event_type": "dataset.branch_deleted",
            "threshold": 1,
            "window_seconds": 300,
            "group_by": "resource_id",
            "match_attributes": {},
        },
    },
    {
        "name": "Unexpected RayJob Submission Spikes",
        "description": "Detects rapid repeated RayJob submissions that may indicate compute hijacking or runaway training loops.",
        "rule_type": "threshold",
        "severity": "MEDIUM",
        "is_enabled": True,
        "mitre_attack_id": "T1496",
        "mitre_atlas_id": "AML.T0002",
        "config": {
            "event_type": "ml.rayjob_submitted",
            "threshold": 5,
            "window_seconds": 300,
            "group_by": "actor_id",
            "match_attributes": {},
        },
    },
    {
        "name": "Statistical Data Poisoning on Dataset Merge",
        "description": "Detects statistical distribution shifts and suspected data poisoning attempts blocked during lakeFS pre-merge validation.",
        "rule_type": "threshold",
        "severity": "HIGH",
        "is_enabled": True,
        "mitre_attack_id": "T1565.001",
        "mitre_atlas_id": "AML.T0020",
        "config": {
            "event_type": "dataset.poisoning_suspected",
            "threshold": 1,
            "window_seconds": 300,
            "group_by": "resource_id",
            "match_attributes": {},
        },
    },
    {
        "name": "Pickle Deserialization RCE Payload Detected",
        "description": "Detects arbitrary shellcode, remote code execution payloads, or dangerous opcodes (GLOBAL, REDUCE pointing to os, sys, posix, subprocess, socket) discovered during static AST disassembly of model files.",
        "rule_type": "threshold",
        "severity": "CRITICAL",
        "is_enabled": True,
        "mitre_attack_id": "T1203",
        "mitre_atlas_id": "AML.T0025",
        "config": {
            "event_type": "model.rce_payload_detected",
            "threshold": 1,
            "window_seconds": 300,
            "group_by": "resource_id",
            "match_attributes": {},
        },
    },
    {
        "name": "Algorithmic Denial of Service (Tabular Memory Flooding)",
        "description": "Detects massive tabular matrix payloads (exceeding 10^5 cells) designed to trigger Out-Of-Memory (OOM) crashes on serving replicas.",
        "rule_type": "threshold",
        "severity": "HIGH",
        "is_enabled": True,
        "mitre_attack_id": "T1499",
        "mitre_atlas_id": "AML.T0029",
        "config": {
            "event_type": "inference.payload_shape_excessive",
            "threshold": 1,
            "window_seconds": 300,
            "group_by": "resource_id",
            "match_attributes": {},
        },
    },
    {
        "name": "Model Inversion & Decision-Boundary Scraping",
        "description": "Detects high-density inference querying clustered tightly around model classification decision thresholds (0.48 <= P <= 0.52).",
        "rule_type": "threshold",
        "severity": "HIGH",
        "is_enabled": True,
        "mitre_attack_id": None,
        "mitre_atlas_id": "AML.T0024",
        "config": {
            "event_type": "inference.decision_boundary_scraping",
            "threshold": 1,
            "window_seconds": 300,
            "group_by": "resource_id",
            "match_attributes": {},
        },
    },
    {
        "name": "Systematic Feature Perturbation (Black-box Extraction)",
        "description": "Detects sequential inference queries where exactly one continuous feature is nudged incrementally (L0=1) to estimate gradients or map boundaries.",
        "rule_type": "threshold",
        "severity": "HIGH",
        "is_enabled": True,
        "mitre_attack_id": None,
        "mitre_atlas_id": "AML.T0040",
        "config": {
            "event_type": "inference.systematic_perturbation",
            "threshold": 1,
            "window_seconds": 300,
            "group_by": "resource_id",
            "match_attributes": {},
        },
    },
    {
        "name": "Legacy Pickle Serialization Promoted to Production",
        "description": "Detects attempts to promote or deploy legacy serialized pickle formats (.pkl) to Production. Safe weight formats (Safetensors / ONNX) must be enforced to prevent RCE.",
        "rule_type": "threshold",
        "severity": "HIGH",
        "is_enabled": True,
        "mitre_attack_id": "T1203",
        "mitre_atlas_id": "AML.T0025",
        "config": {
            "event_type": "model.unsafe_format_promoted",
            "threshold": 1,
            "window_seconds": 300,
            "group_by": "resource_id",
            "match_attributes": {},
        },
    },
    {
        "name": "Unsigned or Invalidly Attested Model Promoted to Production",
        "description": "Detects deployment or promotion of models lacking valid Sigstore/Cosign attestation or cryptographic hash verification to Staging or Production.",
        "rule_type": "threshold",
        "severity": "HIGH",
        "is_enabled": True,
        "mitre_attack_id": "T1584",
        "mitre_atlas_id": "AML.T0010",
        "config": {
            "event_type": "model.unattested_promotion",
            "threshold": 1,
            "window_seconds": 300,
            "group_by": "resource_id",
            "match_attributes": {},
        },
    },
    {
        "name": "Model Artifact Integrity Digest Mismatch (Tampering)",
        "description": "Detects cryptographic SHA-256 digest discrepancies between registered model attestation and deployed artifact, indicating post-registration supply chain tampering.",
        "rule_type": "threshold",
        "severity": "CRITICAL",
        "is_enabled": True,
        "mitre_attack_id": "T1584",
        "mitre_atlas_id": "AML.T0010",
        "config": {
            "event_type": "model.tampering_detected",
            "threshold": 1,
            "window_seconds": 300,
            "group_by": "resource_id",
            "match_attributes": {},
        },
    },
    {
        "name": "Container Runtime Unauthorized Process Execution (Falco eBPF)",
        "description": "Detects unauthorized shells (/bin/sh, bash) or exfiltration utilities (curl, wget, nc) spawned inside ML training or serving container workloads.",
        "rule_type": "threshold",
        "severity": "CRITICAL",
        "is_enabled": True,
        "mitre_attack_id": "T1059.004",
        "mitre_atlas_id": "AML.T0038",
        "config": {
            "event_type": "runtime.container_unauthorized_execution",
            "threshold": 1,
            "window_seconds": 300,
            "group_by": "resource_id",
            "match_attributes": {},
        },
    },
    {
        "name": "Unregistered Shadow Model Artifact Detected in S3 Storage",
        "description": "Detects orphaned or shadow model artifact directories in S3/MinIO storage that lack corresponding valid experiment runs in MLflow or PostgreSQL.",
        "rule_type": "threshold",
        "severity": "HIGH",
        "is_enabled": True,
        "mitre_attack_id": "T1584",
        "mitre_atlas_id": "AML.T0010",
        "config": {
            "event_type": "artifact.orphan_detected",
            "threshold": 1,
            "window_seconds": 300,
            "group_by": "resource_id",
            "match_attributes": {},
        },
    },
    {
        "name": "Unregistered Rogue RayServe Deployment Detected (Direct Injection)",
        "description": "Detects active RayService CRDs or RayServe controller serving deployments running in the cluster that do not correspond to any registered deployment or approved model in MLflow.",
        "rule_type": "threshold",
        "severity": "CRITICAL",
        "is_enabled": True,
        "mitre_attack_id": "T1584",
        "mitre_atlas_id": "AML.T0038",
        "config": {
            "event_type": "rayserve.rogue_deployment_detected",
            "threshold": 1,
            "window_seconds": 300,
            "group_by": "resource_id",
            "match_attributes": {},
        },
    },
    {
        "name": "Model Behavioral Drift: Abrupt Confidence Degradation",
        "description": "Detects sudden collapse in model prediction confidence distributions, indicating data poisoning, distribution shift, or adversarial evasion attacks.",
        "rule_type": "threshold",
        "severity": "HIGH",
        "is_enabled": True,
        "mitre_attack_id": None,
        "mitre_atlas_id": "AML.T0024",
        "config": {
            "event_type": "inference.confidence_drift_detected",
            "threshold": 1,
            "window_seconds": 300,
            "group_by": "resource_id",
            "match_attributes": {},
        },
    },
    {
        "name": "Algorithmic Denial of Service: Inference Latency Spike Anomaly",
        "description": "Detects sudden extreme inference latency spikes occurring under normal request volumes, characteristic of algorithmic exhaustion attacks forcing worst-case computation.",
        "rule_type": "threshold",
        "severity": "HIGH",
        "is_enabled": True,
        "mitre_attack_id": "T1499",
        "mitre_atlas_id": "AML.T0029",
        "config": {
            "event_type": "inference.latency_spike_anomaly",
            "threshold": 1,
            "window_seconds": 300,
            "group_by": "resource_id",
            "match_attributes": {},
        },
    },
]


class DetectionRuleService:
    """
    Manages detection rules lifecycle, validation, versioning, and testing.
    """

    def create_rule(
        self,
        db: Session,
        rule_in: DetectionRuleCreate,
        creator: Optional[str] = None,
    ) -> DetectionRule:
        """
        Validates configuration and creates a new detection rule.
        """
        # Validate config structure strictly
        RuleConfigThreshold(**rule_in.config)

        rule = DetectionRule(
            name=rule_in.name,
            description=rule_in.description,
            rule_type=rule_in.rule_type,
            is_enabled=rule_in.is_enabled,
            severity=rule_in.severity.upper(),
            config=rule_in.config,
            mitre_attack_id=rule_in.mitre_attack_id,
            mitre_atlas_id=rule_in.mitre_atlas_id,
            version=1,
            created_by=creator,
        )
        db.add(rule)
        db.commit()
        db.refresh(rule)
        return rule

    def update_rule(
        self,
        db: Session,
        rule_id: UUID,
        rule_update: DetectionRuleUpdate,
    ) -> DetectionRule:
        """
        Updates rule metadata and increments version if configuration or severity changes.
        """
        rule = self.get_rule(db, rule_id)
        if not rule:
            raise ValueError(f"Detection rule with id {rule_id} not found.")

        config_changed = False
        if rule_update.config is not None and rule_update.config != rule.config:
            RuleConfigThreshold(**rule_update.config)
            rule.config = rule_update.config
            config_changed = True

        if rule_update.name is not None:
            rule.name = rule_update.name
        if rule_update.description is not None:
            rule.description = rule_update.description
        if rule_update.is_enabled is not None:
            rule.is_enabled = rule_update.is_enabled
        if rule_update.severity is not None:
            if rule_update.severity.upper() != rule.severity:
                config_changed = True
            rule.severity = rule_update.severity.upper()
        if rule_update.mitre_attack_id is not None:
            rule.mitre_attack_id = rule_update.mitre_attack_id
        if rule_update.mitre_atlas_id is not None:
            rule.mitre_atlas_id = rule_update.mitre_atlas_id

        if config_changed:
            rule.version += 1

        db.commit()
        db.refresh(rule)
        return rule

    def get_rule(self, db: Session, rule_id: UUID) -> Optional[DetectionRule]:
        return db.get(DetectionRule, rule_id)

    def list_rules(
        self,
        db: Session,
        is_enabled: Optional[bool] = None,
    ) -> list[DetectionRule]:
        query = select(DetectionRule)
        if is_enabled is not None:
            query = query.where(DetectionRule.is_enabled == is_enabled)
        query = query.order_by(DetectionRule.name.asc())
        return list(db.execute(query).scalars().all())

    def seed_default_rules(self, db: Session) -> int:
        """
        Idempotently seeds canonical default rules if not already present.
        """
        created = 0
        for r_data in DEFAULT_RULES:
            existing = db.execute(
                select(DetectionRule).where(DetectionRule.name == r_data["name"])
            ).scalar_one_or_none()
            if not existing:
                rule = DetectionRule(
                    name=r_data["name"],
                    description=r_data["description"],
                    rule_type=r_data["rule_type"],
                    severity=r_data["severity"],
                    is_enabled=r_data["is_enabled"],
                    mitre_attack_id=r_data["mitre_attack_id"],
                    mitre_atlas_id=r_data["mitre_atlas_id"],
                    config=r_data["config"],
                    version=1,
                    created_by="system",
                )
                db.add(rule)
                created += 1
            elif existing.created_by == "system":
                # Ensure canonical system rules stay synchronized with default configs
                if existing.config != r_data["config"] or existing.description != r_data["description"]:
                    existing.config = r_data["config"]
                    existing.description = r_data["description"]
                    existing.version += 1
                    created += 1
        if created > 0:
            db.commit()
            logger.info(f"Seeded or updated {created} default detection rules.")
        return created

    def test_rule(
        self,
        db: Session,
        rule_id: UUID,
        window_seconds: Optional[int] = None,
    ) -> DetectionRuleTestResponse:
        """
        Tests a rule against recent historical security events without persisting alerts.
        """
        from app.services.threat_hunting.detection_engine import detection_engine

        rule = self.get_rule(db, rule_id)
        if not rule:
            raise ValueError(f"Detection rule with id {rule_id} not found.")

        t0 = time.time()
        win = window_seconds or rule.config.get("window_seconds", 300)
        detections = detection_engine.evaluate_single_rule(db, rule, window_seconds_override=win)
        duration_ms = round((time.time() - t0) * 1000, 2)

        candidate_alerts = [
            {
                "title": d.title,
                "severity": d.severity,
                "event_count": len(d.event_ids),
                "actor_id": d.actor_id,
                "resource_id": d.resource_id,
                "first_seen": d.first_seen.isoformat(),
                "last_seen": d.last_seen.isoformat(),
                "evidence": d.supporting_evidence,
            }
            for d in detections
        ]

        return DetectionRuleTestResponse(
            rule_id=rule.id,
            matches_found=len(candidate_alerts),
            candidate_alerts=candidate_alerts,
            evaluation_window_seconds=win,
            duration_ms=duration_ms,
        )


rule_service = DetectionRuleService()
