# SentinelML — Enterprise MLSecOps & Runtime Protection Platform

[![Python 3.12](https://img.shields.io/badge/Python-3.12-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688.svg)](https://fastapi.tiangolo.com/)
[![React 18](https://img.shields.io/badge/React-18.3-61DAFB.svg)](https://react.dev/)
[![Kubernetes / KubeRay](https://img.shields.io/badge/Kubernetes-KubeRay-326CE5.svg)](https://ray.io/)
[![lakeFS](https://img.shields.io/badge/lakeFS-Git--for--Data-1D63ED.svg)](https://lakefs.io/)
[![MLflow](https://img.shields.io/badge/MLflow-Registry-0194E2.svg)](https://mlflow.org/)
[![Falco eBPF](https://img.shields.io/badge/Falco-eBPF_Kernel-00AEC7.svg)](https://falco.org/)
[![MITRE ATLAS](https://img.shields.io/badge/Compliance-MITRE_ATLAS-FF6F00.svg)](https://atlas.mitre.org/)

**SentinelML** is an enterprise-grade MLSecOps platform that unifies **machine learning operational agility** with **zero-trust security enforcement**. It secures the complete machine learning lifecycle—from raw dataset ingestion and distributed training to live Kubernetes inference serving—protecting AI assets against supply chain poisoning, deserialization exploits, runtime container escapes, and model theft.

---

## Architecture Overview

```
                                      SENTINELML ARCHITECTURE
 ┌──────────────────────────────────────────────────────────────────────────────────────────────────┐
 │                                   REACT 18 SECURITY DASHBOARD                                    │
 │       Datasets • Model Registry • Distributed Training • Deployments • Threat Hunting (SIEM)     │
 └────────────────────────────────┬─────────────────────────────────────────────────────────────────┘
                                  │ REST / JSON (JWT RBAC)
 ┌────────────────────────────────▼─────────────────────────────────────────────────────────────────┐
 │                                    FASTAPI BACKEND GATEWAY                                       │
 │  ┌───────────────────────┬───────────────────────┬───────────────────────┬────────────────────┐  │
 │  │ Model Governance Gate │ Data Poisoning Shield │ Inference Rate & DoS  │ SIEM Threat Engine │  │
 │  │ Safe Format / Cosign  │ LakeFS Pre-Merge Gate │ Sliding Window & Drift│ 23 Detection Rules │  │
 │  └───────────────────────┴───────────────────────┴───────────────────────┴────────────────────┘  │
 └───────┬──────────────────────────┬──────────────────────────┬─────────────────────────────┬──────┘
         │                          │                          │                             │
 ┌───────▼────────┐         ┌───────▼────────┐         ┌───────▼────────┐            ┌───────▼──────┐
 │  lakeFS / S3   │         │ MLflow / MinIO │         │ KubeRay Server │            │  Falco eBPF  │
 │ Git-for-Data   │         │ Signed Models  │         │ RayJob / Serve │            │ Kernel Probe │
 │ Data Poisoning │         │ Attestation    │         │ Microservices  │            │ Container RCE│
 └────────────────┘         └────────────────┘         └────────────────┘            └──────────────┘
```

---

## Core Capabilities & Security Pillars

### 1. Model Supply Chain & Provenance Governance
* **Zero-Trust Format Enforcement:** Blocks legacy executable pickle formats (`.pkl`, `.joblib`) from entering staging and production pipelines, enforcing tamper-resistant tensor formats (**Safetensors** and **ONNX**).
* **Static AST Opcode Scanner:** Disassembles candidate pickle bytecodes using `pickletools` to identify and reject dangerous functions (`os.system`, `subprocess.Popen`, `posix`, `socket`) before storage.
* **Cryptographic Attestation & Signatures:** Signs approved model versions with HMAC-SHA256 and Cosign/Sigstore provenance digests. Unsigned or tampered artifacts are rejected at deployment.
* **Continuous Digest Integrity:** Computes SHA-256 digests in real time and verifies them against the registered manifest, catching "man-in-the-storage" tampering.
* **Neural Backdoor & Trojan Scanner:** Scans model weight matrices for extreme outlier activations ($Z\text{-score} > 5.0$) characteristic of injected trojan trigger neurons (BadNets).

### 2. Dataset Versioning & In-Flight Storage Security
* **LakeFS Git-for-Data Version Control:** Immutable, branch-based dataset versioning with atomic commits, branch rollbacks, and reproducible lineage.
* **Statistical Data Poisoning Pre-Merge Gate:** LakeFS pre-merge action calculates class distributions and Kolmogorov-Smirnov statistics. If class balances deviate drastically ($> 25\%$ label flip), the merge to `main` is blocked.
* **Direct MinIO Storage Bypass Detection:** Webhook listeners capture direct S3 mutations in underlying MinIO buckets that bypass LakeFS version control.
* **Tabular Schema Injection Shield:** Detects duplicate header collisions (`Label` vs `label`), target column manipulation, and injected privilege columns (`admin`, `override`).

### 3. Distributed Compute & Pipeline Training
* **KubeRay RayJob Orchestration:** Automatically renders Kubernetes RayJob CRD manifests with isolated, short-lived scoped credentials and executes distributed training.
* **Multi-Model Candidate Search:** Concurrently trains and cross-evaluates multiple algorithms (**Logistic Regression, Random Forest, XGBoost, LightGBM**), automatically registering the top-performing candidate as the champion.
* **Local Ray Execution Fallback:** Built-in sandboxed local fallback for resource-constrained development environments without Kubernetes.

### 4. Real-Time Inference Serving & API Threat Defense
* **RayServe Serving Microservices:** Manages live model serving using RayService CRDs with zero-downtime rolling updates.
* **Algorithmic Memory Flooding DoS Shield:** Flags batch matrices exceeding $100,000$ cells to prevent Ray worker Out-Of-Memory (OOM) crashes.
* **Decision Boundary Scraping (Model Inversion):** Detects actors probing predictions near decision thresholds ($0.48 \le P \le 0.52$) to reverse-engineer private training data (MITRE ATLAS `AML.T0024`).
* **Black-Box Model Stealing Protection:** Identifies systematic single-feature stepping ($L_0 = 1$) queries used to estimate numerical gradients (MITRE ATLAS `AML.T0040`).
* **Behavioral Confidence Collapse Monitoring:** Real-time sliding window detector tracks mean confidence scores; drops to $\le 60\%$ trigger drift alerts.
* **Adversarial Noise Detection:** Flags high-frequency perturbations characteristic of Fast Gradient Sign Method (FGSM) or Projected Gradient Descent (PGD) evasion attacks.

### 5. Container Runtime Security & eBPF Telemetry
* **Falco eBPF Kernel Webhooks:** Ingests Linux kernel system call telemetry to catch interactive shells (`bash`, `sh`) or network exfiltration utilities (`curl`, `nc`) spawned inside model serving pods.
* **Rogue RayServe Deployment Reconciler:** Scans the cluster for unmanaged RayService pods that bypass SentinelML governance approval.

### 6. Full-Stack Observability
* **Distributed Tracing:** Automated OpenTelemetry instrumentation spanning HTTP requests, database transactions, and ML training spans.
* **Metrics & Dashboards:** Pre-configured Prometheus scrapers, Grafana dashboards, Loki log aggregators, and Tempo distributed trace visualizers.

---

## Technology Stack

| Layer | Technologies |
| :--- | :--- |
| **Backend & API** | FastAPI, Python 3.12, SQLAlchemy 2.0, Pydantic v2, Uvicorn |
| **Frontend** | React 18, Vite, Vanilla CSS Design System, Lucide React, Recharts |
| **Data & Storage** | PostgreSQL 16, MinIO S3 Object Storage, LakeFS (Git-for-Data) |
| **Machine Learning** | MLflow 2.x / 3.x, Ray / KubeRay, Scikit-learn, XGBoost, LightGBM, ONNX, Safetensors |
| **Security & eBPF** | Falco eBPF, Cosign / HMAC Cryptographic Attestation, Pickle AST Disassembler |
| **Observability** | OpenTelemetry, Prometheus, Grafana, Loki, Tempo |

---

## Project Structure

```
Capstone_MLSecOps/
├── backend/
│   ├── alembic/                      # Database migrations (PostgreSQL)
│   ├── app/
│   │   ├── api/                      # REST endpoints (auth, models, datasets, alerts, webhooks)
│   │   ├── core/                     # Configuration, security, logging, OpenTelemetry telemetry
│   │   ├── db/                       # Database session, base models, seed scripts
│   │   ├── models/                   # SQLAlchemy ORM schemas
│   │   ├── repositories/             # Data access repository layer
│   │   ├── schemas/                  # Pydantic request/response validation
│   │   ├── services/
│   │   │   ├── auth/                 # RBAC, password hashing, JWT token lifecycle
│   │   │   ├── dataset/              # LakeFS SDK, catalog, statistical poisoning tests
│   │   │   ├── ml_ops/               # Model registry, RayJobs, RayService deployment governance
│   │   │   └── threat_hunting/       # Detection engine, inference monitor, storage detector
│   │   └── templates/                # RayJob & RayService Kubernetes YAML templates
│   ├── tests/                        # Comprehensive PyTest test suite (unit, integration, security)
│   └── run.sh                        # Automated backend startup script with database wait & migrations
├── frontend/
│   ├── src/
│   │   ├── components/               # UI components, Layout, Navigation, Error Boundary
│   │   ├── pages/                    # Dashboard, Datasets, Models, Pipeline, Deployments, Alerts
│   │   └── services/                 # Axios API clients
│   ├── package.json
│   └── vite.config.js
├── docker-compose.yml                # Multi-service infrastructure orchestration
├── init-db.sql                       # Initial PostgreSQL database setup
└── README.md
```

---

## Quickstart & Setup Guide

### 1. Prerequisites
* **Docker & Docker Compose** (v24.0+)
* **Python 3.12+** and `uv` or `pip`
* **Node.js 18+** and `npm`

---

### 2. Configure Environment Variables
Copy and inspect the environment configuration file:
```bash
cp .env.example .env   # Or review existing .env in repository root
```

Key environment configuration variables (configured in `.env`):
```ini
# Application
APP_NAME=SentinelML
DEBUG=true
PORT=8000

# PostgreSQL
DATABASE_URL=postgresql+psycopg://<db_user>:<db_password>@localhost:5432/<db_name>

# MinIO S3 Storage
MINIO_ENDPOINT=http://localhost:9000
MINIO_ROOT_USER=<minio_user>
MINIO_ROOT_PASSWORD=<minio_password>

# LakeFS Version Control
LAKEFS_ENDPOINT=http://localhost:8000
LAKEFS_ACCESS_KEY_ID=<lakefs_access_key>
LAKEFS_SECRET_ACCESS_KEY=<lakefs_secret_key>

# MLflow Tracking
MLFLOW_TRACKING_URI=http://localhost:5000
MLFLOW_S3_ENDPOINT_URL=http://localhost:9000
```

---

### 3. Start Infrastructure Services
Launch PostgreSQL, MinIO, LakeFS, MLflow, and the Observability stack:
```bash
docker compose up -d
```

Verify that all service healthchecks report healthy:
```bash
docker compose ps
```

---

### 4. Start the Backend API Server
The backend startup script automatically waits for PostgreSQL, applies Alembic migrations, seeds default users, and starts the Uvicorn server:
```bash
cd backend
./run.sh
```
* Backend API documentation is live at **`http://localhost:8001/docs`**.

---

### 5. Start the Frontend Dashboard
In a separate terminal, start the React dev server:
```bash
cd frontend
npm install
npm run dev
```
* Open your browser and navigate to **`http://localhost:5173`**.

---

## Role-Based Access Control (RBAC) Accounts

SentinelML provisions role-based user accounts for role segregation:

| Username | Default Role | Permissions & Scopes |
| :--- | :--- | :--- |
| **`admin_user`** | `admin` | Full administrative control, user management, rules, alerts |
| **`ds_user`** | `data_scientist` | Dataset management, pipeline training, model uploads |
| **`mle_user`** | `ml_engineer` | Model deployments, RayService management, inference |
| **`viewer_user`** | `viewer` | Read-only inspection of models, metrics, and jobs |

> [!NOTE]
> Initial account passwords for local development are configured via `.env` / `SEED_ADMIN_PASSWORD` (see `backend/app/db/seed_db.py`). In production environments, credentials must be supplied via secure environment variables or an enterprise secret manager.

---

## Threat Detection Matrix (MITRE ATLAS / ATT&CK)

SentinelML's built-in Threat Hunting SIEM monitors 23 distinct detection rules:

| Category | Rule / Trigger Name | Severity | MITRE ATLAS / ATT&CK |
| :--- | :--- | :---: | :--- |
| **Model Governance** | Pickle Deserialization RCE Payload | `CRITICAL` | `AML.T0025` / `T1203` |
| | Model Artifact Integrity SHA-256 Digest Mismatch | `CRITICAL` | `AML.T0010` / `T1584` |
| | Neural Backdoor / Trojan Trigger in Weights | `CRITICAL` | `AML.T0018` / `T1584` |
| | Legacy Pickle Serialization in Production | `HIGH` | `AML.T0025` / `T1203` |
| | Unsigned / Invalidly Attested Model Promotion | `HIGH` | `AML.T0010` / `T1584` |
| | Unregistered Shadow Model Artifact in S3 | `HIGH` | `AML.T0010` / `T1584` |
| **Dataset & Storage** | Direct MinIO Storage Bypass (Unversioned Mutation) | `HIGH` | `AML.T0020` / `T1565.001` |
| | Tabular Dataset Schema Injection Anomaly | `HIGH` | `AML.T0020` / `T1565.001` |
| | Statistical Data Poisoning on Dataset Merge | `HIGH` | `AML.T0020` / `T1565.001` |
| | Unauthorized Dataset Branch Mutation / Deletion | `HIGH` | `AML.T0031` / `T1485` |
| **Inference Serving** | Algorithmic DoS: Tabular Memory Flooding | `HIGH` | `AML.T0029` / `T1499` |
| | Decision Boundary Scraping (Model Inversion) | `HIGH` | `AML.T0024` |
| | Systematic Feature Perturbation (Model Stealing) | `HIGH` | `AML.T0040` |
| | Adversarial Perturbation (FGSM/PGD Noise) | `HIGH` | `AML.T0043` / `T1499` |
| | Model Behavioral Drift: Abrupt Confidence Degradation | `HIGH` | `AML.T0024` |
| | Algorithmic DoS: Latency Spike Anomaly | `HIGH` | `AML.T0029` / `T1499` |
| | Anomalous Inference Rate Burst | `MEDIUM` | `AML.T0024` |
| **Cluster & Runtime** | Unauthorized Container Process (Falco eBPF) | `CRITICAL` | `AML.T0038` / `T1059.004` |
| | Unregistered Rogue RayServe Deployment | `CRITICAL` | `AML.T0038` / `T1584` |
| | Unexpected RayJob Submission Spikes | `MEDIUM` | `AML.T0002` / `T1496` |
| **Authentication** | Repeated Authentication Failures (Brute Force) | `HIGH` | `T1110.001` |
| | Revoked Token Reuse Replay Attempt | `HIGH` | `T1550` |
| | Excessive 401/403 Requests (Privilege Enumeration) | `MEDIUM` | `T1078` |

---

## Testing & Quality Assurance

SentinelML features an automated test suite covering unit tests, integration pipelines, and adversarial attack simulations:

### Backend Test Suite
```bash
cd backend
pytest -v
```

Run specific security test suites:
```bash
# Model upload & safe serialization tests
pytest tests/test_model_upload.py -v

# Real-time inference security & scraping detector tests
pytest tests/test_inference_security.py -v

# LakeFS pre-merge poisoning & schema injection tests
pytest tests/test_dataset_catalog.py -v
```

### Frontend Linting & Production Build
```bash
cd frontend
npx oxlint --deny-warnings
npm run build
```

---

## License

This project is licensed under the **Apache License 2.0**.
