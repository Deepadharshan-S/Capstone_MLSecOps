# SentinelML — Prioritized Audit Remediation Plan

**Date:** October 2, 2026  
**Status:** Baseline Established — Pending Execution  
**Scope:** Remediation of findings from `SOFTWARE_AUDIT_REPORT.md` and `SECURITY_AUDIT_REPORT.md`  
**Rule Reminder:** READ-ONLY baseline audit completed. Remediation actions below are prioritized for subsequent implementation turns.

---

## 1. Remediation Priority Matrix

| Finding ID | Title | Category | Severity | Priority Tier | Effort |
| :--- | :--- | :---: | :---: | :---: | :---: |
| **FINDING-SW-001** | Unresolved Git Conflict Markers on Branch `develop` | CORRECTNESS | CRITICAL | **Immediate** | 1–2 hours |
| **FINDING-SW-002** | Divergent Alembic Migration Heads (`8a1b2c3d4e5f` vs `bee587bde3b6`) | DATABASE | CRITICAL | **Immediate** | 1 hour |
| **FINDING-SW-003** | Model Argument Mismatch in `JobService` | ARCHITECTURE | HIGH | **Immediate** | 1 hour |
| **FINDING-SEC-001** | Committed Repository Dump (`repomix-output.xml`) in Git | SECRETS | HIGH | **Immediate** | 15 mins |
| **FINDING-SW-004** | Duplicate Port Mapping in RayJob NetworkPolicy | NETWORKING | MEDIUM | **Before Next Feature** | 15 mins |
| **FINDING-SW-005** | Synchronous Full-File Buffering in Dataset Download | PERFORMANCE | MEDIUM | **Before Next Feature** | 1–2 hours |
| **FINDING-SEC-002** | Broken Object-Level Authorization (BOLA) on Datasets | AUTHORIZATION | MEDIUM | **Before Next Feature** | 1–2 hours |
| **FINDING-SEC-003** | Dynamic Unpinned Dependencies in MLflow Container | SUPPLY CHAIN | MEDIUM | **Before Next Feature** | 1 hour |
| **FINDING-SW-006** | Port Discrepancy Between `run.sh` (8001) and Frontend (8000) | CONFIGURATION | LOW | **Later** | 30 mins |
| **FINDING-SEC-004** | Potential Content-Disposition Header Injection | API SECURITY | LOW | **Later** | 30 mins |
| **FINDING-SEC-005** | In-Memory Access Token Storage in Frontend | SECURITY | INFORMATIONAL | **Informational** | None |

---

## 2. Immediate Remediation (Blocking Current Development)

### 2.1 Remediate FINDING-SW-001: Resolve Merge Conflicts on `develop`
- **Action Items:**
  1. Perform a clean merge or rebase of `develop` against `main` (`f2e1c4e`).
  2. In `backend/app/core/config.py`, merge the CORS configuration and add `COOKIE_SECURE: bool = False` (defaulting to False for local dev, True for production via `.env`).
  3. In `backend/app/main.py`, preserve CORS allow-origins list reading from `ALLOWED_ORIGINS` / `CORS_ORIGINS`.
  4. In `docker-compose.yml`, keep official images (`minio/minio:latest` and `minio/mc:latest`).
  5. Verify that `git grep -n "<<<<<<< "` returns 0 results.
  6. Confirm conftest imports cleanly and `PYTHONPATH=backend uv run pytest backend/tests/ -ra` passes.
- **Verification Criteria:**
  - Zero conflict markers in repository.
  - All 143 existing backend tests pass.

### 2.2 Remediate FINDING-SW-002: Resolve Dual Alembic Heads
- **Action Items:**
  1. Inspect `backend/alembic/versions/bee587bde3b6_add_training_jobs_table.py`.
  2. Because the canonical `training_jobs` table was already established on `main` in `7ef148a0dad6` and extended with indexes in `710094a7e4a1` and `8a1b2c3d4e5f`, delete or re-head `bee587bde3b6`.
  3. If extra metric fields (`accuracy`, `f1_score`, `confusion_matrix`) are desired on `training_jobs` for the frontend dashboard, create a clean linear migration revising `8a1b2c3d4e5f`:
     ```python
     # alembic revision --message "add_training_metrics_to_jobs" --head 8a1b2c3d4e5f
     ```
  4. Run `.venv/bin/alembic -c backend/alembic.ini heads` and verify that exactly one head is reported.
- **Verification Criteria:**
  - `alembic heads` outputs a single unified revision head.
  - `alembic upgrade head` runs cleanly without error.

### 2.3 Remediate FINDING-SW-003: Unify Job Tracking Architecture
- **Action Items:**
  1. Reconcile `JobService` with `TrainingJobService`.
  2. Ensure all fields instantiated when creating a job match the `TrainingJob` SQLAlchemy model (`rayjob_name`, `created_by_id`, `ref`, `epochs`, `hyperparameters`).
  3. Remove `HTTPException` imports from `job_service.py` to preserve pure domain service layering.
- **Verification Criteria:**
  - Automated sklearn pipeline training endpoints (`/models/train-pipeline`) execute without `TypeError`.

### 2.4 Remediate FINDING-SEC-001: Purge Repository Dump (`repomix-output.xml`)
- **Action Items:**
  1. Remove `repomix-output.xml` from git tracking (`git rm repomix-output.xml`).
  2. Add `repomix-output.xml` and `*repomix*` to `.gitignore`.
- **Verification Criteria:**
  - `repomix-output.xml` is removed from git working tree and tracked index.

---

## 3. Before Next Feature Remediation (Pre-Requisites for Drift & Continuous Training)

### 3.1 Remediate FINDING-SW-004: Fix RayJob NetworkPolicy
- **Action Items:**
  1. Edit `k8s/rayjob-network-policy.yaml`.
  2. Separate duplicate `port` keys into discrete YAML array items:
     ```yaml
     ports:
       - protocol: TCP
         port: 9000
       - protocol: TCP
         port: 5000
     ```
- **Verification Criteria:**
  - `kubectl apply --dry-run=client -f k8s/rayjob-network-policy.yaml` validates successfully.

### 3.2 Remediate FINDING-SW-005: Stream Large Dataset Downloads
- **Action Items:**
  1. Deprecate `download_file` in `backend/app/services/dataset/lakefs_service.py` in favor of `stream_file`.
  2. Ensure `FastAPI`'s `StreamingResponse` consumes chunks of 64KB directly from the lakeFS reader generator.
- **Verification Criteria:**
  - Multi-gigabyte file downloads consume constant memory (< 50MB RSS) in the backend worker process.

### 3.3 Remediate FINDING-SEC-002: Enforce Object-Level Ownership on Datasets
- **Action Items:**
  1. In `backend/app/api/datasets.py` (`delete_dataset`):
     ```python
     if "admin" not in user.role and dataset.user_id != user.id:
         raise HTTPException(status_code=403, detail="Forbidden: You do not own this dataset.")
     ```
- **Verification Criteria:**
  - Data Scientist A receives 403 Forbidden when attempting to delete Data Scientist B's dataset.
  - Admin users can successfully manage/delete any dataset.

### 3.4 Remediate FINDING-SEC-003: Pre-Build MLflow Docker Image
- **Action Items:**
  1. Create a clean `Dockerfile.mlflow` installing pinned `psycopg2-binary==2.9.9` and `boto3==1.34.131`.
  2. Update `docker-compose.yml` to build or reference this pinned image rather than running `pip install` at runtime.
- **Verification Criteria:**
  - MLflow container starts offline without network access to public PyPI.

---

## 4. Architectural Readiness for Next Milestones

### 4.1 Data Drift & Concept Drift Architecture
- **Planned Placement:**
  - Create dedicated domain service `app/services/monitoring/drift_service.py`.
  - Store baseline dataset distributions (reference datasets) in lakeFS under tags `baseline-v*`.
  - Ingest inference feature payloads into an isolated time-partitioned MinIO bucket (`s3://sentinelml-inference-logs/YYYY/MM/DD/`).
  - Offload drift statistical tests (Kolmogorov-Smirnov, PSI, Wasserstein Distance) to Kubernetes RayJobs so the FastAPI event loop is never blocked.

### 4.2 Model Performance Monitoring & Continuous Training (CT) Pipeline
- **Triggering Workflow:**
  1. **Drift Detection Trigger:** When drift metric exceeds threshold (e.g., PSI > 0.25), emit an internal event.
  2. **Data Pull:** RayJob pulls recent labeled data from lakeFS branch/tag.
  3. **Automated Retraining:** KubeRay launches training pod; logs metrics to MLflow.
  4. **Model Validation Gate:** If new model accuracy > incumbent model accuracy + margin, transition MLflow model stage to `Staging` and deploy canary RayService.
  5. **Promotion:** Admin / ML Engineer approves deployment via `POST /api/deployments/manage`.
