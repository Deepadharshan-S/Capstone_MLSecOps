import { useAuth } from './auth/AuthContext.jsx'
import LoginPage from './auth/LoginPage.jsx'
import App from './App.jsx'

export default function AppShell() {
  const { user, loading } = useAuth()

  if (loading) {
    return (
      <div style={{
        minHeight: '100vh',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        background: 'var(--bg-base)',
        color: 'var(--text-400)',
        fontSize: 13,
        fontFamily: 'Inter, sans-serif',
      }}>
        Loading…
      </div>
    )
  }

  if (!user) return <LoginPage />

  return <App />
}
