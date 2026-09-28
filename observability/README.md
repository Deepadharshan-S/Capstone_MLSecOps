# SentinelML Observability Stack (Phase 5)

Local observability infrastructure for SentinelML utilizing OpenTelemetry Collector Contrib, Grafana Tempo, Prometheus, Grafana Loki, and Grafana.

---

## 1. Architecture Overview

SentinelML utilizes an external Dockerized observability stack while the FastAPI backend runs locally as a standard host Python process:

```text
Host (FastAPI Process)
    │
    │ OTLP/HTTP: http://localhost:4318
    ▼
┌────────────────────────────────────────────────────────┐
│ sentinelml-otel-collector (port 4318, 4317, 8889, 13133)│
└───────────────┬─────────────────┬──────────────────────┘
                │ OTLP gRPC       │ Scrape (:8889)
                ▼                 ▼
┌────────────────────────┐  ┌────────────────────────┐
│ sentinelml-tempo (:3200)│  │sentinelml-prometheus   │
│ (Trace Storage)        │  │(:9090, Metrics Storage)│
└───────────────┬────────┘  └────────┬───────────────┘
                │                    │
                └─────────┬──────────┘
                          ▼
             ┌────────────────────────┐
             │ sentinelml-grafana     │
             │ (:3000, Visualization) │
             └────────────────────────┘
```

### Telemetry Ingestion Status

#### Active (Verified)
```text
FastAPI
   │
   └── OpenTelemetry
          ├── traces → Collector → Tempo
          └── metrics → Collector → Prometheus
```
- **Traces**: FastAPI server spans, application ML business spans (`model.inference`, `training.submit`, `deployment.create`, etc.), child SQLAlchemy database spans, and child HTTPX client spans flow to the OTel Collector via OTLP/HTTP (`localhost:4318`) and are forwarded via OTLP gRPC to Grafana Tempo.
- **Metrics**: Bounded, low-cardinality ML business metrics (`inference_requests_total`, `inference_duration_seconds`, `training_jobs_submitted_total`, `training_jobs_completed_total`, `deployment_operations_total`) and HTTP server metrics are exported from the OTel Collector on `:8889` and scraped by Prometheus every 5 seconds.

#### Configured but Deferred
```text
FastAPI application logs
          │
          X
          │
          └──> OpenTelemetry Collector → Loki
```
> Loki infrastructure and the OpenTelemetry Collector log pipeline are deployed and ready. OpenTelemetry-based application log export from FastAPI is intentionally deferred. Existing Python rotating-file logging, structured audit logging (`log_audit_event`), and RequestIDMiddleware remain unchanged and serve as the authoritative application logging mechanisms. Application logs are not currently exported to or ingested by Loki.

---

## 2. Managing the Stack

### Start Stack
```bash
docker compose up -d
```

### Stop Stack (Preserving Persistent Volumes)
```bash
docker compose down
```

### Stop Stack & Wipe Volumes (Clean Slate Reset)
```bash
docker compose down -v
```

---

## 3. Configuration & Credentials

### Environment Variables
Grafana credentials are configured via environment variables in `.env` (gitignored) and documented in `.env.example`:

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

### Persistent Volume Credential Behavior
Grafana stores its administrative credentials inside its persistent SQLite database (`/var/lib/grafana/grafana.db` backed by the `sentinelml_grafana_data` volume):
- **Initial Setup**: When the volume is first created, Grafana initializes the admin password from `GF_SECURITY_ADMIN_PASSWORD`.
- **Subsequent Starts**: Changing `GF_SECURITY_ADMIN_PASSWORD` in the environment does **not** overwrite the password in an existing, already-initialized Grafana volume.
- **Resetting Password on an Existing Volume**: If you wish to change the password on an existing volume without deleting dashboard data, run:
  ```bash
  docker exec sentinelml-grafana grafana-cli admin reset-admin-password <new-password>
  ```
  Alternatively, pruning the volume via `docker compose down -v` reinitializes the volume with the current `GF_SECURITY_ADMIN_PASSWORD`.

---

## 4. Endpoints & Access

| Service | Port | Endpoint | Purpose |
| :--- | :--- | :--- | :--- |
| **Grafana** | `3000` | http://localhost:3000 | Unified observability UI and dashboards |
| **OTel Collector (HTTP)** | `4318` | http://localhost:4318/v1/traces | OTLP/HTTP ingest from local FastAPI process |
| **OTel Collector (gRPC)** | `4317` | localhost:4317 | OTLP/gRPC ingest |
| **OTel Collector (Metrics)** | `8889` | http://localhost:8889/metrics | Prometheus scrape exporter |
| **OTel Collector (Health)** | `13133`| http://localhost:13133/ | Health check endpoint |
| **Prometheus** | `9090` | http://localhost:9090 | Metrics queries & debugging |
| **Tempo** | `3200` | http://localhost:3200/ready | Distributed tracing query API |
| **Loki** | `3100` | http://localhost:3100/ready | Log aggregation endpoint |

---

## 5. Provisioned Resources

- **Data Sources**:
  - `Prometheus` (default, UID: `prometheus`)
  - `Tempo` (UID: `tempo`)
  - `Loki` (UID: `loki`)
- **Dashboards**:
  - `SentinelML - Application & MLOps Telemetry` (UID: `sentinelml-core-telemetry` in folder `SentinelML`)
    - Panel 1: HTTP Request Rate
    - Panel 2: HTTP Request Latency (p95)
    - Panel 3: HTTP Client & Server Errors
    - Panel 4: Inference Request Count
    - Panel 5: Inference Latency (p95)
    - Panel 6: Training Jobs Submitted
    - Panel 7: Training Jobs Completed
    - Panel 8: Model Deployment Operations
