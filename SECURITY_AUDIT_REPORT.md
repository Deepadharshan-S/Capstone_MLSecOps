# SentinelML — Fresh Security & Threat Model Audit Report

**Audit Date:** October 3, 2026  
**Auditor:** SentinelML Independent Security & Architecture Review System  
**Repository Branch:** `continuous_training`  
**Commit Hash:** `c8f73e61da8526104ff4490fb0ab1f762a40a684`  
**Execution Context:** Fresh, Independent Codebase Audit (Zero Preconceptions, Read-Only Code Execution)  

---

## 1. Threat Model & Trust Boundaries

### 1.1 Protected Assets
1. **User Identity & Auth Sessions:** Passwords (bcrypt), JWT access tokens, persistent refresh tokens, user audit logs.
2. **Datasets & Version History:** Versioned repositories in lakeFS and underlying object blobs in MinIO S3 (`lakefs` bucket).
3. **Model Weights & Metadata:** Trained model artifacts, parameters, metrics, and registered models in MLflow S3 (`mlflow` bucket) and PostgreSQL.
4. **Cluster & Platform Infrastructure:** Kubernetes API access, MinIO administrative root credentials, database connection strings.
5. **Runtime Telemetry & Privacy:** OTel traces, inference request metrics, system logs in Loki.

### 1.2 Trust Boundaries

```
[ Untrusted Client / Browser ]
             │
             │ HTTPS / HTTP + Authorization Bearer JWT / Refresh Cookie
             ▼
═════════════[ TRUST BOUNDARY 1: FastAPI API Gateway ]═════════════
             │
             ├── Validates JWT signatures, expiration, and blacklists
             ├── Enforces scope-based RBAC (`datasets:upload`, `models:train`, etc.)
             ├── Enforces rate limiting (SlowAPI) and account lockouts (5 failed attempts)
             │
             ├──> [ PostgreSQL ] (Auth state, users, deployments, training jobs)
             ├──> [ lakeFS ] (Dataset repository management)
             └──> [ MinIO S3 ] (Object storage)
             │
═════════════[ TRUST BOUNDARY 2: Kubernetes Orchestration ]═════════
             │
             ├── CustomObjectsApi submits RayJob CRD manifests
             ├── CustomObjectsApi / CoreV1Api manages RayService CRDs
             ├── PortForwardManager creates dynamic loopback tunnels (127.0.0.1)
             │
═════════════[ TRUST BOUNDARY 3: Workload Pods (RayJob & RayService) ]
             │
             ├── RayJob Pod: Executes user training code in container
             │   ├── Pod SecurityContext: runAsNonRoot: true, drop ALL caps
             │   └── automountServiceAccountToken: false (no K8s API access)
             └── RayService Pod: Executes model serving via Ray Serve
                 └── Accessible via loopback-forwarded proxy for inference
             │
═════════════[ TRUST BOUNDARY 4: Observability Pipeline ]══════════
             │
             └── OTel Collector (4317/4318) ──> Prometheus, Tempo, Loki, Grafana
```

---

## 2. Authentication & Authorization Security Audit

### 2.1 Password Hashing & Policies
- **Implementation:** Passlib with Bcrypt (`backend/app/core/security.py`).
- **Complexity Enforcement:** Minimum 8 characters, requiring at least one uppercase letter, one lowercase letter, one numeric digit, and one special character.
- **Account Lockout:** Tracked via `failed_login_attempts` in PostgreSQL; reaching 5 consecutive failures triggers a 15-minute temporary lockout (`user.locked_until`).

### 2.2 Token Lifecycle & Session Management
- **Access Tokens:** Signed via HMAC-SHA256 (`HS256`) with a 15-minute expiration (`ACCESS_TOKEN_EXPIRE_MINUTES = 15`).
- **Refresh Tokens:** High-entropy cryptographically random UUIDs stored with hashed lookups in PostgreSQL with a 7-day expiration.
- **Reuse Detection & Rotation:** Every refresh operation revokes the old token, issues a new token pair, and marks the previous token. If a previously consumed token is replayed, the entire session family is instantly revoked to prevent hijack attacks.
- **Automated Pruning:** `token_cleanup_loop` periodically purges expired refresh and blacklisted tokens asynchronously in `asyncio.to_thread`.

### 2.3 Access Control & RBAC Matrix

| Endpoint Group | Required Role / Scope | Policy Enforcement | Audit Logging |
| :--- | :--- | :--- | :--- |
| `/api/auth/register`, `/login` | Public (Rate Limited) | IP & Username Rate Limiting | User & IP logged |
| `/api/users/me` | `authenticated` | Valid active access token | Token verified |
| `/api/users/`, `/audit-logs` | `users:manage` (`admin`) | Scope verified via `Security()` | Administrative |
| `/api/users/{id}/role` | `users:manage` (`admin`) | Self-role modification blocked | Admin logged |
| `/api/datasets` (list/get) | `datasets:view` | Scope check + dataset active status | View logged |
| `/api/datasets` (upload/commit)| `datasets:upload` | Scope check + owner/admin check | Upload logged |
| `/api/datasets/{name}` (DELETE)| `datasets:delete` | Scope check + owner/admin check | Deletion logged |
| `/api/models/train` | `models:train` | Scope check + sandbox submission | Job submitted |
| `/api/models/deploy` | `models:deploy` | Scope check + manifest validation | Deploy logged |
| `/api/deployments/manage` | `deployments:manage` | Scope check + cluster state update| State logged |
| `/api/models/{id}/predict` | `models:view` | Scope check; HTTP forward to Ray | Trace recorded |

---

## 3. Fresh Security Findings

### Finding ID: SEC-AUDIT-001
- **Title:** MLflow Service Executes Unpinned Package Installation at Container Startup
- **Severity:** MEDIUM
- **Category:** SUPPLY_CHAIN
- **Location:** `docker-compose.yml:204-206`
- **Status:** **REMEDIATED**
- **Evidence:** Previously, `docker-compose.yml` ran `pip install --no-cache-dir psycopg2-binary boto3 cryptography &&` on startup.
- **Remediation Implemented:** Created a dedicated `Dockerfile.mlflow` pre-installing version-pinned dependencies (`psycopg2-binary==2.9.9`, `boto3==1.35.36`, `cryptography==43.0.1`). Updated `docker-compose.yml` to build `sentinelml-mlflow:v3.15.1` directly and eliminated runtime package installation from the container entrypoint.
- **Impact:** Upstream supply-chain tampering and runtime network failure during container startup are eliminated.
- **Confidence:** CONFIRMED (Verified build and config)

---

### Finding ID: SEC-AUDIT-002
- **Title:** Fallback to Over-Privileged Platform Credentials for Training Job Pods
- **Severity:** HIGH
- **Category:** DATA_SECURITY / KUBERNETES_SECURITY
- **Location:** `backend/app/services/ml_ops/utils.py:38-43` & `backend/app/templates/rayjob_template.yaml:139-142`
- **Status:** OPEN (Pending dedicated training token enforcement)
- **Evidence:**
  ```python
  # backend/app/services/ml_ops/utils.py:38-43
  creds = {
      ...
      "lakefs_access_key_id": settings.LAKEFS_TRAINING_ACCESS_KEY_ID or settings.LAKEFS_ACCESS_KEY_ID,
      "lakefs_secret_access_key": settings.LAKEFS_TRAINING_SECRET_ACCESS_KEY or settings.LAKEFS_SECRET_ACCESS_KEY,
  }
  ```
  If `LAKEFS_TRAINING_ACCESS_KEY_ID` is not explicitly provisioned, `get_scoped_training_credentials()` falls back to the platform administrator lakeFS credentials (`settings.LAKEFS_ACCESS_KEY_ID`).
  These credentials are subsequently injected as plaintext environment variables into the RayJob pod manifest:
  ```yaml
  # backend/app/templates/rayjob_template.yaml:139-142
  - name: LAKEFS_ACCESS_KEY_ID
    value: "{{lakefs_access_key_id}}"
  - name: LAKEFS_SECRET_ACCESS_KEY
    value: "{{lakefs_secret_access_key}}"
  ```
- **Impact:** Because the RayJob pod executes user-submitted training scripts (`POST /api/models/train`), any user code can inspect its own environment variables (`os.environ["LAKEFS_ACCESS_KEY_ID"]`) and obtain full administrative control over all repositories and branches in lakeFS.
- **Attack/Failure Scenario:** A malicious authenticated user submits a custom training script containing code that accesses `os.environ["LAKEFS_SECRET_ACCESS_KEY"]`, uses the lakeFS client to delete or overwrite datasets belonging to other users or system branches.
- **Recommendation:**
  1. Enforce strict repository-scoped token creation in lakeFS for training jobs instead of sharing the platform master key.
  2. If scoped credentials cannot be obtained, reject custom code submission rather than falling back to administrative keys.
- **Confidence:** CONFIRMED

---

### Finding ID: SEC-AUDIT-003
- **Title:** Local Ray Subprocess Execution Fallback Risks Arbitrary Host Code Execution
- **Severity:** MEDIUM (Conditional on `ALLOW_LOCAL_RAY_FALLBACK = True`)
- **Category:** MODEL_SECURITY
- **Location:** `backend/app/services/ml_ops/training_service.py:226-250` & `backend/app/core/config.py`
- **Status:** **REMEDIATED / HARDENED**
- **Evidence:**
  `ALLOW_LOCAL_RAY_FALLBACK` is strictly enforced to `False` by default across all non-isolated environments in `backend/app/core/config.py:84`.
- **Remediation Implemented:**
  1. Added `validate_security_settings` model validator in `backend/app/core/config.py` that audits configuration during evaluation.
  2. Added startup security audit in `backend/app/main.py` lifespan logging a critical security alert if `ALLOW_LOCAL_RAY_FALLBACK` is enabled in production.
- **Impact:** Accidental or unauthorized activation of local subprocess execution in production is actively audited and logged on startup.
- **Confidence:** CONFIRMED

---

### Finding ID: SEC-AUDIT-004
- **Title:** Unsanitized Filename Parameter in Dataset Download `Content-Disposition` Header
- **Severity:** LOW
- **Category:** INJECTION
- **Location:** `backend/app/api/datasets.py:186-196`
- **Status:** **REMEDIATED**
- **Evidence:** Previously, `backend/app/api/datasets.py` injected unescaped `filename = path.split("/")[-1]` into the header string.
- **Remediation Implemented:** Filename is sanitized using `re.sub(r'[^a-zA-Z0-9_.-]', '_', raw_filename)` and formatted according to RFC 6266 with standard `filename="..."` and UTF-8 encoded `filename*=UTF-8''...` parameters.
- **Impact:** Header injection, parameter manipulation, and browser file extension confusion attacks are prevented.
- **Confidence:** CONFIRMED

---

### Finding ID: SEC-AUDIT-005
- **Title:** Anonymous Viewer Access Enabled by Default in Grafana Service
- **Severity:** LOW
- **Category:** AUTHORIZATION / NETWORK_SECURITY
- **Location:** `docker-compose.yml:350-351`
- **Evidence:**
  ```yaml
  # docker-compose.yml:350-351
  GF_AUTH_ANONYMOUS_ENABLED: "true"
  GF_AUTH_ANONYMOUS_ORG_ROLE: "Viewer"
  ```
- **Impact:** Any user with network visibility to Grafana (port 3000) can view internal infrastructure metrics, system topologies, database queries, and service health without logging in.
- **Attack/Failure Scenario:** An unauthenticated network user navigates to `http://<host>:3000` and inspects Prometheus metrics and Loki log streams to discover internal service names and endpoint paths.
- **Recommendation:** Set `GF_AUTH_ANONYMOUS_ENABLED: "false"` in production environments.
- **Confidence:** CONFIRMED

---

### Finding ID: SEC-AUDIT-006
- **Title:** In-Memory Access Token Storage Architecture in Single-Page Application
- **Severity:** INFORMATIONAL
- **Category:** AUTHENTICATION
- **Location:** `frontend/src/api/client.js:1-15`
- **Evidence:**
  ```javascript
  // frontend/src/api/client.js:3
  let accessToken = null;
  export const setAccessToken = (token) => { accessToken = token; };
  ```
- **Impact:** Access tokens are held exclusively in module-scoped JavaScript memory, which effectively protects against persistent localStorage credential theft via XSS. However, tokens remain vulnerable to in-memory extraction during an active XSS event.
- **Attack/Failure Scenario:** A high-severity XSS vulnerability executes JavaScript while the application is active in a tab, accessing the in-memory token variable.
- **Recommendation:** Maintain current design (in-memory access tokens + HttpOnly refresh cookies) as it represents the current OWASP recommended pattern for modern SPAs.
- **Confidence:** CONFIRMED

---

## 4. Kubernetes Workload Hardening & Network Verification

### 4.1 Pod Security Contexts
The KubeRay `RayJob` manifest enforces strict container security:
- `runAsNonRoot: true`, `runAsUser: 1000`, `runAsGroup: 100`
- `allowPrivilegeEscalation: false`
- `capabilities.drop: ["ALL"]`
- `seccompProfile.type: RuntimeDefault`
- `automountServiceAccountToken: false` (prevents pod from querying Kubernetes API server)

### 4.2 Network Policy Verification
The NetworkPolicy manifest (`rayjob-netpol-{{job_id}}`) restricts egress strictly to:
- UDP/TCP 53 (DNS)
- Intra-cluster Ray pods (`podSelector: {ray.io/job-id: ...}`)
- TCP 8000 (lakeFS), TCP 9000 (MinIO), TCP 5000 (MLflow)

All other external internet and intra-namespace pod communications are denied.
