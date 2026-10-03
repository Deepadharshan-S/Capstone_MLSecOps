import { useState } from 'react'
import { useAuth } from './AuthContext'
import { ApiError } from '../api/client'
import {
  ShieldCheck, Database, Rocket, FlaskConical, Eye, EyeOff, Check, X,
  ArrowRight, Boxes, Users,
} from '../components/icons.jsx'
import '../styles/auth.css'

// Mirrors backend/app/core/security.py validate_password_strength().
// Client-side checking just gives faster feedback — the backend is the
// real gatekeeper and re-validates on every /auth/register call.
function checkPasswordStrength(password) {
  const issues = []
  if (password.length < 8) issues.push('at least 8 characters')
  if (password.length > 64) issues.push('no more than 64 characters')
  if (!/[A-Z]/.test(password)) issues.push('an uppercase letter')
  if (!/[a-z]/.test(password)) issues.push('a lowercase letter')
  if (!/\d/.test(password)) issues.push('a digit')
  if (!/[!@#$%^&*(),.?":{}|<>]/.test(password)) issues.push('a special character')
  return issues
}

const RULES = [
  { label: '8+ characters', test: (p) => p.length >= 8 },
  { label: 'Uppercase', test: (p) => /[A-Z]/.test(p) },
  { label: 'Lowercase', test: (p) => /[a-z]/.test(p) },
  { label: 'Digit', test: (p) => /\d/.test(p) },
  { label: 'Special char', test: (p) => /[!@#$%^&*(),.?":{}|<>]/.test(p) },
  { label: 'Max 64 chars', test: (p) => p.length > 0 && p.length <= 64 },
]

// Seeded in app/db/seed_db.py — each role gets its own password, so the
// fill buttons must carry the matching one (they used to all send Admin's).
const DEMO_ACCOUNTS = [
  { username: 'admin_user', password: 'AdminPassword123!', role: 'Admin', icon: ShieldCheck },
  { username: 'ds_user', password: 'DataScientist123!', role: 'Data Sci', icon: Database },
  { username: 'mle_user', password: 'MLEngineerPassword123!', role: 'ML Eng', icon: Rocket },
  { username: 'viewer_user', password: 'ViewerPassword123!', role: 'Viewer', icon: Eye },
]

export default function LoginPage({ initialNotice = null }) {
  const { login, register } = useAuth()
  const [mode, setMode] = useState('login') // 'login' | 'register'
  const [username, setUsername] = useState('')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [showPw, setShowPw] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(initialNotice)
  const [notice, setNotice] = useState(null)

  const rules = checkPasswordStrength(password)
  const pwOk = rules.length === 0

  async function submit(e) {
    e.preventDefault()
    setError(null)
    setNotice(null)

    if (mode === 'register') {
      if (!pwOk) { setError('Password does not meet all requirements yet.'); return }
      if (password !== confirm) { setError('Passwords do not match.'); return }
    }
    if (!username.trim() || !password) { setError('Username and password are required.'); return }

    setBusy(true)
    try {
      if (mode === 'login') {
        await login(username.trim(), password)
      } else {
        await register(username.trim(), email.trim() || `${username.trim()}@example.com`, password)
        // Auto sign-in after successful registration.
        await login(username.trim(), password)
      }
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Unexpected error. Please try again.')
    } finally {
      setBusy(false)
    }
  }

  function switchMode(next) {
    setMode(next)
    setError(null)
    setNotice(null)
    setPassword('')
    setConfirm('')
  }

  function fillDemo(d) {
    setMode('login')
    setError(null)
    setNotice(null)
    setUsername(d.username)
    setPassword(d.password)
    setNotice(`Credentials filled for ${d.username} — press Sign in.`)
  }

  return (
    <div className="auth-page">
      {/* ── Brand panel ─────────────────────────────── */}
      <aside className="auth-brand-panel">
        <div className="auth-brand-top">
          <div className="auth-brand-mark"><Boxes size={19} /></div>
          <div className="auth-brand-name">SentinelML <span>Platform</span></div>
        </div>

        <div className="auth-brand-center">
          <h1 className="auth-brand-title">
            Governed ML pipelines,<br />from dataset to deployment.
          </h1>
          <p className="auth-brand-lede">
            A single workspace for training, versioning, and shipping models — with
            lakeFS-backed dataset lineage, MLflow experiment tracking, and RBAC
            enforced at every step.
          </p>

          <div className="auth-feature-list">
            <div className="auth-feature">
              <span className="auth-feature-icon"><Database size={14} /></span>
              Version-controlled datasets with branches, tags &amp; rollback
            </div>
            <div className="auth-feature">
              <span className="auth-feature-icon"><FlaskConical size={14} /></span>
              Automated &amp; custom training runs with live metric streams
            </div>
            <div className="auth-feature">
              <span className="auth-feature-icon"><Rocket size={14} /></span>
              One-click deployment with drift monitoring and instant rollback
            </div>
            <div className="auth-feature">
              <span className="auth-feature-icon"><Users size={14} /></span>
              Role-based access: admin, data scientist, ML engineer, viewer
            </div>
          </div>
        </div>

        <div className="auth-brand-foot">
          Capstone Project · SentinelML Platform v1.0
        </div>
      </aside>

      {/* ── Form panel ──────────────────────────────── */}
      <main className="auth-form-panel">
        <div className="auth-card">
          <div className="auth-mobile-brand">
            <div className="auth-mobile-mark">S</div>
            <div className="auth-mobile-name">Sentinel<span>ML</span></div>
          </div>

          <h2 className="auth-title">{mode === 'login' ? 'Welcome back' : 'Create account'}</h2>
          <p className="auth-subtitle">
            {mode === 'login'
              ? 'Sign in to your workspace to continue.'
              : 'Register a new account. An admin assigns your role.'}
          </p>

          {error && (
            <div className="alert alert-error mb-4" role="alert">
              <X size={15} />
              <div className="alert-body">{error}</div>
            </div>
          )}
          {notice && (
            <div className="alert alert-info mb-4" role="status">
              <Check size={15} />
              <div className="alert-body">{notice}</div>
            </div>
          )}

          <form className="auth-form" onSubmit={submit} noValidate>
            <div className="input-group">
              <label className="input-label" htmlFor="login-username">Username <span className="req">*</span></label>
              <input
                id="login-username"
                className="input-field"
                autoComplete="username"
                placeholder="e.g. admin_user"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                required
              />
            </div>

            {mode === 'register' && (
              <div className="input-group">
                <label className="input-label" htmlFor="login-email">Email</label>
                <input
                  id="login-email"
                  className="input-field"
                  type="email"
                  autoComplete="email"
                  placeholder="you@example.com"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                />
              </div>
            )}

            <div className="input-group">
              <label className="input-label" htmlFor="login-password">Password <span className="req">*</span></label>
              <div className="input-eye-wrap">
                <input
                  id="login-password"
                  className="input-field has-eye"
                  type={showPw ? 'text' : 'password'}
                  autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
                  placeholder="••••••••"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  required
                />
                <button
                  type="button"
                  className="input-eye"
                  onClick={() => setShowPw((s) => !s)}
                  aria-label={showPw ? 'Hide password' : 'Show password'}
                >
                  {showPw ? <EyeOff size={15} /> : <Eye size={15} />}
                </button>
              </div>
            </div>

            {mode === 'register' && (
              <>
                <div className="input-group">
                  <label className="input-label" htmlFor="login-confirm">Confirm password <span className="req">*</span></label>
                  <input
                    id="login-confirm"
                    className="input-field"
                    type={showPw ? 'text' : 'password'}
                    autoComplete="new-password"
                    placeholder="••••••••"
                    value={confirm}
                    onChange={(e) => setConfirm(e.target.value)}
                  />
                </div>

                <div className="auth-rules">
                  {RULES.map((r) => {
                    const met = r.test(password)
                    return (
                      <span key={r.label} className={`auth-rule ${met ? 'is-met' : ''}`}>
                        <span className="auth-rule-dot">{met ? <Check size={9} /> : ''}</span>
                        {r.label}
                      </span>
                    )
                  })}
                </div>
              </>
            )}

            <button type="submit" className="auth-submit" disabled={busy}>
              {busy ? 'Please wait…' : mode === 'login' ? 'Sign in' : 'Create account'}
              {!busy && <ArrowRight size={15} />}
            </button>
          </form>

          <button type="button" className="auth-switch" onClick={() => switchMode(mode === 'login' ? 'register' : 'login')}>
            {mode === 'login'
              ? "Don't have an account? Register"
              : 'Already registered? Sign in'}
          </button>

          <div className="auth-demo">
            <div className="auth-demo-label">Seeded demo accounts</div>
            <div className="auth-demo-grid">
              {DEMO_ACCOUNTS.map((d) => (
                <button key={d.username} type="button" className="auth-demo-btn" onClick={() => fillDemo(d)}>
                  <span className="demo-icon"><d.icon size={13} /></span>
                  <span className="demo-role">{d.role}</span>
                </button>
              ))}
            </div>
          </div>
        </div>
      </main>
    </div>
  )
}
