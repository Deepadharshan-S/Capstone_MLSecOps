# SentinelML — Fresh Software Engineering & Architecture Audit Report

**Audit Date:** October 3, 2026  
**Auditor:** SentinelML Independent Security & Architecture Review System  
**Repository Branch:** `continuous_training`  
**Commit Hash:** `c8f73e61da8526104ff4490fb0ab1f762a40a684`  
**Execution Context:** Fresh, Independent Codebase Audit (Zero Preconceptions, Read-Only Code Execution)  

---

## 1. Executive Baseline & Environment Assessment

### 1.1 Git Status & Commit History
- **Current Branch:** `continuous_training`
- **Head Commit:** `c8f73e6` — *fix(cleanup): ensure comprehensive cleanup of ConfigMaps, RayJobs, RayClusters, and temp files post-training*
- **Previous Commits:**
  - `5ab066d`: *fix(serving): enable dynamic host port-forwarding and DB resolution for RayService inference*
  - `b68be08`: *fix(jobs): reconcile active jobs against live Kubernetes RayJob state and MLflow artifacts*
  - `e697394`: *fix(scripts): support root .env resolution in backend/run.sh*
  - `311935a`: *fix(server): add --reload flag to uvicorn in run.sh for development hot reloading*
- **Working Tree State:** Clean (only audit report documents written per instruction).

### 1.2 Automated Tooling & Test Execution Baseline
- **Pytest Test Suite:**
  - Command: `PYTHONPATH=backend .venv/bin/pytest backend/tests/ -q`
  - Result: **145 passed, 18 warnings in 426.11s (07:06)**
  - Note: Tests require `PYTHONPATH=backend` when run via raw `.venv/bin/pytest` due to direct imports of package namespace `app`.
- **Alembic Database Migrations:**
  - Command: `PYTHONPATH=backend .venv/bin/alembic -c backend/alembic.ini heads`
  - Output: `8a1b2c3d4e5f (head)`
  - Command: `PYTHONPATH=backend .venv/bin/alembic -c backend/alembic.ini current`
  - Output: `8a1b2c3d4e5f (head)` — Single authoritative head matches active PostgreSQL schema.
- **Frontend Linter & Production Build:**
  - Linter (`oxlint`): **0 errors**, 4 fast-refresh export warnings on 30 files.
  - Build (`vite build`): **Succeeded in 3.06s**, generated 31 bundled chunks (gzip: 83.55 kB index JS).
- **Docker Compose Configuration:**
  - Command: `docker compose config --quiet`
  - Result: 0 syntax or validation errors. All 4 core services (`sentinelml-postgres`, `sentinelml-minio`, `sentinelml-lakefs`, `sentinelml-mlflow`) are running and healthy.

---

## 2. Actual Codebase Architecture Map

Based on direct source code inspection of `backend/app/`, `frontend/src/`, `k8s/`, and `docker-compose.yml`:

```
                       ┌────────────────────────────────────────────────────────┐
                       │           Frontend Client (React 19 + Vite 8)          │
                       │   In-Memory JWT Access Token + HttpOnly Refresh Cookie  │
                       └───────────────────────────┬────────────────────────────┘
                                                   │ HTTP / REST / JSON
                                                   ▼
                       ┌────────────────────────────────────────────────────────┐
                       │                  FastAPI API Gateway                   │
                       │     Auth, Datasets, MLOps, Deployments, Telemetry      │
                       └────────┬──────────────┬──────────────┬──────────────┬──┘
                                │              │              │              │
           ┌────────────────────┼──────────────┼──────────────┼──────────────┴─────┐
           ▼                    ▼              ▼              ▼                    ▼
    ┌──────────────┐     ┌──────────────┐┌──────────────┐┌─────────────┐    ┌──────────────┐
    │  PostgreSQL  │     │    lakeFS    ││   MinIO S3   ││   MLflow    │    │  Kubernetes  │
    │  (Relational │     │  (Data Lake  ││  (Artifact   ││ (Experiment │    │ (Custom API, │
    │   Database)  │     │  Versioning) ││   Storage)   ││  Registry)  │    │  CoreV1Api)  │
    └──────────────┘     └──────────────┘└──────────────┘└─────────────┘    └──────┬───────┘
                                                                                   │
                                                        ┌──────────────────────────┴─────────┐
                                                        ▼                                    ▼
                                                ┌──────────────┐                     ┌──────────────┐
                                                │    RayJob    │                     │  RayService  │
                                                │ (Batch Train │                     │ (HTTP Serve  │
                                                │  in Cluster) │                     │  in Cluster) │
                                                └──────┬───────┘                     └──────┬───────┘
                                                       │ Post-Job Sweep                     │ PortForward
                                                       ▼                                    ▼ Manager (127.0.0.1)
                                                ┌──────────────┐                     ┌──────────────┐
                                                │ Auto-Cleanup │                     │ Local Proxy  │
                                                │ (CM & Pods)  │                     │ Host Ingress │
                                                └──────────────┘                     └──────────────┘
```

---

## 3. Software Engineering Findings

### Finding ID: SW-AUDIT-001
- **Title:** Undefined Name `logger` in `TrainingJobService.reconcile_active_jobs()`
- **Severity:** HIGH
- **Category:** CORRECTNESS
- **Location:** `backend/app/services/ml_ops/training_job_service.py:269`
- **Evidence:**
  ```python
  # backend/app/services/ml_ops/training_job_service.py:266-270
  self.repository.save_all(active_jobs)
  try:
      self.rayjob_service.sweep_orphaned_job_resources()
  except Exception as sweep_err:
      logger.debug(f"Resource sweep note: {sweep_err}")  # <-- NameError: name 'logger' is not defined
  ```
  Linter verification: `ruff check backend/app/services/ml_ops/training_job_service.py` returns:
  `F821 Undefined name logger` at line 269.
- **Impact:** If `sweep_orphaned_job_resources()` encounters any exception during post-job reconciliation, the except block crashes with `NameError: name 'logger' is not defined`, aborting job reconciliation and failing the API response with HTTP 500.
- **Attack/Failure Scenario:** A user calls `GET /api/jobs` or `GET /api/jobs/{job_id}` while a cluster resource error occurs during sweeping. Instead of gracefully ignoring or logging the warning, the request crashes with unhandled `NameError`.
- **Recommendation:** Import and configure standard logging in `training_job_service.py`:
  ```python
  import logging
  logger = logging.getLogger("training_job_service")
  ```
- **Confidence:** CONFIRMED

---

### Finding ID: SW-AUDIT-002
- **Title:** Missing Top-Level Module Export Requires `PYTHONPATH=backend` for Standalone Tooling
- **Severity:** MEDIUM
- **Category:** MAINTAINABILITY
- **Location:** Project layout (`backend/app`) and `pyproject.toml`
- **Evidence:**
  Running `pytest backend/tests/` without `PYTHONPATH=backend` produces:
  ```
  ImportError while loading conftest '.../backend/tests/conftest.py'.
  backend/tests/conftest.py:6: in <module>
      from app.core.config import settings
  E   ModuleNotFoundError: No module named 'app'
  ```
  Similarly, running `alembic current` without `PYTHONPATH=backend` produces `ModuleNotFoundError: No module named 'app'`.
- **Impact:** Developers or automated CI runners running standard `pytest` or `alembic` commands without explicitly prepending `PYTHONPATH=backend` fail immediately.
- **Attack/Failure Scenario:** CI/CD pipeline triggers test run with standard `pytest` command; build fails on conftest load.
- **Recommendation:** Add a `[tool.pytest.ini_options]` configuration in `pyproject.toml` containing `pythonpath = ["backend"]` or install backend as an editable package (`pip install -e .`).
- **Confidence:** CONFIRMED

---

### Finding ID: SW-AUDIT-003
- **Title:** Duplicated Kubernetes API Client Instantiation Across Domain Services
- **Severity:** LOW
- **Category:** ARCHITECTURE
- **Location:**
  - `backend/app/services/ml_ops/training_service.py:34-45`
  - `backend/app/services/ml_ops/deployment_service.py:30-41`
  - `backend/app/services/ml_ops/rayjob_service.py:28-36`
- **Evidence:**
  Each of these domain services implements its own private `_get_k8s_apis(self)` helper that loads `config.load_incluster_config()` / `config.load_kube_config()` and instantiates `client.CustomObjectsApi()`, `client.CoreV1Api()`, and `client.AppsV1Api()`.
- **Impact:** Lack of a centralized Kubernetes adapter layer increases code duplication and complicates mock injection during unit testing.
- **Attack/Failure Scenario:** A change in Kubernetes cluster authentication or API client configuration must be manually replicated across three distinct service files.
- **Recommendation:** Refactor Kubernetes client initialization into a dedicated adapter provider (`backend/app/core/k8s_client.py`) and inject it via FastAPI dependencies.
- **Confidence:** CONFIRMED

---

### Finding ID: SW-AUDIT-004
- **Title:** Direct FastAPI `HTTPException` Coupling in Domain Service Classes
- **Severity:** LOW
- **Category:** ARCHITECTURE
- **Location:**
  - `backend/app/services/dataset/storage_service.py:55`
  - `backend/app/services/dataset/catalog_service.py:4`
  - `backend/app/services/dataset/versioning_service.py:2`
  - `backend/app/services/dataset/diff_service.py:2`
  - `backend/app/services/ml_ops/serving_service.py:22`
  - `backend/app/services/ml_ops/deployment_service.py:18`
- **Evidence:**
  Domain services directly import `from fastapi import HTTPException, status` and raise HTTP exceptions within business logic methods rather than raising pure domain exceptions.
- **Impact:** Prevents domain service reusability outside FastAPI context (such as standalone background scripts, Ray worker actors, or CLI utilities).
- **Attack/Failure Scenario:** Calling `storage_service.upload_file()` inside a standalone maintenance task forces FastAPI dependencies and raises HTTP-specific errors.
- **Recommendation:** Raise domain-specific exceptions (e.g. `StorageOperationError`, `DeploymentNotFoundError`) and translate them at the router boundary using FastAPI exception handlers.
- **Confidence:** CONFIRMED

---

### Finding ID: SW-AUDIT-005
- **Title:** Unused Imports and Unassigned Variables Flagged Across Service Layer
- **Severity:** LOW
- **Category:** MAINTAINABILITY
- **Location:**
  - `backend/app/api/datasets.py:14, 105` (`io` imported but unused; unused exception variable `e`)
  - `backend/app/api/permissions.py:12` (`BlacklistedToken` unused)
  - `backend/app/repositories/deployment_repository.py:3` (`sqlalchemy.update` unused)
  - `backend/app/services/auth/auth_service.py:23` (`settings` unused)
  - `backend/app/services/ml_ops/deployment_service.py:296, 531` (`mlflow_client` assigned but unused; `e` unused)
  - `backend/app/services/ml_ops/serving_service.py:2` (`json` unused)
  - `backend/app/services/ml_ops/utils.py:6` (`shutil` unused)
- **Evidence:** `ruff check backend/app` reports 14 lint errors across core application modules.
- **Impact:** Code clutter and slight cognitive overhead during code reviews; potential confusion regarding intended logic.
- **Attack/Failure Scenario:** N/A (Code hygiene issue).
- **Recommendation:** Run `ruff check --fix backend/app` to clean up unused imports and dead local variables.
- **Confidence:** CONFIRMED

---

### Finding ID: SW-AUDIT-006
- **Title:** Frontend React Fast-Refresh Export Violations
- **Severity:** INFORMATIONAL
- **Category:** MAINTAINABILITY
- **Location:**
  - `frontend/src/auth/AuthContext.jsx:132` (`useAuth`)
  - `frontend/src/components/ui/index.jsx:93` (`statusTone`)
  - `frontend/src/components/ui/index.jsx:321` (`useToast`)
  - `frontend/src/components/ui/index.jsx:373` (`useConfirm`)
- **Evidence:** `npm run lint` (`oxlint`) reports 4 warnings:
  `react(only-export-components): Fast refresh only works when a file only exports components. Use a new file to share constants or functions between components.`
- **Impact:** Vite React Fast Refresh may perform full-page reloads instead of hot module replacement when editing UI components.
- **Attack/Failure Scenario:** Developer productivity degradation during local UI development due to state loss on full reload.
- **Recommendation:** Extract hook definitions (`useAuth`, `useToast`, `useConfirm`) and helper functions (`statusTone`) into separate dedicated modules (e.g. `src/hooks/` and `src/utils/`).
- **Confidence:** CONFIRMED

---

## 4. Software Quality & Testing Verification

### 4.1 Pytest Suite Execution
- **Collected:** 145 items
- **Passed:** 145 tests
- **Failed:** 0
- **Duration:** 426.11 seconds
- **Warnings:** 18 deprecation warnings (primarily Pydantic V2 deprecations inside third-party `ray.serve._private.config`).

### 4.2 API Endpoint Coverage Verification
All 42 registered API endpoints in FastAPI route tables were verified against implementation:
- 5 Auth routes (`/api/auth/*`)
- 4 User & Audit routes (`/api/users/*`)
- 15 Dataset Versioning routes (`/api/datasets/*`)
- 18 MLOps, Training & Deployment routes (`/api/models/*`, `/api/jobs/*`, `/api/deployments/*`, `/api/experiments/*`)

All routes properly implement Pydantic request/response validation schemas and use dependency injection for database sessions.
