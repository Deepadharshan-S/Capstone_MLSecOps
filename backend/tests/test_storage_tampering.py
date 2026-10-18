import time
import uuid
import pytest
from unittest.mock import patch
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import settings
from app.db.session import SessionLocal
from app.main import app
from app.models.dataset import Dataset
from app.models.user import User
from app.models.threat_hunting import Alert, DetectionRule, SecurityEvent
from app.services.threat_hunting.rule_service import rule_service
from app.services.threat_hunting.storage_detector import (
    StorageMutationDetector,
    storage_mutation_detector,
    CLASSIFICATION_AUTHORIZED,
    CLASSIFICATION_SUSPICIOUS,
    CLASSIFICATION_UNVERIFIED,
    CLASSIFICATION_IGNORED,
)

client = TestClient(app)


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        rule_service.seed_default_rules(session)
        yield session
    finally:
        session.close()


@pytest.fixture
def minio_headers():
    return {"X-MinIO-Webhook-Secret": settings.MINIO_WEBHOOK_SECRET}


@pytest.fixture
def bearer_headers():
    return {"Authorization": f"Bearer {settings.MINIO_WEBHOOK_SECRET}"}


@pytest.fixture
def test_dataset(db):
    """Creates a sample registered dataset in DB for storage namespace correlation."""
    unique_id = uuid.uuid4().hex[:8]
    ds_name = f"test-tamper-{unique_id}"
    
    # Get or create admin user for FK
    admin_user = db.execute(select(User).where(User.username == "admin_user")).scalar_one_or_none()
    if not admin_user:
        admin_user = User(
            username=f"admin_u_{unique_id}",
            email=f"admin_{unique_id}@sentinelml.com",
            password_hash="hashed_pw",
            role="admin",
            is_active=True,
        )
        db.add(admin_user)
        db.commit()
        db.refresh(admin_user)

    ds = Dataset(
        name=ds_name,
        description="Dataset for testing storage mutation detection",
        storage_namespace=f"s3://lakefs/{ds_name}",
        default_branch="main",
        created_by_id=admin_user.id,
    )
    db.add(ds)
    db.commit()
    db.refresh(ds)
    yield ds

    # Cleanup
    try:
        db.delete(ds)
        db.commit()
    except Exception:
        pass


# ===============================================================================
# 1. Webhook Authentication & Payload Parsing (SEC-01)
# ===============================================================================

def test_p1_webhook_valid_header_authentication(minio_headers, bearer_headers):
    """P1.1: Webhook accepts valid X-MinIO-Webhook-Secret and Authorization Bearer headers."""
    payload = {
        "Records": [
            {
                "eventName": "s3:ObjectCreated:Put",
                "s3": {
                    "bucket": {"name": "lakefs"},
                    "object": {"key": "test-repo/data.csv", "size": 100},
                },
                "userIdentity": {"principalId": "minioadmin"},
            }
        ]
    }
    # 1. Via X-MinIO-Webhook-Secret
    resp1 = client.post("/api/webhooks/minio/storage-events", json=payload, headers=minio_headers)
    assert resp1.status_code == 200
    assert resp1.json()["status"] == "processed"

    # 2. Via Bearer
    resp2 = client.post("/api/webhooks/minio/storage-events", json=payload, headers=bearer_headers)
    assert resp2.status_code == 200
    assert resp2.json()["status"] == "processed"


def test_p1_webhook_missing_or_invalid_auth_returns_401():
    """P1.3: Webhook rejects missing or invalid authentication with 401 Unauthorized."""
    payload = {
        "Records": [
            {
                "eventName": "s3:ObjectCreated:Put",
                "s3": {
                    "bucket": {"name": "lakefs"},
                    "object": {"key": "test-repo/data.csv"},
                },
            }
        ]
    }
    # Missing auth
    resp_missing = client.post("/api/webhooks/minio/storage-events", json=payload)
    assert resp_missing.status_code == 401
    assert resp_missing.json()["reason"] == "invalid_or_missing_webhook_secret"

    # Invalid secret
    resp_invalid = client.post(
        "/api/webhooks/minio/storage-events",
        json=payload,
        headers={"X-MinIO-Webhook-Secret": "completely_wrong_secret"},
    )
    assert resp_invalid.status_code == 401
    assert resp_invalid.json()["reason"] == "invalid_or_missing_webhook_secret"


def test_p1_webhook_rejects_query_param_auth(minio_headers):
    """P1.3: Secrets in query parameters must not be accepted."""
    payload = {"Records": []}
    resp = client.post(
        f"/api/webhooks/minio/storage-events?secret={settings.MINIO_WEBHOOK_SECRET}",
        json=payload,
    )
    assert resp.status_code == 401


def test_p1_webhook_malformed_payload_and_missing_optional_fields(minio_headers):
    """P1.4: Handles empty or partial payloads safely without crashing."""
    # Completely empty body
    resp_empty = client.post("/api/webhooks/minio/storage-events", json={}, headers=minio_headers)
    assert resp_empty.status_code == 200
    assert resp_empty.json()["status"] == "skipped"

    # Missing optional fields in record (no userIdentity, no size, no etag)
    payload_minimal = {
        "Records": [
            {
                "eventName": "s3:ObjectCreated:Put",
                "s3": {
                    "bucket": {"name": "lakefs"},
                    "object": {"key": "minimal/path"},
                },
            }
        ]
    }
    resp_min = client.post("/api/webhooks/minio/storage-events", json=payload_minimal, headers=minio_headers)
    assert resp_min.status_code == 200
    assert resp_min.json()["records_evaluated"] == 1


def test_p1_webhook_multiple_records_processed(minio_headers):
    """P1.5: Safely processes multiple records in one notification payload."""
    payload = {
        "Records": [
            {
                "eventName": "s3:ObjectCreated:Put",
                "s3": {
                    "bucket": {"name": "lakefs"},
                    "object": {"key": "batch/rec1.parquet", "size": 10},
                },
                "userIdentity": {"principalId": "minioadmin"},
            },
            {
                "eventName": "s3:ObjectCreated:Put",
                "s3": {
                    "bucket": {"name": "lakefs"},
                    "object": {"key": "batch/rec2.parquet", "size": 20},
                },
                "userIdentity": {"principalId": "minioadmin"},
            },
        ]
    }
    resp = client.post("/api/webhooks/minio/storage-events", json=payload, headers=minio_headers)
    assert resp.status_code == 200
    assert resp.json()["records_evaluated"] == 2
    assert len(resp.json()["results"]) == 2


def test_p1_webhook_url_encoded_object_key_decoding(minio_headers, db):
    """P1.6: Correctly decodes URL-encoded object keys (e.g. data%2Fdir%2Ffile.csv -> data/dir/file.csv)."""
    raw_key = "repo_name%2Fsubfolder%2Fencoded%20data%2Btest.csv"
    payload = {
        "Records": [
            {
                "eventName": "s3:ObjectCreated:Put",
                "s3": {
                    "bucket": {"name": "lakefs"},
                    "object": {"key": raw_key},
                },
                "userIdentity": {"principalId": "minioadmin"},
            }
        ]
    }
    resp = client.post("/api/webhooks/minio/storage-events", json=payload, headers=minio_headers)
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert len(results) == 1
    # Key must be unquoted
    assert results[0]["object_key"] == "repo_name/subfolder/encoded data+test.csv"


def test_p1_unrelated_bucket_notifications_ignored(minio_headers):
    """P1.7: Notifications targeting buckets other than monitored storage namespaces are ignored."""
    payload = {
        "Records": [
            {
                "eventName": "s3:ObjectCreated:Put",
                "s3": {
                    "bucket": {"name": "unrelated-marketing-bucket"},
                    "object": {"key": "images/banner.png"},
                },
                "userIdentity": {"principalId": "some_developer"},
            }
        ]
    }
    resp = client.post("/api/webhooks/minio/storage-events", json=payload, headers=minio_headers)
    assert resp.status_code == 200
    assert resp.json()["results"][0]["classification"] == CLASSIFICATION_IGNORED


# ===============================================================================
# 2. Classification & Detection Logic
# ===============================================================================

def test_p1_expected_lakefs_writes_do_not_generate_bypass_alerts(minio_headers, test_dataset, db):
    """P1.8: Normal lakeFS uploads by lakeFS service user are classified as authorized and do not alert."""
    payload = {
        "Records": [
            {
                "eventName": "s3:ObjectCreated:Put",
                "eventTime": "2026-10-17T18:00:00Z",
                "s3": {
                    "bucket": {"name": "lakefs"},
                    "object": {
                        "key": f"{test_dataset.name}/data/physical_object_hash_1",
                        "size": 2048,
                        "sequencer": "SEQ_LAKEFS_001",
                    },
                },
                "userIdentity": {"principalId": "minioadmin"},  # expected lakeFS service user
            }
        ]
    }
    resp = client.post("/api/webhooks/minio/storage-events", json=payload, headers=minio_headers)
    assert resp.status_code == 200
    res = resp.json()["results"][0]
    assert res["classification"] == CLASSIFICATION_AUTHORIZED
    assert res["should_alert"] is False
    assert res["severity"] == "LOW"


def test_p1_unauthorized_identity_triggers_suspicious_bypass_alert(minio_headers, test_dataset, db):
    """P1.9: Direct write by an unauthorized identity into dataset storage triggers HIGH severity Alert."""
    seq_id = uuid.uuid4().hex[:12]
    payload = {
        "Records": [
            {
                "eventName": "s3:ObjectCreated:Put",
                "eventTime": "2026-10-17T18:01:00Z",
                "s3": {
                    "bucket": {"name": "lakefs"},
                    "object": {
                        "key": f"{test_dataset.name}/injected_backdoor_data.parquet",
                        "size": 4096,
                        "sequencer": seq_id,
                    },
                },
                "userIdentity": {"principalId": "attacker_s3_key_999"},  # Rogue identity!
                "requestParameters": {"sourceIPAddress": "198.51.100.44"},
            }
        ]
    }
    resp = client.post("/api/webhooks/minio/storage-events", json=payload, headers=minio_headers)
    assert resp.status_code == 200
    res = resp.json()["results"][0]
    assert res["classification"] == CLASSIFICATION_SUSPICIOUS
    assert res["should_alert"] is True
    assert res["severity"] == "HIGH"
    assert "attacker_s3_key_999" in res["reason"]

    # Verify SecurityEvent was persisted with HIGH severity
    resource_id = f"lakefs/{test_dataset.name}/injected_backdoor_data.parquet"
    ev = db.execute(
        select(SecurityEvent).where(
            SecurityEvent.resource_id == resource_id,
            SecurityEvent.event_type == "storage.unversioned_mutation",
        )
    ).scalar_one_or_none()
    assert ev is not None
    assert ev.severity == "HIGH"
    assert ev.actor_id == "attacker_s3_key_999"
    assert ev.attributes["source_ip"] == "198.51.100.44"

    # Verify an Alert was generated in the Alert engine
    alert = db.execute(
        select(Alert).where(
            Alert.mitre_attack_id == "T1565.001",
            Alert.status == "OPEN",
        ).order_by(Alert.id.desc())
    ).scalars().first()
    assert alert is not None
    assert "Direct MinIO Storage Bypass" in alert.title or alert.severity == "HIGH"


def test_p1_unauthorized_deletion_triggers_bypass_alert(minio_headers, test_dataset, db):
    """P1.2: Direct S3 object deletion by unauthorized identity triggers HIGH severity alert."""
    seq_id = uuid.uuid4().hex[:12]
    payload = {
        "Records": [
            {
                "eventName": "s3:ObjectRemoved:Delete",
                "eventTime": "2026-10-17T18:02:00Z",
                "s3": {
                    "bucket": {"name": "lakefs"},
                    "object": {
                        "key": f"{test_dataset.name}/production_data.csv",
                        "sequencer": seq_id,
                    },
                },
                "userIdentity": {"principalId": "unknown_actor"},
            }
        ]
    }
    resp = client.post("/api/webhooks/minio/storage-events", json=payload, headers=minio_headers)
    assert resp.status_code == 200
    res = resp.json()["results"][0]
    assert res["classification"] == CLASSIFICATION_SUSPICIOUS
    assert res["severity"] == "HIGH"


def test_p1_lakefs_metadata_deletion_detected(minio_headers, db):
    """P1.11: Deletion of lakeFS metadata paths is detected as suspicious mutation."""
    seq_id = uuid.uuid4().hex[:12]
    payload = {
        "Records": [
            {
                "eventName": "s3:ObjectRemoved:Delete",
                "eventTime": "2026-10-17T18:03:00Z",
                "s3": {
                    "bucket": {"name": "lakefs"},
                    "object": {
                        "key": "test-repo/_lakefs/manifests/deadbeef.json",
                        "sequencer": seq_id,
                    },
                },
                "userIdentity": {"principalId": "minioadmin"},
            }
        ]
    }
    resp = client.post("/api/webhooks/minio/storage-events", json=payload, headers=minio_headers)
    assert resp.status_code == 200
    res = resp.json()["results"][0]
    assert res["classification"] == CLASSIFICATION_SUSPICIOUS
    assert res["severity"] == "HIGH"


def test_p1_garbage_collection_deletes_not_falsely_alerted(minio_headers, test_dataset, db):
    """P1.12: Normal garbage-collection deletion by lakeFS service user does not create false bypass alert."""
    payload = {
        "Records": [
            {
                "eventName": "s3:ObjectRemoved:Delete",
                "eventTime": "2026-10-17T18:04:00Z",
                "s3": {
                    "bucket": {"name": "lakefs"},
                    "object": {
                        "key": f"{test_dataset.name}/unreferenced_uncommitted_blob",
                    },
                },
                "userIdentity": {"principalId": "minioadmin"},  # lakeFS identity
            }
        ]
    }
    resp = client.post("/api/webhooks/minio/storage-events", json=payload, headers=minio_headers)
    assert resp.status_code == 200
    res = resp.json()["results"][0]
    assert res["classification"] == CLASSIFICATION_AUTHORIZED
    assert res["should_alert"] is False


# ===============================================================================
# 3. Idempotency, Deduplication & Error Handling
# ===============================================================================

def test_p1_duplicate_notification_delivery_is_idempotent(minio_headers, test_dataset, db):
    """P1.13: Duplicate delivery of the exact same notification produces only one SecurityEvent."""
    seq_id = "STABLE_SEQ_" + uuid.uuid4().hex[:8]
    payload = {
        "Records": [
            {
                "eventName": "s3:ObjectCreated:Put",
                "eventTime": "2026-10-17T18:05:00Z",
                "s3": {
                    "bucket": {"name": "lakefs"},
                    "object": {
                        "key": f"{test_dataset.name}/duplicate_test.csv",
                        "size": 512,
                        "eTag": "etag12345",
                        "sequencer": seq_id,
                    },
                },
                "userIdentity": {"principalId": "rogue_user"},
            }
        ]
    }
    # Delivery 1
    resp1 = client.post("/api/webhooks/minio/storage-events", json=payload, headers=minio_headers)
    assert resp1.status_code == 200
    ev1_id = resp1.json()["results"][0]["security_event_id"]

    # Delivery 2 (Identical delivery retry)
    resp2 = client.post("/api/webhooks/minio/storage-events", json=payload, headers=minio_headers)
    assert resp2.status_code == 200
    ev2_id = resp2.json()["results"][0]["security_event_id"]

    assert ev1_id == ev2_id, "Idempotent processing must return the same SecurityEvent without duplicates!"


def test_p1_distinct_events_for_same_object_not_erroneously_deduplicated(minio_headers, test_dataset, db):
    """P1.14: Two distinct sequential events on the same object are preserved as distinct events."""
    obj_key = f"{test_dataset.name}/multi_event_file.csv"

    # Event 1: Creation
    p1 = {
        "Records": [
            {
                "eventName": "s3:ObjectCreated:Put",
                "eventTime": "2026-10-17T18:06:00Z",
                "s3": {
                    "bucket": {"name": "lakefs"},
                    "object": {"key": obj_key, "size": 100, "sequencer": "SEQ_001"},
                },
                "userIdentity": {"principalId": "rogue_user"},
            }
        ]
    }
    resp1 = client.post("/api/webhooks/minio/storage-events", json=p1, headers=minio_headers)
    ev1_id = resp1.json()["results"][0]["security_event_id"]

    # Event 2: Deletion (different timestamp, different eventName, different sequencer)
    p2 = {
        "Records": [
            {
                "eventName": "s3:ObjectRemoved:Delete",
                "eventTime": "2026-10-17T18:07:00Z",
                "s3": {
                    "bucket": {"name": "lakefs"},
                    "object": {"key": obj_key, "sequencer": "SEQ_002"},
                },
                "userIdentity": {"principalId": "rogue_user"},
            }
        ]
    }
    resp2 = client.post("/api/webhooks/minio/storage-events", json=p2, headers=minio_headers)
    ev2_id = resp2.json()["results"][0]["security_event_id"]

    assert ev1_id != ev2_id, "Distinct events on the same object key must not be falsely coalesced!"


def test_p1_database_persistence_failure_returns_500(minio_headers):
    """P1.15: Database error during processing returns HTTP 500 to signal delivery retry."""
    payload = {
        "Records": [
            {
                "eventName": "s3:ObjectCreated:Put",
                "s3": {
                    "bucket": {"name": "lakefs"},
                    "object": {"key": "test/db_fail.csv"},
                },
                "userIdentity": {"principalId": "rogue_actor"},
            }
        ]
    }
    with patch("app.services.threat_hunting.storage_detector.storage_mutation_detector.process_minio_record") as mock_proc:
        mock_proc.side_effect = RuntimeError("Database connection pool exhausted")
        resp = client.post("/api/webhooks/minio/storage-events", json=payload, headers=minio_headers)
        assert resp.status_code == 500
        assert "error" in resp.json()["status"]


def test_p1_rule_seeding_is_idempotent(db):
    """P1.16: Seeding default rules multiple times is strictly idempotent."""
    created1 = rule_service.seed_default_rules(db)
    created2 = rule_service.seed_default_rules(db)
    assert created2 == 0, "Second seed run must create 0 duplicate rules!"

    rule = db.execute(
        select(DetectionRule).where(
            DetectionRule.name == "Direct MinIO Storage Bypass (Unversioned Storage Mutation)"
        )
    ).scalar_one_or_none()
    assert rule is not None
    assert rule.config["event_type"] == "storage.unversioned_mutation"
    assert rule.severity == "HIGH"
    assert rule.mitre_attack_id == "T1565.001"
    assert rule.mitre_atlas_id == "AML.T0020"


def test_p1_direct_write_using_lakefs_service_identity_triggers_suspicious_bypass_alert(
    minio_headers, test_dataset, db
):
    """P1.17: A direct S3 write of a logical dataset file using the lakeFS credentials bypasses blockstore and alerts."""
    payload = {
        "Records": [
            {
                "eventName": "s3:ObjectCreated:Put",
                "eventTime": "2026-10-18T00:00:00Z",
                "s3": {
                    "bucket": {"name": "lakefs"},
                    "object": {
                        "key": f"{test_dataset.name}/direct_bypass_payload.parquet",
                        "size": 4096,
                        "sequencer": "SEQ_DIRECT_BYPASS_01",
                    },
                },
                "userIdentity": {"principalId": "minioadmin"},  # uses same credentials as lakeFS
            }
        ]
    }
    resp = client.post("/api/webhooks/minio/storage-events", json=payload, headers=minio_headers)
    assert resp.status_code == 200
    res = resp.json()["results"][0]
    assert res["classification"] == CLASSIFICATION_SUSPICIOUS
    assert res["should_alert"] is True
    assert res["severity"] == "HIGH"


def test_p1_missing_or_inconclusive_correlation_evidence_retained_as_unverified(
    minio_headers, db
):
    """P1.18: Mutation by service identity without confirmed commit or catalog correlation is unverified."""
    payload = {
        "Records": [
            {
                "eventName": "s3:ObjectCreated:Put",
                "eventTime": "2026-10-18T00:01:00Z",
                "s3": {
                    "bucket": {"name": "lakefs"},
                    "object": {
                        "key": "unregistered_external_repo/blob_12345",
                        "size": 1024,
                        "sequencer": "SEQ_UNVERIFIED_01",
                    },
                },
                "userIdentity": {"principalId": "minioadmin"},
            }
        ]
    }
    resp = client.post("/api/webhooks/minio/storage-events", json=payload, headers=minio_headers)
    assert resp.status_code == 200
    res = resp.json()["results"][0]
    assert res["classification"] == CLASSIFICATION_UNVERIFIED
    assert res["should_alert"] is False
    assert res["severity"] == "LOW"


def test_p1_production_webhook_secret_required():
    """P1.19: Predictable default or empty webhook secret is rejected in production."""
    import pytest
    from app.core.config import Settings

    with pytest.raises(ValueError, match="Production environment requires an explicitly configured"):
        Settings(
            ENVIRONMENT="production",
            MINIO_WEBHOOK_SECRET="sentinelml-dev-minio-webhook-secret-32b",
            LAKEFS_WEBHOOK_SECRET="custom-secret-key-32b-length-ok",
            MINIO_ACCESS_KEY="prod-key",
            MINIO_SECRET_KEY="prod-secret",
        )
