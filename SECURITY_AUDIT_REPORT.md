# SentinelML — Comprehensive Security Audit Report

**Date:** October 2, 2026  
**Auditor:** SentinelML Automated Security & Architecture Review System  
**Repository Branch:** `develop` (commit `e4f0ac9`) vs `main` (commit `f2e1c4e`)  
**Audit Scope:** Full codebase (Backend, Frontend, Infrastructure, Database, Observability)  
**Execution Mode:** Read-Only Audit (Zero Production Modifications)

---

## 1. Threat Model & Trust Boundaries

### 1.1 Assets Under Protection
1. **Model Artifacts & Weights:** MLflow registry artifacts, serialized model packages (`.pkl`, `.joblib`, `.onnx`), and RayService serving replicas.
2. **Datasets & Version History:** Sensitive customer or enterprise training datasets stored across lakeFS repositories and MinIO S3 buckets.
3. **Identity & Session Credentials:** User account passwords (bcrypt), JWT private secrets, refresh tokens in PostgreSQL, and audit log trails.
4. **Infrastructure & Cluster Secrets:** Kubernetes cluster service account tokens, PostgreSQL master credentials, MinIO access keys, and lakeFS encryption secrets.
5. **Observability & Operational Traces:** Distributed trace spans, metrics series, inference payload telemetry, and system audit logs.

### 1.2 Trust Boundaries

```
[ Untrusted Internet / Browser Clients ]
                    │
                    │ HTTPS / HTTP + JWT Bearer / Refresh Cookie
                    ▼
═══════════════════[ TRUST BOUNDARY 1: API Gateway (FastAPI) ]═══════════════════
                    │
                    ├── Authenticates identity & validates JWT claims / RBAC scopes
                    ├── Enforces rate limiting & security headers
                    │
                    ├──> [ PostgreSQL ] (Auth State, User DB, Token Whitelist/Blacklist)
                    ├──> [ lakeFS ] (Dataset Versioning)
                    └──> [ MinIO S3 ] (Object Storage)
                    │
═══════════════════[ TRUST BOUNDARY 2: Kubernetes Orchestration ]════════════════
                    │
                    ├── CustomObjectsApi submits RayJob CRD manifests
                    ├── CustomObjectsApi / CoreV1Api deploys & scales RayService
                    │
═══════════════════[ TRUST BOUNDARY 3: Workload Isolation (Ray Pods) ]════════════
                    │
                    ├── RayJob Pod: Executes untrusted/dynamic training code (isolated namespace)
                    └── RayService Pod: Executes trained model inference workers (isolated namespace)
                    │
═══════════════════[ TRUST BOUNDARY 4: Observability Pipeline ]═════════════════
                    │
                    └── OTel Collector ──> Prometheus / Tempo / Loki / Grafana
```

---

## 2. Authentication & Session Security Audit

### 2.1 Password Hashing & Policies
- **Implementation:** Passlib with Bcrypt (`bcrypt__rounds = 12`).
- **Complexity Policy:** Enforced via `app.services.auth.password_policy.py`:
  - Minimum 12 characters, uppercase, lowercase, numeric digit, and special character required.
  - Prevents common dictionary passwords.

### 2.2 Token Architecture & Refresh Token Rotation (RTR)
- **Access Tokens:** Short-lived JWTs (`ACCESS_TOKEN_EXPIRE_MINUTES: 30`) signed via HMAC-SHA256 (`HS256`).
- **Refresh Tokens:** High-entropy cryptographic secrets stored hashed in PostgreSQL with strict Refresh Token Rotation (RTR).
- **Automatic Reuse Detection:** If an invalidated or already-rotated refresh token is submitted, the system flags token theft, revokes all child tokens, and evicts the compromised session.
- **Automated Token Cleanup:** Background lifecycle task in FastAPI sweeps expired blacklisted tokens and refresh tokens periodically.

### 2.3 Local Development Cookie Security Conflict
- In `backend/app/api/auth.py` (lines 72–74):
  ```python
  response.set_cookie(
      key="refresh_token",
      value=token_dict["refresh_token"],
      httponly=True,
      secure=True,       # Hardcoded secure flag!
      samesite="lax",
      max_age=7 * 24 * 3600,
      path="/api/auth",
  )
  ```
- *Finding:* On local development over plain HTTP (`http://localhost`), modern browsers reject `secure=True` cookies outright. On `develop`, a conflict was created attempting to add `COOKIE_SECURE: bool = False` into `config.py`, which remained unmerged and broke configuration parsing.

---

## 3. Authorization & RBAC Audit

### 3.1 Role-Based Access Control Matrix

| Role | Datasets View | Datasets Upload | Datasets Delete | Models View | Models Train | Models Deploy | Deployments Manage | Users Manage |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **Admin** | Yes | Yes | Yes | Yes | Yes | Yes | Yes | Yes |
| **Data Scientist** | Yes | Yes | Yes | Yes | Yes | Yes | Yes | No |
| **ML Engineer** | Yes | No | No | Yes | Yes | Yes | Yes | No |
| **Viewer** | No (Frontend hides) | No | No | Yes | No | No | No | No |

### 3.2 Authorization Weakness: Missing Object-Level Authorization (BOLA/IDOR)
- **Finding:** In `backend/app/api/datasets.py`, all endpoints checking `datasets:upload`, `datasets:view`, or `datasets:delete` only verify that the user holds the general role scope.
- **Evidence:** Any user with `datasets:delete` (both `admin` and `data_scientist`) can execute `DELETE /api/datasets/{dataset_name}` against **any** dataset in the system, even if owned or created by another data scientist.
- *Impact:* Horizontal privilege escalation and accidental or malicious deletion of peer datasets.

---

## 4. Model & ML Supply Chain Security

### 4.1 FastAPI Model Execution Isolation
- **Rule Verification:** FastAPI **must not** execute arbitrary user model code or unpickled weights in its own process.
- **Audited Status:**
  - Training execution is offloaded to Kubernetes RayJob pods (`submit_rayjob_to_k8s`).
  - Serving inference is routed over HTTPX to RayService (`rayserve.inference`).
  - **CONFIRMED:** FastAPI does not directly deserialize untrusted pickles or execute user code in-process for inference.

### 4.2 Dynamic Unpinned Dependencies in MLflow Container
- In `docker-compose.yml` (lines 142–153):
  ```yaml
  entrypoint: >
    /bin/sh -c "
      pip install psycopg2-binary boto3 &&
      mlflow server ...
    "
  ```
- *Finding:* Dynamic, unpinned `pip install` from public PyPI at container runtime introduces supply-chain risks. An upstream PyPI outage, network disruption, or dependency compromise directly compromises or halts the MLflow server.

---

## 5. Secrets Exposure & Static Audit

### 5.1 Repository Dump Committed to Version Control (`repomix-output.xml`)
- **Finding:** Commit `e4f0ac9` on branch `develop` added `repomix-output.xml` (14,398 lines, 513 KB) into git.
- **Evidence:** This file contains a merged plain-text dump of the entire repository codebase, including configuration structures, internal endpoint paths, default secret keys (`minioadmin123`), and architectural layouts.
- *Impact:* Information disclosure; exposes internal system topography and increases attack surface if published or pushed to a public/shared remote repository.

### 5.2 Default Hardcoded Fallbacks in Configuration
- In `backend/app/core/config.py`:
  - `MINIO_ROOT_PASSWORD: str = "minioadmin123"`
  - `LAKEFS_SECRET_ACCESS_KEY: str = "lakefssecretkey123"`
- *Mitigation in place:* Production configurations override these via environment variables, but local dev environments remain vulnerable if exposed to network interfaces.

---

## 6. Network & Container Security

### 6.1 Unrestricted Port Bindings
- In `docker-compose.yml`, multiple stateful and internal services bind to `0.0.0.0`:
  - PostgreSQL (`5432:5432`)
  - MinIO API & Console (`9000:9000`, `9001:9001`)
  - MLflow Server (`5000:5000`)
  - lakeFS (`8000:8000` / mapped ports)
- *Recommendation:* Bind database and internal object stores to `127.0.0.1` unless external ingress is strictly required.

---

## 7. Observability & Privacy Audit

### 7.1 Sensitive Data Verification
- **Spans & Traces:** Checked `backend/app/core/telemetry.py` and HTTP middleware. Traces propagate `traceparent` headers correctly without injecting raw request bodies, prediction arrays, user passwords, or JWT secrets into span attributes.
- **Metric Cardinality:** Prometheus metrics generated by SentinelML use bounded label sets (`status_code`, `method`, `endpoint`). User IDs, dataset IDs, and trace IDs are properly excluded from metric labels.

---

## 8. Security Findings Classification

### FINDING-SEC-001
**Title:** Committed Full Repository Dump (`repomix-output.xml`) in Git  
**Category:** SECURITY / SECRETS  
**Severity:** HIGH  
**Location:** `repomix-output.xml` (Repository Root)  
**Evidence:** 14,398 lines of full source dump containing architecture, database configuration, and default credentials committed in `e4f0ac9`.  
**Impact:** Excessive information disclosure and leakage of system internals.  
**Exploitability:** Readily accessible to any entity with read access to the git repository.  
**Recommendation:** Remove `repomix-output.xml` from git tracking, add to `.gitignore`, and scrub git commit history if the repo is ever mirrored externally.  
**Priority:** Immediate  
**Confidence:** Confirmed  

### FINDING-SEC-002
**Title:** Broken Object-Level Authorization (BOLA/IDOR) on Dataset Deletion  
**Category:** SECURITY / AUTHORIZATION  
**Severity:** MEDIUM  
**Location:** `backend/app/api/datasets.py:164-180`  
**Evidence:** `delete_dataset` verifies `datasets:delete` scope but performs no ownership check against `dataset.user_id` or `dataset.created_by_id`.  
**Impact:** Any user with the `data_scientist` role can delete datasets registered by other users.  
**Exploitability:** Send `DELETE /api/datasets/{victim_dataset_name}` with valid Data Scientist bearer token.  
**Recommendation:** Implement tenant/user ownership validation: allow non-admin users to delete only datasets where `dataset.user_id == current_user.id`.  
**Priority:** Before next feature  
**Confidence:** Confirmed  

### FINDING-SEC-003
**Title:** Dynamic Unpinned Dependency Installation in MLflow Service  
**Category:** SECURITY / SUPPLY CHAIN  
**Severity:** MEDIUM  
**Location:** `docker-compose.yml:142-146`  
**Evidence:** Container entrypoint runs `pip install psycopg2-binary boto3` without version pinning or hash validation on every container startup.  
**Impact:** Susceptible to PyPI dependency confusion/typosquatting and unexpected startup failures during PyPI outages.  
**Exploitability:** Compromise of public PyPI package or network MITM during startup.  
**Recommendation:** Pre-build a dedicated Docker image for MLflow containing pinned `psycopg2-binary` and `boto3` binaries.  
**Priority:** Before next feature  
**Confidence:** Confirmed  

### FINDING-SEC-004
**Title:** Potential Content-Disposition Header Injection in Dataset Download  
**Category:** SECURITY / API  
**Severity:** LOW  
**Location:** `backend/app/api/datasets.py:221-227`  
**Evidence:**
```python
filename = path.split("/")[-1]
return StreamingResponse(..., headers={"Content-Disposition": f"attachment; filename={filename}"})
```
If `path` contains newlines (`\r\n`) or unescaped quotes, HTTP response splitting or header injection may occur.  
**Impact:** Browser response tampering or unexpected filename interpretation.  
**Exploitability:** Pass an encoded path containing CRLF sequences to `GET /api/datasets/{name}/download?path=...`.  
**Recommendation:** Sanitize `filename` using `urllib.parse.quote` and strip non-ASCII / control characters.  
**Priority:** Later  
**Confidence:** Confirmed  

### FINDING-SEC-005
**Title:** In-Memory Access Token Storage in Frontend SPA  
**Category:** SECURITY / FRONTEND  
**Severity:** INFORMATIONAL  
**Location:** `frontend/src/api/client.js:13-22`  
**Evidence:** Access token is stored in a private module-level variable `let accessToken = null` and cleared on page refresh. Refresh is performed silently via HttpOnly cookies.  
**Impact:** Highly positive security pattern. Mitigates token exfiltration via XSS compared to `localStorage`.  
**Recommendation:** Maintain this pattern; ensure CSP (Content Security Policy) headers are served in production to further protect against XSS.  
**Priority:** Informational  
**Confidence:** Confirmed  

---

## 9. Executive Security Summary

- **Confirmed Vulnerabilities:**
  - `repomix-output.xml` source dump committed to branch `develop` (Information Disclosure).
  - Broken Object-Level Authorization on dataset deletion (BOLA/IDOR).
  - Unpinned runtime `pip install` in MLflow Docker container (Supply Chain Risk).
- **Security Strengths:**
  - Industry-standard password hashing (Bcrypt 12 rounds) and robust complexity policies.
  - Refresh Token Rotation (RTR) with automatic token reuse theft detection.
  - Strict isolation: FastAPI never executes untrusted user model code in-process.
  - Frontend SPA keeps JWT access tokens strictly in-memory, avoiding `localStorage` XSS theft.
- **Deployment Recommendation:**
  - Branch `develop` must be cleaned of conflict markers and sensitive dumps before being promoted or merged to `main`.
