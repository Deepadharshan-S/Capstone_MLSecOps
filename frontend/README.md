# SentinelML Security Dashboard (Frontend)

The **SentinelML Frontend** is a modern security dashboard and operations console built with **React 18**, **Vite**, and a responsive **Vanilla CSS Design System**. It provides real-time visibility and management across machine learning datasets, model governance pipelines, distributed training jobs, model deployments, and the threat hunting SIEM.

---

## 1. Responsibilities & Architecture

```text
┌────────────────────────────────────────────────────────────────────────┐
│                        SENTINELML SECURITY DASHBOARD                   │
├───────────────────────────────────┬────────────────────────────────────┤
│ Machine Learning Operations       │ Security & Threat Defense          │
│ • Datasets & lakeFS Catalog       │ • Security Center (SIEM Triage)    │
│ • Candidate Training Pipelines    │ • Ingested Security Events Feed    │
│ • Model Registry & Governance     │ • 23 MITRE Detection Rules Engine  │
│ • RayJob Distributed Clusters     │ • Historical SQL Threat Hunts      │
│ • RayService Inference Serving    │ • Label-Free Drift Monitor (D1-D3) │
│ • Real-time Test Predictions      │ • Adversarial Attack Simulator     │
└───────────────────────────────────┴────────────────────────────────────┘
                                    │
                                    │ Axios HTTP Clients (Bearer JWT)
                                    ▼
                     FastAPI Backend Gateway (:8001)
```

### Core Subsystems
- **Role-Based UI Gating (`RoleGate`)**: Dynamically renders navigation links, action buttons, and pages according to user roles (`admin`, `data_scientist`, `ml_engineer`, `viewer`).
- **Route-Level Code Splitting**: Every top-level page and heavy tab component is lazy-loaded using React `lazy` and `Suspense` to ensure fast initial page loads.
- **Backend Health Chip**: Polled periodically against `GET /health` to display live dependency health (PostgreSQL, lakeFS, MinIO, MLflow) in the topbar.
- **Visual Analytics**: Interactive telemetry visualizers and statistical distributions powered by **Recharts** and **Lucide React**.

---

## 2. Directory Structure

```text
frontend/
├── dist/                         # Compiled production bundle assets
├── public/                       # Static public assets and favicons
├── src/
│   ├── api/                      # Centralized Axios API client modules
│   │   ├── client.js             # Base Axios instance with JWT interceptors
│   │   └── endpoints.js          # Typed API endpoint methods
│   ├── auth/                     # Authentication context and role gate
│   │   └── AuthContext.jsx       # AuthProvider, useAuth hook, and RoleGate
│   ├── components/               # Reusable UI component library
│   │   ├── icons.jsx             # Lucide React icon wrappers
│   │   └── ui/                   # Modal, Badge, Tabs, Table, Card, Button
│   ├── hooks/                    # Custom React hooks (useFetch, usePolling)
│   ├── pages/                    # Code-split application route views
│   │   ├── Admin.jsx             # User administration & role management
│   │   ├── Dashboard.jsx         # Executive overview, deployments, alerts
│   │   ├── DatasetDetail.jsx     # lakeFS branch browser, commits, file upload
│   │   ├── Datasets.jsx          # Dataset catalog & repository creation
│   │   ├── Deployments.jsx       # RayService live deployments & replicas
│   │   ├── Experiments.jsx       # MLflow experiment and run inspector
│   │   ├── Jobs.jsx              # KubeRay RayJob submission list & live logs
│   │   ├── Pipeline.jsx          # Multi-algorithm candidate training tournament
│   │   ├── Predict.jsx           # Live model prediction & latency testing
│   │   ├── Registry.jsx          # Registered model versions, tags, safe upload
│   │   ├── Security.jsx          # Security Center container with SIEM tabs:
│   │   │   └── security/
│   │   │       ├── AlertsTab.jsx # Real-time alert triage & status updates
│   │   │       ├── DriftTab.jsx  # Feature (D1), Prediction (D2), Confidence (D3) drift
│   │   │       ├── EventsTab.jsx # Raw security event stream & correlation IDs
│   │   │       ├── HuntsTab.jsx  # Historical SQL threat hunting execution
│   │   │       └── RulesTab.jsx  # Detection rules manager & test runner
│   │   └── Testing.jsx           # Adversarial attack & security test workbench
│   ├── shell/                    # Topbar, Sidebar navigation, and UserMenu
│   ├── index.css                 # Vanilla CSS design tokens & stylesheet
│   ├── main.jsx                  # React DOM root entrypoint
│   └── App.jsx                   # Application layout and router definition
├── index.html                    # Single-page application HTML entrypoint
├── package.json                  # Node.js dependencies and build scripts
└── vite.config.js                # Vite build and development proxy configuration
```

---

## 3. Setup & Development

### Prerequisites
- **Node.js 18+**
- **npm** (v9+)
- Running SentinelML Backend service on `http://127.0.0.1:8001`

### Installation
From the `frontend/` directory:

```bash
npm install
```

### Starting the Development Server
```bash
npm run dev
```

* The dashboard is accessible at **`http://localhost:5173`**.
* The development server enforces `strictPort: true` on port `5173` to prevent CORS issues with the backend.

### Development Proxy (`vite.config.js`)
During local development, API requests are automatically reverse-proxied to the backend:
- Requests to `/api/*` -> `http://127.0.0.1:8001/api/*`
- Requests to `/health` -> `http://127.0.0.1:8001/health`

---

## 4. Authentication & Role-Based Access Control

The frontend includes role-based route and UI gating:

| Role | Permitted Access | Key Pages |
| :--- | :--- | :--- |
| **`admin`** | Full access to all features, user administration, rule management | All pages including `/admin` and `/security` |
| **`data_scientist`** | Dataset management, branch creation, training pipelines, model uploads | `/datasets`, `/pipeline`, `/jobs`, `/experiments`, `/registry` |
| **`ml_engineer`** | Model deployments, RayService scaling, inference testing, model uploads | `/deployments`, `/predict`, `/jobs`, `/registry` |
| **`viewer`** | Read-only inspection of metrics, jobs, models, and datasets | `/`, `/datasets`, `/jobs`, `/experiments`, `/registry` |

---

## 5. Quality Assurance & Building for Production

### Linting
Validate the codebase using Oxlint:
```bash
npx oxlint --deny-warnings
```

### Production Build
Compile and bundle the production assets into `dist/`:
```bash
npm run build
```

### Previewing Production Build
```bash
npm run preview
```
