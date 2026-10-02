# SentinelML — Comprehensive Security Audit Report

**Date:** September 29, 2026  
**Auditor:** SentinelML Automated Security & Software Assurance (Read-Only Mode)  
**Target Git Branch:** `continuous_training`  
**Git Baseline Commit:** `1596034`  
**Classification:** Defensive Cybersecurity & Software Architecture Audit

---

## 1. Executive Summary & Threat Profile

SentinelML is an enterprise MLSecOps platform operating at the intersection of web APIs, distributed data versioning, containerized ML training, and real-time model serving.

The system interacts with untrusted external inputs across multiple attack surfaces:
1. **Public Web Endpoints**: Authentication, user management, and prediction APIs.
2. **Data & Artifact Ingestion**: Multi-part dataset file uploads (CSV, JSON), pre-trained model uploads (`.pkl`).
3. **Distributed Workload Execution**: User-supplied Python training scripts submitted to Kubernetes RayJobs.
4. **Model Serving**: Dynamic inference routing to RayService deployments.

### Vulnerability Summary Table

| ID | Title | Severity | CVSS v3.1 | Status | Location |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **SEC-01** | Remote Code Execution (RCE) via Unsafe Pickle Deserialization in Web Process | **CRITICAL** | **9.8** (CVSS:3.1/AV:N/AC:L/PR:L/UI:N/S:C/C:H/I:H/A:H) | **Confirmed Defect** | `backend/app/services/ml_ops/registry_service.py:326-336` |
| **SEC-02** | Arbitrary Host Code Execution Risk via Local Ray Fallback | **HIGH** | **8.8** (CVSS:3.1/AV:N/AC:L/PR:L/UI:N/S:U/C:H/I:H/A:H) | **Confirmed Defect** | `backend/app/services/ml_ops/training_service.py:227-244` |
| **SEC-03** | Potential Path Traversal & Unvalidated File Streaming in Datasets API | **HIGH** | **7.5** (CVSS:3.1/AV:N/AC:L/PR:L/UI:N/S:U/C:H/I:N/A:N) | **Confirmed Defect** | `backend/app/api/datasets.py:147-165` |
| **SEC-04** | Invalid YAML Syntax in Kubernetes NetworkPolicy Disables MinIO Egress | **MEDIUM** | **6.5** (CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:H) | **Confirmed Defect** | `k8s/rayjob-network-policy.yaml:50-52` |
| **SEC-05** | Lack of Object-Level Authorization (Horizontal Privilege Escalation / IDOR) | **MEDIUM** | **6.5** (CVSS:3.1/AV:N/AC:L/PR:L/UI:N/S:U/C:H/I:N/A:N) | **Confirmed Defect** | `backend/app/api/datasets.py`, `backend/app/api/ml_ops.py` |
| **SEC-06** | Dynamic Unpinned PyPI Installation at Runtime & Overprivileged Root S3 Access | **MEDIUM** | **5.9** (CVSS:3.1/AV:N/AC:H/PR:N/UI:N/S:U/C:L/I:H/A:N) | **Confirmed Defect** | `docker-compose.yml:198-212` |
| **SEC-07** | In-Memory Sliding Window Rate Limiter Memory Leak (DoS Vector) | **LOW** | **4.3** (CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:L) | **Confirmed Defect** | `backend/app/core/rate_limiter.py:20-56` |
| **SEC-08** | Wildcard Host Header Acceptance & Permissive Network Port Bindings | **LOW** | **3.7** (CVSS:3.1/AV:N/AC:H/PR:N/UI:N/S:U/C:L/I:N/A:N) | **Design Issue** | `docker-compose.yml:18,53,97,196,211` |

---

## 2. Threat Modeling (STRIDE / MITRE ATT&CK for ML)

| Threat Category | SentinelML Component | Specific Threat / Attack Vector |
| :--- | :--- | :--- |
| **Elevation of Privilege** | `ModelRegistryService` | Deserializing malicious pickle files inside FastAPI web process grants root/host execution privileges. |
| **Tampering** | `k8s/rayjob-network-policy.yaml` | Duplicate YAML keys cause silent dropping of network egress rules. |
| **Information Disclosure** | `DatasetCatalogService` / `Datasets API` | Lack of per-tenant/per-user object authorization allows viewers to access arbitrary datasets. |
| **Denial of Service** | `RateLimiter` | Unbounded dictionary keys in sliding window limiter allow memory exhaustion (OOM). |
| **Supply Chain Compromise** | Docker Compose MLflow | Dynamic unpinned `pip install` from public PyPI at container entrypoint introduces dependency tampering risk. |

---

## 3. Deep-Dive Vulnerability Analysis

### 3.1 SEC-01: Remote Code Execution (RCE) via Unsafe Pickle Deserialization (CRITICAL)

#### Vulnerability Location
- **File**: [backend/app/services/ml_ops/registry_service.py:326-336](file:///home/deepadharshan/Desktop/Capstone_MLSecOps/backend/app/services/ml_ops/registry_service.py#L326-L336)
- **Endpoint**: `POST /api/models/upload`
- **Method**: `ModelRegistryService._execute_model_upload`

#### Vulnerable Code
```python
# Save uploaded file to temp file
with tempfile.NamedTemporaryFile(delete=False, suffix=".pkl") as f:
    f.write(file_content)
    temp_path = f.name

# Load model to infer signature
try:
    with open(temp_path, "rb") as f:
        try:
            model = cloudpickle.load(f)
        except Exception:
            f.seek(0)
            model = pickle.load(f)
```

#### Detailed Technical Mechanics
1. An authenticated attacker with role `data_scientist`, `ml_engineer`, or `admin` (or any account possessing the `models:train` scope) invokes `POST /api/models/upload`.
2. The attacker submits a file containing serialized Python bytecode using the Python `pickle` or `cloudpickle` protocol.
3. The server writes the payload to `/tmp/*.pkl` and immediately calls `cloudpickle.load(f)` followed by `pickle.load(f)` **directly within the synchronous thread of the host FastAPI web process**.
4. In Python, `pickle.load()` reconstructs objects by executing the class's `__reduce__` method. An attacker crafts a payload specifying callable arbitrary commands (e.g., `os.system`, `subprocess.Popen`, reverse shells).
5. The payload executes with the privileges of the operating system user running the FastAPI process (`deepadharshan` on the host, UID 1000).

#### Security Policy & Architectural Violation
This vulnerability directly violates the core SentinelML architecture constraint:
> *"Strict Process Isolation: Under NO circumstances should untrusted user model code or arbitrary model files execute inside the FastAPI server process."*

---

### 3.2 SEC-02: Arbitrary Host Execution Risk via Local Ray Fallback (HIGH)

#### Vulnerability Location
- **File**: [backend/app/services/ml_ops/training_service.py:227-244](file:///home/deepadharshan/Desktop/Capstone_MLSecOps/backend/app/services/ml_ops/training_service.py#L227-L244)
- **File**: [backend/app/services/ml_ops/utils.py:291-321](file:///home/deepadharshan/Desktop/Capstone_MLSecOps/backend/app/services/ml_ops/utils.py#L291-L321)
- **Endpoint**: `POST /api/models/train`

#### Vulnerable Code
```python
# utils.py: spawn_local_ray_subprocess
def spawn_local_ray_subprocess(
    train_info: TrainingJobCreate,
    ...
) -> None:
    ...
    # Write user code directly to disk
    with open(user_code_path, "w") as f:
        f.write(train_info.code)
        
    cmd = [
        sys.executable,
        runner_path,
        "--dataset-id", train_info.dataset_id,
        ...
    ]
    subprocess.Popen(cmd, env=env, stdout=log_file, stderr=subprocess.STDOUT)
```

#### Detailed Technical Mechanics
1. When `settings.ALLOW_LOCAL_RAY_FALLBACK=True`, the training service detects if Kubernetes is unreachable or fallback is requested.
2. `ModelTrainingService` extracts the user-submitted Python string `train_info.code` from the request JSON body.
3. It creates a temporary directory and writes the raw text to `user_code.py`.
4. It calls `subprocess.Popen([sys.executable, ...])` directly on the host machine.
5. The user script executes on the host machine without:
   - Linux cgroups (no memory, CPU, or process limits).
   - Linux namespaces (no network isolation, PID isolation, or filesystem mounts).
   - Seccomp filters (all syscalls permitted).
6. While `.env.example` sets `ALLOW_LOCAL_RAY_FALLBACK=false`, the presence of this unsandboxed execution pathway represents an inherent high-severity security hazard if toggled on in development, staging, or accidentally in production.

---

### 3.3 SEC-03: Potential Path Traversal & Unvalidated File Streaming (HIGH)

#### Vulnerability Location
- **File**: [backend/app/api/datasets.py:147-165](file:///home/deepadharshan/Desktop/Capstone_MLSecOps/backend/app/api/datasets.py#L147-L165)
- **File**: [backend/app/services/dataset/storage_service.py:73-87](file:///home/deepadharshan/Desktop/Capstone_MLSecOps/backend/app/services/dataset/storage_service.py#L73-L87)

#### Vulnerable Code
```python
# datasets.py: stream_file
@router.get("/datasets/{dataset_name}/stream")
def stream_file(
    dataset_name: str,
    path: str = Query(..., description="File path in the repository"),
    ref: str = Query("main", description="Branch name or commit ID"),
    ...
):
    ...
    stream = storage_service.stream_file(db, dataset_name, path, ref)
    return StreamingResponse(stream, media_type="application/octet-stream")
```

#### Detailed Technical Mechanics
1. The `path` parameter is taken directly from the query string without canonicalization or validation.
2. No check is performed to reject directory traversal sequences (such as `../`, `..\\`, `%2e%2e%2f`, null bytes).
3. The parameter is passed directly into `lakeFS` and underlying `boto3` S3 storage service calls:
   ```python
   ref = repo.ref(ref_id)
   obj = ref.object(file_path)
   with obj.reader(mode="rb") as reader: ...
   ```
4. Depending on the underlying lakeFS repository configuration and blockstore namespaces, path traversal strings can cause unexpected object access or access control circumvention within the storage namespace.

---

### 3.4 SEC-04: Invalid YAML Syntax in Kubernetes NetworkPolicy (MEDIUM)

#### Vulnerability Location
- **File**: [k8s/rayjob-network-policy.yaml:47-53](file:///home/deepadharshan/Desktop/Capstone_MLSecOps/k8s/rayjob-network-policy.yaml#L47-L53)

#### Vulnerable Code
```yaml
    # 3. Allow egress strictly to SentinelML platform data services
    - ports:
        - protocol: TCP
          port: 8000   # LakeFS data versioning API
        - protocol: TCP
          port: 9000   # MinIO S3 object storage
          port: 5000   # MLflow tracking server
```

#### Detailed Technical Mechanics
1. In the YAML 1.2 specification, a mapping cannot contain duplicate keys (`port: 9000` followed by `port: 5000` under the same sequence mapping item).
2. When `kubectl apply -f k8s/rayjob-network-policy.yaml` parses this manifest, standard YAML decoders silently overwrite the first key `port: 9000` with the second key `port: 5000`.
3. The compiled NetworkPolicy only permits egress on port 8000 (lakeFS) and port 5000 (MLflow), while port 9000 (MinIO S3) is **completely omitted**.
4. When Kubernetes NetworkPolicy enforcement is enabled via CNI (e.g., Calico, Cilium), Ray workers attempting to stream dataset artifacts directly from MinIO on port 9000 have their packets dropped by the kernel netfilter/iptables rules.

---

### 3.5 SEC-05: Lack of Object-Level Authorization (Horizontal Privilege Escalation) (MEDIUM)

#### Vulnerability Location
- **Files**: [backend/app/api/datasets.py](file:///home/deepadharshan/Desktop/Capstone_MLSecOps/backend/app/api/datasets.py), [backend/app/api/ml_ops.py](file:///home/deepadharshan/Desktop/Capstone_MLSecOps/backend/app/api/ml_ops.py)

#### Detailed Technical Mechanics
1. SentinelML implements role-based access control (RBAC) via `require_permission()`:
   - `viewer` has `datasets:view`, `models:view`.
   - `data_scientist` has `datasets:view`, `datasets:manage`, `models:view`, `models:train`.
   - `ml_engineer` has `models:deploy`.
   - `admin` has all permissions.
2. The database schema stores `created_by_id` on both `datasets` and `training_jobs`.
3. However, none of the dataset reading, downloading, diffing, or model inspection endpoints check whether the requesting user owns the dataset, belongs to the same project/tenant, or has been explicitly granted access to that object.
4. Any user with a `viewer` role can view, download, diff, and inspect the datasets, commits, model artifacts, and training jobs created by any other user or data science team in the organization.

---

### 3.6 SEC-06: Dynamic Unpinned PyPI Installation at Runtime & Overprivileged Root S3 Access (MEDIUM)

#### Vulnerability Location
- **File**: [docker-compose.yml:198-212](file:///home/deepadharshan/Desktop/Capstone_MLSecOps/docker-compose.yml#L198-L212)

#### Vulnerable Code
```yaml
  mlflow:
    image: ghcr.io/mlflow/mlflow:v3.15.1
    environment:
      - AWS_ACCESS_KEY_ID=${MINIO_ROOT_USER}
      - AWS_SECRET_ACCESS_KEY=${MINIO_ROOT_PASSWORD}
    entrypoint: >
      /bin/sh -c "
      pip install --no-cache-dir psycopg2-binary boto3 cryptography &&
      mlflow server ...
```

#### Detailed Technical Mechanics
1. **Supply Chain Vulnerability**: Running `pip install` at container boot time dynamically fetches the latest versions of `psycopg2-binary`, `boto3`, and `cryptography` from public PyPI without version pinning or SHA-256 hash pinning. If a compromised package is pushed to PyPI, or if PyPI is unreachable during a container restart, MLflow will either fail to boot or run compromised code.
2. **Overprivileged S3 Access**: MLflow and lakeFS are configured using `MINIO_ROOT_USER` and `MINIO_ROOT_PASSWORD` (the full administrative root credentials of MinIO) rather than dedicated least-privilege IAM service accounts with access restricted strictly to their respective buckets (`s3://mlflow` and `s3://lakefs`).

---

### 3.7 SEC-07: In-Memory Sliding Window Rate Limiter Memory Leak (LOW)

#### Vulnerability Location
- **File**: [backend/app/core/rate_limiter.py:20-56](file:///home/deepadharshan/Desktop/Capstone_MLSecOps/backend/app/core/rate_limiter.py#L20-L56)

#### Vulnerable Code
```python
self.requests = defaultdict(list)
...
with self.lock:
    self.requests[key] = [
        t for t in self.requests[key] if now - t < self.seconds
    ]
    if len(self.requests[key]) >= self.times:
        raise HTTPException(...)
    self.requests[key].append(now)
```

#### Detailed Technical Mechanics
1. The dictionary `self.requests` keys entries by `(ip_address, endpoint_path)`.
2. When timestamps expire, the list value becomes empty (`[]`), but the dictionary key `(ip_address, endpoint_path)` is **never removed**.
3. An attacker generating requests with random or spoofed IP addresses and diverse URL query paths causes the dictionary to grow without bound, resulting in gradual memory consumption and potential Out-Of-Memory (OOM) crashes of the API worker.
4. The rate limiter operates strictly in-memory within a single Python process. In a production multi-worker deployment (e.g., Uvicorn with 4 workers or multiple Kubernetes pods), each worker maintains an independent rate limit counter, effectively multiplying the allowed request quota by the number of instances.

---

## 4. Authentication, Session Management & RBAC Audit

### 4.1 Strengths Identified
- **Password Hashing**: Uses `pwdlib` with `BcryptHasher` and automatically validates complexity rules (8+ chars, upper, lower, digit, special character).
- **Brute-Force Protection**: 5 consecutive failed login attempts trigger a 15-minute account lockout (`user.locked_until`).
- **Refresh Token Rotation (RTR)**: Refresh tokens are single-use. Upon use, the current token is revoked and a new pair is issued.
- **Token Reuse Detection**: If an already-revoked refresh token is presented, the system detects compromise, invalidates **all active sessions** for that user, and emits an audit event.
- **Audit Logging**: Comprehensive structured audit log capturing login successes, lockouts, token reuses, and model deployments.

### 4.2 Deficiencies Identified
- **Missing Revocation Check in JWT Authentication Dependency**:
  In `backend/app/api/auth.py`, `get_current_user` decodes the access token using `decode_token()`, but does not verify whether the access token's `jti` exists in `blacklisted_tokens` in PostgreSQL. When a user calls `/api/auth/logout`, the token is written to the blacklist table, but subsequent API requests presenting the logged-out access token still succeed until JWT expiration (30 minutes).

---

## 5. ML Pipeline Security & Untrusted Code Execution

```
[Attacker / Untrusted User]
           │
           ▼
    FastAPI Web Process
           │
   ┌───────┴────────────────────────┐
   │                                │
   ▼                                ▼
[SEC-01: Upload Model]      [SEC-02: Train Model (Fallback)]
pickle.load(f)               sys.executable user_code.py
      │                                │
      ▼                                ▼
💥 Arbitrary RCE              💥 Host Process RCE
   in Web Server                 on Host Machine
```

### Process Isolation Evaluation
- **RayService Serving**: Properly decoupled. `ModelServingService` sends pure HTTP POST requests to RayService endpoints via HTTPX. It does NOT load model weights into the FastAPI process during inference.
- **KubeRay Training**: Properly decoupled when running on Kubernetes. User code is mounted into an ephemeral RayJob pod governed by Kubernetes RBAC and resource limits.
- **Critical Flaw**: As shown in SEC-01, the `POST /api/models/upload` endpoint completely bypasses this architecture and deserializes untrusted pickle objects locally in FastAPI.

---

## 6. Container & Infrastructure Security

### 6.1 Port Bindings
In [docker-compose.yml](file:///home/deepadharshan/Desktop/Capstone_MLSecOps/docker-compose.yml):
- `postgres`: `0.0.0.0:5432:5432` (Exposed to all interfaces, should be internal or `127.0.0.1:5432`).
- `minio`: `0.0.0.0:9000-9001:9000-9001` (Exposed to all interfaces).
- `lakefs`: `0.0.0.0:8000:8000` (Exposed to all interfaces).
- `mlflow`: `0.0.0.0:5000:5000` (Exposed to all interfaces).

Binding database and storage services to `0.0.0.0` exposes them to the local physical network interface (LAN/Wi-Fi), allowing any machine on the same network to attempt authentication attacks against PostgreSQL and MinIO.

---

## 7. Compliance Checklist

- [x] OWASP Top 10 A01: Broken Access Control — Role scopes enforced; object-level IDOR needs remediation (SEC-05).
- [ ] OWASP Top 10 A02: Cryptographic Failures — Passwords hashed with bcrypt; access token revocation check missing.
- [ ] OWASP Top 10 A03: Injection — Safe from SQLi (SQLAlchemy ORM); Critical RCE via Deserialization Injection (SEC-01).
- [ ] OWASP Top 10 A05: Security Misconfiguration — Unpinned Docker tags, exposed 0.0.0.0 ports (SEC-06, SEC-08).
- [ ] OWASP Top 10 for LLM/ML: ML02 (Model Inversion & Data Leakage) — Need object-level tenancy controls.
- [ ] OWASP Top 10 for LLM/ML: ML03 (Supply Chain & Arbitrary Code Execution) — Direct pickle deserialization (SEC-01).
