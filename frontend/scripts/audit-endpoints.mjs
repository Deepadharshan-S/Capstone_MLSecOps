// ─────────────────────────────────────────────────────────────────────────────
// Endpoint coverage audit
//
// Exercises every route in the backend FastAPI app (42 paths / 43 handlers,
// counting GET / and GET /health) the same way the frontend does: form-encoded
// login, bearer tokens, multipart uploads, query-string filters. Reports the
// status each route returned so a contract drift (404/422) is obvious.
//
//   node scripts/audit-endpoints.mjs
//
// NB: this writes real records (a dataset, an MLflow model, a training job,
//   a deployment attempt). Re-runnable — names are timestamped.
// ─────────────────────────────────────────────────────────────────────────────
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const BASE = process.env.API_BASE || 'http://127.0.0.1:8001'
const enc = encodeURIComponent
const __dirname = dirname(fileURLToPath(import.meta.url))

const ADMIN = { username: 'admin_user', password: 'AdminPassword123!' }
const DS = { username: 'ds_user', password: 'DataScientist123!' }
const VIEWER = { username: 'viewer_user', password: 'ViewerPassword123!' }

const results = []
function record(group, method, path, status, ms, desc, note = '') {
  results.push({ group, method, path, status, ms, desc, note })
}

/** Fetch wrapper that never throws — a transport error is itself a finding. */
async function call(method, path, { token, json, form, multipart, raw } = {}) {
  const headers = {}
  if (token) headers.Authorization = `Bearer ${token}`
  let body
  if (multipart) {
    body = multipart
  } else if (form) {
    headers['Content-Type'] = 'application/x-www-form-urlencoded'
    body = new URLSearchParams(form)
  } else if (json !== undefined) {
    headers['Content-Type'] = 'application/json'
    body = JSON.stringify(json)
  }
  const t0 = Date.now()
  let res
  try {
    res = await fetch(`${BASE}${path}`, { method, headers, body })
  } catch (e) {
    record(currentGroup, method, path, 'ERR', Date.now() - t0, e.message, 'transport')
    return { ok: false, status: 0, json: null, res: null }
  }
  const ms = Date.now() - t0
  let desc = ''
  let parsed = null
  const ct = res.headers.get('content-type') || ''
  if (ct.includes('application/json')) {
    parsed = await res.json().catch(() => null)
    if (Array.isArray(parsed)) desc = `array[${parsed.length}]`
    else if (parsed && typeof parsed === 'object') {
      const detail = parsed.detail
      desc = typeof detail === 'string' ? detail.slice(0, 60)
        : Array.isArray(detail) ? `422:${detail.length} field(s)`
          : (Object.keys(parsed).slice(0, 5).join(',') || '{}')
    } else desc = String(parsed).slice(0, 60)
  } else if (raw) {
    const buf = Buffer.from(await res.arrayBuffer())
    desc = `${ct || 'binary'} · ${buf.length}B`
    parsed = { __bytes: buf.length }
  }
  const g = currentGroup
  record(g, method, path, res.status, ms, desc)
  return { ok: res.ok, status: res.status, json: parsed, res }
}

let currentGroup = '—'
async function group(name, fn) { currentGroup = name; await fn() }

async function login({ username, password }, attempt = 0) {
  const r = await call('POST', '/api/auth/login', {
    form: { username, password },
  })
  if (r.status === 429 && attempt < 4) {
    // login is rate limited to 5/min — back off and retry rather than fail.
    await new Promise((res) => setTimeout(res, 15000))
    return login({ username, password }, attempt + 1)
  }
  if (!r.ok) throw new Error(`login ${username} -> ${r.status} ${JSON.stringify(r.json).slice(0, 140)}`)
  return {
    token: r.json.access_token,
    cookie: (r.res.headers.getSetCookie?.() || [r.res.headers.get('set-cookie')])
      .filter(Boolean).map((c) => c.split(';')[0]).join('; '),
  }
}

function multipart(fields, fileField, filename, content, type = 'text/csv') {
  const fd = new FormData()
  for (const [k, v] of Object.entries(fields)) {
    if (v !== undefined && v !== null) fd.append(k, String(v))
  }
  fd.append(fileField, new Blob([content], { type }), filename)
  return fd
}

const CSV = (() => {
  const rows = ['feature1,feature2,feature3,label']
  for (let i = 0; i < 120; i++) {
    const label = i % 2
    const f1 = (i % 7) * 0.31 + 0.1 + label * 0.4
    const f2 = (i % 5) * 0.17 + 0.2 - label * 0.3
    const f3 = (i % 11) * 0.09 + 0.05 + label * 0.2
    rows.push(`${f1.toFixed(3)},${f2.toFixed(3)},${f3.toFixed(3)},${label}`)
  }
  return rows.join('\n')
})()

async function pollJob(token, jobId, timeoutMs = 120000) {
  const t0 = Date.now()
  let last = null
  while (Date.now() - t0 < timeoutMs) {
    const r = await call('GET', `/api/jobs/${enc(jobId)}`, { token })
    last = r.json
    if (last && ['completed', 'failed', 'error', 'cancelled'].includes(last.status)) return last
    await new Promise((r2) => setTimeout(r2, 3000))
  }
  return last
}

const main = async () => {
  // ── health (app root, outside /api) ────────────────────────────────────────
  await group('health', async () => {
    await call('GET', '/')
    await call('GET', '/health')
  })

  // ── auth ───────────────────────────────────────────────────────────────────
  let admin, ds, viewer
  const stamp = Date.now().toString(36)
  const registerName = `audit_${stamp}`

  await group('auth', async () => {
    await call('POST', '/api/auth/register', {
      json: { username: registerName, email: `${registerName}@example.com`, password: 'AuditPassword123!' },
    })
    admin = await login(ADMIN)
    // The refresh token is an httpOnly cookie set by login — send it back
    // explicitly (fetch does not attach cookies on its own).
    const t0 = Date.now()
    try {
      const r = await fetch(`${BASE}/api/auth/refresh`, {
        method: 'POST',
        headers: { Authorization: `Bearer ${admin.token}`, Cookie: admin.cookie },
      })
      const j = await r.json().catch(() => null)
      record('auth', 'POST', '/api/auth/refresh', r.status, Date.now() - t0,
        j?.access_token ? 'new access token issued' : JSON.stringify(j || {}).slice(0, 60))
      if (j?.access_token) admin.token = j.access_token
    } catch (e) {
      record('auth', 'POST', '/api/auth/refresh', 'ERR', Date.now() - t0, e.message)
    }
    ds = await login(DS)
    viewer = await login(VIEWER)
  })

  // ── users (admin only) ─────────────────────────────────────────────────────
  await group('users', async () => {
    await call('GET', '/api/users/me', { token: admin.token })
    await call('GET', '/api/users/', { token: admin.token })
    await call('GET', '/api/users/', { token: viewer.token }) // RBAC: expect 403
    await call('GET', '/api/users/audit-logs', { token: admin.token })
    const list = (await call('GET', '/api/users/', { token: admin.token })).json
    const target = (Array.isArray(list) ? list : []).find((u) => u.username === 'viewer_user')
    if (target) {
      await call('PUT', `/api/users/${enc(target.id || target.user_id)}/role`,
        { token: admin.token, json: { role: 'viewer' } })
    }
  })

  // ── datasets ───────────────────────────────────────────────────────────────
  const DS_NAME = `audit-ds-${stamp}`
  let firstCommit = null

  await group('datasets', async () => {
    await call('POST', '/api/datasets', {
      token: ds.token,
      multipart: multipart({ name: DS_NAME, description: 'Created by the endpoint audit' },
        'file', 'data.csv', CSV),
    })
    await call('GET', '/api/datasets', { token: ds.token })
    await call('GET', `/api/datasets/${enc(DS_NAME)}`, { token: ds.token })
    await call('PUT', `/api/datasets/${enc(DS_NAME)}`, {
      token: ds.token,
      json: { metadata: { owner: 'audit', reviewed: 'false' } },
    })
    await call('GET', `/api/datasets/${enc(DS_NAME)}/branches`, { token: ds.token })
    await call('POST', `/api/datasets/${enc(DS_NAME)}/branches`, {
      token: ds.token, json: { branch_name: 'audit-branch', source_branch: 'main' },
    })
    await call('GET', `/api/datasets/${enc(DS_NAME)}/commits?ref=main&limit=10`, { token: ds.token })
    await call('POST', `/api/datasets/${enc(DS_NAME)}/upload?branch=main`, {
      token: ds.token,
      multipart: multipart({}, 'file', 'extra.csv', CSV),
    })
    const commit = await call('POST', `/api/datasets/${enc(DS_NAME)}/commit?branch=main`, {
      token: ds.token, json: { message: 'audit: add extra.csv' },
    })
    firstCommit = commit.json?.id || null
    await call('GET', `/api/datasets/${enc(DS_NAME)}/tags`, { token: ds.token })
    await call('POST', `/api/datasets/${enc(DS_NAME)}/tags`, {
      token: ds.token, json: { tag_name: 'audit-tag', target_ref: 'main' },
    })
    await call('GET',
      `/api/datasets/${enc(DS_NAME)}/compare?left_ref=main&right_ref=audit-branch&type=three_dot`,
      { token: ds.token })
    await call('GET', `/api/datasets/${enc(DS_NAME)}/download?path=data.csv&ref=main`,
      { token: ds.token, raw: true })
    await call('POST', `/api/datasets/${enc(DS_NAME)}/rollback`, {
      token: ds.token, json: { branch: 'main', commit_id: firstCommit || 'main' },
    })
    await call('DELETE', `/api/datasets/${enc(DS_NAME)}/branches/audit-branch`, { token: ds.token })
    await call('DELETE', `/api/datasets/${enc(DS_NAME)}/tags/audit-tag`, { token: ds.token })
  })

  // ── ml_ops: registry, experiments, jobs ────────────────────────────────────
  let jobId = null
  let customJobId = null
  const UPLOADED_MODEL = `audit-model-${stamp}`

  await group('ml_ops', async () => {
    await call('GET', '/api/models/supported', { token: ds.token })
    await call('GET', '/api/models', { token: ds.token })
    const exp = await call('GET', '/api/experiments', { token: ds.token })
    const firstExp = exp.json?.experiments?.[0]
    if (firstExp) {
      await call('GET', `/api/experiments/${enc(firstExp.experiment_id)}/runs?limit=20`,
        { token: ds.token })
    }
    // GET /api/jobs/{job_id} — exercise it against a job that already exists
    // (the two training routes below may be rejected by the cluster guard).
    const jl = await call('GET', '/api/jobs?limit=20', { token: ds.token })
    const priorJob = Array.isArray(jl.json) ? jl.json[0] : null
    if (priorJob?.job_id) {
      await call('GET', `/api/jobs/${enc(priorJob.job_id)}`, { token: ds.token })
    }

    const tp = await call('POST', '/api/models/train-pipeline', {
      token: ds.token,
      json: {
        dataset_id: DS_NAME,
        ref: 'main',
        target_column: 'label',
        model_type: 'logistic_regression',
        hyperparameters: { max_iter: 400 },
        experiment_name: 'audit-experiment',
        model_name: UPLOADED_MODEL,
      },
    })
    jobId = tp.json?.job_id || null

    const custom = await call('POST', '/api/models/train', {
      token: ds.token,
      json: {
        dataset_id: DS_NAME,
        ref: 'main',
        epochs: 5,
        hyperparameters: { n_estimators: 50 },
        code: readFileSync(join(__dirname, 'fixtures', 'trainer.py'), 'utf8'),
        experiment_name: 'audit-experiment',
        model_name: `${UPLOADED_MODEL}-custom`,
      },
    })
    customJobId = custom.json?.job_id || null
  })

  // ── upload a .pkl (separate rate bucket from the two training routes) ─────
  await group('ml_ops', async () => {
    const pkl = join(__dirname, 'fixtures', 'model.pkl')
    await call('POST', '/api/models/upload', {
      token: ds.token,
      multipart: multipart(
        { model_name: UPLOADED_MODEL, experiment_name: 'audit-experiment', metrics: '{"accuracy":0.99}' },
        'file', 'model.pkl', readFileSync(pkl), 'application/octet-stream',
      ),
    })
    const versions = await call('GET', `/api/models/${enc(UPLOADED_MODEL)}/versions`, { token: ds.token })
    if (versions.ok) {
      await call('POST', `/api/models/${enc(UPLOADED_MODEL)}/predict?version=1`, {
        token: ds.token,
        json: { dataframe_records: [{ feature1: 0.5, feature2: 0.4, feature3: 0.2 }] },
      })
    }
  })

  // ── wait for the automated pipeline so registry/deploy see a real model ────
  if (jobId) {
    currentGroup = 'training'
    const final = await pollJob(ds.token, jobId)
    record('training', 'poll', `/api/jobs/${enc(jobId)}`, final?.status || 'timeout', 0,
      `accuracy=${final?.accuracy ?? '—'}`)
  }
  if (customJobId) {
    currentGroup = 'training'
    const final = await pollJob(ds.token, customJobId, 90000)
    record('training', 'poll', `/api/jobs/${enc(customJobId)}`, final?.status || 'timeout', 0,
      `accuracy=${final?.accuracy ?? '—'}`)
  }

  // ── deployments ────────────────────────────────────────────────────────────
  await group('deployments', async () => {
    const dep = await call('POST', '/api/models/deploy', {
      token: ds.token,
      json: { model_id: UPLOADED_MODEL, environment: 'staging', version: 'latest', replicas: 1 },
    })
    await call('GET', '/api/deployments', { token: ds.token })
    await call('GET', '/api/deployments?active_only=true&environment=staging', { token: ds.token })
    const depId = dep.json?.deployment_id || 'nonexistent-deployment'
    await call('POST', '/api/deployments/manage', {
      token: ds.token, json: { deployment_id: depId, action: 'restart' },
    })
    await call('POST', `/api/deployments/${enc(depId)}/predict`, {
      token: ds.token,
      json: { dataframe_records: [{ feature1: 0.5, feature2: 0.4, feature3: 0.2 }] },
    })
  })

  // ── RBAC probes (viewer must be refused) ───────────────────────────────────
  await group('rbac', async () => {
    await call('POST', '/api/models/train-pipeline', {
      token: viewer.token,
      json: { dataset_id: DS_NAME, ref: 'main', target_column: 'label', model_type: 'random_forest' },
    })
    await call('DELETE', `/api/datasets/${enc(DS_NAME)}`, { token: viewer.token })
    await call('POST', '/api/models/deploy', {
      token: viewer.token,
      json: { model_id: UPLOADED_MODEL, environment: 'staging', version: 'latest' },
    })
    await call('GET', '/api/jobs', { token: undefined }) // unauthenticated
  })

  // ── logout, then a fresh login (final login budget) ────────────────────────
  await group('auth', async () => {
    await call('POST', '/api/auth/logout', { token: admin.token })
    admin = await login(ADMIN)
  })

  // ── cleanup ────────────────────────────────────────────────────────────────
  await group('cleanup', async () => {
    await call('DELETE', `/api/datasets/${enc(DS_NAME)}`, { token: ds.token })
  })

  // ── report ─────────────────────────────────────────────────────────────────
  const cls = (s) => {
    if (s === 'ERR') return 'transport'
    const n = Number(s)
    if (n >= 200 && n < 300) return 'ok'
    if (n === 401 || n === 403) return 'denied'
    if (n === 404) return 'not-found'
    if (n === 422) return 'validation'
    if (n === 429) return 'rate-limited'
    return 'server-error'
  }
  const w = Math.max(...results.map((r) => r.path.length))
  const gw = Math.max(...results.map((r) => r.group.length))
  let bad = 0
  let prev = ''
  for (const r of results) {
    if (r.group !== prev) { console.log(`\n── ${r.group} ${'─'.repeat(Math.max(0, 78 - r.group.length))}`); prev = r.group }
    const c = cls(r.status)
    if (c === 'transport' || c === 'server-error') bad++
    const mark = c === 'ok' ? '✓' : c === 'denied' ? '⌐' : c === 'rate-limited' ? '⧗' : c === 'transport' ? '✗' : '!'
    console.log(
      `${mark} ${r.group.padEnd(gw)} ${String(r.method).padEnd(6)} ${r.path.padEnd(w)} ` +
      `${String(r.status).padStart(4)}  ${String(r.ms).padStart(5)}ms  ${c.padEnd(13)} ${r.desc}`,
    )
  }

  // ── coverage diff: every handler in openapi.json must have been probed ────
  const spec = await fetch(`${BASE}/openapi.json`).then((r) => r.json()).catch(() => null)
  const toPattern = (p) => '^' + p.split('/')
    .map((seg) => (seg.startsWith('{') && seg.endsWith('}') ? '[^/]+' : seg.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')))
    .join('/') + '$'
  const probed = new Set(results.map((r) => `${r.method} ${r.path.replace(/\?.*$/, '')}`))
  const specRoutes = Object.entries(spec?.paths || {})
    .flatMap(([p, ops]) => Object.keys(ops)
      .filter((m) => ['get', 'post', 'put', 'delete', 'patch'].includes(m))
      .map((m) => `${m.toUpperCase()} ${p}`))
    .filter((r) => !r.includes('/openapi.json'))
  const covered = specRoutes.filter((route) => {
    const [method, p] = [route.slice(0, route.indexOf(' ')), route.slice(route.indexOf(' ') + 1)]
    const rx = new RegExp(toPattern(p))
    return [...probed].some((probe) => probe.startsWith(`${method} `) && rx.test(probe.slice(method.length + 1)))
  })
  const uncovered = specRoutes.filter((r) => !covered.includes(r))

  const unreachable = results.filter((r) => r.status === 'ERR').length
  const routes = new Set(results.map((r) => r.path.replace(/\?.*/g, '')))
  console.log(`\n${results.length} probes · ${routes.size} distinct paths · ` +
    `${results.filter((r) => Number(r.status) >= 200 && Number(r.status) < 300).length} 2xx · ` +
    `${results.filter((r) => Number(r.status) === 401 || Number(r.status) === 403).length} 401/403 · ` +
    `${unreachable} unreachable · ${bad} hard failures`)
  console.log(`\ncoverage: ${covered.length}/${specRoutes.length} handlers exercised`)
  if (uncovered.length) {
    console.log('UNCOVERED:')
    uncovered.forEach((r) => console.log(`  ✗ ${r}`))
  } else {
    console.log('every documented route was exercised.')
  }
}

main().catch((e) => { console.error(e); process.exit(1) })
