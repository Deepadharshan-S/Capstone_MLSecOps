import { createContext, useContext, useEffect, useState, useCallback } from 'react'
import { api, setAccessToken, refreshAccessToken, ApiError, API_BASE } from '../api/client'

// Mirrors backend/app/core/roles.py ROLE_PERMISSIONS.
// IMPORTANT: this is for UI decisions only (what to show/hide/disable).
// The backend re-validates every scope on every request via Security(...),
// so this list has zero effect on actual access control — don't extend
// its purpose beyond "should I render this button".
const ROLE_PERMISSIONS = {
  admin: [
    'datasets:view', 'datasets:upload', 'datasets:delete',
    'models:train', 'models:view', 'models:deploy',
    'deployments:manage', 'users:manage',
  ],
  data_scientist: [
    'datasets:view', 'datasets:upload', 'datasets:delete',
    'models:train', 'models:view', 'models:deploy',
    'deployments:manage',
  ],
  ml_engineer: [
    'datasets:view',
    'models:train', 'models:view', 'models:deploy',
    'deployments:manage',
  ],
  viewer: ['models:view'],
}

const AuthContext = createContext(null)

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null)
  const [loading, setLoading] = useState(true)
  const [authError, setAuthError] = useState(null)

  const loadUser = useCallback(async () => {
    const me = await api.get('/users/me')
    setUser(me)
    return me
  }, [])

  useEffect(() => {
    let settled = false
    // Safety net: never leave the UI on "Loading…" forever. The api client
    // already times out, but this guards against any future hanging promise.
    const safety = setTimeout(() => {
      if (!settled) {
        settled = true
        setUser(null)
        setAuthError('Backend unreachable. Please start the API server and reload.')
        setLoading(false)
      }
    }, 12000)
    // On first load there's no access token in memory (it never persists
    // across a reload), but the httpOnly refresh cookie might still be
    // valid. Try a silent refresh to restore the session transparently.
    ;(async () => {
      try {
        await refreshAccessToken()
        await loadUser()
        if (!settled) setAuthError(null)
      } catch (err) {
        if (!settled) {
          setUser(null)
          setAccessToken(null)
          if (err instanceof ApiError && err.status === 0) {
            setAuthError(err.message)
          }
        }
      } finally {
        if (!settled) {
          settled = true
          clearTimeout(safety)
          setLoading(false)
        }
      }
    })()
    return () => clearTimeout(safety)
  }, [loadUser])

  async function login(username, password) {
    // /auth/login uses FastAPI's OAuth2PasswordRequestForm, which expects
    // application/x-www-form-urlencoded, not JSON.
    const body = new URLSearchParams({ username, password })
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), 10000)
    let res
    try {
      res = await fetch(`${API_BASE}/auth/login`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        credentials: 'include',
        body,
        signal: controller.signal,
      })
    } catch (err) {
      if (err?.name === 'AbortError') throw new ApiError('Backend unreachable. Please try again.', 0)
      throw new ApiError('Network error. Please try again.', 0)
    } finally {
      clearTimeout(timer)
    }
    const data = await res.json().catch(() => null)
    if (!res.ok) throw new ApiError(data?.detail || 'Login failed.', res.status)

    setAccessToken(data.access_token)
    await loadUser()
  }

  async function register(username, email, password) {
    await api.post('/auth/register', { username, email, password }, { skipAuth: true })
  }

  async function logout() {
    try {
      await api.post('/auth/logout')
    } catch {
      // Even if the network call fails, clear local state so the UI
      // doesn't get stuck "logged in" with a dead session.
    }
    setAccessToken(null)
    setUser(null)
  }

  function hasPermission(scope) {
    if (!user) return false
    return (ROLE_PERMISSIONS[user.role] || []).includes(scope)
  }

  const value = { user, loading, authError, login, register, logout, hasPermission }
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth() {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used inside <AuthProvider>')
  return ctx
}

// Convenience wrapper for RBAC-gated UI, e.g.:
//   <RoleGate scope="users:manage"><AdminPanel /></RoleGate>
export function RoleGate({ scope, children, fallback = null }) {
  const { hasPermission } = useAuth()
  return hasPermission(scope) ? children : fallback
}
