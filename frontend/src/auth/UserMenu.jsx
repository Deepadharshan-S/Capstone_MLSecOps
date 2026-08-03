import { useState } from 'react'
import { useAuth } from './AuthContext'

// Drop this into the existing <Topbar> in App.jsx, e.g. inside .topbar-right.
export default function UserMenu() {
  const { user, logout } = useAuth()
  const [loggingOut, setLoggingOut] = useState(false)

  if (!user) return null

  async function handleLogout() {
    setLoggingOut(true)
    try {
      await logout()
    } finally {
      setLoggingOut(false)
    }
  }

  return (
    <div className="topbar-badge" style={{ gap: 10 }}>
      <span>{user.username}</span>
      <span style={{ opacity: 0.6, fontSize: 11, textTransform: 'uppercase' }}>{user.role}</span>
      <button
        className="btn btn-ghost btn-sm"
        onClick={handleLogout}
        disabled={loggingOut}
      >
        {loggingOut ? 'Signing out…' : 'Sign out'}
      </button>
    </div>
  )
}
