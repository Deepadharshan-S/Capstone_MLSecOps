import { lazy, Suspense, useState, useEffect } from 'react'
import { Routes, Route, Navigate, useLocation, Link } from 'react-router-dom'
import { RoleGate } from './auth/AuthContext.jsx'
import Sidebar from './shell/Sidebar.jsx'
import { routeMeta } from './shell/routeMeta.js'
import UserMenu from './shell/UserMenu.jsx'
import { healthApi } from './api/endpoints.js'
import { useLocalStorage, usePolling } from './hooks/useFetch.js'
import { ChevronRight, Menu } from './components/icons.jsx'
import { LoadingState } from './components/ui/index.jsx'

// Route-level code splitting: every page ships as its own chunk (charts,
// heavy widgets and page-only deps stay out of the first paint).
const Dashboard = lazy(() => import('./pages/Dashboard.jsx'))
const Pipeline = lazy(() => import('./pages/Pipeline.jsx'))
const Datasets = lazy(() => import('./pages/Datasets.jsx'))
const DatasetDetail = lazy(() => import('./pages/DatasetDetail.jsx'))
const Registry = lazy(() => import('./pages/Registry.jsx'))
const Deployments = lazy(() => import('./pages/Deployments.jsx'))
const Predict = lazy(() => import('./pages/Predict.jsx'))
const Jobs = lazy(() => import('./pages/Jobs.jsx'))
const Experiments = lazy(() => import('./pages/Experiments.jsx'))
const Admin = lazy(() => import('./pages/Admin.jsx'))

function Topbar({ onMenuOpen, menuOpen, health, onHealthClick }) {
  const { title, crumbs } = routeMeta(useLocation().pathname)

  return (
    <header className="topbar">
      <div className="topbar-left">
        <button
          type="button"
          className="btn btn-ghost btn-icon mobile-nav-toggle"
          onClick={onMenuOpen}
          aria-label={menuOpen ? 'Close navigation' : 'Open navigation'}
          aria-expanded={menuOpen}
          aria-controls="app-sidebar"
        >
          <Menu size={18} />
        </button>
        <div className="topbar-title">{title}</div>
        <nav className="breadcrumb" aria-label="Breadcrumb">
          {crumbs
            // The trailing crumb duplicates the topbar title — hide it so the
            // bar reads "Datasets › my-data" instead of "Datasets › SentinelML › Datasets".
            .filter((c, i, arr) => !(i === arr.length - 1 && c.label === title))
            .map((c, i, arr) => (
            <span key={`${c.label}-${i}`} className="flex items-center gap-1">
              {i > 0 && <ChevronRight size={12} />}
              {c.to && i < arr.length - 1
                ? <Link to={c.to}>{c.label}</Link>
                : <span className="crumb-current">{c.label}</span>}
            </span>
          ))}
        </nav>
      </div>

      <div className="topbar-right">
        <button type="button" className="topbar-health" onClick={onHealthClick} title="Backend health — click to refresh">
          <span className={`health-dot ${health ? 'is-ok' : 'is-err'}`} />
          <span>{health ? 'Connected' : 'Offline'}</span>
        </button>
        <UserMenu />
      </div>
    </header>
  )
}

export default function App() {
  const [collapsed, setCollapsed] = useLocalStorage('sentinelml.sidebar.collapsed', false)
  const [mobileOpen, setMobileOpen] = useState(false)
  const location = useLocation()

  // GET /health — polled every 30s; drives the topbar connection chip.
  const { data: health, refresh: refreshHealth } = usePolling(
    () => healthApi.check(),
    30000,
    { immediate: true },
  )

  // Close the mobile drawer whenever the route changes.
  useEffect(() => { setMobileOpen(false) }, [location.pathname])

  const shellCls = [
    'shell',
    collapsed ? 'sidebar-collapsed' : '',
    mobileOpen ? 'sidebar-open' : '',
  ].filter(Boolean).join(' ')

  return (
    <div className={shellCls}>
      <Sidebar
        collapsed={collapsed}
        onToggle={() => setCollapsed((c) => !c)}
        open={mobileOpen}
        onNavigate={() => setMobileOpen(false)}
        health={health}
      />
      {mobileOpen && <div className="sidebar-scrim" onClick={() => setMobileOpen(false)} />}

      <Topbar
        onMenuOpen={() => setMobileOpen((o) => !o)}
        menuOpen={mobileOpen}
        health={health}
        onHealthClick={refreshHealth}
      />

      <main className="main-area">
        <Suspense fallback={<LoadingState label="Loading view…" />}>
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/pipeline" element={<Pipeline />} />
            <Route path="/datasets" element={<Datasets />} />
            <Route path="/datasets/:name" element={<DatasetDetail />} />
            <Route path="/jobs" element={<Jobs />} />
            <Route path="/registry" element={<Registry />} />
            <Route path="/deployments" element={<Deployments />} />
            <Route path="/predict" element={<Predict />} />
            <Route path="/experiments" element={<Experiments />} />
            <Route
              path="/admin"
              element={
                <RoleGate scope="users:manage" fallback={<Navigate to="/" replace />}>
                  <Admin />
                </RoleGate>
              }
            />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </Suspense>
      </main>
    </div>
  )
}
