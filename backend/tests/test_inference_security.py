import uuid
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.session import get_db
from app.models.user import User
from app.core.security import hash_password
from app.services.threat_hunting.inference_detector import InferenceRateDetector, inference_rate_detector
from app.services.threat_hunting.event_service import event_service
from app.services.threat_hunting.rule_service import rule_service
from app.services.threat_hunting.detection_engine import detection_engine
from app.services.threat_hunting.alert_service import alert_service

# Isolated SQLite in-memory test database
TEST_DB_URL = "sqlite:///:memory:"
test_engine = create_engine(
    TEST_DB_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)

@event.listens_for(test_engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()

TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)


@pytest.fixture(scope="module", autouse=True)
def setup_test_db():
    Base.metadata.create_all(bind=test_engine)
    db = TestingSessionLocal()
    try:
        rule_service.seed_default_rules(db)
    finally:
        db.close()
    yield


@pytest.fixture
def db():
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()


def test_tabular_memory_flooding_guard(db):
    """
    Verifies that input matrix exceeding 10^5 cells is blocked as Algorithmic Exhaustion DoS.
    """
    detector = InferenceRateDetector()
    actor = "attacker_client"
    model_id = "test-tabular-model"

    # 1. Normal payload (10 rows x 5 cols = 50 cells) -> ALLOWED
    is_valid, err = detector.validate_payload_shape(
        num_rows=10, num_cols=5, actor_id=actor, resource_id=model_id, db=db
    )
    assert is_valid is True
    assert err is None

    # 2. Flooding payload (500 rows x 250 cols = 125,000 cells > 10^5) -> REJECTED
    is_valid, err = detector.validate_payload_shape(
        num_rows=500, num_cols=250, actor_id=actor, resource_id=model_id, db=db
    )
    assert is_valid is False
    assert "exceeds maximum allowed threshold" in err

    # 3. Verify SecurityEvent is generated
    detections = detection_engine.evaluate_all_rules(db)
    dos_detections = [d for d in detections if "Algorithmic Denial of Service" in d.title]
    assert len(dos_detections) > 0, "Expected an alert for Algorithmic Exhaustion DoS!"
    
    alert = alert_service.process_detection(db, dos_detections[0])
    assert alert.severity == "HIGH"
    assert "Algorithmic Denial of Service" in alert.title


def test_decision_boundary_scraping_detection(db):
    """
    Verifies that high-density queries clustering at decision margins (0.48 <= P <= 0.52)
    trigger a Decision-Boundary Scraping alert.
    """
    detector = InferenceRateDetector()
    actor = f"scraper_{uuid.uuid4().hex[:6]}"
    model_id = "target-classifier"

    # Diverse predictions (e.g. 0.95, 0.12, 0.88, 0.05) -> No alert
    assert detector.record_and_check_decision_boundary(
        actor_id=actor, resource_id=model_id, predictions=[0.95, 0.12], db=db
    ) is False

    # 5 queries clustering directly in the critical boundary [0.48, 0.52]
    boundary_queries = [0.49, 0.50, 0.51, 0.495, 0.505]
    triggered = False
    for p in boundary_queries:
        if detector.record_and_check_decision_boundary(
            actor_id=actor, resource_id=model_id, predictions=[p], db=db
        ):
            triggered = True

    assert triggered is True, "Decision boundary scraping should be flagged!"

    # Verify Threat Hunting Alert
    detections = detection_engine.evaluate_all_rules(db)
    boundary_detections = [d for d in detections if "Decision-Boundary Scraping" in d.title]
    assert len(boundary_detections) > 0, "Expected alert for Decision-Boundary Scraping!"
    alert = alert_service.process_detection(db, boundary_detections[0])
    assert alert.severity == "HIGH"


def test_systematic_feature_perturbation_detection(db):
    """
    Verifies that sequential queries nudging only 1 feature incrementally (L0=1)
    trigger a Systematic Feature Perturbation alert (indicating gradient/boundary estimation).
    """
    detector = InferenceRateDetector()
    actor = f"perturb_crawler_{uuid.uuid4().hex[:6]}"
    model_id = "blackbox-target"

    base_vector = {"feat_0": 1.0, "feat_1": 2.0, "feat_2": 3.0, "feat_3": 4.0}

    # Query 1: Base vector
    assert detector.record_and_check_feature_perturbation(
        actor_id=actor, resource_id=model_id, feature_record=base_vector, db=db
    ) is False

    # Query 2: Nudge only feat_0 (L0 = 1)
    q2 = {"feat_0": 1.05, "feat_1": 2.0, "feat_2": 3.0, "feat_3": 4.0}
    detector.record_and_check_feature_perturbation(actor_id=actor, resource_id=model_id, feature_record=q2, db=db)

    # Query 3: Nudge only feat_0 again (L0 = 1)
    q3 = {"feat_0": 1.10, "feat_1": 2.0, "feat_2": 3.0, "feat_3": 4.0}
    detector.record_and_check_feature_perturbation(actor_id=actor, resource_id=model_id, feature_record=q3, db=db)

    # Query 4: Nudge only feat_0 again (L0 = 1) -> 3rd consecutive perturbation -> TRIGGER
    q4 = {"feat_0": 1.15, "feat_1": 2.0, "feat_2": 3.0, "feat_3": 4.0}
    triggered = detector.record_and_check_feature_perturbation(
        actor_id=actor, resource_id=model_id, feature_record=q4, db=db
    )
    assert triggered is True, "Systematic feature perturbation should be flagged on 3rd consecutive L0=1 nudge!"

    # Verify Threat Hunting Alert
    detections = detection_engine.evaluate_all_rules(db)
    pert_detections = [d for d in detections if "Systematic Feature Perturbation" in d.title]
    assert len(pert_detections) > 0, "Expected alert for Systematic Feature Perturbation!"
    alert = alert_service.process_detection(db, pert_detections[0])
    assert alert.severity == "HIGH"


def test_model_confidence_drift_detection(db):
    """
    Verifies that an abrupt collapse in prediction confidence distributions
    triggers a Model Behavioral Drift alert.
    """
    detector = InferenceRateDetector()
    model_id = f"drift_model_{uuid.uuid4().hex[:6]}"

    # Normal healthy high-confidence predictions (0.95, 0.90)
    for _ in range(5):
        assert (
            detector.record_and_check_confidence_drift(
                resource_id=model_id, predictions=[0.95, 0.90], db=db
            )
            is False
        )

    # Abrupt degradation in confidence (collapsing to near 0.50 uniform uncertainty)
    triggered = False
    for _ in range(12):
        if detector.record_and_check_confidence_drift(
            resource_id=model_id, predictions=[0.52, 0.51], db=db
        ):
            triggered = True

    assert triggered is True, "Confidence distribution collapse should trigger Behavioral Drift alert!"

    # Verify Threat Hunting Alert
    detections = detection_engine.evaluate_all_rules(db)
    drift_detections = [d for d in detections if "Confidence Degradation" in d.title]
    assert len(drift_detections) > 0, "Expected alert for Confidence Degradation Drift!"
    alert = alert_service.process_detection(db, drift_detections[0])
    assert alert.severity == "HIGH"


def test_algorithmic_dos_latency_spike_detection(db):
    """
    Verifies that an extreme inference latency spike (e.g. 1800ms vs 50ms baseline)
    triggers an Algorithmic DoS alert under stable traffic.
    """
    detector = InferenceRateDetector()
    model_id = f"dos_target_{uuid.uuid4().hex[:6]}"

    # Normal fast latencies (baseline ~ 50ms)
    for lat in [45.0, 50.0, 48.0, 52.0, 50.0]:
        assert (
            detector.record_and_check_latency_spike(
                resource_id=model_id, latency_ms=lat, db=db
            )
            is False
        )

    # Extreme latency spike: 1800ms (> 3x baseline and > 1500ms)
    triggered = detector.record_and_check_latency_spike(
        resource_id=model_id, latency_ms=1800.0, db=db
    )
    assert triggered is True, "Latency spike to 1800ms should trigger Algorithmic DoS alert!"

    # Verify Threat Hunting Alert
    detections = detection_engine.evaluate_all_rules(db)
    dos_detections = [d for d in detections if "Inference Latency Spike Anomaly" in d.title]
    assert len(dos_detections) > 0, "Expected alert for Inference Latency Spike Anomaly!"
    alert = alert_service.process_detection(db, dos_detections[0])
    assert alert.severity == "HIGH"
    assert alert.mitre_attack_id == "T1499"
    assert alert.mitre_atlas_id == "AML.T0029"
