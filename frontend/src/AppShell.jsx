import { useAuth } from './auth/AuthContext.jsx'
import LoginPage from './auth/LoginPage.jsx'
import App from './App.jsx'
import ErrorBoundary from './components/ErrorBoundary.jsx'
import { ToastProvider, ConfirmProvider } from './components/ui/index.jsx'

function BootScreen() {
  return (
    <div className="boot-screen">
      <div className="boot-mark">M</div>
      <div className="spinner" />
      <div className="boot-text">Restoring your session…</div>
    </div>
  )
}

export default function AppShell() {
  const { user, loading, authError } = useAuth()

  if (loading) return <BootScreen />

  if (!user) return <LoginPage initialNotice={authError} />

  return (
    <ToastProvider>
      <ConfirmProvider>
        <ErrorBoundary>
          <App />
        </ErrorBoundary>
      </ConfirmProvider>
    </ToastProvider>
  )
}
