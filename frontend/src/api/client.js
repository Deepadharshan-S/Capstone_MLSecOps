// Thin fetch wrapper for the MLSecOps API.
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

export async function refreshAccessToken() {
  if (!refreshPromise) {
    refreshPromise = fetch(`${API_BASE}/auth/refresh`, {
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

  const res = await fetch(`${API_BASE}${path}`, {
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

export const api = {
  get: (path) => request(path),
  post: (path, body, opts = {}) => request(path, { method: 'POST', body, ...opts }),
  put: (path, body) => request(path, { method: 'PUT', body }),
}

export { API_BASE }
