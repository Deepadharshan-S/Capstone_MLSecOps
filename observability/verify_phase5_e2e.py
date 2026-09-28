#!/usr/bin/env python3
"""
SentinelML Phase 5 End-to-End Observability Stack Verification Script.

Validates the complete path:
FastAPI Application Telemetry -> OTel Collector (OTLP/HTTP:4318)
    ├── Tempo (Traces :3200)
    ├── Prometheus (Metrics :9090)
    └── Grafana (Unified UI :3000)
"""

import sys
import os
import time
import json
import httpx
from opentelemetry import trace
from opentelemetry.trace import SpanKind

# Add backend directory to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend")))

from app.core.config import Settings
from app.db.session import engine
from app.main import app
from app.core.telemetry import (
    initialize_telemetry,
    flush_telemetry,
    shutdown_telemetry,
    _reset_telemetry_state,
    instrument_fastapi_app,
    instrument_sqlalchemy_engine,
    instrument_httpx,
    get_tracer,
    trace_ml_operation,
    record_training_job_submitted,
    record_training_job_completed,
    record_training_duration,
    record_deployment_operation,
    record_inference_request,
    record_inference_duration,
)


def run_e2e_verification():
    print("=" * 70)
    print("SENTINELML PHASE 5 — END-TO-END OBSERVABILITY STACK VERIFICATION")
    print("=" * 70)

    # 1. Verify Collector, Prometheus, Tempo, Loki, Grafana Health
    print("\n[Step 1] Checking Observability Infrastructure Services...")
    services = {
        "OTel Collector Health": "http://localhost:13133/",
        "Prometheus Ready": "http://localhost:9090/-/ready",
        "Tempo Ready": "http://localhost:3200/ready",
        "Loki Ready": "http://localhost:3100/ready",
        "Grafana API": "http://localhost:3000/api/health",
    }
    with httpx.Client(timeout=5.0) as client:
        for name, url in services.items():
            resp = client.get(url)
            print(f"  ✓ {name}: HTTP {resp.status_code}")
            assert resp.status_code == 200, f"Service {name} unhealthy: {resp.status_code}"

    # 2. Reset telemetry & configure with real OTLP/HTTP endpoint
    print("\n[Step 2] Initializing Application Telemetry pointing to http://localhost:4318...")
    _reset_telemetry_state(app=app, engine=engine)

    cfg = Settings(
        SECRET_KEY="test-key",
        LAKEFS_ENDPOINT="http://localhost:8000",
        LAKEFS_ACCESS_KEY_ID="test",
        LAKEFS_SECRET_ACCESS_KEY="test",
        OTEL_ENABLED=True,
        OTEL_EXPORTER_OTLP_ENDPOINT="http://localhost:4318",
        OTEL_METRICS_EXPORT_INTERVAL_MS=1000,
    )

    init_success = initialize_telemetry(cfg=cfg)
    assert init_success, "Failed to initialize telemetry with live OTLP exporter"
    instrument_fastapi_app(app, cfg=cfg)
    instrument_sqlalchemy_engine(engine, cfg=cfg)
    instrument_httpx(cfg=cfg)
    print("  ✓ Telemetry initialized and instrumented (FastAPI, SQLAlchemy, HTTPX)")

    # 3. Generate Trace Hierarchy & Bounded Metrics
    print("\n[Step 3] Generating Application ML Trace and Bounded Metrics...")
    tracer = get_tracer("sentinelml.e2e_verification")
    trace_id_hex = None

    with tracer.start_as_current_span(
        "POST /api/models/phase5-verification-model/predict",
        kind=SpanKind.SERVER,
    ) as server_span:
        trace_id = server_span.get_span_context().trace_id
        trace_id_hex = format(trace_id, "032x")

        with trace_ml_operation(
            "model.inference",
            attributes={
                "ml.operation": "model.inference",
                "ml.serving.target": "rayservice",
                "ml.model.name": "phase5-verification-model",
                "ml.inference.batch_size": 2,
            },
        ):
            # Outbound call to exercise child HTTPX span & context propagation
            with httpx.Client() as http_client:
                try:
                    http_client.get("http://localhost:13133/")
                except Exception:
                    pass

        record_inference_request(result="success")
        record_inference_duration(0.048)

    # Record additional ML business metrics
    record_training_job_submitted(framework="custom", result="success")
    record_training_job_completed(status="SUCCEEDED")
    record_training_duration(112.5)
    record_deployment_operation(operation="create", result="success")

    print(f"  ✓ Emitted 3-level Trace [Trace ID: {trace_id_hex}]")
    print("  ✓ Emitted ML metrics (inference.requests, training.jobs.submitted, etc.)")

    # 4. Flush Telemetry to OTel Collector
    print("\n[Step 4] Flushing Telemetry to OTel Collector via OTLP/HTTP...")
    flushed = flush_telemetry()
    assert flushed, "Telemetry flush failed"
    print("  ✓ Telemetry flushed to OTel Collector successfully")

    # 5. Wait for Prometheus scrape cycle
    print("\n[Step 5] Waiting 8s for Prometheus to scrape OTel Collector metrics (5s interval)...")
    time.sleep(8)

    # 6. Verify Trace in Tempo
    print(f"\n[Step 6] Querying Tempo for Trace {trace_id_hex}...")
    tempo_found = False
    with httpx.Client(timeout=10.0) as client:
        # Retry up to 5 times for Tempo ingestion to flush
        for attempt in range(5):
            try:
                tempo_resp = client.get(f"http://localhost:3200/api/traces/{trace_id_hex}")
                if tempo_resp.status_code == 200:
                    tempo_found = True
                    print(f"  ✓ Trace found in Tempo! HTTP {tempo_resp.status_code}")
                    print(f"    Payload size: {len(tempo_resp.content)} bytes")
                    break
            except Exception as e:
                print(f"    Notice during Tempo query: {e}")
            time.sleep(2)

    assert tempo_found, f"Trace {trace_id_hex} was not found in Tempo!"

    # 7. Verify Metrics in Prometheus
    print("\n[Step 7] Querying Prometheus for SentinelML Metrics...")
    expected_metrics = [
        "inference_requests_total",
        "training_jobs_submitted_total",
        "training_jobs_completed_total",
        "deployment_operations_total",
    ]
    with httpx.Client(timeout=10.0) as client:
        for metric in expected_metrics:
            p_resp = client.get(f"http://localhost:9090/api/v1/query?query={metric}")
            assert p_resp.status_code == 200, f"Prometheus query failed for {metric}"
            data = p_resp.json()
            results = data.get("data", {}).get("result", [])
            print(f"  ✓ Metric '{metric}' in Prometheus: {len(results)} series found")
            assert len(results) > 0, f"Metric '{metric}' returned no results in Prometheus"
            for res in results:
                val = res.get("value", [None, None])[1]
                print(f"    -> Labels: {res.get('metric', {})} | Value: {val}")

    # 8. Verify Grafana Datasource Queries
    print("\n[Step 8] Verifying Grafana can query Prometheus and Tempo datasources...")
    grafana_user = os.getenv("GRAFANA_ADMIN_USER", "admin")
    grafana_password = os.getenv("GRAFANA_ADMIN_PASSWORD")
    auth_credentials = (grafana_user, grafana_password) if grafana_password else None
    with httpx.Client(timeout=10.0, auth=auth_credentials) as client:
        # Check Prometheus proxy through Grafana
        ds_resp = client.get("http://localhost:3000/api/datasources")
        assert ds_resp.status_code == 200
        datasources = ds_resp.json()
        prom_uid = next(ds["uid"] for ds in datasources if ds["type"] == "prometheus")
        tempo_uid = next(ds["uid"] for ds in datasources if ds["type"] == "tempo")

        # Query Prometheus via Grafana
        g_prom_resp = client.get(
            f"http://localhost:3000/api/datasources/proxy/uid/{prom_uid}/api/v1/query?query=inference_requests_total"
        )
        assert g_prom_resp.status_code == 200
        print(f"  ✓ Grafana Prometheus proxy query successful: HTTP {g_prom_resp.status_code}")

        # Query Tempo via Grafana
        g_tempo_resp = client.get(
            f"http://localhost:3000/api/datasources/proxy/uid/{tempo_uid}/api/traces/{trace_id_hex}"
        )
        assert g_tempo_resp.status_code == 200
        print(f"  ✓ Grafana Tempo proxy query successful: HTTP {g_tempo_resp.status_code}")

    # 9. Clean up test telemetry state
    _reset_telemetry_state(app=app, engine=engine)

    print("\n" + "=" * 70)
    print("ALL PHASE 5 E2E OBSERVABILITY VALIDATIONS PASSED SUCCESSFULLY!")
    print("=" * 70)


if __name__ == "__main__":
    run_e2e_verification()
