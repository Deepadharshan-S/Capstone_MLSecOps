# SentinelML Observability Stack

The **SentinelML Observability Stack** provides full-stack telemetry and monitoring for the SentinelML platform utilizing **OpenTelemetry Collector Contrib**, **Grafana Tempo**, **Prometheus**, **Grafana Loki**, and **Grafana Dashboards**.

---

## 1. Architecture Overview

SentinelML utilizes an external Dockerized observability stack while the FastAPI backend runs locally as a standard host Python process (or within Kubernetes in cluster deployments):

```text
Host (FastAPI Backend Process :8001)
    │
    │ OTLP/HTTP: http://localhost:4318
    ▼
┌────────────────────────────────────────────────────────┐
│ sentinelml-otel-collector (port 4318, 4317, 8889, 13133)│
└───────────────┬─────────────────┬──────────────────────┘
                │ OTLP gRPC       │ Scrape (:8889)
                ▼                 ▼
┌────────────────────────┐  ┌────────────────────────┐
│ sentinelml-tempo (:3200)│  │ sentinelml-prometheus  │
│ (Trace Storage)        │  │ (:9090, Metrics)       │
└───────────────┬────────┘  └────────┬───────────────┘
                │                    │
                └─────────┬──────────┘
                          ▼
             ┌────────────────────────┐
             │ sentinelml-grafana     │
             │ (:3000, Visualization) │
             └────────────────────────┘
```

### Telemetry Status in V1

#### Active & Verified
```text
FastAPI Backend
   │
   └── OpenTelemetry SDK
          ├── Traces  → OTel Collector (:4318) → Tempo (:3200)
          └── Metrics → OTel Collector (:8889) → Prometheus (:9090)
```
- **Traces**: FastAPI server spans, application ML operations (`model.inference`, `training.submit`, `deployment.create`), child SQLAlchemy database spans, and child HTTPX client spans flow to the OTel Collector via OTLP/HTTP (`localhost:4318`) and are forwarded via OTLP gRPC to Grafana Tempo.
- **Metrics**: Low-cardinality ML business metrics (`inference_requests_total`, `inference_duration_seconds`, `training_jobs_submitted_total`, `training_jobs_completed_total`, `deployment_operations_total`) and HTTP server metrics are exported from the OTel Collector on `:8889` and scraped by Prometheus every 5 seconds.

#### Application Logging Notice
```text
FastAPI application logs
          │
          ├── Rotating File Logs (logs/sentinelml.log)
          └── Security Audit Logs (audit_logs table in PostgreSQL)
```
> Loki infrastructure and the OpenTelemetry Collector log receiver pipeline are deployed in Docker Compose. Direct OpenTelemetry-based log export from FastAPI to Loki is intentionally deferred. Authoritative application logging in V1 is handled via structured Python rotating-file logging and the database-backed audit log engine (`log_audit_event`).

---

## 2. Managing the Stack

### Start Stack
From the repository root:
```bash
docker compose up -d
```

### Stop Stack (Preserving Persistent Volumes)
```bash
docker compose down
```

### Reset Stack & Wipe Volumes (Clean Slate Reset)
```bash
docker compose down -v
```

---

## 3. Configuration & Credentials

### Environment Variables
Grafana credentials are configured via environment variables in `.env` (see `.env.example`):

```env
# Grafana administrator credentials
GRAFANA_ADMIN_USER=admin
GRAFANA_ADMIN_PASSWORD=change-me
```

In `docker-compose.yml`:
```yaml
environment:
  GF_SECURITY_ADMIN_USER: ${GRAFANA_ADMIN_USER:-admin}
  GF_SECURITY_ADMIN_PASSWORD: ${GRAFANA_ADMIN_PASSWORD}
  GF_USERS_ALLOW_SIGN_UP: "false"
  GF_AUTH_ANONYMOUS_ENABLED: "true"
  GF_AUTH_ANONYMOUS_ORG_ROLE: "Viewer"
```

### Persistent Volume Behavior
Grafana stores credentials inside its persistent SQLite database (`/var/lib/grafana/grafana.db` backed by `sentinelml_grafana_data` volume):
- **Initial Start**: Grafana initializes the admin password from `GF_SECURITY_ADMIN_PASSWORD`.
- **Subsequent Starts**: Changing the variable in `.env` does not alter the password in an existing volume.
- **Resetting Password**: Run `docker exec sentinelml-grafana grafana-cli admin reset-admin-password <new-password>`, or reset volumes via `docker compose down -v`.

---

## 4. Endpoints & Access

| Service | Host Port | Ingest / Web Endpoint | Purpose |
| :--- | :--- | :--- | :--- |
| **Grafana** | `3000` | [http://localhost:3000](http://localhost:3000) | Visual dashboard & trace explorer |
| **OTel Collector (HTTP)** | `4318` | `http://localhost:4318/v1/traces` | OTLP/HTTP trace ingest from backend |
| **OTel Collector (gRPC)** | `4317` | `localhost:4317` | OTLP/gRPC ingest |
| **OTel Collector (Metrics)** | `8889` | [http://localhost:8889/metrics](http://localhost:8889/metrics) | Prometheus scrape target |
| **OTel Collector (Health)** | `13133`| [http://localhost:13133/](http://localhost:13133/) | Collector health check endpoint |
| **Prometheus** | `9090` | [http://localhost:9090](http://localhost:9090) | Metrics exploration & queries |
| **Tempo** | `3200` | [http://localhost:3200/ready](http://localhost:3200/ready) | Distributed trace query engine |
| **Loki** | `3100` | [http://localhost:3100/ready](http://localhost:3100/ready) | Log storage engine |

---

## 5. Pre-Configured Dashboards & Data Sources

- **Data Sources**:
  - `Prometheus` (default metrics source, UID: `prometheus`)
  - `Tempo` (distributed tracing source, UID: `tempo`)
  - `Loki` (log aggregation source, UID: `loki`)
- **Provisioned Dashboards**:
  - **`SentinelML - Application & MLOps Telemetry`** (UID: `sentinelml-core-telemetry` under folder `SentinelML`):
    - Panel 1: HTTP Request Rate (req/sec)
    - Panel 2: HTTP Request Latency (p95)
    - Panel 3: HTTP Client & Server Error Rates
    - Panel 4: Model Inference Request Count
    - Panel 5: Inference Latency Distribution (p95)
    - Panel 6: Ray Training Jobs Submitted
    - Panel 7: Ray Training Jobs Completed
    - Panel 8: Model Deployment Lifecycle Operations

---

## 6. End-to-End Verification Scripts

The `observability/` directory includes end-to-end telemetry verification scripts:
- **`verify_phase5_e2e.py`**: Validates OTel Collector pipeline connectivity, Prometheus metric scrapes, and Tempo trace delivery.
- **`verify_phase6a_e2e.py`**: Exercises live inference requests and checks trace propagation across FastAPI, SQLAlchemy, and mock RayService endpoints.

Run verification:
```bash
python observability/verify_phase5_e2e.py
python observability/verify_phase6a_e2e.py
```
