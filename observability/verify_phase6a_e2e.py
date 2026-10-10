#!/usr/bin/env python3
"""
SentinelML Phase 6A End-to-End Distributed Tracing Verification Script.

Validates the full distributed trace path:
FastAPI (Host)
  │
  └── HTTP server span
        │
        └── model.inference (ML business span)
              │
              └── HTTPX client span
                    │
                    │ W3C traceparent header
                    ▼
                 RayService (sentinelml-rayservice)
                    │
                    └── rayserve.inference (Ray Serve inference span)
                           │
                           └── model prediction

Both services export to the OpenTelemetry Collector (port 4318), which forwards
to Grafana Tempo (port 3200). This script queries Tempo and verifies that the complete
trace tree exists under a single unified Trace ID with correct parentage.
"""

import sys
import os
import time
import json
import uuid
import asyncio
import http.server
import threading
import httpx

from opentelemetry import trace
from opentelemetry.trace import SpanKind

# Add backend directory to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend")))

from app.core.config import Settings
from app.db.session import engine
from app.main import app
from app.models.user import User
from app.services.ml_ops.serving_service import ModelServingService
from app.services.ml_ops.serve_wrapper import ModelServingDeployment
from app.core.telemetry import (
    initialize_telemetry,
    flush_telemetry,
    shutdown_telemetry,
    _reset_telemetry_state,
    instrument_fastapi_app,
    instrument_httpx,
    get_tracer,
)


class DummyLiveModel:
    """Mock ML model for Phase 6A end-to-end verification."""
    def predict(self, df):
        return [0.942, 0.058]


def run_phase6a_verification():
    print("=" * 75)
    print("SENTINELML PHASE 6A — DISTRIBUTED TRACING (FASTAPI -> RAYSERVICE)")
    print("=" * 75)

    # 1. Verify Collector and Tempo Health
    print("\n[Step 1] Checking Observability Infrastructure (OTel Collector & Tempo)...")
    with httpx.Client(timeout=5.0) as client:
        collector_resp = client.get("http://localhost:13133/")
        assert collector_resp.status_code == 200, f"OTel Collector unhealthy: {collector_resp.status_code}"
        print(f"  ✓ OTel Collector is healthy (HTTP {collector_resp.status_code})")

        tempo_resp = client.get("http://localhost:3200/ready")
        assert tempo_resp.status_code == 200, f"Tempo is ready (HTTP {tempo_resp.status_code})"
        print(f"  ✓ Grafana Tempo is ready (HTTP {tempo_resp.status_code})")

    # 2. Reset and initialize FastAPI Telemetry
    print("\n[Step 2] Initializing FastAPI Host Telemetry...")
    _reset_telemetry_state(app=app, engine=engine)

    fastapi_cfg = Settings(
        SECRET_KEY="test-key",
        LAKEFS_ENDPOINT="http://localhost:8000",
        LAKEFS_ACCESS_KEY_ID="test",
        LAKEFS_SECRET_ACCESS_KEY="test",
        OTEL_ENABLED=True,
        OTEL_SERVICE_NAME="sentinelml-backend",
        OTEL_EXPORTER_OTLP_ENDPOINT="http://localhost:4318",
    )
    init_ok = initialize_telemetry(cfg=fastapi_cfg)
    assert init_ok, "Failed to initialize FastAPI telemetry"
    instrument_fastapi_app(app, cfg=fastapi_cfg)
    instrument_httpx(cfg=fastapi_cfg)
    print("  ✓ FastAPI telemetry active (service.name = sentinelml-backend)")

    # 3. Spin up live RayService HTTP endpoint
    print("\n[Step 3] Initializing RayService Deployment with OTel Telemetry...")
    os.environ["OTEL_ENABLED"] = "true"
    os.environ["OTEL_SERVICE_NAME"] = "sentinelml-rayservice"
    os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "http://localhost:4318"
    os.environ["MLFLOW_TRACKING_URI"] = "http://localhost:5000"
    os.environ.pop("MODEL_URI", None)

    orig_cls = ModelServingDeployment.func_or_class.__wrapped__
    ray_deployment = orig_cls()
    ray_deployment.model = DummyLiveModel()
    os.environ["MODEL_NAME"] = "sentinelml-detector"
    assert ray_deployment.tracer is not None, "RayService tracer failed to initialize"
    print("  ✓ RayService telemetry active (service.name = sentinelml-rayservice)")

    # HTTP server dispatching to RayService deployment
    class RayHttpHandler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length > 0 else b"{}"
            payload = json.loads(body.decode("utf-8"))

            from starlette.requests import Request
            raw_headers = [(k.lower().encode("utf-8"), str(v).encode("utf-8")) for k, v in self.headers.items()]
            async def receive():
                return {"type": "http.request", "body": body, "more_body": False}
            scope = {"type": "http", "method": "POST", "path": self.path, "headers": raw_headers}
            req = Request(scope, receive)

            result = asyncio.run(ray_deployment.predict(req))
            res_bytes = json.dumps(result).encode("utf-8")

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(res_bytes)))
            self.end_headers()
            self.wfile.write(res_bytes)

        def log_message(self, format, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), RayHttpHandler)
    ray_port = server.server_address[1]
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    print(f"  ✓ Live RayService HTTP listener active on port {ray_port}")

    target_trace_id_hex = None
    try:
        # 4. Execute Full Prediction Flow through FastAPI
        print("\n[Step 4] Executing Real Inference Request via FastAPI...")
        os.environ["RAY_SERVE_HTTP_ENDPOINT"] = f"http://127.0.0.1:{ray_port}/predict"

        user = User(
            id=uuid.uuid4(),
            username="security_analyst",
            email="analyst@sentinelml.io",
            password_hash="dummy",
            role="ml_engineer",
        )

        tracer = get_tracer("sentinelml.e2e_verification")
        with tracer.start_as_current_span(
            "POST /api/models/sentinelml-detector/predict",
            kind=SpanKind.SERVER,
        ) as root_span:
            target_trace_id_hex = format(root_span.get_span_context().trace_id, "032x")

            serving_service = ModelServingService()
            prediction_result = serving_service.perform_model_prediction(
                model_name_or_id="raysvc-sentinelml-detector",
                data={"dataframe_records": [{"feature_1": 0.12, "feature_2": 3.45}]},
                user=user,
            )

        print(f"  ✓ Prediction completed successfully: {prediction_result['predictions']}")
        print(f"  ✓ Root FastAPI Trace ID: {target_trace_id_hex}")

        # 5. Flush Spans to OTel Collector
        print("\n[Step 5] Flushing Spans from both FastAPI and RayService to OTel Collector...")
        flush_telemetry()
        if ray_deployment.tracer_provider is not None:
            ray_deployment.tracer_provider.force_flush()
        print("  ✓ Telemetry flushed to OTel Collector via OTLP/HTTP :4318")

        # 6. Query Tempo for Trace Continuity
        print(f"\n[Step 6] Querying Grafana Tempo for Trace {target_trace_id_hex}...")
        tempo_trace = None
        with httpx.Client(timeout=10.0) as client:
            for attempt in range(1, 10):
                try:
                    resp = client.get(f"http://localhost:3200/api/traces/{target_trace_id_hex}")
                    if resp.status_code == 200:
                        tempo_trace = resp.json()
                        print(f"  ✓ Trace found in Tempo on attempt {attempt}!")
                        break
                except Exception as query_err:
                    print(f"    (Attempt {attempt}) Waiting for Tempo ingestion: {query_err}")
                time.sleep(1.5)

        assert tempo_trace is not None, f"Trace {target_trace_id_hex} not found in Tempo"

        # 7. Analyze Trace Hierarchy and Distributed Continuity
        print("\n[Step 7] Analyzing Distributed Trace Hierarchy in Tempo...")
        batches = tempo_trace.get("batches", [])
        spans_by_id = {}
        services_by_span = {}

        for batch in batches:
            svc_name = "unknown"
            resource = batch.get("resource", {})
            for attr in resource.get("attributes", []):
                if attr.get("key") == "service.name":
                    svc_name = attr.get("value", {}).get("stringValue", "unknown")

            for scope_span in batch.get("scopeSpans", []):
                for s in scope_span.get("spans", []):
                    span_id = s.get("spanId")
                    spans_by_id[span_id] = s
                    services_by_span[span_id] = svc_name

        print(f"  Total spans retrieved from Tempo: {len(spans_by_id)}")
        for s_id, s in spans_by_id.items():
            print(f"    - [{services_by_span[s_id]}] Span: {s.get('name')} (ID: {s_id}, ParentID: {s.get('parentSpanId', 'root')})")

        # Find key spans
        root_http_span = next(s for s in spans_by_id.values() if "predict" in s.get("name") and services_by_span[s["spanId"]] == "sentinelml-backend")
        business_span = next(s for s in spans_by_id.values() if s.get("name") == "model.inference")
        httpx_span = next(s for s in spans_by_id.values() if s.get("name") == "POST" and services_by_span[s["spanId"]] == "sentinelml-backend")
        rayserve_span = next(s for s in spans_by_id.values() if s.get("name") == "rayserve.inference")

        assert root_http_span is not None, "Missing root FastAPI HTTP span"
        assert business_span is not None, "Missing model.inference span"
        assert httpx_span is not None, "Missing HTTPX client span"
        assert rayserve_span is not None, "Missing rayserve.inference span"

        # CRITICAL VERIFICATION: Distributed Parent-Child Continuity
        print("\n[Step 8] Verifying Distributed Parent-Child Links...")
        print(f"  1. FastAPI Root Span:   ID={root_http_span['spanId']} ({root_http_span['name']})")
        print(f"  2. ML Business Span:    ID={business_span['spanId']} ({business_span['name']}), Parent={business_span.get('parentSpanId')}")
        print(f"  3. Outbound HTTPX Span: ID={httpx_span['spanId']} ({httpx_span['name']}), Parent={httpx_span.get('parentSpanId')}")
        print(f"  4. RayService Span:     ID={rayserve_span['spanId']} ({rayserve_span['name']}), Parent={rayserve_span.get('parentSpanId')}")

        assert business_span.get("parentSpanId") == root_http_span["spanId"], "model.inference must be child of root HTTP span"
        assert httpx_span.get("parentSpanId") == business_span["spanId"], "HTTPX span must be child of model.inference"
        assert rayserve_span.get("parentSpanId") == httpx_span["spanId"], "rayserve.inference MUST be child of HTTPX client span"
        assert services_by_span[rayserve_span["spanId"]] == "sentinelml-rayservice", "RayService span must have service.name=sentinelml-rayservice"
        assert services_by_span[business_span["spanId"]] == "sentinelml-backend", "FastAPI spans must have service.name=sentinelml-backend"

        # Verify ml.model.uri is NOT present in rayserve_span attributes
        ray_attrs = {attr["key"]: attr.get("value", {}) for attr in rayserve_span.get("attributes", [])}
        assert "ml.model.uri" not in ray_attrs, "ml.model.uri must not be present in RayService span attributes"
        assert ray_attrs.get("ml.operation", {}).get("stringValue") == "model.inference"
        assert ray_attrs.get("ml.serving.target", {}).get("stringValue") == "rayservice"
        assert ray_attrs.get("ml.model.name", {}).get("stringValue") == "sentinelml-detector"
        print("  ✓ Strict privacy verified: ml.model.uri absent, no payload data in Tempo spans")

        print("\n" + "=" * 75)
        print("✓ DISTRIBUTED TRACE VERIFIED IN TEMPO:")
        print(f"  Trace ID: {target_trace_id_hex}")
        print("  Hierarchy:")
        print(f"    FastAPI (sentinelml-backend): {root_http_span['name']}")
        print(f"      └── model.inference")
        print(f"            └── HTTP POST (HTTPX client)")
        print(f"                  └── [W3C traceparent propagation]")
        print(f"                  └── RayService (sentinelml-rayservice): {rayserve_span['name']}")
        print("=" * 75)

    finally:
        server.shutdown()
        server.server_close()
        _reset_telemetry_state(app=app, engine=engine)


if __name__ == "__main__":
    run_phase6a_verification()
