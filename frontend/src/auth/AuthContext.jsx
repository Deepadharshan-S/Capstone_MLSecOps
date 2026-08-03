import { createContext, useContext, useEffect, useState, useCallback } from 'react'
import { api, setAccessToken, refreshAccessToken, ApiError, API_BASE } from '../api/client'

// Mirrors backend/app/core/roles.py ROLE_PERMISSIONS.
// IMPORTANT: this is for UI decisions only (what to show/hide/disable).
// The backend re-validates every scope on every request via Security(...),
// so this list has zero effect on actual access control — don't extend
// its purpose beyond "should I render this button".
const ROLE_PERMISSIONS = {
  admin: [
    'datasets:upload', 'models:train', 'models:view',
    'models:deploy', 'deployments:manage', 'users:manage',
  ],
  data_scientist: ['datasets:upload', 'models:train', 'models:view'],
  ml_engineer: ['models:deploy', 'deployments:manage', 'models:view'],
  viewer: ['models:view'],
}

const AuthContext = createContext(null)

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null)
  const [loading, setLoading] = useState(true)

  const loadUser = useCallback(async () => {
    const me = await api.get('/users/me')
    setUser(me)
    return me
  }, [])

  useEffect(() => {
    // On first load there's no access token in memory (it never persists
    // across a reload), but the httpOnly refresh cookie might still be
    // valid. Try a silent refresh to restore the session transparently.
    ;(async () => {
      try {
        await refreshAccessToken()
        await loadUser()
      } catch {
        setUser(null)
        setAccessToken(null)
      } finally {
        setLoading(false)
      }
    })()
  }, [loadUser])

  async function login(username, password) {
    // /auth/login uses FastAPI's OAuth2PasswordRequestForm, which expects
    // application/x-www-form-urlencoded, not JSON.
    const body = new URLSearchParams({ username, password })
    const res = await fetch(`${API_BASE}/auth/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      credentials: 'include',
      body,
    })
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

  const value = { user, loading, login, register, logout, hasPermission }
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
