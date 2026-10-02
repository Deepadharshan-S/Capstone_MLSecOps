// Typed wrappers for every backend endpoint (app/api/*).
// Keeping them in one place means the UI never hand-builds a path, and a
// grep of this file is an accurate coverage report of the API surface.
import { api, API_BASE } from './client.js'

const enc = encodeURIComponent

// GET / and GET /health are registered on the app root, *outside* the /api
// router prefix, so they must bypass api.get()'s API_BASE. No auth required.
const ROOT_BASE = API_BASE.replace(/\/api\/?$/, '')
async function rootGet(path) {
  const res = await fetch(`${ROOT_BASE}${path}`, {
    method: 'GET',
    headers: { Accept: 'application/json' },
    signal: AbortSignal.timeout(8000),
  })
  if (!res.ok) throw new Error(`Health check failed: HTTP ${res.status}`)
  return res.json()
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
  uploadFile: (name, formData) => api.upload(`/datasets/${enc(name)}/upload`, formData),
  downloadUrl: (name, path, ref = 'main') =>
    `/datasets/${enc(name)}/download?path=${enc(path)}&ref=${enc(ref)}`,

  /* ── Versioning ─────────────────────────────────── */
  // POST /datasets/{name}/commit · GET /datasets/{name}/commits
  commit: (name, body) => api.post(`/datasets/${enc(name)}/commit`, body),
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
