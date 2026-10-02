# SentinelML — Software & Architecture Audit Report

**Date:** October 2, 2026  
**Auditor:** SentinelML Automated Security & Architecture Review System  
**Repository Branch:** `develop` (commit `e4f0ac9`) vs `main` (commit `f2e1c4e`)  
**Audit Scope:** Full codebase (Backend, Frontend, Infrastructure, Database, Observability)  
**Execution Mode:** Read-Only Audit (Zero Production Modifications)

---

## 1. Executive Baseline & Environment Assessment

### 1.1 Git Status & Baseline
- **Current Branch:** `develop`
- **Head Commit:** `e4f0ac9` ("frontend fix")
- **Base Reference:** `main` (commit `f2e1c4e` - "Merge branch 'model_deployment' into main")
- **Working Tree:** Clean (no uncommitted tracked modifications)
- **Baseline Test Status:**
  - **On `main`:** 143 unit/integration tests passing cleanly (`pytest backend/tests/`).
  - **On `develop`:** **BLOCKED (CRITICAL REGRESSION)**. The test suite fails to import during conftest collection due to a `SyntaxError: invalid syntax` in `backend/app/core/config.py` caused by raw merge conflict markers (`<<<<<<< Updated upstream`).

### 1.2 Actual Repository Architecture Map

```
                    ┌───────────────────────────────────────────────┐
                    │       Frontend Client (React 19 + Vite)       │
                    │   In-Memory JWT Access Token + HttpOnly Cookie │
                    └───────────────────────┬───────────────────────┘
                                            │ HTTP / JSON (CORS)
                                            ▼
                    ┌───────────────────────────────────────────────┐
                    │               FastAPI Gateway                 │
                    │   Auth, Users, Datasets, MLOps, Deployments   │
                    └───────┬──────────────┬──────────────┬─────────┘
                            │              │              │
           ┌────────────────┼──────────────┼──────────────┼────────────────┐
           ▼                ▼              ▼              ▼                ▼
    ┌────────────┐   ┌────────────┐ ┌────────────┐ ┌─────────────┐ ┌──────────────┐
    │ PostgreSQL │   │   lakeFS   │ │   MinIO    │ │   MLflow    │ │  Kubernetes  │
    │  Database  │   │ Versioning │ │ S3 Storage │ │ Tracking &  │ │ KubeRay CRDs │
    │ (Auth/Jobs)│   │  (Git-like)│ │ (Artifacts)│ │  Registry   │ │(Job/Service) │
    └────────────┘   └────────────┘ └────────────┘ └─────────────┘ └──────┬───────┘
           │                                                              │
           │                                       ┌──────────────────────┴───────┐
           │                                       ▼                              ▼
           │                               ┌──────────────┐               ┌──────────────┐
           │                               │    RayJob    │               │  RayService  │
           │                               │ Batch Train  │               │ HTTP Serving │
           │                               └──────────────┘               └──────┬───────┘
           │                                                                     │
           ▼                                                                     │
    ┌───────────────────────────────────────────────────────────────────┐        │
    │                     OpenTelemetry Ecosystem                       │        │
    │  FastAPI (HTTP) ──> OTel Collector ──> Tempo (Distributed Tracing)│<───────┘
    │                     OTel Collector ──> Prometheus (Metrics)       │
    │                     OTel Collector ──> Loki (Logs)                │
    │                     Grafana (Unified Dashboards)                  │
    └───────────────────────────────────────────────────────────────────┘
```

---

## 2. Software Architecture & Layering Audit

### 2.1 Layering & Separation of Concerns
1. **HTTP Coupling in Service Layers:**
   - Multiple domain services directly import and raise FastAPI HTTP exceptions (`from fastapi import HTTPException, status`).
   - Examples:
     - `backend/app/services/dataset/storage_service.py` (lines 2, 55-58)
     - `backend/app/services/ml_ops/job_service.py` (lines 7, 72)
     - `backend/app/services/ml_ops/training_service.py` (lines 8, 87)
     - `backend/app/services/dataset/catalog_service.py`
   - *Impact:* Prevents domain services from being reused outside FastAPI (e.g., in background CLI tasks, Ray workers, Celery/cron handlers, or pure unit tests).

2. **Scattered Infrastructure Logic (Kubernetes API):**
   - Kubernetes API clients are instantiated ad-hoc inside domain services rather than accessed via an injected abstraction adapter:
     - `backend/app/services/ml_ops/training_service.py` lines 30–37 (`_get_k8s_apis`)
     - `backend/app/services/ml_ops/deployment_service.py` lines 28–35 (`_get_k8s_apis`)
     - `backend/app/services/ml_ops/rayjob_service.py` lines 34–41 (`_get_k8s_apis`)
   - *Impact:* Tight coupling to the Kubernetes SDK, complicating mockability and unit test execution without an active cluster context.

3. **Duplicated & Incompatible Job Tracking Abstractions:**
   - `main` branch implemented `TrainingJobService` (`backend/app/services/ml_ops/training_job_service.py`) backed by the authoritative `TrainingJob` schema (`job_id`, `rayjob_name`, `dataset_id`, `ref`, `epochs`, `hyperparameters`, `entrypoint`).
   - `develop` introduced a conflicting second service `JobService` in `backend/app/services/ml_ops/job_service.py` expecting completely different fields (`model_type`, `target_column`, `progress`, `accuracy`, `precision_score`, `user_id`).
   - *Impact:* Severe architectural fragmentation and immediate runtime type errors when calling `job_service.create_job()`.

---

## 3. Code Quality & Lifecycle Audit

### 3.1 Unresolved Git Merge Conflict Markers
- **Finding:** Branch `develop` contains **31 raw merge conflict markers** (`<<<<<<< Updated upstream`, `=======`, `>>>>>>> Stashed changes`) checked into version control across 16 core files.
- **Affected Files:**
  - `.env.example`
  - `README.md`
  - `docker-compose.yml`
  - `backend/app/api/datasets.py`
  - `backend/app/api/ml_ops.py`
  - `backend/app/core/config.py`
  - `backend/app/db/session.py`
  - `backend/app/main.py`
  - `backend/app/models/__init__.py`
  - `backend/app/schemas/ml_ops/__init__.py`
  - `backend/app/schemas/ml_ops/training.py`
  - `backend/app/services/auth/auth_service.py`
  - `backend/app/services/dependencies.py`
  - `backend/app/services/ml_ops/__init__.py`
  - `backend/app/services/ml_ops/deployment_service.py`
  - `backend/app/services/ml_ops/registry_service.py`
  - `backend/app/services/ml_ops/serving_service.py`
  - `backend/app/services/ml_ops/training_service.py`
  - `backend/app/services/ml_ops/utils.py`

### 3.2 Synchronous Blocking I/O in Async Request Paths
- In `backend/app/services/dataset/lakefs_service.py` (lines 103–110), `download_file` executes:
  ```python
  with obj.reader(mode="rb") as reader:
      return reader.read()
  ```
- *Impact:* Synchronously buffers multi-gigabyte dataset files directly into memory during HTTP request handling, blocking Python's event loop and risking Out-Of-Memory (OOM) worker crashes.

### 3.3 Redundant Telemetry Initialization
- In `backend/app/main.py`:
  - `setup_telemetry()` is called at module evaluation time.
  - Telemetry instrumentation is also triggered inside FastAPI's async lifespan context manager.
- *Impact:* Redundant tracer provider initializations, duplicate metric reader registrations, and warning noise during startup.

---

## 4. Database & Alembic Migrations Audit

### 4.1 Multiple Migration Heads (Split Head Conflict)
- Running `.venv/bin/alembic -c backend/alembic.ini heads` reveals **two divergent heads**:
  1. `8a1b2c3d4e5f (head)`: Created by migration sequence `46bfc95b5dd9 -> 7ef148a0dad6 -> 710094a7e4a1 -> 8a1b2c3d4e5f`.
  2. `bee587bde3b6 (head)`: Added on `develop`, branching off `46bfc95b5dd9`.
- **Root Cause:** Both `7ef148a0dad6` and `bee587bde3b6` attempt to create the `training_jobs` table with incompatible schemas:
  - `7ef148a0dad6`: Table columns `job_id`, `rayjob_name`, `status`, `dataset_id`, `ref`, `model_name`, `experiment_name`, `epochs`, `hyperparameters`, `entrypoint`, `duration_seconds`, `created_by_id`.
  - `bee587bde3b6`: Table columns `job_id` (String(12)), `user_id`, `dataset_id`, `model_type`, `target_column`, `progress`, `accuracy`, `precision_score`, `confusion_matrix`.
- *Impact:* `alembic upgrade head` aborts with `Multiple head revisions are present`. The database cannot be reliably migrated on new deployments.

---

## 5. API Surface & Endpoint Coverage Audit

The FastAPI application exposes **42 operational endpoints** across four core routers plus root/health endpoints:

| Endpoint Route | Method | Auth / Scope | Input Validation Schema | Response Model | Database Interaction | External System Calls | Status |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| `/` | `GET` | Public | None | `dict` | None | None | Clean |
| `/health` | `GET` | Public | None | `JSONResponse` | `SELECT 1` | lakeFS, MinIO, MLflow | Clean |
| `/api/auth/register` | `POST` | Public (RateLimited) | `UserCreate` | `UserResponse` | User INSERT | None | Clean |
| `/api/auth/login` | `POST` | Public (RateLimited) | `OAuth2PasswordRequestForm` | `Token` + HttpOnly Cookie | User query, RefreshToken INSERT | None | Conflict in CORS/Cookie |
| `/api/auth/refresh` | `POST` | Cookie (RateLimited) | Cookie (`refresh_token`) | `Token` + HttpOnly Cookie | Token lookup, RTR UPDATE | None | Conflict in config |
| `/api/auth/logout` | `POST` | Authenticated | None | `MessageResponse` | Token Revocation/DELETE | None | Clean |
| `/api/users/me` | `GET` | `authenticated` | None | `UserResponse` | User query | None | Clean |
| `/api/users/` | `GET` | `users:manage` | Limit/Offset query | `list[UserResponse]` | User SELECT | None | Clean |
| `/api/users/audit-logs` | `GET` | `users:manage` | Limit/Offset query | `list[AuditLogResponse]` | AuditLog SELECT | None | Clean |
| `/api/users/{user_id}/role` | `PUT` | `users:manage` | `UserUpdateRole` | `UserResponse` | User UPDATE, AuditLog INSERT | None | Clean |
| `/api/datasets` | `POST` | `datasets:upload` | Form + File | `DatasetResponse` | Dataset INSERT | lakeFS repo create | **Merge Conflict** |
| `/api/datasets` | `GET` | `datasets:view` | Limit/Offset query | `list[DatasetResponse]` | Dataset SELECT | None | Clean |
| `/api/datasets/{name}` | `GET` | `datasets:view` | Path | `DatasetResponse` | Dataset SELECT | lakeFS metadata | Clean |
| `/api/datasets/{name}` | `PUT` | `datasets:upload` | `DatasetMetadataUpdate` | `DatasetResponse` | Dataset UPDATE | lakeFS metadata | Clean |
| `/api/datasets/{name}` | `DELETE`| `datasets:delete` | Path | `MessageResponse` | Dataset DELETE | lakeFS repo delete | Clean |
| `/api/datasets/{name}/upload` | `POST` | `datasets:upload` | Multipart File | `dict` | Dataset lookup | lakeFS upload | Clean |
| `/api/datasets/{name}/download` | `GET` | `datasets:view` | Query (`path`, `ref`) | `StreamingResponse` | Dataset lookup | lakeFS stream | Header injection risk |
| `/api/datasets/{name}/commit` | `POST` | `datasets:upload` | `DatasetCommitRequest` | `CommitResponse` | Dataset lookup | lakeFS commit | Clean |
| `/api/datasets/{name}/commits` | `GET` | `datasets:view` | Query (`ref`, `limit`)| `list[CommitResponse]` | Dataset lookup | lakeFS log | Clean |
| `/api/datasets/{name}/compare` | `GET` | `datasets:view` | Query (`left_ref`, `right_ref`) | `CompareResponse` | Dataset lookup | lakeFS diff | Clean |
| `/api/datasets/{name}/rollback`| `POST` | `datasets:upload` | `RollbackRequest` | `RollbackResponse` | Dataset lookup | lakeFS revert | Clean |
| `/api/datasets/{name}/branches`| `GET` | `datasets:view` | Path | `list[BranchResponse]` | Dataset lookup | lakeFS branches | Clean |
| `/api/datasets/{name}/branches`| `POST` | `datasets:upload` | `BranchCreate` | `BranchResponse` | Dataset lookup | lakeFS branch create | Clean |
| `/api/datasets/{name}/branches/{b}`| `DELETE`| `datasets:delete` | Path | `MessageResponse` | Dataset lookup | lakeFS branch delete | Clean |
| `/api/datasets/{name}/tags` | `GET` | `datasets:view` | Path | `list[TagResponse]` | Dataset lookup | lakeFS tags | Clean |
| `/api/datasets/{name}/tags` | `POST` | `datasets:upload` | `TagCreate` | `TagResponse` | Dataset lookup | lakeFS tag create | Clean |
| `/api/datasets/{name}/tags/{t}` | `DELETE`| `datasets:delete` | Path | `MessageResponse` | Dataset lookup | lakeFS tag delete | Clean |
| `/api/models/train` | `POST` | `models:train` | `TrainModelSchema` | `TrainModelResponse` | TrainingJob INSERT | K8s RayJob submit | **Merge Conflict** |
| `/api/models/train-pipeline` | `POST` | `models:train` | `TrainPipelineSchema` | `TrainModelResponse` | TrainingJob INSERT | K8s RayJob / local Ray | **Merge Conflict** |
| `/api/models/supported` | `GET` | `models:view` | None | `dict` | None | None | Clean |
| `/api/models` | `GET` | `models:view` | None | `ModelListResponse` | None | MLflow registered models | Clean |
| `/api/models/{name}/versions` | `GET` | `models:view` | Path | `ModelDetailResponse` | None | MLflow model versions | Clean |
| `/api/models/upload` | `POST` | `models:train` | Form + File (`model_file`)| `UploadModelResponse` | None | MLflow log_model | Clean |
| `/api/models/deploy` | `POST` | `models:deploy` | `DeployModelSchema` | `DeployModelResponse` | Deployment INSERT | K8s RayService apply | **Merge Conflict** |
| `/api/deployments` | `GET` | `models:view` | Query filters | `DeploymentListResponse` | Deployment SELECT | None | Clean |
| `/api/deployments/{id}` | `GET` | `models:view` | Path | `DeploymentDetailResponse`| Deployment SELECT | K8s RayService status | Clean |
| `/api/deployments/manage` | `POST` | `deployments:manage` | `ManageDeploymentSchema` | `ManageDeploymentResponse` | Deployment UPDATE | K8s RayService patch/delete | Clean |
| `/api/models/{name}/predict` | `POST` | `models:view` | `PredictionRequestSchema` | `PredictionResponseSchema` | Deployment lookup | HTTPX -> RayService | Clean (Tracing OK) |
| `/api/deployments/{id}/predict` | `POST` | `models:view` | `PredictionRequestSchema` | `PredictionResponseSchema` | Deployment lookup | HTTPX -> RayService | Clean (Tracing OK) |
| `/api/jobs` | `GET` | `models:view` | Query (`limit`) | `RayJobListResponse` | TrainingJob SELECT | K8s CustomObjectsApi | **Merge Conflict** |
| `/api/jobs/{job_id}` | `GET` | `models:view` | Path | `RayJobDetailResponse` | TrainingJob lookup | K8s CustomObjectsApi | **Merge Conflict** |
| `/api/jobs/{job_id}/logs` | `GET` | `models:view` | Path | `RayJobLogsResponse` | TrainingJob lookup | K8s CoreV1Api pod logs | Clean |
| `/api/experiments` | `GET` | `models:view` | None | `list[dict]` | None | MLflow search_experiments | Clean |
| `/api/experiments/{id}/runs` | `GET` | `models:view` | Path + Query | `list[dict]` | None | MLflow search_runs | Clean |

---

## 6. Docker & Kubernetes Software Audit

### 6.1 Docker Compose Parsing Failure
- `docker-compose.yml` has syntax errors on lines 44–50 and 227–235 due to unresolved git conflict markers.
- `docker compose config` and `docker compose ps` fail outright with `mapping values are not allowed in this context`.

### 6.2 RayJob Network Policy Syntax Error
- In `k8s/rayjob-network-policy.yaml`:
  ```yaml
  ports:
    - protocol: TCP
      port: 9000
      port: 5000  # Duplicate YAML mapping key!
  ```
- *Impact:* The second `port: 5000` overwrites `port: 9000`. Traffic destined to port 9000 (MinIO S3) will be blocked if parsed by strict YAML loaders.

---

## 7. Frontend Architecture Audit (Branch `develop`)

### 7.1 Tech Stack & Structure
- **Framework:** React 19 + Vite 8 SPA.
- **Routing:** React Router v7 (`react-router-dom`) with lazy-loaded route chunks (`Dashboard`, `Pipeline`, `Datasets`, `DatasetDetail`, `Registry`, `Deployments`, `Predict`, `Jobs`, `Experiments`, `Admin`).
- **Icons & Visuals:** `lucide-react`, `recharts` for metrics and evaluation visualizations.
- **Linter Status:** `oxlint` passes with 0 errors (4 Fast-Refresh export warnings).
- **Build Status:** `npm run build` (`vite build`) succeeds cleanly without bundling errors.

### 7.2 Authentication & State Architecture
- Access tokens reside exclusively in module memory (`let accessToken = null` in `frontend/src/api/client.js`).
- Refresh tokens are transported via HttpOnly cookies (`credentials: 'include'`).
- Silent session restoration on initial page load via `refreshAccessToken()` and `/users/me`.
- Gating via `<RoleGate scope="users:manage">` protects UI elements according to the active role.

### 7.3 Integration & Configuration Mismatches
1. **Port Mismatch:**
   - Frontend API client defaults to `http://localhost:8000/api` (`client.js` line 11).
   - `backend/run.sh` launches Uvicorn on port `8001` (`uvicorn app.main:app --host 127.0.0.1 --port 8001`).
   - `frontend/scripts/audit-endpoints.mjs` targets `http://127.0.0.1:8001`.
   - *Impact:* Out-of-the-box local execution fails to connect unless `VITE_API_URL` is explicitly overridden.
2. **CORS Origins Collision:**
   - Stash changes introduced multi-port Vite support (`ALLOWED_ORIGINS` for 5173, 5174, 5175), but conflicts with `CORS_ORIGINS` in `main.py`.

---

## 8. Software Findings Classification

### FINDING-SW-001
**Title:** Unresolved Git Conflict Markers Committed to Branch `develop`  
**Category:** CORRECTNESS / MAINTAINABILITY  
**Severity:** CRITICAL  
**Location:** 16 files across backend, compose, and documentation  
**Evidence:** `git grep -n "<<<<<<< "` returns 31 conflict occurrences. Python imports fail with `SyntaxError: invalid syntax` in `backend/app/core/config.py:29`.  
**Impact:** Total application breakage. Backend API cannot start; test suite cannot run; docker compose cannot parse configuration.  
**Recommendation:** Perform a clean, surgical three-way merge between `main` and `develop`, resolving all conflicts in favor of the unified SentinelML configuration.  
**Priority:** Immediate  
**Confidence:** Confirmed  

### FINDING-SW-002
**Title:** Divergent Alembic Migration Heads (`8a1b2c3d4e5f` vs `bee587bde3b6`)  
**Category:** DATABASE / CORRECTNESS  
**Severity:** CRITICAL  
**Location:** `backend/alembic/versions/bee587bde3b6_add_training_jobs_table.py` & `7ef148a0dad6_create_training_jobs_table.py`  
**Evidence:** Running `alembic heads` yields two distinct revisions branching from `46bfc95b5dd9`. Both attempt to create `training_jobs` with conflicting columns.  
**Impact:** `alembic upgrade head` aborts. Database migrations fail for all clean environments.  
**Recommendation:** Remove the redundant migration `bee587bde3b6` (or merge revisions if any new columns are required) and preserve `7ef148a0dad6 -> 710094a7e4a1 -> 8a1b2c3d4e5f`.  
**Priority:** Immediate  
**Confidence:** Confirmed  

### FINDING-SW-003
**Title:** Model Argument Type Mismatch in `JobService`  
**Category:** CORRECTNESS / ARCHITECTURE  
**Severity:** HIGH  
**Location:** `backend/app/services/ml_ops/job_service.py:28-43`  
**Evidence:** `JobService.create_job` instantiates `TrainingJob(user_id=..., model_type=..., target_column=...)`. None of these fields exist in `backend/app/models/training_job.py`.  
**Impact:** Runtime `TypeError` upon submitting automated training pipeline jobs.  
**Recommendation:** Unify `JobService` with `TrainingJobService` and align arguments with the authoritative SQLAlchemy model.  
**Priority:** Immediate  
**Confidence:** Confirmed  

### FINDING-SW-004
**Title:** Duplicate Port Mapping in RayJob NetworkPolicy Manifest  
**Category:** RELIABILITY / NETWORKING  
**Severity:** MEDIUM  
**Location:** `k8s/rayjob-network-policy.yaml:26-28`  
**Evidence:**
```yaml
ports:
  - protocol: TCP
    port: 9000
    port: 5000
```
**Impact:** Port 9000 is overwritten by port 5000. Outbound traffic to MinIO S3 is denied under strict CNI policy enforcement.  
**Recommendation:** Split into two distinct port list items (`- protocol: TCP, port: 9000` and `- protocol: TCP, port: 5000`).  
**Priority:** Before next feature  
**Confidence:** Confirmed  

### FINDING-SW-005
**Title:** Synchronous Full-File Buffering in Dataset Download Path  
**Category:** PERFORMANCE  
**Severity:** MEDIUM  
**Location:** `backend/app/services/dataset/lakefs_service.py:109-110`  
**Evidence:** `with obj.reader(mode="rb") as reader: return reader.read()` loads the entire file into memory before returning.  
**Impact:** Memory exhaustion (OOM) under concurrent large dataset transfers.  
**Recommendation:** Replace `download_file` with `stream_file` yielding chunked generator iterators.  
**Priority:** Before next feature  
**Confidence:** Confirmed  

### FINDING-SW-006
**Title:** Port Discrepancy Between Backend Startup Script and Frontend Default  
**Category:** MAINTAINABILITY  
**Severity:** LOW  
**Location:** `backend/run.sh:60` vs `frontend/src/api/client.js:11`  
**Evidence:** `run.sh` sets `--port 8001`, while frontend defaults to `http://localhost:8000/api`.  
**Impact:** Local developer confusion; frontend displays "Backend unreachable" out-of-the-box.  
**Recommendation:** Standardize default port to 8000 across `run.sh`, `run.ps1`, `docker-compose.yml`, and frontend configuration.  
**Priority:** Later  
**Confidence:** Confirmed  

---

## 9. Executive Software Summary

- **Architecture Strengths:**
  - Clean domain-driven modularization across Auth, Users, Datasets, and MLOps.
  - Robust modern frontend with clean lazy routing and component isolation.
  - Comprehensive OpenTelemetry instrumentation across HTTP and model inference.
- **Architectural & Correctness Risks:**
  - Raw git conflict markers and dual Alembic heads on `develop` make the branch completely non-functional.
  - Job tracking schema divergence between `JobService` and `TrainingJobService`.
- **Feature Readiness:**
  - **Feature development (Data Drift / Continuous Training) MUST NOT proceed on `develop` until git conflict markers and Alembic dual heads are completely resolved.**
