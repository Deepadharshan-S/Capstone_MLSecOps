import http.server
import os
import threading
import httpx
import pytest
from sqlalchemy import text
from starlette.testclient import TestClient

from opentelemetry import trace
from opentelemetry.trace import SpanKind, StatusCode
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import (
    ALWAYS_OFF,
    ALWAYS_ON,
    ParentBased,
    TraceIdRatioBased,
)

from app.core.config import Settings
from app.db.session import engine
from app.core.telemetry import (
    _reset_telemetry_state,
    build_resource,
    flush_telemetry,
    get_meter,
    get_sampler,
    get_tracer,
    initialize_telemetry,
    instrument_fastapi_app,
    instrument_sqlalchemy_engine,
    uninstrument_sqlalchemy_engine,
    instrument_httpx,
    uninstrument_httpx,
    is_telemetry_enabled,
    normalize_metrics_endpoint,
    normalize_traces_endpoint,
    shutdown_telemetry,
    uninstrument_fastapi_app,
    get_ml_tracer,
    get_ml_meter,
    trace_ml_operation,
    record_training_job_submitted,
    record_training_job_completed,
    record_training_duration,
    record_deployment_operation,
    record_inference_request,
    record_inference_duration,
)
from app.main import app


@pytest.fixture(autouse=True)
def clean_telemetry_state():
    """Ensures a clean telemetry state before and after each test."""
    _reset_telemetry_state(app=app, engine=engine)
    yield
    _reset_telemetry_state(app=app, engine=engine)


@pytest.fixture
def mock_http_server():
    """Runs a lightweight local HTTP server in a thread to test HTTPX outbound requests."""
    received = []

    class MockHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            received.append({"method": "GET", "path": self.path, "headers": dict(self.headers)})
            if "/error/400" in self.path:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b'{"detail": "bad request"}')
            elif "/error/500" in self.path:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(b'{"detail": "internal error"}')
            else:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"status": "ok"}')

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length > 0 else b""
            received.append({
                "method": "POST",
                "path": self.path,
                "headers": dict(self.headers),
                "body": body,
            })
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"predictions": [1, 0]}')

        def log_message(self, format, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), MockHandler)
    host, port = server.server_address
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    server_url = f"http://{host}:{port}"
    try:
        yield server_url, received
    finally:
        server.shutdown()
        server.server_close()


def test_telemetry_disabled_by_default():
    """Test 1: When OTEL_ENABLED is False, telemetry is a safe no-op and app works without a collector."""
    cfg = Settings(
        SECRET_KEY="test-key",
        LAKEFS_ENDPOINT="http://localhost:8000",
        LAKEFS_ACCESS_KEY_ID="test",
        LAKEFS_SECRET_ACCESS_KEY="test",
        OTEL_ENABLED=False,
    )
    result = initialize_telemetry(cfg=cfg)
    assert result is False
    assert is_telemetry_enabled() is False

    # Standard tracers and meters return safe no-ops
    tracer = get_tracer("test_disabled")
    assert tracer is not None
    with tracer.start_as_current_span("test_span") as span:
        assert span is not None

    meter = get_meter("test_disabled")
    assert meter is not None

    # Calling shutdown when disabled is completely safe
    shutdown_telemetry()
    assert is_telemetry_enabled() is False


def test_telemetry_enabled_in_memory():
    """Test 2: When enabled, TracerProvider and MeterProvider are configured with expected resource attributes."""
    cfg = Settings(
        SECRET_KEY="test-key",
        LAKEFS_ENDPOINT="http://localhost:8000",
        LAKEFS_ACCESS_KEY_ID="test",
        LAKEFS_SECRET_ACCESS_KEY="test",
        OTEL_ENABLED=True,
        OTEL_SERVICE_NAME="sentinelml-test-suite",
        OTEL_SERVICE_VERSION="1.2.3",
        ENVIRONMENT="testing",
        OTEL_TRACES_SAMPLER="always_on",
    )

    span_exporter = InMemorySpanExporter()
    metric_reader = InMemoryMetricReader()

    result = initialize_telemetry(
        cfg=cfg,
        trace_exporter=span_exporter,
        metric_reader=metric_reader,
    )
    assert result is True
    assert is_telemetry_enabled() is True

    # Emit a span
    tracer = get_tracer("sentinelml.test")
    with tracer.start_as_current_span("unit_test_span", attributes={"test.key": "val"}):
        pass

    # Flush spans to the in-memory exporter
    flush_success = flush_telemetry()
    assert flush_success is True

    # Verify span was collected in-memory
    spans = span_exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.name == "unit_test_span"
    assert span.attributes.get("test.key") == "val"

    # Verify resource attributes
    assert span.resource.attributes.get("service.name") == "sentinelml-test-suite"
    assert span.resource.attributes.get("service.version") == "1.2.3"
    assert span.resource.attributes.get("deployment.environment") == "testing"

    # Emit a metric
    meter = get_meter("sentinelml.test")
    counter = meter.create_counter("test_counter", unit="1", description="Test counter")
    counter.add(5, {"status": "ok"})

    metrics_data = metric_reader.get_metrics_data()
    assert metrics_data is not None
    metric_names = [
        m.name
        for rm in metrics_data.resource_metrics
        for sm in rm.scope_metrics
        for m in sm.metrics
    ]
    assert "test_counter" in metric_names

    # Shutdown should flush and clean up
    shutdown_telemetry()
    assert is_telemetry_enabled() is False


def test_telemetry_configuration_and_samplers():
    """Test 3: Verify environment/settings interpretation, URL normalization, and samplers."""
    # 1. URL normalization
    assert normalize_traces_endpoint("http://localhost:4318") == "http://localhost:4318/v1/traces"
    assert normalize_traces_endpoint("http://localhost:4318/") == "http://localhost:4318/v1/traces"
    assert normalize_traces_endpoint("http://localhost:4318/v1/traces") == "http://localhost:4318/v1/traces"

    assert normalize_metrics_endpoint("http://localhost:4318") == "http://localhost:4318/v1/metrics"
    assert normalize_metrics_endpoint("http://localhost:4318/") == "http://localhost:4318/v1/metrics"
    assert normalize_metrics_endpoint("http://localhost:4318/v1/metrics") == "http://localhost:4318/v1/metrics"

    # 2. Sampler mapping
    assert get_sampler("always_on") == ALWAYS_ON
    assert get_sampler("always_off") == ALWAYS_OFF
    assert isinstance(get_sampler("traceidratio", 0.5), TraceIdRatioBased)
    assert isinstance(get_sampler("parentbased_always_on"), ParentBased)
    assert isinstance(get_sampler("parentbased_always_off"), ParentBased)
    assert isinstance(get_sampler("parentbased_traceidratio", 0.2), ParentBased)
    assert get_sampler("unknown_sampler") == ALWAYS_ON

    # 3. Resource construction
    res = build_resource(
        service_name="custom-service",
        service_version="9.9.9",
        environment="staging",
        additional_attributes={"custom.attr": "hello"},
    )
    assert res.attributes.get("service.name") == "custom-service"
    assert res.attributes.get("service.version") == "9.9.9"
    assert res.attributes.get("deployment.environment") == "staging"
    assert res.attributes.get("custom.attr") == "hello"


def test_collector_unavailable_resilience():
    """Test 4: When OTEL_ENABLED is True but collector is unreachable, app starts, serves, and shuts down."""
    cfg = Settings(
        SECRET_KEY="test-key",
        LAKEFS_ENDPOINT="http://localhost:8000",
        LAKEFS_ACCESS_KEY_ID="test",
        LAKEFS_SECRET_ACCESS_KEY="test",
        OTEL_ENABLED=True,
        OTEL_EXPORTER_OTLP_ENDPOINT="http://127.0.0.1:9999",  # Unroutable local port
        OTEL_EXPORTER_OTLP_TIMEOUT_SECONDS=1,
    )

    # Initialization must not raise an exception
    init_success = initialize_telemetry(cfg=cfg)
    assert init_success is True
    assert is_telemetry_enabled() is True

    # Spans can be created without blocking or failing
    tracer = get_tracer("resilience.test")
    with tracer.start_as_current_span("resilient_span"):
        pass

    # Shutdown completes cleanly without hanging indefinitely
    shutdown_telemetry()
    assert is_telemetry_enabled() is False


def test_shutdown_lifecycle_safety():
    """Test 5: Telemetry shutdown is safe, idempotent, and works when uninitialized."""
    # Multiple shutdowns without init
    shutdown_telemetry()
    shutdown_telemetry()

    # Init then double shutdown
    cfg = Settings(
        SECRET_KEY="test-key",
        LAKEFS_ENDPOINT="http://localhost:8000",
        LAKEFS_ACCESS_KEY_ID="test",
        LAKEFS_SECRET_ACCESS_KEY="test",
        OTEL_ENABLED=True,
    )
    initialize_telemetry(cfg=cfg, trace_exporter=InMemorySpanExporter(), metric_reader=InMemoryMetricReader())
    assert is_telemetry_enabled() is True

    shutdown_telemetry()
    assert is_telemetry_enabled() is False
    shutdown_telemetry()
    assert is_telemetry_enabled() is False


def test_fastapi_lifespan_integration_with_token_cleanup():
    """Test 6: Existing FastAPI lifespan runs token cleanup and shuts down telemetry without regressions."""
    with TestClient(app) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert response.json() == {"message": "Welcome to SentinelML"}

    # After TestClient context exit, shutdown_telemetry() should have run cleanly
    assert is_telemetry_enabled() is False


# ==============================================================================
# Phase 2 — FastAPI Automatic HTTP Tracing & Request Telemetry Tests
# ==============================================================================


def test_phase2_http_tracing_disabled_produces_no_spans():
    """Phase 2 Test A: When OTEL_ENABLED is False, requests succeed with 0 OTel spans."""
    cfg = Settings(
        SECRET_KEY="test-key",
        LAKEFS_ENDPOINT="http://localhost:8000",
        LAKEFS_ACCESS_KEY_ID="test",
        LAKEFS_SECRET_ACCESS_KEY="test",
        OTEL_ENABLED=False,
    )
    span_exporter = InMemorySpanExporter()
    initialize_telemetry(cfg=cfg, trace_exporter=span_exporter)
    instrument_fastapi_app(app, cfg=cfg)

    with TestClient(app) as client:
        resp = client.get("/")
        assert resp.status_code == 200

    flush_telemetry()
    assert len(span_exporter.get_finished_spans()) == 0


def test_phase2_http_tracing_enabled_produces_server_span_and_metrics():
    """Phase 2 Test B: When OTEL_ENABLED is True, requests automatically produce server spans and HTTP metrics."""
    cfg = Settings(
        SECRET_KEY="test-key",
        LAKEFS_ENDPOINT="http://localhost:8000",
        LAKEFS_ACCESS_KEY_ID="test",
        LAKEFS_SECRET_ACCESS_KEY="test",
        OTEL_ENABLED=True,
        OTEL_SERVICE_NAME="sentinelml-http-test",
    )
    span_exporter = InMemorySpanExporter()
    metric_reader = InMemoryMetricReader()

    initialize_telemetry(cfg=cfg, trace_exporter=span_exporter, metric_reader=metric_reader)
    instrument_fastapi_app(app, cfg=cfg)

    with TestClient(app) as client:
        resp = client.get("/")
        assert resp.status_code == 200
        assert resp.json() == {"message": "Welcome to SentinelML"}

    flush_telemetry()
    spans = span_exporter.get_finished_spans()
    assert len(spans) >= 1

    server_span = next((s for s in spans if s.name == "GET /"), None)
    assert server_span is not None, f"Expected 'GET /' span, found: {[s.name for s in spans]}"
    assert server_span.attributes.get("http.method") == "GET"
    assert server_span.attributes.get("http.route") == "/"
    assert server_span.attributes.get("http.status_code") == 200

    # Verify standard HTTP duration metric was recorded
    metrics_data = metric_reader.get_metrics_data()
    assert metrics_data is not None
    metric_names = [
        m.name
        for rm in metrics_data.resource_metrics
        for sm in rm.scope_metrics
        for m in sm.metrics
    ]
    assert "http.server.duration" in metric_names or "http.server.active_requests" in metric_names


def test_phase2_http_tracing_error_status_code_representation():
    """Phase 2 Test C: 4xx HTTP responses are correctly captured in span attributes without altering error response."""
    cfg = Settings(
        SECRET_KEY="test-key",
        LAKEFS_ENDPOINT="http://localhost:8000",
        LAKEFS_ACCESS_KEY_ID="test",
        LAKEFS_SECRET_ACCESS_KEY="test",
        OTEL_ENABLED=True,
    )
    span_exporter = InMemorySpanExporter()
    initialize_telemetry(cfg=cfg, trace_exporter=span_exporter)
    instrument_fastapi_app(app, cfg=cfg)

    with TestClient(app) as client:
        # Request a non-existent endpoint to trigger standard FastAPI 404
        resp = client.get("/api/nonexistent_test_route_404")
        assert resp.status_code == 404
        assert resp.json() == {"detail": "Not Found"}

    flush_telemetry()
    spans = span_exporter.get_finished_spans()
    err_span = next((s for s in spans if s.attributes.get("http.status_code") == 404), None)
    assert err_span is not None
    assert err_span.attributes.get("http.method") == "GET"


def test_phase2_http_tracing_idempotency_prevents_duplicate_spans():
    """Phase 2 Test D: Repeated instrumentation calls do not cause duplicate middleware or duplicate spans."""
    cfg = Settings(
        SECRET_KEY="test-key",
        LAKEFS_ENDPOINT="http://localhost:8000",
        LAKEFS_ACCESS_KEY_ID="test",
        LAKEFS_SECRET_ACCESS_KEY="test",
        OTEL_ENABLED=True,
    )
    span_exporter = InMemorySpanExporter()
    initialize_telemetry(cfg=cfg, trace_exporter=span_exporter)

    # Double instrumentation call
    res1 = instrument_fastapi_app(app, cfg=cfg)
    res2 = instrument_fastapi_app(app, cfg=cfg)
    assert res1 is True
    assert res2 is True

    with TestClient(app) as client:
        resp = client.get("/")
        assert resp.status_code == 200

    flush_telemetry()
    spans = span_exporter.get_finished_spans()
    root_spans = [s for s in spans if s.name == "GET /"]
    assert len(root_spans) == 1, f"Expected exactly 1 'GET /' span, found {len(root_spans)}"


def test_phase2_request_id_middleware_preservation():
    """Phase 2 Test E: RequestIDMiddleware and X-Request-ID headers remain completely functional with OTel active."""
    cfg = Settings(
        SECRET_KEY="test-key",
        LAKEFS_ENDPOINT="http://localhost:8000",
        LAKEFS_ACCESS_KEY_ID="test",
        LAKEFS_SECRET_ACCESS_KEY="test",
        OTEL_ENABLED=True,
    )
    span_exporter = InMemorySpanExporter()
    initialize_telemetry(cfg=cfg, trace_exporter=span_exporter)
    instrument_fastapi_app(app, cfg=cfg)

    custom_id = "test-phase2-correlation-id-9988"
    with TestClient(app) as client:
        resp = client.get("/", headers={"X-Request-ID": custom_id})
        assert resp.status_code == 200
        assert resp.headers.get("X-Request-ID") == custom_id

    flush_telemetry()
    spans = span_exporter.get_finished_spans()
    assert any(s.name == "GET /" for s in spans)


def test_phase2_health_endpoint_excluded_from_telemetry_noise():
    """Phase 2 Test G: /health endpoint is excluded from tracing to prevent synthetic polling noise."""
    cfg = Settings(
        SECRET_KEY="test-key",
        LAKEFS_ENDPOINT="http://localhost:8000",
        LAKEFS_ACCESS_KEY_ID="test",
        LAKEFS_SECRET_ACCESS_KEY="test",
        OTEL_ENABLED=True,
        OTEL_EXCLUDED_URLS="health",
    )
    span_exporter = InMemorySpanExporter()
    initialize_telemetry(cfg=cfg, trace_exporter=span_exporter)
    instrument_fastapi_app(app, cfg=cfg)

    with TestClient(app) as client:
        client.get("/health")
        client.get("/")

    flush_telemetry()
    spans = span_exporter.get_finished_spans()
    # Ensure / was captured but /health was excluded
    assert any(s.name == "GET /" for s in spans)
    assert not any(s.attributes.get("http.route") == "/health" for s in spans)
    assert not any("health" in s.name for s in spans)


def test_phase2_collector_unavailable_resilience_with_http_traffic():
    """Phase 2 Test F: When collector endpoint is unreachable, API requests still serve 200 OK without delay."""
    cfg = Settings(
        SECRET_KEY="test-key",
        LAKEFS_ENDPOINT="http://localhost:8000",
        LAKEFS_ACCESS_KEY_ID="test",
        LAKEFS_SECRET_ACCESS_KEY="test",
        OTEL_ENABLED=True,
        OTEL_EXPORTER_OTLP_ENDPOINT="http://127.0.0.1:9999",
        OTEL_EXPORTER_OTLP_TIMEOUT_SECONDS=1,
    )
    init_success = initialize_telemetry(cfg=cfg)
    assert init_success is True
    instrument_fastapi_app(app, cfg=cfg)

    with TestClient(app) as client:
        resp = client.get("/")
        assert resp.status_code == 200
        assert resp.json() == {"message": "Welcome to SentinelML"}

    shutdown_telemetry()
    assert is_telemetry_enabled() is False


# ==============================================================================
# Phase 3A — SQLAlchemy / PostgreSQL Engine Instrumentation Tests
# ==============================================================================


def test_phase3a_sqlalchemy_disabled_produces_no_spans():
    """Phase 3A Test 1: When OTEL_ENABLED is False, database queries produce no SQL spans."""
    cfg = Settings(
        SECRET_KEY="test-key",
        LAKEFS_ENDPOINT="http://localhost:8000",
        LAKEFS_ACCESS_KEY_ID="test",
        LAKEFS_SECRET_ACCESS_KEY="test",
        OTEL_ENABLED=False,
    )
    span_exporter = InMemorySpanExporter()
    initialize_telemetry(cfg=cfg, trace_exporter=span_exporter)
    instrument_sqlalchemy_engine(engine, cfg=cfg)

    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))

    flush_telemetry()
    spans = span_exporter.get_finished_spans()
    sql_spans = [
        s for s in spans
        if "SELECT" in s.name or "connect" in s.name or "db.system" in s.attributes
    ]
    assert len(sql_spans) == 0


def test_phase3a_sqlalchemy_enabled_produces_db_spans():
    """Phase 3A Test 2: When OTEL_ENABLED is True, database operations produce spans with db semantic conventions."""
    cfg = Settings(
        SECRET_KEY="test-key",
        LAKEFS_ENDPOINT="http://localhost:8000",
        LAKEFS_ACCESS_KEY_ID="test",
        LAKEFS_SECRET_ACCESS_KEY="test",
        OTEL_ENABLED=True,
    )
    span_exporter = InMemorySpanExporter()
    initialize_telemetry(cfg=cfg, trace_exporter=span_exporter)
    instrument_sqlalchemy_engine(engine, cfg=cfg)

    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))

    flush_telemetry()
    spans = span_exporter.get_finished_spans()
    assert len(spans) >= 1
    query_span = next((s for s in spans if "SELECT" in s.name), None)
    assert query_span is not None, f"Expected SQL query span, got {[s.name for s in spans]}"
    assert query_span.kind == SpanKind.CLIENT
    assert "db.system" in query_span.attributes or "db.operation" in query_span.attributes


def test_phase3a_sql_parameter_privacy_never_exposes_values():
    """Phase 3A Test 3: Bound parameter values (tokens, passwords, secrets) are never exposed in span attributes."""
    cfg = Settings(
        SECRET_KEY="test-key",
        LAKEFS_ENDPOINT="http://localhost:8000",
        LAKEFS_ACCESS_KEY_ID="test",
        LAKEFS_SECRET_ACCESS_KEY="test",
        OTEL_ENABLED=True,
    )
    span_exporter = InMemorySpanExporter()
    initialize_telemetry(cfg=cfg, trace_exporter=span_exporter)
    instrument_sqlalchemy_engine(engine, cfg=cfg)

    secret_val = "SUPER_SECRET_AUTHENTICATION_TOKEN_XYZ_99"
    with engine.connect() as conn:
        conn.execute(
            text("SELECT 1 WHERE :val = :val"),
            {"val": secret_val},
        )

    flush_telemetry()
    spans = span_exporter.get_finished_spans()
    assert len(spans) >= 1

    # Verify secret_val does not appear in any attribute of any span
    for s in spans:
        for attr_key, attr_val in s.attributes.items():
            assert secret_val not in str(attr_val), (
                f"Secret parameter exposed in span '{s.name}' attribute '{attr_key}': {attr_val}"
            )


def test_phase3a_database_span_participates_in_current_trace():
    """Phase 3A Test 4: Database operations performed inside a parent span become child spans of that trace."""
    cfg = Settings(
        SECRET_KEY="test-key",
        LAKEFS_ENDPOINT="http://localhost:8000",
        LAKEFS_ACCESS_KEY_ID="test",
        LAKEFS_SECRET_ACCESS_KEY="test",
        OTEL_ENABLED=True,
    )
    span_exporter = InMemorySpanExporter()
    initialize_telemetry(cfg=cfg, trace_exporter=span_exporter)
    instrument_sqlalchemy_engine(engine, cfg=cfg)

    tracer = get_tracer("test_trace_tree")
    with tracer.start_as_current_span("parent_fastapi_request") as parent:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))

    flush_telemetry()
    spans = span_exporter.get_finished_spans()
    parent_span = next(s for s in spans if s.name == "parent_fastapi_request")
    sql_span = next(s for s in spans if "SELECT" in s.name)

    # Verify trace parentage
    assert sql_span.context.trace_id == parent_span.context.trace_id
    assert sql_span.parent.span_id == parent_span.context.span_id


def test_phase3a_sqlalchemy_idempotency_prevents_duplicate_spans():
    """Phase 3A Test 5: Multiple calls to instrument_sqlalchemy_engine do not register duplicate listeners or duplicate spans."""
    cfg = Settings(
        SECRET_KEY="test-key",
        LAKEFS_ENDPOINT="http://localhost:8000",
        LAKEFS_ACCESS_KEY_ID="test",
        LAKEFS_SECRET_ACCESS_KEY="test",
        OTEL_ENABLED=True,
    )
    span_exporter = InMemorySpanExporter()
    initialize_telemetry(cfg=cfg, trace_exporter=span_exporter)

    # Double instrumentation
    res1 = instrument_sqlalchemy_engine(engine, cfg=cfg)
    res2 = instrument_sqlalchemy_engine(engine, cfg=cfg)
    assert res1 is True
    assert res2 is True

    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))

    flush_telemetry()
    spans = span_exporter.get_finished_spans()
    select_spans = [s for s in spans if "SELECT" in s.name]
    assert len(select_spans) == 1, f"Expected 1 SELECT span, found {len(select_spans)}"


# ==============================================================================
# Phase 3B — HTTPX Client Instrumentation & Trace Propagation Tests
# ==============================================================================


def test_phase3b_httpx_disabled_produces_no_spans(mock_http_server):
    """Phase 3B Test 6: When OTEL_ENABLED is False, HTTPX requests produce 0 spans."""
    server_url, _ = mock_http_server
    cfg = Settings(
        SECRET_KEY="test-key",
        LAKEFS_ENDPOINT="http://localhost:8000",
        LAKEFS_ACCESS_KEY_ID="test",
        LAKEFS_SECRET_ACCESS_KEY="test",
        OTEL_ENABLED=False,
    )
    span_exporter = InMemorySpanExporter()
    initialize_telemetry(cfg=cfg, trace_exporter=span_exporter)
    instrument_httpx(cfg=cfg)

    with httpx.Client() as client:
        resp = client.get(f"{server_url}/test")
        assert resp.status_code == 200

    flush_telemetry()
    spans = span_exporter.get_finished_spans()
    httpx_spans = [s for s in spans if "http" in s.name.lower() or s.name in ("GET", "POST")]
    assert len(httpx_spans) == 0


def test_phase3b_httpx_enabled_produces_client_span(mock_http_server):
    """Phase 3B Test 7: When OTEL_ENABLED is True, HTTPX requests produce CLIENT spans with method, url, status_code."""
    server_url, _ = mock_http_server
    cfg = Settings(
        SECRET_KEY="test-key",
        LAKEFS_ENDPOINT="http://localhost:8000",
        LAKEFS_ACCESS_KEY_ID="test",
        LAKEFS_SECRET_ACCESS_KEY="test",
        OTEL_ENABLED=True,
    )
    span_exporter = InMemorySpanExporter()
    initialize_telemetry(cfg=cfg, trace_exporter=span_exporter)
    instrument_httpx(cfg=cfg)

    with httpx.Client() as client:
        resp = client.get(f"{server_url}/test")
        assert resp.status_code == 200

    flush_telemetry()
    spans = span_exporter.get_finished_spans()
    client_span = next((s for s in spans if s.name == "GET"), None)
    assert client_span is not None, f"Expected GET client span, found {[s.name for s in spans]}"
    assert client_span.kind == SpanKind.CLIENT
    assert client_span.attributes.get("http.method") == "GET"
    assert client_span.attributes.get("http.status_code") == 200
    assert f"{server_url}/test" in client_span.attributes.get("http.url")


def test_phase3b_traceparent_propagation_to_downstream(mock_http_server):
    """Phase 3B Test 8: HTTPX automatically injects W3C traceparent header into outgoing requests matching active parent trace."""
    server_url, received_requests = mock_http_server
    cfg = Settings(
        SECRET_KEY="test-key",
        LAKEFS_ENDPOINT="http://localhost:8000",
        LAKEFS_ACCESS_KEY_ID="test",
        LAKEFS_SECRET_ACCESS_KEY="test",
        OTEL_ENABLED=True,
    )
    span_exporter = InMemorySpanExporter()
    initialize_telemetry(cfg=cfg, trace_exporter=span_exporter)
    instrument_httpx(cfg=cfg)

    tracer = get_tracer("sentinelml.rayservice_caller")
    with tracer.start_as_current_span("fastapi_inference_route") as parent_span:
        with httpx.Client() as client:
            resp = client.post(f"{server_url}/predict", json={"inputs": [[1.0, 2.0]]})
            assert resp.status_code == 200

    flush_telemetry()
    spans = span_exporter.get_finished_spans()
    parent = next(s for s in spans if s.name == "fastapi_inference_route")
    client_span = next(s for s in spans if s.name == "POST")

    # Verify HTTPX client span is child of parent
    assert client_span.context.trace_id == parent.context.trace_id
    assert client_span.parent.span_id == parent.context.span_id

    # Verify downstream received W3C traceparent header
    last_req = received_requests[-1]
    traceparent = last_req["headers"].get("traceparent")
    assert traceparent is not None, "traceparent header was not injected into outgoing HTTP request"

    # traceparent format: 00-{trace_id}-{parent_span_id}-{flags}
    parts = traceparent.split("-")
    assert len(parts) == 4
    assert parts[0] == "00"
    expected_trace_id = format(parent.context.trace_id, "032x")
    expected_span_id = format(client_span.context.span_id, "016x")
    assert parts[1] == expected_trace_id
    assert parts[2] == expected_span_id


def test_phase3b_request_response_bodies_not_captured(mock_http_server):
    """Phase 3B Test 9: Request and response payloads (sensitive ML features and model outputs) are never captured in span attributes."""
    server_url, _ = mock_http_server
    cfg = Settings(
        SECRET_KEY="test-key",
        LAKEFS_ENDPOINT="http://localhost:8000",
        LAKEFS_ACCESS_KEY_ID="test",
        LAKEFS_SECRET_ACCESS_KEY="test",
        OTEL_ENABLED=True,
    )
    span_exporter = InMemorySpanExporter()
    initialize_telemetry(cfg=cfg, trace_exporter=span_exporter)
    instrument_httpx(cfg=cfg)

    sensitive_feature_str = "SENSITIVE_FEATURE_RECORD_999888"
    sensitive_response_str = "CONFIDENTIAL_CLASSIFICATION_OUTPUT_4455"
    with httpx.Client() as client:
        client.post(
            f"{server_url}/predict",
            json={"feature_secret": sensitive_feature_str, "resp_override": sensitive_response_str},
        )

    flush_telemetry()
    spans = span_exporter.get_finished_spans()
    client_span = next(s for s in spans if s.name == "POST")

    for attr_key, attr_val in client_span.attributes.items():
        assert sensitive_feature_str not in str(attr_val), f"Payload leaked into span attribute '{attr_key}'"
        assert sensitive_response_str not in str(attr_val), f"Response leaked into span attribute '{attr_key}'"
        assert "body" not in attr_key.lower()


def test_phase3b_sensitive_headers_not_captured(mock_http_server):
    """Phase 3B Test 10: Sensitive HTTP headers (Authorization, Cookie, API keys) are never captured in span attributes."""
    server_url, _ = mock_http_server
    cfg = Settings(
        SECRET_KEY="test-key",
        LAKEFS_ENDPOINT="http://localhost:8000",
        LAKEFS_ACCESS_KEY_ID="test",
        LAKEFS_SECRET_ACCESS_KEY="test",
        OTEL_ENABLED=True,
    )
    span_exporter = InMemorySpanExporter()
    initialize_telemetry(cfg=cfg, trace_exporter=span_exporter)
    instrument_httpx(cfg=cfg)

    secret_auth_token = "Bearer TOP_SECRET_JWT_TOKEN_ABC123"
    secret_cookie = "session_id=SECRET_SESSION_456"
    secret_key = "MINIO_AWS_SECRET_ACCESS_KEY_XYZ"

    with httpx.Client() as client:
        client.post(
            f"{server_url}/test",
            headers={
                "Authorization": secret_auth_token,
                "Cookie": secret_cookie,
                "X-Api-Key": secret_key,
            },
        )

    flush_telemetry()
    spans = span_exporter.get_finished_spans()
    client_span = next(s for s in spans if s.name == "POST")

    for attr_key, attr_val in client_span.attributes.items():
        assert secret_auth_token not in str(attr_val)
        assert secret_cookie not in str(attr_val)
        assert secret_key not in str(attr_val)
        assert "authorization" not in attr_key.lower()
        assert "cookie" not in attr_key.lower()


def test_phase3b_httpx_error_and_timeout_representation(mock_http_server):
    """Phase 3B Test 11: HTTP 4xx, 5xx, and connection/timeout failures are accurately represented as error spans without suppressing exceptions."""
    server_url, _ = mock_http_server
    cfg = Settings(
        SECRET_KEY="test-key",
        LAKEFS_ENDPOINT="http://localhost:8000",
        LAKEFS_ACCESS_KEY_ID="test",
        LAKEFS_SECRET_ACCESS_KEY="test",
        OTEL_ENABLED=True,
    )
    span_exporter = InMemorySpanExporter()
    initialize_telemetry(cfg=cfg, trace_exporter=span_exporter)
    instrument_httpx(cfg=cfg)

    with httpx.Client() as client:
        # 1. 400 Bad Request
        r400 = client.get(f"{server_url}/error/400")
        assert r400.status_code == 400

        # 2. 500 Internal Server Error
        r500 = client.get(f"{server_url}/error/500")
        assert r500.status_code == 500

        # 3. Connection failure / timeout (must raise exception as normal)
        with pytest.raises(httpx.RequestError):
            client.get("http://127.0.0.1:1/unreachable", timeout=0.01)

    flush_telemetry()
    spans = span_exporter.get_finished_spans()
    span_400 = next(s for s in spans if s.attributes.get("http.status_code") == 400)
    span_500 = next(s for s in spans if s.attributes.get("http.status_code") == 500)
    span_err = next(s for s in spans if "unreachable" in s.attributes.get("http.url", ""))

    assert span_400.status.status_code == StatusCode.ERROR
    assert span_500.status.status_code == StatusCode.ERROR
    assert span_err.status.status_code == StatusCode.ERROR


def test_phase3b_httpx_idempotency_prevents_duplicate_spans(mock_http_server):
    """Phase 3B Test 12: Multiple calls to instrument_httpx do not wrap transports multiple times or duplicate spans."""
    server_url, _ = mock_http_server
    cfg = Settings(
        SECRET_KEY="test-key",
        LAKEFS_ENDPOINT="http://localhost:8000",
        LAKEFS_ACCESS_KEY_ID="test",
        LAKEFS_SECRET_ACCESS_KEY="test",
        OTEL_ENABLED=True,
    )
    span_exporter = InMemorySpanExporter()
    initialize_telemetry(cfg=cfg, trace_exporter=span_exporter)

    # Double instrumentation
    res1 = instrument_httpx(cfg=cfg)
    res2 = instrument_httpx(cfg=cfg)
    assert res1 is True
    assert res2 is True

    with httpx.Client() as client:
        resp = client.get(f"{server_url}/test")
        assert resp.status_code == 200

    flush_telemetry()
    spans = span_exporter.get_finished_spans()
    get_spans = [s for s in spans if s.name == "GET"]
    assert len(get_spans) == 1, f"Expected exactly 1 GET span, found {len(get_spans)}"


def test_phase3_integrated_fastapi_sqlalchemy_httpx_trace_hierarchy(mock_http_server):
    """Phase 3 Integration: Verify full trace hierarchy across FastAPI -> SQLAlchemy DB -> HTTPX RayService call."""
    server_url, received_requests = mock_http_server
    cfg = Settings(
        SECRET_KEY="test-key",
        LAKEFS_ENDPOINT="http://localhost:8000",
        LAKEFS_ACCESS_KEY_ID="test",
        LAKEFS_SECRET_ACCESS_KEY="test",
        OTEL_ENABLED=True,
    )
    span_exporter = InMemorySpanExporter()
    initialize_telemetry(cfg=cfg, trace_exporter=span_exporter)
    instrument_fastapi_app(app, cfg=cfg)
    instrument_sqlalchemy_engine(engine, cfg=cfg)
    instrument_httpx(cfg=cfg)

    # Define an active parent span mimicking FastAPI serving a request
    tracer = get_tracer("sentinelml.integrated_test")
    with tracer.start_as_current_span("GET /api/test_integrated_pipeline") as root_span:
        # 1. Database operation (SQLAlchemy)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))

        # 2. Outgoing RayService HTTP call (HTTPX)
        with httpx.Client() as client:
            client.post(f"{server_url}/predict", json={"features": [1.0, 2.0, 3.0]})

    flush_telemetry()
    spans = span_exporter.get_finished_spans()

    root = next(s for s in spans if s.name == "GET /api/test_integrated_pipeline")
    sql_span = next(s for s in spans if "SELECT" in s.name)
    httpx_span = next(s for s in spans if s.name == "POST")

    # Verify both DB span and HTTPX span share root trace_id and have root span_id as parent
    assert sql_span.context.trace_id == root.context.trace_id
    assert sql_span.parent.span_id == root.context.span_id

    assert httpx_span.context.trace_id == root.context.trace_id
    assert httpx_span.parent.span_id == root.context.span_id

    # Verify W3C traceparent header sent downstream
    last_req = received_requests[-1]
    assert "traceparent" in last_req["headers"]
    traceparent = last_req["headers"]["traceparent"]
    assert format(root.context.trace_id, "032x") in traceparent
    assert format(httpx_span.context.span_id, "016x") in traceparent


# ==============================================================================
# Phase 4 — Application ML Business Spans & Bounded Metrics Tests
# ==============================================================================


def test_phase4_business_spans_disabled_when_otel_disabled():
    """Phase 4 Test 1: ML business spans and metrics safely no-op when OTEL_ENABLED is False."""
    cfg = Settings(
        SECRET_KEY="test-key",
        LAKEFS_ENDPOINT="http://localhost:8000",
        LAKEFS_ACCESS_KEY_ID="test",
        LAKEFS_SECRET_ACCESS_KEY="test",
        OTEL_ENABLED=False,
    )
    span_exporter = InMemorySpanExporter()
    reader = InMemoryMetricReader()
    initialize_telemetry(cfg=cfg, trace_exporter=span_exporter, metric_reader=reader)

    # Calling trace_ml_operation and metric recorders should not raise
    with trace_ml_operation("model.inference", attributes={"ml.operation": "model.inference"}):
        pass

    record_training_job_submitted(framework="custom", result="success")
    record_training_job_completed(status="SUCCEEDED")
    record_training_duration(duration_seconds=12.5)
    record_deployment_operation(operation="create", result="success")
    record_inference_request(result="success")
    record_inference_duration(duration_seconds=0.042)

    flush_telemetry()
    assert len(span_exporter.get_finished_spans()) == 0
    metric_data = reader.get_metrics_data()
    assert metric_data is None or len(metric_data.resource_metrics) == 0


def test_phase4_model_inference_span_and_metrics(mock_http_server, monkeypatch):
    """Phase 4 Test 2: model.inference span is created with attributes, child HTTPX span, and metrics."""
    server_url, _ = mock_http_server
    monkeypatch.setenv("RAY_SERVE_HTTP_ENDPOINT", f"{server_url}/predict")

    cfg = Settings(
        SECRET_KEY="test-key",
        LAKEFS_ENDPOINT="http://localhost:8000",
        LAKEFS_ACCESS_KEY_ID="test",
        LAKEFS_SECRET_ACCESS_KEY="test",
        OTEL_ENABLED=True,
    )
    span_exporter = InMemorySpanExporter()
    reader = InMemoryMetricReader()
    initialize_telemetry(cfg=cfg, trace_exporter=span_exporter, metric_reader=reader)
    instrument_httpx(cfg=cfg)

    import uuid
    from app.models.user import User
    from app.services.ml_ops.serving_service import ModelServingService

    user = User(
        id=uuid.uuid4(),
        username="mle_test",
        email="mle@test.local",
        password_hash="dummy",
        role="ml_engineer",
    )

    serving = ModelServingService()
    res = serving.perform_model_prediction(
        model_name_or_id="raysvc-test-inference",
        data={"dataframe_records": [{"f1": 1.0, "f2": 2.0}, {"f1": 3.0, "f2": 4.0}]},
        user=user,
    )
    assert res["predictions"] == [1, 0]
    assert res["model_name"] == "raysvc-test-inference"

    flush_telemetry()
    spans = span_exporter.get_finished_spans()

    # Find model.inference span
    inf_span = next((s for s in spans if s.name == "model.inference"), None)
    assert inf_span is not None, "Expected 'model.inference' span"
    assert inf_span.status.status_code != StatusCode.ERROR
    assert inf_span.attributes.get("ml.operation") == "model.inference"
    assert inf_span.attributes.get("ml.serving.target") == "rayservice"
    assert inf_span.attributes.get("ml.model.name") == "raysvc-test-inference"
    assert inf_span.attributes.get("ml.inference.batch_size") == 2

    # Verify sensitive data is NOT in span attributes
    for key in inf_span.attributes:
        assert key not in ("dataframe_records", "inputs", "predictions", "f1", "f2")

    # Find child HTTPX span
    httpx_span = next((s for s in spans if s.name == "POST"), None)
    assert httpx_span is not None, "Expected child HTTPX POST span"
    assert httpx_span.context.trace_id == inf_span.context.trace_id
    assert httpx_span.parent.span_id == inf_span.context.span_id

    # Verify metrics
    metrics = reader.get_metrics_data()
    assert metrics is not None
    metric_names = [m.name for rm in metrics.resource_metrics for sm in rm.scope_metrics for m in sm.metrics]
    assert "inference.requests" in metric_names
    assert "inference.duration" in metric_names


def test_phase4_model_inference_error_span_and_metrics():
    """Phase 4 Test 3: model.inference span captures error status and records error metric on failure."""
    cfg = Settings(
        SECRET_KEY="test-key",
        LAKEFS_ENDPOINT="http://localhost:8000",
        LAKEFS_ACCESS_KEY_ID="test",
        LAKEFS_SECRET_ACCESS_KEY="test",
        OTEL_ENABLED=True,
    )
    span_exporter = InMemorySpanExporter()
    reader = InMemoryMetricReader()
    initialize_telemetry(cfg=cfg, trace_exporter=span_exporter, metric_reader=reader)

    import uuid
    from fastapi import HTTPException
    from app.models.user import User
    from app.services.ml_ops.serving_service import ModelServingService

    user = User(
        id=uuid.uuid4(),
        username="mle_test",
        email="mle@test.local",
        password_hash="dummy",
        role="ml_engineer",
    )

    serving = ModelServingService()
    # Sending invalid data or non-existent model causes HTTPException
    with pytest.raises(HTTPException) as exc_info:
        serving.perform_model_prediction(
            model_name_or_id="non-existent-model",
            data={"unsupported_key": 123},
            user=user,
        )
    assert exc_info.value.status_code in (400, 503)

    flush_telemetry()
    spans = span_exporter.get_finished_spans()
    inf_span = next((s for s in spans if s.name == "model.inference"), None)
    assert inf_span is not None
    assert inf_span.status.status_code == StatusCode.ERROR

    # Check exception event on span
    events = [e.name for e in inf_span.events]
    assert "exception" in events

    # Verify error metric recorded
    metrics = reader.get_metrics_data()
    assert metrics is not None
    inf_req_metric = next(
        (m for rm in metrics.resource_metrics for sm in rm.scope_metrics for m in sm.metrics if m.name == "inference.requests"),
        None,
    )
    assert inf_req_metric is not None
    data_points = inf_req_metric.data.data_points
    assert any(dp.attributes.get("result") == "error" for dp in data_points)


def test_phase4_trace_hierarchy_fastapi_to_inference(mock_http_server, monkeypatch):
    """Phase 4 Test 4: Full 3-level trace hierarchy FastAPI Server Span -> ML Business Span -> HTTPX Outbound."""
    server_url, _ = mock_http_server
    monkeypatch.setenv("RAY_SERVE_HTTP_ENDPOINT", f"{server_url}/predict")

    cfg = Settings(
        SECRET_KEY="test-key",
        LAKEFS_ENDPOINT="http://localhost:8000",
        LAKEFS_ACCESS_KEY_ID="test",
        LAKEFS_SECRET_ACCESS_KEY="test",
        OTEL_ENABLED=True,
    )
    span_exporter = InMemorySpanExporter()
    initialize_telemetry(cfg=cfg, trace_exporter=span_exporter)
    instrument_httpx(cfg=cfg)

    import uuid
    from app.models.user import User
    from app.services.ml_ops.serving_service import ModelServingService

    user = User(
        id=uuid.uuid4(),
        username="mle_test",
        email="mle@test.local",
        password_hash="dummy",
        role="ml_engineer",
    )

    tracer = get_tracer("sentinelml.fastapi_mock")
    with tracer.start_as_current_span("POST /api/models/test/predict", kind=SpanKind.SERVER) as root_server_span:
        serving = ModelServingService()
        serving.perform_model_prediction(
            model_name_or_id="raysvc-hier-model",
            data={"dataframe_records": [{"x": 10.0}]},
            user=user,
        )

    flush_telemetry()
    spans = span_exporter.get_finished_spans()

    root = next(s for s in spans if s.name == "POST /api/models/test/predict")
    ml_span = next(s for s in spans if s.name == "model.inference")
    httpx_span = next(s for s in spans if s.name == "POST")

    # Verify 3-level hierarchy
    assert ml_span.context.trace_id == root.context.trace_id
    assert ml_span.parent.span_id == root.context.span_id

    assert httpx_span.context.trace_id == root.context.trace_id
    assert httpx_span.parent.span_id == ml_span.context.span_id


def test_phase4_training_submit_span_and_metrics():
    """Phase 4 Test 5: training.submit span encapsulates execution and records submitted metric."""
    cfg = Settings(
        SECRET_KEY="test-key",
        LAKEFS_ENDPOINT="http://localhost:8000",
        LAKEFS_ACCESS_KEY_ID="test",
        LAKEFS_SECRET_ACCESS_KEY="test",
        OTEL_ENABLED=True,
    )
    span_exporter = InMemorySpanExporter()
    reader = InMemoryMetricReader()
    initialize_telemetry(cfg=cfg, trace_exporter=span_exporter, metric_reader=reader)
    instrument_sqlalchemy_engine(engine, cfg=cfg)

    with trace_ml_operation("training.submit", attributes={"training.framework": "custom"}):
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        record_training_job_submitted(framework="custom", result="success")

    flush_telemetry()
    spans = span_exporter.get_finished_spans()

    submit_span = next((s for s in spans if s.name == "training.submit"), None)
    assert submit_span is not None
    assert submit_span.attributes.get("training.framework") == "custom"

    # Child DB span should be parented by training.submit
    db_span = next((s for s in spans if "SELECT" in s.name), None)
    assert db_span is not None
    assert db_span.context.trace_id == submit_span.context.trace_id
    assert db_span.parent.span_id == submit_span.context.span_id

    # Verify metric
    metrics = reader.get_metrics_data()
    train_metric = next(
        (m for rm in metrics.resource_metrics for sm in rm.scope_metrics for m in sm.metrics if m.name == "training.jobs.submitted"),
        None,
    )
    assert train_metric is not None
    dp = train_metric.data.data_points[0]
    assert dp.attributes.get("framework") == "custom"
    assert dp.attributes.get("result") == "success"
    assert dp.value == 1


def test_phase4_training_status_and_completion_metrics():
    """Phase 4 Test 6: training.status span and training completion counter/duration metrics."""
    cfg = Settings(
        SECRET_KEY="test-key",
        LAKEFS_ENDPOINT="http://localhost:8000",
        LAKEFS_ACCESS_KEY_ID="test",
        LAKEFS_SECRET_ACCESS_KEY="test",
        OTEL_ENABLED=True,
    )
    span_exporter = InMemorySpanExporter()
    reader = InMemoryMetricReader()
    initialize_telemetry(cfg=cfg, trace_exporter=span_exporter, metric_reader=reader)

    with trace_ml_operation("training.status"):
        pass

    record_training_job_completed(status="SUCCEEDED")
    record_training_duration(duration_seconds=128.5)

    flush_telemetry()
    spans = span_exporter.get_finished_spans()

    assert any(s.name == "training.status" for s in spans)

    metrics = reader.get_metrics_data()
    completed_m = next(
        (m for rm in metrics.resource_metrics for sm in rm.scope_metrics for m in sm.metrics if m.name == "training.jobs.completed"),
        None,
    )
    assert completed_m is not None
    assert completed_m.data.data_points[0].attributes.get("status") == "SUCCEEDED"

    dur_m = next(
        (m for rm in metrics.resource_metrics for sm in rm.scope_metrics for m in sm.metrics if m.name == "training.duration"),
        None,
    )
    assert dur_m is not None
    assert dur_m.data.data_points[0].count == 1
    assert dur_m.data.data_points[0].sum == 128.5


def test_phase4_deployment_create_and_management_spans_and_metrics():
    """Phase 4 Test 7: deployment.create and deployment.delete/update spans and deployment.operations metric."""
    cfg = Settings(
        SECRET_KEY="test-key",
        LAKEFS_ENDPOINT="http://localhost:8000",
        LAKEFS_ACCESS_KEY_ID="test",
        LAKEFS_SECRET_ACCESS_KEY="test",
        OTEL_ENABLED=True,
    )
    span_exporter = InMemorySpanExporter()
    reader = InMemoryMetricReader()
    initialize_telemetry(cfg=cfg, trace_exporter=span_exporter, metric_reader=reader)

    with trace_ml_operation("deployment.create", attributes={"deployment.environment": "staging"}):
        record_deployment_operation(operation="create", result="success")

    with trace_ml_operation("deployment.delete", attributes={"deployment.action": "stop"}):
        record_deployment_operation(operation="stop", result="success")

    flush_telemetry()
    spans = span_exporter.get_finished_spans()
    span_names = [s.name for s in spans]
    assert "deployment.create" in span_names
    assert "deployment.delete" in span_names

    metrics = reader.get_metrics_data()
    dep_m = next(
        (m for rm in metrics.resource_metrics for sm in rm.scope_metrics for m in sm.metrics if m.name == "deployment.operations"),
        None,
    )
    assert dep_m is not None
    assert len(dep_m.data.data_points) == 2
    ops = [(dp.attributes.get("operation"), dp.attributes.get("result")) for dp in dep_m.data.data_points]
    assert ("create", "success") in ops
    assert ("stop", "success") in ops


def test_phase4_dataset_business_spans():
    """Phase 4 Test 8: dataset.register and dataset.commit spans."""
    cfg = Settings(
        SECRET_KEY="test-key",
        LAKEFS_ENDPOINT="http://localhost:8000",
        LAKEFS_ACCESS_KEY_ID="test",
        LAKEFS_SECRET_ACCESS_KEY="test",
        OTEL_ENABLED=True,
    )
    span_exporter = InMemorySpanExporter()
    initialize_telemetry(cfg=cfg, trace_exporter=span_exporter)

    with trace_ml_operation("dataset.register"):
        pass

    with trace_ml_operation("dataset.commit"):
        pass

    flush_telemetry()
    spans = span_exporter.get_finished_spans()
    span_names = [s.name for s in spans]
    assert "dataset.register" in span_names
    assert "dataset.commit" in span_names


def test_phase4_high_cardinality_prohibition_in_metrics():
    """Phase 4 Test 9: Strictly prohibit high-cardinality attributes in ML metrics."""
    cfg = Settings(
        SECRET_KEY="test-key",
        LAKEFS_ENDPOINT="http://localhost:8000",
        LAKEFS_ACCESS_KEY_ID="test",
        LAKEFS_SECRET_ACCESS_KEY="test",
        OTEL_ENABLED=True,
    )
    reader = InMemoryMetricReader()
    initialize_telemetry(cfg=cfg, metric_reader=reader)

    # Exercise all metric functions
    record_training_job_submitted(framework="custom", result="success")
    record_training_job_completed(status="SUCCEEDED")
    record_training_duration(duration_seconds=75.0)
    record_deployment_operation(operation="create", result="success")
    record_inference_request(result="success")
    record_inference_duration(duration_seconds=0.035)

    metrics = reader.get_metrics_data()
    assert metrics is not None

    FORBIDDEN_KEYS = {
        "user_id", "user", "username", "request_id", "trace_id", "span_id",
        "job_id", "deployment_id", "model_name", "dataset_id", "sql",
        "query", "error_message", "detail", "client_ip", "path",
    }
    ALLOWED_KEYS = {"framework", "result", "status", "operation"}

    for rm in metrics.resource_metrics:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                for dp in metric.data.data_points:
                    dp_keys = set(dp.attributes.keys())
                    forbidden_found = dp_keys.intersection(FORBIDDEN_KEYS)
                    assert not forbidden_found, (
                        f"Metric '{metric.name}' contains forbidden high-cardinality keys: {forbidden_found}"
                    )
                    unallowed = dp_keys - ALLOWED_KEYS
                    assert not unallowed, (
                        f"Metric '{metric.name}' contains unexpected attribute keys: {unallowed}"
                    )


def test_phase4_sensitive_payload_exclusion_in_spans(mock_http_server, monkeypatch):
    """Phase 4 Test 10: Verify sensitive inputs and payloads are never recorded in ML span attributes."""
    server_url, _ = mock_http_server
    monkeypatch.setenv("RAY_SERVE_HTTP_ENDPOINT", f"{server_url}/predict")

    cfg = Settings(
        SECRET_KEY="test-key",
        LAKEFS_ENDPOINT="http://localhost:8000",
        LAKEFS_ACCESS_KEY_ID="test",
        LAKEFS_SECRET_ACCESS_KEY="test",
        OTEL_ENABLED=True,
    )
    span_exporter = InMemorySpanExporter()
    initialize_telemetry(cfg=cfg, trace_exporter=span_exporter)
    instrument_httpx(cfg=cfg)

    import uuid
    from app.models.user import User
    from app.services.ml_ops.serving_service import ModelServingService

    user = User(
        id=uuid.uuid4(),
        username="mle_sec_test",
        email="sec@test.local",
        password_hash="dummy",
        role="ml_engineer",
    )

    sensitive_payload = {
        "dataframe_records": [
            {
                "raw_ssn": "000-12-3456",
                "api_secret": "my-ultra-secret-api-key",
                "val": 42.0,
            }
        ]
    }

    serving = ModelServingService()
    serving.perform_model_prediction(
        model_name_or_id="raysvc-sec-test",
        data=sensitive_payload,
        user=user,
    )

    flush_telemetry()
    spans = span_exporter.get_finished_spans()
    assert len(spans) >= 2  # model.inference and child POST

    for span in spans:
        # Check attribute keys and values for secrets
        for attr_key, attr_val in span.attributes.items():
            assert "ssn" not in attr_key.lower()
            assert "secret" not in attr_key.lower()
            assert "000-12-3456" not in str(attr_val)
            assert "my-ultra-secret-api-key" not in str(attr_val)


# ==============================================================================
# Phase 6A — RayService Distributed Tracing & W3C Trace Context Propagation Tests
# ==============================================================================


class _DummyServingModel:
    """Mock ML model for testing RayService inference execution and error handling."""

    def __init__(self, should_fail: bool = False):
        self.should_fail = should_fail

    def predict(self, df):
        if self.should_fail:
            raise RuntimeError("Underlying model matrix computation failed")
        return [0.87, 0.13]


def _build_mock_serve_request(payload: dict, headers: dict = None):
    """Builds a Starlette/FastAPI Request object simulating an incoming HTTP request to RayService."""
    import json
    from starlette.requests import Request

    raw_headers = []
    if headers:
        for k, v in headers.items():
            raw_headers.append((k.lower().encode("utf-8"), str(v).encode("utf-8")))
    if not any(k == b"content-type" for k, _ in raw_headers):
        raw_headers.append((b"content-type", b"application/json"))

    body_bytes = json.dumps(payload).encode("utf-8")

    async def receive():
        return {"type": "http.request", "body": body_bytes, "more_body": False}

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/predict",
        "headers": raw_headers,
    }
    return Request(scope, receive)


def test_phase6a_rayservice_trace_context_extraction():
    """Phase 6A Test A: RayService extracts incoming W3C traceparent and creates child span with matching trace ID."""
    import asyncio
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.resources import Resource
    from app.services.ml_ops.serve_wrapper import ModelServingDeployment

    tp = TracerProvider(resource=Resource.create({"service.name": "sentinelml-rayservice"}))
    exporter = InMemorySpanExporter()
    tp.add_span_processor(SimpleSpanProcessor(exporter))

    orig_cls = ModelServingDeployment.func_or_class.__wrapped__
    deployment = orig_cls(tracer_provider=tp)
    deployment.model = _DummyServingModel()

    incoming_trace_id = "4bf92f3577b34da6a3ce929d0e0e4736"
    incoming_parent_id = "00f067aa0ba902b7"
    w3c_traceparent = f"00-{incoming_trace_id}-{incoming_parent_id}-01"

    req = _build_mock_serve_request(
        payload={"dataframe_records": [{"feature_a": 1.0, "feature_b": 2.0}]},
        headers={"traceparent": w3c_traceparent},
    )

    res = asyncio.run(deployment.predict(req))
    assert res["status"] == "success"

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    ray_span = spans[0]

    assert format(ray_span.context.trace_id, "032x") == incoming_trace_id
    assert format(ray_span.parent.span_id, "016x") == incoming_parent_id


def test_phase6a_rayservice_span_creation_and_attributes():
    """Phase 6A Test B: Verify rayserve.inference span is created with bounded attributes and correct service identity."""
    import asyncio
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.resources import Resource
    from app.services.ml_ops.serve_wrapper import ModelServingDeployment

    tp = TracerProvider(resource=Resource.create({"service.name": "sentinelml-rayservice"}))
    exporter = InMemorySpanExporter()
    tp.add_span_processor(SimpleSpanProcessor(exporter))

    orig_cls = ModelServingDeployment.func_or_class.__wrapped__
    deployment = orig_cls(tracer_provider=tp)
    deployment.model = _DummyServingModel()

    req = _build_mock_serve_request(
        payload={"dataframe_records": [{"feature_a": 10.5}]},
        headers={"traceparent": "00-5bb92f3577b34da6a3ce929d0e0e4736-11f067aa0ba902b7-01"},
    )

    asyncio.run(deployment.predict(req))

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    ray_span = spans[0]

    assert ray_span.name == "rayserve.inference"
    assert ray_span.kind == SpanKind.SERVER
    assert ray_span.attributes.get("ml.operation") == "model.inference"
    assert ray_span.attributes.get("ml.serving.target") == "rayservice"
    assert ray_span.resource.attributes.get("service.name") == "sentinelml-rayservice"


def test_phase6a_rayservice_inference_success_status_ok():
    """Phase 6A Test C: Successful model inference marks the rayserve span with StatusCode.OK."""
    import asyncio
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.resources import Resource
    from app.services.ml_ops.serve_wrapper import ModelServingDeployment

    tp = TracerProvider(resource=Resource.create({"service.name": "sentinelml-rayservice"}))
    exporter = InMemorySpanExporter()
    tp.add_span_processor(SimpleSpanProcessor(exporter))

    orig_cls = ModelServingDeployment.func_or_class.__wrapped__
    deployment = orig_cls(tracer_provider=tp)
    deployment.model = _DummyServingModel(should_fail=False)

    req = _build_mock_serve_request(payload={"inputs": [1.0, 2.0, 3.0]})
    res = asyncio.run(deployment.predict(req))

    assert res["status"] == "success"
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].status.status_code == StatusCode.OK


def test_phase6a_rayservice_inference_failure_records_error_and_reraises():
    """Phase 6A Test D: Inference exceptions record exception on span, set StatusCode.ERROR, and re-raise HTTPException."""
    import asyncio
    from fastapi import HTTPException
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.resources import Resource
    from app.services.ml_ops.serve_wrapper import ModelServingDeployment

    tp = TracerProvider(resource=Resource.create({"service.name": "sentinelml-rayservice"}))
    exporter = InMemorySpanExporter()
    tp.add_span_processor(SimpleSpanProcessor(exporter))

    orig_cls = ModelServingDeployment.func_or_class.__wrapped__
    deployment = orig_cls(tracer_provider=tp)
    deployment.model = _DummyServingModel(should_fail=True)

    req = _build_mock_serve_request(payload={"dataframe_records": [{"x": 1}]})

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(deployment.predict(req))

    assert exc_info.value.status_code == 400
    assert "Inference error" in exc_info.value.detail

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.status.status_code == StatusCode.ERROR
    assert any(event.name == "exception" for event in span.events)


def test_phase6a_rayservice_strict_privacy_payload_exclusion():
    """Phase 6A Test E: RayService span never records input features, predictions, passwords, or auth headers."""
    import asyncio
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.resources import Resource
    from app.services.ml_ops.serve_wrapper import ModelServingDeployment

    tp = TracerProvider(resource=Resource.create({"service.name": "sentinelml-rayservice"}))
    exporter = InMemorySpanExporter()
    tp.add_span_processor(SimpleSpanProcessor(exporter))

    orig_cls = ModelServingDeployment.func_or_class.__wrapped__
    deployment = orig_cls(tracer_provider=tp)
    deployment.model = _DummyServingModel()

    sensitive_ssn = "999-00-1111"
    sensitive_token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.supersecret"
    sensitive_cookie = "session_cookie_secret_value"

    req = _build_mock_serve_request(
        payload={
            "dataframe_records": [
                {"patient_ssn": sensitive_ssn, "blood_type": "O-", "age": 45}
            ]
        },
        headers={
            "authorization": f"Bearer {sensitive_token}",
            "cookie": sensitive_cookie,
            "traceparent": "00-6cc92f3577b34da6a3ce929d0e0e4736-22f067aa0ba902b7-01",
        },
    )

    asyncio.run(deployment.predict(req))

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]

    assert "ml.model.uri" not in span.attributes

    for key, val in span.attributes.items():
        assert sensitive_ssn not in str(val)
        assert sensitive_token not in str(val)
        assert sensitive_cookie not in str(val)
        assert "ssn" not in key.lower()
        assert "authorization" not in key.lower()
        assert "cookie" not in key.lower()
        assert "patient" not in key.lower()


def test_phase6a_rayservice_telemetry_disabled_works_normally(monkeypatch):
    """Phase 6A Test F: When OTEL_ENABLED=false, RayService performs inference normally without tracing."""
    import asyncio
    from app.services.ml_ops.serve_wrapper import ModelServingDeployment

    monkeypatch.setenv("OTEL_ENABLED", "false")

    orig_cls = ModelServingDeployment.func_or_class.__wrapped__
    deployment = orig_cls()
    deployment.model = _DummyServingModel()

    assert deployment.tracer is None

    req = _build_mock_serve_request(payload={"dataframe_records": [{"a": 1, "b": 2}]})
    res = asyncio.run(deployment.predict(req))

    assert res["status"] == "success"
    assert res["predictions"] == [0.87, 0.13]


def test_phase6a_rayservice_collector_unavailable_resilience(monkeypatch):
    """Phase 6A Test G: If collector is unreachable, RayService inference continues uninterrupted."""
    import asyncio
    from app.services.ml_ops.serve_wrapper import ModelServingDeployment

    monkeypatch.setenv("OTEL_ENABLED", "true")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:9998")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TIMEOUT_SECONDS", "1")

    orig_cls = ModelServingDeployment.func_or_class.__wrapped__
    deployment = orig_cls()
    deployment.model = _DummyServingModel()

    req = _build_mock_serve_request(payload={"dataframe_records": [{"a": 100}]})
    res = asyncio.run(deployment.predict(req))

    assert res["status"] == "success"
    assert res["predictions"] == [0.87, 0.13]


def test_phase6a_fastapi_to_rayservice_distributed_trace_continuity(monkeypatch):
    """
    Phase 6A Test H: Full distributed trace continuity verification across FastAPI and RayService.
    FastAPI (HTTP server/business span) -> HTTPX (client span + W3C header) -> RayService (server span).
    All spans must share the EXACT same trace ID and correct parent-child relationship.
    """
    import asyncio
    import json
    import http.server
    import threading
    import uuid
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.resources import Resource
    from app.models.user import User
    from app.services.ml_ops.serving_service import ModelServingService
    from app.services.ml_ops.serve_wrapper import ModelServingDeployment

    # 1. Setup FastAPI telemetry with exporter1
    fastapi_exporter = InMemorySpanExporter()
    fastapi_cfg = Settings(
        SECRET_KEY="test-key",
        LAKEFS_ENDPOINT="http://localhost:8000",
        LAKEFS_ACCESS_KEY_ID="test",
        LAKEFS_SECRET_ACCESS_KEY="test",
        OTEL_ENABLED=True,
    )
    initialize_telemetry(cfg=fastapi_cfg, trace_exporter=fastapi_exporter)
    instrument_httpx(cfg=fastapi_cfg)

    # 2. Setup RayService deployment with exporter2
    rayservice_exporter = InMemorySpanExporter()
    ray_tp = TracerProvider(resource=Resource.create({"service.name": "sentinelml-rayservice"}))
    ray_tp.add_span_processor(SimpleSpanProcessor(rayservice_exporter))

    orig_cls = ModelServingDeployment.func_or_class.__wrapped__
    ray_deployment = orig_cls(tracer_provider=ray_tp)
    ray_deployment.model = _DummyServingModel()

    # 3. Spin up local mock server executing the RayService deployment
    class RayMockHandler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length > 0 else b"{}"
            payload = json.loads(body.decode("utf-8"))

            req = _build_mock_serve_request(payload, headers=dict(self.headers))
            result = asyncio.run(ray_deployment.predict(req))

            res_bytes = json.dumps(result).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(res_bytes)))
            self.end_headers()
            self.wfile.write(res_bytes)

        def log_message(self, format, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), RayMockHandler)
    server_port = server.server_address[1]
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    try:
        monkeypatch.setenv("RAY_SERVE_HTTP_ENDPOINT", f"http://127.0.0.1:{server_port}/predict")

        user = User(
            id=uuid.uuid4(),
            username="dist_trace_tester",
            email="trace@test.local",
            password_hash="dummy",
            role="ml_engineer",
        )

        service = ModelServingService()
        result = service.perform_model_prediction(
            model_name_or_id="raysvc-distributed-test",
            data={"dataframe_records": [{"val": 1.23}]},
            user=user,
        )

        assert result["model_name"] == "raysvc-distributed-test"
        assert result["predictions"] == [0.87, 0.13]

        flush_telemetry()

        # Check FastAPI spans
        fastapi_spans = fastapi_exporter.get_finished_spans()
        assert len(fastapi_spans) >= 2

        business_span = next(s for s in fastapi_spans if s.name == "model.inference")
        httpx_span = next(s for s in fastapi_spans if s.name == "POST")

        # Check RayService span
        ray_spans = rayservice_exporter.get_finished_spans()
        assert len(ray_spans) >= 1
        ray_span = next(s for s in ray_spans if s.name == "rayserve.inference")

        # CRITICAL ASSERTIONS: Trace continuity across distributed boundaries
        fastapi_trace_id = format(business_span.context.trace_id, "032x")
        httpx_trace_id = format(httpx_span.context.trace_id, "032x")
        rayservice_trace_id = format(ray_span.context.trace_id, "032x")

        assert fastapi_trace_id == httpx_trace_id == rayservice_trace_id, (
            f"Trace ID mismatch: FastAPI={fastapi_trace_id}, HTTPX={httpx_trace_id}, Ray={rayservice_trace_id}"
        )

        # Hierarchy assertion:
        # model.inference -> HTTPX client (parent: model.inference) -> rayserve.inference (parent: HTTPX client)
        assert httpx_span.parent.span_id == business_span.context.span_id
        assert ray_span.parent.span_id == httpx_span.context.span_id
        assert ray_span.kind == SpanKind.SERVER

    finally:
        server.shutdown()
        server.server_close()



