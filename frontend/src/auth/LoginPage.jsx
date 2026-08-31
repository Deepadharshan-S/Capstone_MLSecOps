import { useState } from 'react'
import { useAuth } from './AuthContext'
import { ApiError } from '../api/client'
import './LoginPage.css'

// Mirrors backend/app/core/security.py validate_password_strength().
// Checking client-side just gives faster feedback — the backend is the
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

export default function LoginPage() {
  const { login, register } = useAuth()
  const [mode, setMode] = useState('login') // 'login' | 'register'
  const [username, setUsername] = useState('')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState(null)
  const [notice, setNotice] = useState(null)

  const passwordIssues = mode === 'register' && password ? checkPasswordStrength(password) : []

  async function handleSubmit(e) {
    e.preventDefault()
    setError(null)
    setNotice(null)

    if (mode === 'register' && passwordIssues.length > 0) {
      setError(`Password needs ${passwordIssues.join(', ')}.`)
      return
    }

    setSubmitting(true)
    try {
      if (mode === 'login') {
        await login(username, password)
      } else {
        await register(username, email, password)
        setNotice('Account created. You can log in now.')
        setMode('login')
        setPassword('')
      }
    } catch (err) {
      setError(describeError(err))
    } finally {
      setSubmitting(false)
    }
  }

  function describeError(err) {
    if (!(err instanceof ApiError)) return 'Network error. Please try again.'
    if (err.status === 429) return 'Too many attempts. Please wait a minute and try again.'
    if (err.status === 403) return err.message // account-locked message from backend
    if (err.status === 400) return err.message // generic bad credentials / weak password / collision
    return err.message || 'Something went wrong.'
  }

  return (
    <div className="auth-page">
      <div className="auth-card">
        <div className="auth-brand">
          <div className="auth-logo">M</div>
          <span>ML<b>SecOps</b></span>
        </div>

        <h1 className="auth-title">{mode === 'login' ? 'Sign in' : 'Create an account'}</h1>
        <p className="auth-subtitle">
          {mode === 'login'
            ? 'Access the MLSecOps pipeline console.'
            : 'New accounts start with viewer access.'}
        </p>

        {error && <div className="auth-alert auth-alert-error">{error}</div>}
        {notice && <div className="auth-alert auth-alert-ok">{notice}</div>}

        <form onSubmit={handleSubmit} noValidate>
          <label className="auth-field">
            <span>Username or email</span>
            <input
              type="text"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              autoComplete="username"
              required
              minLength={mode === 'register' ? 3 : undefined}
              maxLength={mode === 'register' ? 50 : undefined}
            />
          </label>

          {mode === 'register' && (
            <label className="auth-field">
              <span>Email</span>
              <input
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                autoComplete="email"
                required
              />
            </label>
          )}

          <label className="auth-field">
            <span>Password</span>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
              required
              minLength={8}
              maxLength={64}
            />
          </label>

          {mode === 'register' && password && (
            <ul className="auth-password-hints">
              {['at least 8 characters', 'no more than 64 characters', 'an uppercase letter',
                'a lowercase letter', 'a digit', 'a special character'].map((rule) => (
                <li key={rule} className={passwordIssues.includes(rule) ? 'unmet' : 'met'}>
                  {passwordIssues.includes(rule) ? '○' : '✓'} {rule}
                </li>
              ))}
            </ul>
          )}

          <button type="submit" className="auth-submit" disabled={submitting}>
            {submitting
              ? (mode === 'login' ? 'Signing in…' : 'Creating account…')
              : (mode === 'login' ? 'Sign in' : 'Create account')}
          </button>
        </form>

        <button
          type="button"
          className="auth-switch"
          onClick={() => {
            setMode(mode === 'login' ? 'register' : 'login')
            setError(null)
            setNotice(null)
          }}
        >
          {mode === 'login' ? "Don't have an account? Register" : 'Already have an account? Sign in'}
        </button>
      </div>
    </div>
  )
}
