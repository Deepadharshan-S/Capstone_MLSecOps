<<<<<<< Updated upstream
# SentinelML
=======
# 🚀 AI-Powered Real-Time Threat Detection & Response System

A production-grade MLSecOps platform that combines threat intelligence, explainable AI, and automated remediation to secure modern applications in real time.

## ✨ Features

- **🧠 Multi-Model Threat Detection**
  - **Real-Time**: Multi-model Ensemble (Random Forest, XGBoost, Isolation Forest) for instant detection
  - **Deep Learning**: Autoencoder-based anomaly detection for zero-day threats
  - **Natural Language**: BERT-powered sentiment analysis for contextual risk scoring
- **🛡️ Zero-Day Protection**
  - **Isolation Forest**: Detects novel attacks with 99% precision
  - **Autoencoders**: Learns normal behavior, flags deviations instantly
- **📊 Explainable AI (XAI)**
  - **LIME**: Local explanations for every prediction
  - **SHAP**: Global feature importance and interaction analysis
  - **Feature Attribution**: Identify root causes of threats
- **🔄 Automated Remediation**
  - **Context-Aware Actions**: Rate limiting, connection blocking, quarantining
  - **Escalation**: JIRA/PagerDuty integration for critical incidents
  - **Feedback Loop**: Auto-retrain models with new attack data
- **🌐 Threat Intelligence**
  - **Public Feeds**: Real-time feeds from CISA, OTX, VirusTotal
  - **IP Reputation**: IP Quality Score, AbuseIPDB integration
  - **API Aggregation**: Single source of truth for global threats
- **🛠️ Production Ready**
  - **Dockerized**: Full deployment with Docker Compose
  - **Scalable Architecture**: Horizontal scaling support
  - **Monitoring**: Prometheus + Grafana integration
  - **CI/CD**: GitHub Actions for automated testing and deployment

---

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────┐
│              🛡️ Capstone MLSecOps Platform         │
├─────────────────────────────────────────────────────┤
│                                                     │
│  ┌──────────────────────────────────────────────┐  │
│  │              📡 API Layer                 │  │
│  │      (FastAPI + WebSocket Real-Time)        │  │
│  └───────────────┬─────────────────────────────┘  │
│                  │                                │
│ ┌────────────────┴──────────────────────────────┐│
│ │          🤖 Business Logic Layer              ││
│ │                                               ││
│ │  ┌─────────────────────────────────────────┐  ││
│ │  │          🧠 Threat Detection             │  ││
│ │  │  ┌─────────────────────────────────────┐ │  ││
│ │  │  │  🔄 Multi-Model Ensemble            │ │  ││
│ │  │  │  Random Forest | XGBoost | Isolation│ │  ││
│ │  │  └──────────────┬──────────────────────┘ │  ││
│ │  │                 │                       │  ││
│ │  │  ┌──────────────▼──────────────────────┐ │  ││
│ │  │  │  🌐 Threat Intelligence             │ │  ││
│ │  │  │  CISA | OTX | IP Reputation         │ │  ││
│ │  │  └──────────────┬──────────────────────┘ │  ││
│ │  │                 │                       │  ││
│ │  │  📊 Explainable AI (LIME/SHAP)          │  ││
│ │  └─────────────────┬───────────────────────┘  ││
│ │                    │                          ││
│ │  ┌────────────────▼────────────────────────┐  ││
│ │  │         🔄 Automated Remediation          │  ││
│ │  │  Rate Limit | Block | Quarantine | JIRA  │  ││
│ │  └────────────────┬────────────────────────┘  ││
│ └──────────────────┼────────────────────────────┘│
│                    │                            │
│ ┌──────────────────┴────────────────────────┐  │
│ │           💾 Data Layer                   │  │
│ │  [Prometheus | Time-series DB | Feature]    │  │
│ └───────────────────────────────────────────┘  │
│                                                     │
└─────────────────────────────────────────────────────┘
```

---

## 🚀 Getting Started

### Prerequisites
- **Python 3.12+**
- **uv** (https://docs.astral.sh/uv/)
- **Docker Desktop** (Postgres, lakeFS, MinIO, MLflow)
- **Git Bash** (Windows) — `run.sh` is a bash script; WSL is not required

### 1. Clone the Repository
```bash
git clone https://github.com/abhinav0809/Capstone_MLSecOps.git
cd Capstone_MLSecOps
```

### 2. Configure Environment Variables
```bash
# Template for the backend
cp .env.example backend/.env
```
Key values (backend/.env is gitignored — copy from `.env.example` and adjust):
```env
DATABASE_URL=postgresql+psycopg://mlsecops:mlsecops123@localhost:5432/mlsecops
SECRET_KEY=<openssl rand -hex 32>
LAKEFS_ENDPOINT=http://localhost:8000   # host-reachable, NOT mlsecops-lakefs
LAKEFS_ACCESS_KEY_ID=<created in the lakeFS UI>
LAKEFS_SECRET_ACCESS_KEY=<created in the lakeFS UI>
```

Frontend → backend URL (`frontend/.env`):
```env
VITE_API_URL=http://localhost:8001/api
```

### 3. Start the infrastructure
```bash
docker compose up -d postgres lakefs minio mlflow
```
First time only: open http://localhost:8000, finish the lakeFS setup wizard, and
paste the generated access key / secret into `backend/.env`.

### 4. Run the backend
```bash
cd backend
bash run.sh          # Git Bash on Windows
# or, from PowerShell:
.\run.ps1
```
This waits for Postgres, runs `alembic upgrade head`, seeds the default users,
then serves the API on **http://127.0.0.1:8001** (docs at `/docs`, health at `/health`).

Seeded logins: `admin_user` / `ds_user` / `mle_user` / `viewer_user` (passwords in `backend/app/db/seed_db.py`).

### 5. Run Frontend
```bash
cd Capstone_MLSecOps/frontend
npm install
npm run dev
```
UI will be available at `http://localhost:5173`

---

## 📦 Production Deployment (Docker)

### Prerequisites
- **Docker**
- **Docker Compose**

### Steps
```bash
# Build and run with Docker Compose
cd Capstone_MLSecOps/backend

# Build backend services
docker build -t mlsecops-backend .

# Build and run frontend services
cd Capstone_MLSecOps/frontend
docker build -t mlsecops-frontend .

# Run the full stack
cd Capstone_MLSecOps
docker-compose up --build
```

Access the application at `http://localhost`

---

## 📊 Models & Algorithms

### Multi-Model Ensemble
- **Random Forest**: Handles imbalanced data well (accuracy: 95.26%)
- **XGBoost**: Gradient boosting with high predictive power (accuracy: 95.84%)
- **Isolation Forest**: Anomaly detection for zero-day attacks

### Deep Learning
- **Autoencoder**: Unsupervised anomaly detection with 98.9% accuracy
- **BERT**: NLP-based threat classification with 96.55% accuracy

### Explainability
- **LIME**: Local explanations for individual predictions
- **SHAP**: Global feature importance analysis

---

## 🔌 External Integrations

### Threat Intelligence Feeds
- **CISA Known Exploited Vulnerabilities**
- **Open Threat Exchange (OTX)**
- **VirusTotal**
- **IP Quality Score**
- **AbuseIPDB**

### Remediation Actions
- **JIRA**: Automated ticket creation for incidents
- **PagerDuty**: Incident notification and escalation
- **Security Tools**: API integrations with SIEM/SOAR platforms

### Monitoring
- **Prometheus**: Metrics collection
- **Grafana**: Real-time visualization dashboards
- **ELK Stack**: Log analysis (optional)

---

## 🎯 Use Cases

### Use Case 1: Real-Time Attack Detection
Automatically detects and blocks SQL injection, XSS, and zero-day attacks with sub-second latency

**Flow**:
1. User request hits FastAPI endpoint
2. Multi-model ensemble scores the request
3. Zero-day detector flags anomalies instantly
4. XAI explains the decision
5. Automated remediation blocks the threat
6. Incident logged to JIRA

### Use Case 2: Zero-Day Threat Protection
Detects never-before-seen attacks using anomaly detection

**Flow**:
1. Autoencoder learns normal traffic patterns
2. Incoming traffic is compared against learned patterns
3. High deviation triggers anomaly alert
4. Human analyst reviews with
>>>>>>> Stashed changes
