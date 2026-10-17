// Typed wrappers for every backend endpoint (app/api/*).
// Keeping them in one place means the UI never hand-builds a path, and a
// grep of this file is an accurate coverage report of the API surface.
import { api, API_BASE } from './client.js'

const enc = encodeURIComponent

// GET / and GET /health are registered on the app root, *outside* the /api
// router prefix, so they must bypass api.get()'s API_BASE. No auth required.
//
// /health intentionally answers **503 with a full JSON body** when a single
// dependency is down (backend/app/main.py: status, database, lakefs, minio,
// mlflow), so a non-2xx status here is NOT a transport failure — it carries
// the exact payload the UI needs to say "degraded" rather than "offline".
// Only a dead server / non-JSON reply is treated as unreachable.
const ROOT_BASE = API_BASE.replace(/\/api\/?$/, '')
async function rootGet(path) {
  let res
  try {
    res = await fetch(`${ROOT_BASE}${path}`, {
      method: 'GET',
      headers: { Accept: 'application/json' },
      signal: AbortSignal.timeout(8000),
    })
  } catch {
    throw new Error('Health check failed: backend unreachable.')
  }
  let body = null
  try { body = await res.json() } catch { /* empty or non-JSON reply */ }
  if (!body || typeof body !== 'object') {
    throw new Error(`Health check failed: HTTP ${res.status}`)
  }
  return body
}

export const healthApi = {
  // GET /  +  GET /health
  root: () => rootGet('/'),
  check: () => rootGet('/health'),
}

export const authApi = {
  // POST /auth/register | /auth/login | /auth/refresh | /auth/logout
  register: (body) => api.post('/auth/register', body, { skipAuth: true }),
  logout: () => api.post('/auth/logout'),
}

export const usersApi = {
  // GET /users/me
  me: () => api.get('/users/me'),
  // GET /users/
  list: () => api.get('/users/'),
  // GET /users/audit-logs
  auditLogs: () => api.get('/users/audit-logs'),
  // PUT /users/{user_id}/role
  updateRole: (userId, role) => api.put(`/users/${enc(userId)}/role`, { role }),
}

export const mlOpsApi = {
  /* ── Training ───────────────────────────────────── */
  // POST /models/train — custom training code
  train: (body) => api.post('/models/train', body),
  // POST /models/train-pipeline — automated sklearn pipeline
  trainPipeline: (body) => api.post('/models/train-pipeline', body),
  // GET /jobs/{job_id} · GET /jobs
  getJob: (jobId) => api.get(`/jobs/${enc(jobId)}`),
  listJobs: (limit = 20) => api.get(`/jobs?limit=${Number(limit) || 20}`),

  /* ── Registry ───────────────────────────────────── */
  // GET /models/supported · GET /models · GET /models/{name}/versions
  supportedModels: () => api.get('/models/supported'),
  listModels: () => api.get('/models'),
  modelVersions: (name) => api.get(`/models/${enc(name)}/versions`),
  // POST /models/upload
  uploadModel: (formData) => api.upload('/models/upload', formData),

  /* ── Experiments ────────────────────────────────── */
  // GET /experiments · GET /experiments/{id}/runs
  listExperiments: () => api.get('/experiments'),
  experimentRuns: (experimentId, limit = 50) =>
    api.get(`/experiments/${enc(experimentId)}/runs?limit=${Number(limit) || 50}`),

  /* ── Deployments ────────────────────────────────── */
  // POST /models/deploy · GET /deployments · POST /deployments/manage
  deploy: (body) => api.post('/models/deploy', body),
  listDeployments: (params = {}) => {
    const qs = new URLSearchParams()
    if (params.status) qs.set('status', params.status)
    if (params.environment) qs.set('environment', params.environment)
    if (params.active_only) qs.set('active_only', 'true')
    const s = qs.toString()
    return api.get(`/deployments${s ? `?${s}` : ''}`)
  },
  manageDeployment: (deployment_id, action) =>
    api.post('/deployments/manage', { deployment_id, action }),

  /* ── Inference ──────────────────────────────────── */
  // POST /models/{name}/predict · POST /deployments/{id}/predict
  predictModel: (modelName, payload, version) => {
    const qs = version ? `?version=${enc(version)}` : ''
    return api.post(`/models/${enc(modelName)}/predict${qs}`, payload)
  },
  predictDeployment: (deploymentId, payload) =>
    api.post(`/deployments/${enc(deploymentId)}/predict`, payload),
}

export const datasetsApi = {
  /* ── Catalog ────────────────────────────────────── */
  // POST /datasets (register + upload) · GET /datasets · DELETE /datasets/{name}
  register: (formData) => api.upload('/datasets', formData),
  list: () => api.get('/datasets'),
  remove: (name) => api.del(`/datasets/${enc(name)}`),

  /* ── Metadata ───────────────────────────────────── */
  // GET /datasets/{name} · PUT /datasets/{name}
  metadata: (name) => api.get(`/datasets/${enc(name)}`),
  updateMetadata: (name, metadata) => api.put(`/datasets/${enc(name)}`, { metadata }),

  /* ── Files ──────────────────────────────────────── */
  // POST /datasets/{name}/upload · GET /datasets/{name}/download
  // `branch` must travel as a QUERY param: the backend declares it as
  // `branch: Optional[str] = "main"` (no Form(...)), so a multipart field of
  // the same name is silently dropped and the file would land on the default
  // branch no matter what the UI's branch selector said.
  uploadFile: (name, formData, branch) => api.upload(
    `/datasets/${enc(name)}/upload${branch ? `?branch=${enc(branch)}` : ''}`,
    formData,
  ),
  downloadUrl: (name, path, ref = 'main') =>
    `/datasets/${enc(name)}/download?path=${enc(path)}&ref=${enc(ref)}`,
  // GET /datasets/{name}/files — objects stored under a ref
  files: (name, ref = 'main') =>
    api.get(`/datasets/${enc(name)}/files?ref=${enc(ref)}`),

  /* ── Versioning ─────────────────────────────────── */
  // POST /datasets/{name}/commit · GET /datasets/{name}/commits
  // The backend takes the target branch as a query param (defaults to main).
  commit: (name, body, branch) => api.post(
    `/datasets/${enc(name)}/commit${branch ? `?branch=${enc(branch)}` : ''}`,
    body,
  ),
  commits: (name, ref, limit) => {
    const qs = new URLSearchParams()
    if (ref) qs.set('ref', ref)
    if (limit) qs.set('limit', limit)
    const s = qs.toString()
    return api.get(`/datasets/${enc(name)}/commits${s ? `?${s}` : ''}`)
  },
  // GET /datasets/{name}/compare · POST /datasets/{name}/rollback
  compare: (name, leftRef, rightRef, type = 'three_dot') =>
    api.get(`/datasets/${enc(name)}/compare?left_ref=${enc(leftRef)}&right_ref=${enc(rightRef)}&type=${enc(type)}`),
  rollback: (name, body) => api.post(`/datasets/${enc(name)}/rollback`, body),

  /* ── Branches ───────────────────────────────────── */
  // GET · POST · DELETE /datasets/{name}/branches[/{branch}]
  branches: (name) => api.get(`/datasets/${enc(name)}/branches`),
  createBranch: (name, branch_name, source_branch) =>
    api.post(`/datasets/${enc(name)}/branches`, { branch_name, source_branch }),
  deleteBranch: (name, branchName) =>
    api.del(`/datasets/${enc(name)}/branches/${enc(branchName)}`),

  /* ── Tags ───────────────────────────────────────── */
  // GET · POST · DELETE /datasets/{name}/tags[/{tag}]
  tags: (name) => api.get(`/datasets/${enc(name)}/tags`),
  createTag: (name, tag_name, target_ref) =>
    api.post(`/datasets/${enc(name)}/tags`, { tag_name, target_ref }),
  deleteTag: (name, tagName) => api.del(`/datasets/${enc(name)}/tags/${enc(tagName)}`),
}

// Mirrors backend/app/api/threat_hunting.py — the alert-generation surface:
// security events feed the detection engine, the background worker turns
// matches into alerts, and alerts move through an investigation lifecycle.
export const threatHuntingApi = {
  /* ── Alerts ─────────────────────────────────────── */
  // GET /alerts — server-side filters + pagination (limit ≤ 200)
  listAlerts: (params = {}) => {
    const qs = new URLSearchParams()
    if (params.status) qs.set('status', params.status)
    if (params.severity) qs.set('severity', params.severity)
    if (params.rule_id) qs.set('rule_id', params.rule_id)
    if (params.detection_source) qs.set('detection_source', params.detection_source)
    if (params.assignee_id) qs.set('assignee_id', params.assignee_id)
    if (params.start_time) qs.set('start_time', params.start_time)
    if (params.end_time) qs.set('end_time', params.end_time)
    qs.set('limit', Math.min(Number(params.limit) || 50, 200))
    qs.set('offset', Number(params.offset) || 0)
    return api.get(`/alerts?${qs}`)
  },
  // GET /alerts/{id} — detail incl. investigation history
  getAlert: (alertId) => api.get(`/alerts/${enc(alertId)}`),
  // PATCH /alerts/{id} — status/severity/assignee/notes (partial update)
  updateAlert: (alertId, body) => api.patch(`/alerts/${enc(alertId)}`, body),
  // GET /alerts/{id}/events — triggering security events (limit ≤ 200)
  alertEvents: (alertId, limit = 50, offset = 0) =>
    api.get(`/alerts/${enc(alertId)}/events?limit=${Number(limit) || 50}&offset=${Number(offset) || 0}`),

  /* ── Detection rules ────────────────────────────── */
  // GET /detection-rules · POST /detection-rules
  listRules: (isEnabled) => api.get(
    `/detection-rules${isEnabled === undefined ? '' : `?is_enabled=${isEnabled}`}`,
  ),
  createRule: (body) => api.post('/detection-rules', body),
  // GET · PATCH /detection-rules/{id}
  getRule: (ruleId) => api.get(`/detection-rules/${enc(ruleId)}`),
  updateRule: (ruleId, body) => api.patch(`/detection-rules/${enc(ruleId)}`, body),
  // POST /detection-rules/{id}/test — dry run against recent events, no alerts
  testRule: (ruleId, windowSeconds) => api.post(
    `/detection-rules/${enc(ruleId)}/test`,
    windowSeconds ? { window_seconds: windowSeconds } : undefined,
  ),

  /* ── Security events ────────────────────────────── */
  // GET /security-events — filters + pagination (limit ≤ 500)
  listSecurityEvents: (params = {}) => {
    const qs = new URLSearchParams()
    if (params.event_type) qs.set('event_type', params.event_type)
    if (params.source) qs.set('source', params.source)
    if (params.actor_id) qs.set('actor_id', params.actor_id)
    if (params.resource_id) qs.set('resource_id', params.resource_id)
    if (params.severity) qs.set('severity', params.severity)
    if (params.correlation_id) qs.set('correlation_id', params.correlation_id)
    if (params.start_time) qs.set('start_time', params.start_time)
    if (params.end_time) qs.set('end_time', params.end_time)
    qs.set('limit', Math.min(Number(params.limit) || 50, 500))
    qs.set('offset', Number(params.offset) || 0)
    return api.get(`/security-events?${qs}`)
  },
  // POST /security-events — manual ingest (normalized + redacted server-side)
  ingestSecurityEvent: (body) => api.post('/security-events', body),

  /* ── Threat hunts ───────────────────────────────── */
  // POST /hunts — creates AND executes the hunt immediately (201)
  createHunt: (body) => api.post('/hunts', body),
  // GET /hunts · GET /hunts/{id} · GET /hunts/{id}/results
  listHunts: (params = {}) => {
    const qs = new URLSearchParams()
    if (params.status) qs.set('status', params.status)
    qs.set('limit', Math.min(Number(params.limit) || 50, 200))
    qs.set('offset', Number(params.offset) || 0)
    return api.get(`/hunts?${qs}`)
  },
  getHunt: (huntId) => api.get(`/hunts/${enc(huntId)}`),
  getHuntResults: (huntId) => api.get(`/hunts/${enc(huntId)}/results`),
}
