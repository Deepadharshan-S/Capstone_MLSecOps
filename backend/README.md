# SentinelML Backend Service

The **SentinelML Backend** is a high-performance MLSecOps API service built with **FastAPI**, **SQLAlchemy 2.0**, and **Pydantic v2**. It enforces zero-trust security controls across the machine learning lifecycle, manages distributed training and model deployment pipelines, and correlates security telemetry across storage, serving, and runtime layers.

---

## 1. Responsibilities & Architecture

The backend acts as the central control plane and security gateway:

```text
                               ┌───────────────────────────┐
                               │   React Frontend (:5173)  │
                               └─────────────┬─────────────┘
                                             │ HTTP / JSON (JWT RBAC)
                                             ▼
                             ┌───────────────────────────────┐
                             │    FastAPI Backend (:8001)    │
                             └───────┬───────┬───────┬───────┘
                                     │       │       │
            ┌────────────────────────┼───────┼───────┼────────────────────────┐
            │                        │       │       │                        │
            ▼                        ▼       ▼       ▼                        ▼
┌───────────────────────┐ ┌──────────────┐ ┌───────────────┐ ┌───────────────────────────┐
│ PostgreSQL 16 (:5432) │ │ lakeFS (:8000│ │ MLflow (:5000)│ │ MinIO S3 (:9000 / :9001)  │
│ Users, Jobs, Rules,   │ │ Repositories,│ │ Model Registry│ │ Datasets, Model Artifacts, │
│ Events, Alerts, Drift │ │ Pre-Merge    │ │ Experiments   │ │ Mutation Webhooks         │
└───────────────────────┘ └──────────────┘ └───────────────┘ └───────────────────────────┘
```

### Core Subsystems
1. **Authentication, Authorization & RBAC**: Password complexity enforcement, bcrypt hashing, JWT access/refresh token rotation, in-memory/DB token revocation, and role-based route gating (`admin`, `data_scientist`, `ml_engineer`, `viewer`).
2. **Model Registry & Governance**: Secure model upload, static opcode disassembly via `pickletools`, RCE quarantine isolation, ONNX/Safetensors validation, and HMAC-SHA256 / Cosign cryptographic provenance signing.
3. **Dataset Catalog & lakeFS Integration**: Branch-aware dataset version control, tabular schema validation, and pre-merge statistical data poisoning gatekeepers.
4. **Training Orchestration & Compute**: KubeRay RayJob CRD manifest generation, multi-model candidate tournaments (Logistic Regression, Random Forest, XGBoost, LightGBM), and optional local Ray execution fallback.
5. **Inference Serving & Threat Defense**: KubeRay RayService deployment lifecycle management, real-time sliding-window inference defense (memory flooding DoS, decision boundary scraping, systematic feature perturbation, adversarial FGSM/PGD noise).
6. **Threat Hunting & SIEM Correlation**: Background detection engine evaluating 23 MITRE ATT&CK / ATLAS rules against ingested security events, storage mutation webhooks, and Falco eBPF runtime events.
7. **Drift Detection Engine**: Label-free drift monitoring across feature distributions (D1 data drift), predicted classes/values (D2 prediction drift), and prediction confidence degradation (D3 confidence drift).
8. **Observability**: Native OpenTelemetry distributed tracing and metrics exported to OTel Collector (`localhost:4318`), Grafana Tempo, and Prometheus.

---

## 2. Directory Structure

```text
backend/
├── alembic/                      # SQLAlchemy database migration revisions
│   ├── env.py
│   └── versions/                 # Individual migration scripts
├── alembic.ini                   # Alembic configuration
├── app/
│   ├── api/                      # FastAPI routers and endpoint controllers
│   │   ├── auth.py               # Authentication & token endpoints
│   │   ├── datasets.py           # Dataset catalog and lakeFS operations
│   │   ├── drift.py              # Drift baseline and detection endpoints
│   │   ├── ml_ops.py             # Models, training, jobs, deployments, inference
│   │   ├── permissions.py        # RBAC dependency injection helpers
│   │   ├── threat_hunting.py     # Alerts, detection rules, hunts, events
│   │   ├── users.py              # User administration and role assignments
│   │   └── webhooks.py           # lakeFS, MinIO, and Falco event ingestion
│   ├── core/                     # Application settings, logging, telemetry, security
│   │   ├── config.py             # Pydantic Settings loaded from .env
│   │   ├── logging_config.py     # Structured JSON audit logging
│   │   ├── mlflow_loader.py      # Thread-safe MLflow warming and loader
│   │   ├── rate_limiter.py       # Sliding-window endpoint rate limiting
│   │   ├── security.py           # Password hashing & JWT token encoding
│   │   └── telemetry.py          # OpenTelemetry instrumentation setup
│   ├── db/                       # Database session setup and seeding
│   │   ├── seed_db.py            # Initial user and RBAC seed script
│   │   └── session.py            # SQLAlchemy engine and session factory
│   ├── middleware/               # HTTP middleware
│   │   ├── request_id.py         # X-Request-ID propagation and contextvar tracing
│   │   └── security_headers.py   # HSTS, X-Content-Type-Options, frame protection
│   ├── models/                   # SQLAlchemy ORM database models
│   ├── repositories/             # Database access layer
│   ├── schemas/                  # Pydantic v2 validation models
│   ├── services/                 # Business logic and external service integrations
│   │   ├── auth/                 # Authentication, user management, token cleanup
│   │   ├── dataset/              # lakeFS client, catalog, poisoning detection
│   │   ├── drift/                # D1/D2/D3 drift detectors, stats, and background worker
│   │   ├── ml_ops/               # Model registry, Ray wrapper, KubeRay templates
│   │   └── threat_hunting/       # SIEM detection engine, storage/runtime monitors
│   └── templates/                # YAML manifest templates (RayJob, RayService)
├── run.sh                        # Linux/macOS startup script (waits for DB, migrates, starts)
├── run.ps1                       # Windows PowerShell startup script
├── scripts/                      # Operational utilities (e.g. seed_drift_demo.py)
└── tests/                        # Comprehensive test suite (350+ tests)
```

---

## 3. Prerequisites & Installation

### Requirements
- **Python 3.12+**
- **uv** (recommended) or standard `pip` + `venv`
- Running infrastructure services (PostgreSQL, MinIO, lakeFS, MLflow) via Docker Compose

### Setup Environment
From the repository root:

```bash
# Using uv (recommended)
uv sync

# Or using standard python venv
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e .
```

---

## 4. Environment Configuration

The backend loads configuration from the root `.env` file via `app.core.config.Settings`. Ensure `.env` is populated (copy from `.env.example` if not already present).

Key backend variables:

| Variable | Default (Local Dev) | Description |
| :--- | :--- | :--- |
| `BACKEND_HOST` / `API_HOST` | `0.0.0.0` | Listen interface |
| `BACKEND_PORT` / `API_PORT` | `8001` | Backend port (must be 8001 to avoid lakeFS collision on 8000) |
| `DATABASE_URL` | `postgresql+psycopg://sentinelml:sentinelml123@localhost:5432/sentinelml` | PostgreSQL connection string |
| `SECRET_KEY` | *(Set in .env)* | Cryptographic key for signing JWT tokens |
| `MINIO_ENDPOINT` | `http://localhost:9000` | MinIO S3 endpoint |
| `LAKEFS_ENDPOINT` | `http://localhost:8000` | lakeFS server endpoint |
| `LAKEFS_WEBHOOK_URL` | `http://host.docker.internal:8001/api/webhooks/lakefs/data-validation` | Webhook URL passed to lakeFS pre-merge action |
| `MLFLOW_TRACKING_URI` | `http://localhost:5000` | MLflow tracking server |
| `OTEL_ENABLED` | `false` | Enable/disable OpenTelemetry export |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://localhost:4318` | OpenTelemetry Collector endpoint |

---

## 5. Running the Backend

### Method A: Automated Startup Script (Recommended)
The automated script waits for PostgreSQL to become reachable, applies database migrations, seeds default users/rules, and starts Uvicorn with hot reload:

```bash
cd backend
./run.sh
```

### Method B: Manual Startup
```bash
# 1. Apply database migrations
uv run alembic upgrade head

# 2. Seed default users and detection rules
uv run python -m app.db.seed_db

# 3. Start Uvicorn on port 8001
uv run uvicorn app.main:app --host 0.0.0.0 --port 8001 --reload
```

---

## 6. Background Workers & Lifespan Tasks

When the FastAPI application initializes via `app.main.lifespan`, several background tasks start automatically:
1. **MLflow Pre-Warming**: Single-threaded import of `mlflow` and `mlflow.pyfunc` during startup to prevent concurrent import race conditions.
2. **OpenTelemetry Telemetry**: Traces and metrics exporters initialize and attach to FastAPI, SQLAlchemy, and HTTPX (safe no-op if `OTEL_ENABLED=false`).
3. **Token Cleanup Worker**: Periodically purges expired blacklisted JWT tokens from PostgreSQL (`TOKEN_CLEANUP_INTERVAL_SECONDS=3600`).
4. **Threat Detection Worker**: Polls and correlates ingested `security_events` against 23 active detection rules every 30 seconds (`DETECTION_ENGINE_INTERVAL_SECONDS=30`), generating alerts.
5. **Drift Detection Worker**: Continuously evaluates inference records against registered baselines every 300 seconds (`DRIFT_SWEEP_INTERVAL_SECONDS=300`) across D1, D2, and D3 detectors.

---

## 7. API Discovery & Documentation

Once the server is running on port `8001`:
- **Interactive Swagger UI**: [http://localhost:8001/docs](http://localhost:8001/docs)
- **ReDoc Documentation**: [http://localhost:8001/redoc](http://localhost:8001/redoc)
- **OpenAPI JSON Specification**: [http://localhost:8001/openapi.json](http://localhost:8001/openapi.json)
- **Health Check Endpoint**: [http://localhost:8001/health](http://localhost:8001/health) (reports connectivity to PostgreSQL, lakeFS, MinIO, and MLflow)

---

## 8. Testing & Validation

The backend test suite is located in `backend/tests/` and contains comprehensive unit, integration, and security tests.

```bash
# Run all tests
uv run pytest --no-cov -q

# Run specific security test suites
uv run pytest tests/test_model_governance.py --no-cov -v
uv run pytest tests/test_storage_tampering.py --no-cov -v
uv run pytest tests/test_data_poisoning.py --no-cov -v
uv run pytest tests/test_drift_detection.py --no-cov -v
uv run pytest tests/test_threat_hunting.py --no-cov -v

# Run code style and lint checks
uv run ruff check backend/app/
```
