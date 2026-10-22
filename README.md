# SentinelML — Enterprise MLSecOps & Runtime Protection Platform

[![Python 3.12](https://img.shields.io/badge/Python-3.12-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688.svg)](https://fastapi.tiangolo.com/)
[![React 18](https://img.shields.io/badge/React-18.3-61DAFB.svg)](https://react.dev/)
[![Kubernetes / KubeRay](https://img.shields.io/badge/Kubernetes-KubeRay-326CE5.svg)](https://ray.io/)
[![lakeFS](https://img.shields.io/badge/lakeFS-Git--for--Data-1D63ED.svg)](https://lakefs.io/)
[![MLflow](https://img.shields.io/badge/MLflow-Registry-0194E2.svg)](https://mlflow.org/)
[![Falco eBPF](https://img.shields.io/badge/Falco-eBPF_Kernel-00AEC7.svg)](https://falco.org/)
[![MITRE ATLAS](https://img.shields.io/badge/Compliance-MITRE_ATLAS-FF6F00.svg)](https://atlas.mitre.org/)

**SentinelML** is an enterprise-grade MLSecOps platform that unifies **machine learning operational agility** with **zero-trust security enforcement**. Developed as a cybersecurity capstone, it secures the complete machine learning lifecycle—from raw dataset ingestion and distributed training to live Kubernetes inference serving—protecting AI assets against supply chain poisoning, deserialization exploits, runtime container escapes, adversarial evasion, and model theft.

---

## 1. Problem Statement & Motivation

Modern artificial intelligence and machine learning pipelines face unique, high-consequence security threats across every lifecycle phase:

1. **Supply Chain Compromise & Unsafe Deserialization**: Arbitrary code execution payloads embedded inside serialized pickle models (`.pkl`, `.joblib`) can compromise worker environments at load time.
2. **Dataset Poisoning & Label Manipulation**: Adversaries manipulate training batches to degrade accuracy or insert backdoor triggers.
3. **Storage Tampering**: Bypassing version control via direct object store mutations alters datasets out-of-band.
4. **Inference Attacks & Model Stealing**: Malicious clients query prediction endpoints to reverse-engineer proprietary models (black-box stealing) or extract private training data (model inversion).
5. **Runtime Container Escapes**: Compromised model serving pods can spawn interactive shells or exfiltrate cluster secrets.
6. **Data & Prediction Drift**: In-flight distribution shifts degrade model fidelity without immediate warning.

SentinelML addresses these challenges by embedding continuous verification, cryptographic provenance, pre-merge gatekeepers, and runtime SIEM event correlation directly into MLOps workflows.

---

## 2. V1 Scope vs. Future Roadmap

SentinelML V1 provides an end-to-end, tested, and deployable MLSecOps platform.

### V1 Scope (Implemented & Verified)
- [x] **Zero-Trust Model Governance**: Static pickle AST disassembly, opcode inspection, automated quarantine of malicious payloads, and enforcement of safe formats (**Safetensors**, **ONNX**).
- [x] **Cryptographic Provenance**: HMAC-SHA256 and Cosign/Sigstore attestation signing for approved models.
- [x] **Dataset Versioning & Poisoning Defense**: lakeFS Git-for-Data branch workflows with pre-merge statistical validation gates (KS test with Bonferroni correction, Chi-square, PSI).
- [x] **Direct Storage Bypass Detection**: MinIO S3 bucket notification webhooks flagging unversioned out-of-band storage mutations.
- [x] **Distributed Training**: KubeRay RayJob CRD manifest generation, multi-candidate tournaments (Logistic Regression, Random Forest, XGBoost, LightGBM), and local Ray execution fallback.
- [x] **Inference Serving & Real-Time Defense**: RayService rolling deployments, memory flooding DoS shields, decision-boundary scraping detectors, feature perturbation analysis, and adversarial FGSM/PGD noise detection.
- [x] **Label-Free Drift Engine**: Automated baseline tracking and detection for **D1 Feature Drift**, **D2 Prediction Drift**, and **D3 Confidence Collapse**.
- [x] **Threat Hunting SIEM**: Background correlation engine evaluating 23 built-in detection rules against MITRE ATT&CK / ATLAS frameworks with evidence generation.
- [x] **Runtime Security**: Falco eBPF kernel event ingestion and rogue RayService pod detection.
- [x] **Authentication & RBAC**: JWT access/refresh token rotation, token revocation blacklisting, password complexity rules, and 4 provisioned roles (`admin`, `data_scientist`, `ml_engineer`, `viewer`).
- [x] **Full-Stack Observability**: OpenTelemetry distributed tracing and metrics exported to OTel Collector, Prometheus, Tempo, and Grafana.

### V2 Scope (Planned for Future Releases)
- [ ] **Continuous Training (CT)**: Automated retraining triggers and end-to-end pipeline re-execution upon verified drift thresholds.
- [ ] **Automatic Production Promotion**: Zero-touch promotion of retrained models based on automated validation tournament scoring.
- [ ] **Outcome-Dependent Drift (D4/D5)**: Performance degradation (D4) and true concept drift (D5) monitoring (requires delayed ground-truth label ingestion).
- [ ] **Native OTel Log Export to Loki**: Direct streaming of FastAPI application logs via OTel Collector to Grafana Loki.

---

## 3. Architecture Overview

### System Component Diagram

```
                                      SENTINELML V1 ARCHITECTURE
 ┌──────────────────────────────────────────────────────────────────────────────────────────────────┐
 │                                   REACT 18 SECURITY DASHBOARD                                    │
 │       Datasets • Model Registry • Distributed Training • Deployments • Threat Hunting (SIEM)     │
 └────────────────────────────────┬─────────────────────────────────────────────────────────────────┘
                                  │ HTTP / JSON (JWT RBAC) [Port :5173 -> :8001]
 ┌────────────────────────────────▼─────────────────────────────────────────────────────────────────┐
 │                                    FASTAPI BACKEND GATEWAY (:8001)                               │
 │  ┌───────────────────────┬───────────────────────┬───────────────────────┬────────────────────┐  │
 │  │ Model Governance Gate │ Data Poisoning Shield │ Inference Rate & DoS  │ SIEM Threat Engine │  │
 │  │ Safe Format / Cosign  │ lakeFS Pre-Merge Gate │ Sliding Window & Drift│ 23 Detection Rules │  │
 │  └───────────────────────┴───────────────────────┴───────────────────────┴────────────────────┘  │
 └───────┬──────────────────────────┬──────────────────────────┬─────────────────────────────┬──────┘
         │                          │                          │                             │
 ┌───────▼────────┐         ┌───────▼────────┐         ┌───────▼────────┐            ┌───────▼──────┐
 │  lakeFS (:8000)│         │ MLflow (:5000) │         │ KubeRay Server │            │  Falco eBPF  │
 │ Git-for-Data   │         │ MinIO (:9000)  │         │ RayJob / Serve │            │ Kernel Probe │
 │ Pre-Merge Gate │         │ Signed Models  │         │ Microservices  │            │ Container RCE│
 └────────────────┘         └────────────────┘         └────────────────┘            └──────────────┘
```

### Complete Service Interaction Flow

```mermaid
graph TD
    Client[React Dashboard :5173] -->|HTTP / JWT| Backend[FastAPI Backend :8001]
    
    subgraph Data & Storage Layer
        Backend -->|PostgreSQL Wire| DB[(PostgreSQL :5432)]
        Backend -->|S3 REST API| MinIO[(MinIO S3 :9000)]
        Backend -->|lakeFS SDK| LakeFS[lakeFS Git-for-Data :8000]
        LakeFS -->|Blockstore IO| MinIO
        LakeFS -->|Pre-merge Webhook| Backend
        MinIO -->|Mutation Webhook| Backend
    end
    
    subgraph Machine Learning Lifecycle
        Backend -->|MLflow Client| MLflow[MLflow Server :5000]
        MLflow -->|Artifact Storage| MinIO
        Backend -->|K8s CRD Client| KubeRay[Ray / KubeRay Controller]
        KubeRay -->|RayJob Execution| RayCluster[Ray Worker Pods]
        KubeRay -->|RayService Deployment| RayServe[RayServe Model Endpoints]
        Backend -->|Inference Forwarding| RayServe
    end

    subgraph Security & Runtime Monitoring
        Falco[Falco eBPF Engine] -->|Runtime Alert Webhook| Backend
        Backend -->|Background Detection Worker| DB
    end

    subgraph Observability Stack
        Backend -->|OTLP / HTTP :4318| OTelCol[OTel Collector]
        OTelCol -->|OTLP gRPC| Tempo[Grafana Tempo :3200]
        OTelCol -->|Scrape :8889| Prometheus[Prometheus :9090]
        Grafana[Grafana Dashboards :3000] -->|Queries| Prometheus
        Grafana -->|Queries| Tempo
    end
```

---

## 4. Technology Stack

| Domain | Technologies | Purpose |
| :--- | :--- | :--- |
| **Backend Core** | Python 3.12, FastAPI 0.115, Uvicorn, Pydantic v2 | High-performance asynchronous API gateway |
| **Database & Persistence** | PostgreSQL 16, SQLAlchemy 2.0, Alembic | Relational state, users, alerts, jobs, baselines |
| **Data Versioning** | lakeFS, lakeFS SDK | Immutable branching, commits, and rollback for data |
| **Object Storage** | MinIO (S3 Compatible) | S3 blockstore for lakeFS and MLflow artifacts |
| **Machine Learning** | MLflow 3.x, Scikit-learn, XGBoost, LightGBM | Experiment tracking, model registry, training |
| **Distributed Compute** | Ray 2.57, KubeRay, Kubernetes Python Client | Distributed RayJobs and auto-scaling RayServices |
| **Serialization Security** | ONNX, Safetensors, `pickletools` AST Scanner | Safe tensor formats and malicious opcode blocking |
| **Runtime Security** | Falco eBPF, HMAC-SHA256, Cosign / Sigstore | Kernel syscall monitoring, cryptographic signing |
| **Frontend** | React 18, Vite, Vanilla CSS Design System, Recharts | Real-time operations console & SIEM dashboard |
| **Observability** | OpenTelemetry, Prometheus, Tempo, Loki, Grafana | Distributed tracing, metrics, and visualization |

---

## 5. Repository Structure

```text
SentinelML/
├── backend/                          # FastAPI backend application
│   ├── alembic/                      # Database migrations (PostgreSQL)
│   ├── alembic.ini                   # Alembic environment configuration
│   ├── app/                          # Core application package
│   │   ├── api/                      # REST endpoints (auth, models, datasets, drift, alerts)
│   │   ├── core/                     # Configuration, security, logging, OpenTelemetry
│   │   ├── db/                       # Database session, base models, seed scripts
│   │   ├── models/                   # SQLAlchemy ORM schemas
│   │   ├── repositories/             # Data access repository layer
│   │   ├── schemas/                  # Pydantic request/response validation schemas
│   │   ├── services/                 # Business logic and external service integrations
│   │   └── templates/                # RayJob & RayService Kubernetes YAML templates
│   ├── README.md                     # Dedicated backend architecture and setup guide
│   ├── run.sh                        # Automated backend startup script (Linux/macOS)
│   ├── run.ps1                       # Automated backend startup script (Windows)
│   └── tests/                        # Comprehensive automated test suite (350+ tests)
├── frontend/                         # React 18 / Vite single-page application
│   ├── src/                          # Application source code
│   │   ├── api/                      # Axios API clients
│   │   ├── auth/                     # AuthContext, RoleGate, token handling
│   │   ├── components/               # UI components, layout, icons
│   │   ├── pages/                    # Code-split dashboard pages
│   │   └── shell/                    # Navigation, sidebar, user menu
│   ├── package.json                  # Frontend dependencies
│   ├── README.md                     # Dedicated frontend guide
│   └── vite.config.js                # Vite build and proxy configuration
├── k8s/                              # Kubernetes manifests
│   └── rayjob-network-policy.yaml    # Network isolation policy for training pods
├── observability/                    # Observability infrastructure configuration
│   ├── grafana/                      # Provisioned Grafana datasources and dashboards
│   ├── loki/                         # Loki log aggregator configuration
│   ├── otel-collector/               # OpenTelemetry Collector Contrib pipeline config
│   ├── prometheus/                   # Prometheus scrape configurations
│   ├── tempo/                        # Grafana Tempo trace storage configuration
│   ├── verify_phase5_e2e.py          # OTel telemetry validation script
│   ├── verify_phase6a_e2e.py         # RayService distributed trace validation script
│   └── README.md                     # Dedicated observability stack guide
├── docker-compose.yml                # Multi-service infrastructure orchestration
├── init-db.sql                       # Initial PostgreSQL database setup
├── pyproject.toml                    # Python project configuration (uv / pip)
├── uv.lock                           # Locked Python dependencies
└── README.md                         # Primary project documentation (this file)
```

---

## 6. Prerequisites & Setup Guide

### 1. Prerequisites
- **Operating System**: Linux, macOS, or Windows (WSL2 recommended)
- **Container Runtime**: Docker Desktop or Docker Engine v24.0+ with Docker Compose
- **Python**: Version 3.12+ (managed with `uv` or `python3.12-venv`)
- **Node.js**: Version 18+ with `npm`

---

### 2. Configure Environment Variables
Copy the template configuration file:
```bash
cp .env.example .env
```

Review the key settings in `.env`:
```ini
# Core Application Ports
# Note: Backend runs on 8001 to prevent collision with lakeFS on 8000.
PORT=8001
API_HOST=0.0.0.0

# PostgreSQL
POSTGRES_USER=sentinelml
POSTGRES_PASSWORD=sentinelml123
POSTGRES_DB=sentinelml
DATABASE_URL=postgresql+psycopg://sentinelml:sentinelml123@localhost:5432/sentinelml

# Storage & Services
MINIO_ENDPOINT=http://localhost:9000
LAKEFS_ENDPOINT=http://localhost:8000
LAKEFS_WEBHOOK_URL="http://host.docker.internal:8001/api/webhooks/lakefs/data-validation"
MLFLOW_TRACKING_URI=http://localhost:5000

# Security Keys
SECRET_KEY="your-jwt-secret-key-change-this-in-production"
```

---

### 3. Start Infrastructure Services
Start PostgreSQL, MinIO, lakeFS, MLflow, and the Observability stack:
```bash
docker compose up -d
```

Verify that all service healthchecks report healthy:
```bash
docker compose ps
```

---

### 4. Start the Backend API Server
The backend startup script automatically waits for PostgreSQL, applies Alembic migrations, seeds default users, and starts the Uvicorn server on port `8001`:

```bash
cd backend
./run.sh
```

*(On Windows PowerShell, use `.\run.ps1`)*

* The Backend API Swagger documentation is live at **[http://localhost:8001/docs](http://localhost:8001/docs)**.
* The Health endpoint is live at **[http://localhost:8001/health](http://localhost:8001/health)**.

---

### 5. Start the Frontend Security Dashboard
In a separate terminal, install dependencies and launch the Vite development server:

```bash
cd frontend
npm install
npm run dev
```

* The dashboard is live at **[http://localhost:5173](http://localhost:5173)**.
* The development server proxies all `/api` and `/health` calls to `http://127.0.0.1:8001`.

---

## 7. Role-Based Access Control (RBAC) Accounts

The database seed script (`backend/app/db/seed_db.py`) provisions accounts for role segregation:

| Username | Default Role | Scopes & Permissions | Default Dev Password |
| :--- | :--- | :--- | :--- |
| **`admin_user`** | `admin` | Full system administration, user management, rule configuration, alert triage | `AdminPassword123!` |
| **`ds_user`** | `data_scientist` | Dataset management, branch workflows, training tournaments, model uploads | `DataScientist123!` |
| **`mle_user`** | `ml_engineer` | Model deployment management, RayService scaling, test inference queries | `MLEngineerPassword123!` |
| **`viewer_user`** | `viewer` | Read-only inspection of models, metrics, runs, and job statuses | `ViewerPassword123!` |

> [!IMPORTANT]
> The default development credentials above are intended for local development and demonstration only. In production deployments, specify `SEED_ADMIN_PASSWORD` in your secret manager to securely initialize the administrative user.

---

## 8. Threat Detection Matrix (MITRE ATT&CK / ATLAS)

SentinelML's built-in SIEM evaluates **23 distinct detection rules** against real-time operational events:

| Category | Rule / Trigger Name | Severity | MITRE Framework ID |
| :--- | :--- | :---: | :--- |
| **Model Governance** | Pickle Deserialization RCE Payload Detected | `CRITICAL` | `AML.T0025` / `T1203` |
| | Model Artifact Integrity Digest Mismatch (Tampering) | `CRITICAL` | `AML.T0010` / `T1584` |
| | Hidden Backdoor or Trojan Trigger in Model Weights | `CRITICAL` | `AML.T0018` / `T1584` |
| | Legacy Pickle Serialization Promoted to Production | `HIGH` | `AML.T0025` / `T1203` |
| | Unsigned or Invalidly Attested Model Promoted to Production | `HIGH` | `AML.T0010` / `T1584` |
| | Unregistered Shadow Model Artifact in S3 Storage | `HIGH` | `AML.T0010` / `T1584` |
| **Dataset & Storage** | Direct MinIO Storage Bypass (Unversioned Storage Mutation) | `HIGH` | `AML.T0020` / `T1565.001` |
| | Tabular Schema Injection Detection | `HIGH` | `AML.T0020` / `T1565.001` |
| | Statistical Data Poisoning on Dataset Merge | `HIGH` | `AML.T0020` / `T1565.001` |
| | Unauthorized Dataset Branch Mutation | `HIGH` | `AML.T0031` / `T1485` |
| **Inference Defense** | Algorithmic Denial of Service (Tabular Memory Flooding) | `HIGH` | `AML.T0029` / `T1499` |
| | Model Inversion & Decision-Boundary Scraping | `HIGH` | `AML.T0024` |
| | Systematic Feature Perturbation (Black-box Extraction) | `HIGH` | `AML.T0040` |
| | Adversarial Input Perturbation (FGSM/PGD Noise Detected) | `HIGH` | `AML.T0043` / `T1499` |
| | Model Behavioral Drift: Abrupt Confidence Degradation | `HIGH` | `AML.T0024` |
| | Algorithmic Denial of Service: Inference Latency Spike Anomaly | `HIGH` | `AML.T0029` / `T1499` |
| | Anomalous Inference Request Rate (Model Inversion / Extraction) | `MEDIUM` | `AML.T0024` |
| **Cluster & Runtime** | Container Runtime Unauthorized Process Execution (Falco eBPF) | `CRITICAL` | `AML.T0038` / `T1059.004` |
| | Unregistered Rogue RayServe Deployment Detected (Direct Injection) | `CRITICAL` | `AML.T0038` / `T1584` |
| | Unexpected RayJob Submission Spikes | `MEDIUM` | `AML.T0002` / `T1496` |
| **Authentication** | Repeated Authentication Failures (Brute Force) | `HIGH` | `T1110.001` |
| | Revoked Token Reuse Attempt | `HIGH` | `T1550` |
| | Excessive Unauthorized Requests (Privilege Abuse / Enumeration) | `MEDIUM` | `T1078` |

---

## 9. Drift Detection Engine (D1, D2, D3)

SentinelML implements an automated, label-free statistical drift detection engine designed to monitor active model serving traffic:

1. **D1: Feature Data Drift (`DataDriftDetector`)**:
   - Compares incoming inference feature distributions against the registered training baseline.
   - **Numerical Features**: Two-sample Kolmogorov-Smirnov (`ks_2samp`) with Bonferroni correction for family-wise error rate control ($p < \alpha / m$) alongside Population Stability Index (PSI).
   - **Categorical Features**: Chi-squared goodness-of-fit test (`chisquare`) and category frequency shifts.
2. **D2: Prediction Drift (`PredictionDriftDetector`)**:
   - Detects shifts in model output distribution prior to ground-truth label availability.
   - Evaluates discrete prediction frequencies using Chi-squared tests and PSI thresholds ($> 0.25$).
3. **D3: Confidence Drift (`ConfidenceDriftDetector`)**:
   - Monitors model certainty scores ($P(\hat{y})$) using Jensen-Shannon Divergence (JSD).
   - Flags reference-relative collapse: alerts when observed median confidence ($p_{50}$) drops below baseline $p_{10}$ or when low-confidence samples ($P < 0.60$) spike abnormally.

> [!NOTE]
> Drift detection identifies statistical divergence in input and prediction distributions; it does not in itself prove malicious intent. Findings are saved as structured evidence and forwarded to the SIEM rule engine for correlation with authentication, inference rate, and storage telemetry.

---

## 10. Platform Endpoints & UI Discovery

| Interface / Service | Host URL | Description |
| :--- | :--- | :--- |
| **SentinelML Dashboard** | [http://localhost:5173](http://localhost:5173) | Primary React security & operations console |
| **FastAPI Swagger UI** | [http://localhost:8001/docs](http://localhost:8001/docs) | Interactive API exploration and OpenAPI schemas |
| **FastAPI Health Check** | [http://localhost:8001/health](http://localhost:8001/health) | Subsystem connectivity status (DB, lakeFS, MinIO, MLflow) |
| **Grafana Dashboards** | [http://localhost:3000](http://localhost:3000) | Full-stack telemetry visualizer (Prometheus & Tempo) |
| **MLflow Registry** | [http://localhost:5000](http://localhost:5000) | Experiment runs, parameters, metrics, model artifacts |
| **MinIO Console** | [http://localhost:9001](http://localhost:9001) | S3 bucket browser and object storage administration |
| **lakeFS Console** | [http://localhost:8000](http://localhost:8000) | Git-for-Data repository and branch visualization |
| **Prometheus UI** | [http://localhost:9090](http://localhost:9090) | Direct Prometheus time-series metric queries |
| **Tempo API** | [http://localhost:3200/ready](http://localhost:3200/ready) | Distributed trace query engine |

---

## 11. Testing & Quality Assurance

SentinelML features an automated test suite spanning unit tests, integration pipelines, and adversarial attack simulations:

### Run Backend Test Suite
```bash
# Run the complete test suite (350+ tests)
uv run pytest --no-cov -q

# Run specific subsystem tests
uv run pytest tests/test_model_governance.py --no-cov -v
uv run pytest tests/test_storage_tampering.py --no-cov -v
uv run pytest tests/test_data_poisoning.py --no-cov -v
uv run pytest tests/test_drift_detection.py --no-cov -v
uv run pytest tests/test_threat_hunting.py --no-cov -v
uv run pytest tests/test_rbac.py --no-cov -v

# Run Python static checks
uv run ruff check backend/app/
```

### Run Frontend Linting & Production Build
```bash
cd frontend
npx oxlint --deny-warnings
npm run build
```

---

## 12. Troubleshooting & FAQ

### Port Collision: Port 8000 vs 8001
- **Issue**: Attempting to run the FastAPI backend on port 8000 results in `address already in use`.
- **Cause**: Port `8000` is assigned to lakeFS in `docker-compose.yml`.
- **Solution**: The backend runs on port `8001` (`PORT=8001`). The frontend proxy and startup scripts (`run.sh`) automatically use port 8001.

### lakeFS Pre-Merge Webhook Connection Refused
- **Issue**: Merging a protected lakeFS branch fails with `dial tcp ...:8001: connect: connection refused`.
- **Cause**: lakeFS runs inside Docker and communicates with the host backend using `LAKEFS_WEBHOOK_URL="http://host.docker.internal:8001/api/webhooks/lakefs/data-validation"`.
- **Solution**: Ensure the backend server is running on the host machine before executing branch merges.

### Grafana Admin Password on Existing Volume
- **Issue**: Changing `GRAFANA_ADMIN_PASSWORD` in `.env` has no effect on an existing Grafana container.
- **Cause**: Grafana stores administrative credentials in its persistent SQLite database (`sentinelml_grafana_data`).
- **Solution**: Run `docker exec sentinelml-grafana grafana-cli admin reset-admin-password <new-password>`, or reset volumes via `docker compose down -v`.

---

## 13. Project Documentation Links

For in-depth guides on specific platform layers, refer to:
- [Backend Architecture & API Guide](file:///home/deepadharshan/Desktop/SentinelML/backend/README.md)
- [Frontend Security Dashboard Guide](file:///home/deepadharshan/Desktop/SentinelML/frontend/README.md)
- [Observability Stack & Telemetry Guide](file:///home/deepadharshan/Desktop/SentinelML/observability/README.md)
