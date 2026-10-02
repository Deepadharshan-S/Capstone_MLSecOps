import { useState, useRef, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAuth } from '../auth/AuthContext.jsx'
import {
  LogOut, Settings, User, Shield, ChevronDown, LifeBuoy, Mail,
} from '../components/icons.jsx'

const ROLE_TONE = {
  admin: { bg: 'var(--accent-soft)', fg: 'var(--accent-text)' },
  data_scientist: { bg: 'var(--info-soft)', fg: 'var(--info-text)' },
  ml_engineer: { bg: 'var(--purple-soft)', fg: 'var(--purple)' },
  viewer: { bg: 'var(--bg-muted)', fg: 'var(--text-secondary)' },
}

const ROLE_LABEL = {
  admin: 'Administrator',
  data_scientist: 'Data Scientist',
  ml_engineer: 'ML Engineer',
  viewer: 'Viewer',
}

export default function UserMenu() {
  const { user, logout } = useAuth()
  const [open, setOpen] = useState(false)
  const [loggingOut, setLoggingOut] = useState(false)
  const wrapRef = useRef(null)
  const navigate = useNavigate()

  useEffect(() => {
    if (!open) return undefined
    const onDoc = (e) => { if (wrapRef.current && !wrapRef.current.contains(e.target)) setOpen(false) }
    const onKey = (e) => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', onDoc)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDoc)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  if (!user) return null

  const initials = (user.username || '?').slice(0, 2)
  const tone = ROLE_TONE[user.role] || ROLE_TONE.viewer
  const roleLabel = ROLE_LABEL[user.role] || user.role

  async function handleLogout() {
    setLoggingOut(true)
    try { await logout() } finally { setLoggingOut(false) }
  }

  return (
    <div className="user-menu" ref={wrapRef}>
      <button
        type="button"
        className="user-trigger"
        onClick={() => setOpen((o) => !o)}
        aria-haspopup="menu"
        aria-expanded={open}
      >
        <span className="avatar" style={{ background: tone.bg, color: tone.fg }}>{initials}</span>
        <span className="user-trigger-meta">
          <span className="user-trigger-name">{user.username}</span>
          <span className="user-trigger-role">{roleLabel}</span>
        </span>
        <ChevronDown size={14} style={{ color: 'var(--text-tertiary)', opacity: open ? 1 : .6 }} />
      </button>

      {open && (
        <div className="dropdown" role="menu">
          <div className="dropdown-head">
            <div className="dropdown-head-name">{user.username}</div>
            <div className="dropdown-head-sub">{user.email || '—'}</div>
          </div>

          <div style={{ padding: '4px 10px 8px' }}>
            <span
              className="badge"
              style={{ background: tone.bg, color: tone.fg }}
            >
              <Shield size={11} /> {roleLabel}
            </span>
          </div>

          <button className="dropdown-item" role="menuitem" onClick={() => { setOpen(false); navigate('/') }}>
            <User size={15} /> Profile &amp; overview
          </button>
          <button className="dropdown-item" role="menuitem" onClick={() => { setOpen(false); navigate('/datasets') }}>
            <Settings size={15} /> Workspace settings
          </button>
          <button className="dropdown-item" role="menuitem" onClick={() => { setOpen(false) }}>
            <LifeBuoy size={15} /> Help &amp; docs
          </button>

          <div className="dropdown-divider" />

          <div style={{ padding: '4px 10px 8px', display: 'flex', alignItems: 'center', gap: 6, color: 'var(--text-tertiary)', fontSize: 11 }}>
            <Mail size={12} />
            <span className="truncate">{user.email || 'no-email'}</span>
          </div>

          <button
            className="dropdown-item is-danger"
            role="menuitem"
            onClick={handleLogout}
            disabled={loggingOut}
          >
            <LogOut size={15} /> {loggingOut ? 'Signing out…' : 'Sign out'}
          </button>
        </div>
      )}
    </div>
  )
}
