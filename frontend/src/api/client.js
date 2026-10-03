// Thin fetch wrapper for the SentinelML API.
//
// Security notes:
// - The access token lives ONLY in memory (a module-level variable), never
//   in localStorage/sessionStorage. That means it can't be read by XSS
//   payloads that dig through storage, and it disappears on tab close/reload.
// - The refresh token is an httpOnly cookie set by the backend — this code
//   never touches it directly, it just calls /auth/refresh and the browser
//   attaches the cookie automatically (credentials: 'include').

const API_BASE = import.meta.env.VITE_API_URL || 'http://localhost:8000/api'

let accessToken = null
let refreshPromise = null // de-dupes concurrent refresh calls

export function setAccessToken(token) {
  accessToken = token
}

export function getAccessToken() {
  return accessToken
}

export class ApiError extends Error {
  constructor(message, status) {
    super(message)
    this.status = status
  }
}

// Fail fast instead of hanging forever (e.g. backend down, DB hang,
// wrong port). Without this, AppShell stays on "Loading…" indefinitely.
const DEFAULT_TIMEOUT_MS = Number(import.meta.env.VITE_API_TIMEOUT_MS) || 8000

async function fetchWithTimeout(url, options = {}, timeoutMs = DEFAULT_TIMEOUT_MS) {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeoutMs)
  try {
    return await fetch(url, { ...options, signal: controller.signal })
  } catch (err) {
    if (err?.name === 'AbortError') {
      throw new ApiError('Backend unreachable. Please start the API server and try again.', 0)
    }
    throw new ApiError('Network error. Please check the API server is running.', 0)
  } finally {
    clearTimeout(timer)
  }
}

export async function refreshAccessToken() {
  if (!refreshPromise) {
    refreshPromise = fetchWithTimeout(`${API_BASE}/auth/refresh`, {
      method: 'POST',
      credentials: 'include',
    })
      .then(async (res) => {
        if (!res.ok) throw new ApiError('Session expired.', res.status)
        const data = await res.json()
        setAccessToken(data.access_token)
        return data.access_token
      })
      .finally(() => {
        refreshPromise = null
      })
  }
  return refreshPromise
}

async function request(path, { method = 'GET', body, skipAuth = false, _retried = false } = {}) {
  const headers = { 'Content-Type': 'application/json' }
  if (!skipAuth && accessToken) headers.Authorization = `Bearer ${accessToken}`

  const res = await fetchWithTimeout(`${API_BASE}${path}`, {
    method,
    headers,
    credentials: 'include',
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })

  // Access token likely expired — refresh once and retry, then give up.
  if (res.status === 401 && !skipAuth && !_retried) {
    try {
      await refreshAccessToken()
      return request(path, { method, body, skipAuth, _retried: true })
    } catch {
      setAccessToken(null)
      throw new ApiError('Session expired. Please log in again.', 401)
    }
  }

  let data = null
  try {
    data = await res.json()
  } catch {
    /* empty body, e.g. some 204s */
  }

  if (!res.ok) {
    throw new ApiError(data?.detail || 'Something went wrong.', res.status)
  }
  return data
}

async function uploadRequest(path, formData, _retried = false) {
  const headers = {}
  if (accessToken) headers.Authorization = `Bearer ${accessToken}`

  const res = await fetchWithTimeout(`${API_BASE}${path}`, {
    method: 'POST',
    headers,
    credentials: 'include',
    body: formData,
  }, 30000)

  if (res.status === 401 && !_retried) {
    try {
      await refreshAccessToken()
      return uploadRequest(path, formData, true)
    } catch {
      setAccessToken(null)
      throw new ApiError('Session expired. Please log in again.', 401)
    }
  }

  let data = null
  try {
    data = await res.json()
  } catch { /* empty body */ }

  if (!res.ok) {
    throw new ApiError(data?.detail || 'Upload failed.', res.status)
  }
  return data
}

export const api = {
  get: (path, opts = {}) => request(path, opts),
  post: (path, body, opts = {}) => request(path, { method: 'POST', body, ...opts }),
  put: (path, body, opts = {}) => request(path, { method: 'PUT', body, ...opts }),
  del: (path, opts = {}) => request(path, { method: 'DELETE', ...opts }),
  upload: (path, formData) => uploadRequest(path, formData),
  // Authenticated binary download (file exports) — returns a Blob so the
  // caller can decide how to hand it to the browser.
  download: async (path, _retried = false) => {
    const headers = {}
    if (accessToken) headers.Authorization = `Bearer ${accessToken}`
    const res = await fetchWithTimeout(`${API_BASE}${path}`, {
      method: 'GET',
      headers,
      credentials: 'include',
    }, 60000)

    if (res.status === 401 && !_retried) {
      try {
        await refreshAccessToken()
        return api.download(path, true)
      } catch {
        setAccessToken(null)
        throw new ApiError('Session expired. Please log in again.', 401)
      }
    }
    if (!res.ok) {
      let data = null
      try { data = await res.json() } catch { /* empty body */ }
      throw new ApiError(data?.detail || 'Download failed.', res.status)
    }
    return res.blob()
  },
}

export { API_BASE }
