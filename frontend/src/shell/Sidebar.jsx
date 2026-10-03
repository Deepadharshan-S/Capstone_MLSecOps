import { NavLink } from 'react-router-dom'
import { useAuth } from '../auth/AuthContext.jsx'
import {
  LayoutDashboard, Route, Database, ClipboardList, Boxes, Rocket,
  FlaskConical, Terminal, ShieldCheck, PanelLeftClose, PanelLeftOpen,
} from '../components/icons.jsx'

const NAV_GROUPS = [
  {
    label: 'Overview',
    items: [
      { to: '/', end: true, icon: LayoutDashboard, label: 'Dashboard' },
    ],
  },
  {
    label: 'Build',
    items: [
      { to: '/pipeline', icon: Route, label: 'Pipeline' },
      { to: '/datasets', icon: Database, label: 'Datasets' },
      { to: '/jobs', icon: ClipboardList, label: 'Jobs' },
    ],
  },
  {
    label: 'Operate',
    items: [
      { to: '/registry', icon: Boxes, label: 'Registry' },
      { to: '/deployments', icon: Rocket, label: 'Deployments' },
      { to: '/predict', icon: Terminal, label: 'Predict' },
      { to: '/experiments', icon: FlaskConical, label: 'Experiments' },
    ],
  },
  {
    label: 'Administer',
    scope: 'users:manage',
    items: [
      { to: '/admin', icon: ShieldCheck, label: 'Admin', scope: 'users:manage' },
    ],
  },
]

export default function Sidebar({ collapsed, onToggle, onNavigate, health }) {
  const { hasPermission } = useAuth()

  const healthDot = health?.status === 'healthy' ? 'is-ok'
    : health ? 'is-warn' : ''
  const healthLabel = health?.status === 'healthy' ? 'API healthy'
    : health ? 'API degraded' : 'API unreachable'

  return (
    <aside className="sidebar" id="app-sidebar" aria-label="Primary navigation">
      <div className="sidebar-brand">
        <div className="sidebar-logo" aria-hidden>S</div>
        <span className="sidebar-brand-name">Sentinel<span>ML</span></span>
        <button
          className="sidebar-collapse"
          onClick={onToggle}
          aria-label={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
          title={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
          type="button"
        >
          {collapsed ? <PanelLeftOpen size={15} /> : <PanelLeftClose size={15} />}
        </button>
      </div>

      <div className="sidebar-scroll">
        {NAV_GROUPS.map((g) => {
          if (g.scope && !hasPermission(g.scope)) return null
          const items = g.items.filter((i) => !i.scope || hasPermission(i.scope))
          if (items.length === 0) return null
          return (
            <div key={g.label}>
              <span className="sidebar-section-label">{g.label}</span>
              <nav className="nav-list">
                {items.map((i) => (
                  <NavLink
                    key={i.to}
                    to={i.to}
                    end={i.end}
                    onClick={onNavigate}
                    title={collapsed ? i.label : undefined}
                    className={({ isActive }) => `nav-link${isActive ? ' nav-active' : ''}`}
                  >
                    <i.icon size={17} strokeWidth={2} />
                    <span>{i.label}</span>
                  </NavLink>
                ))}
              </nav>
            </div>
          )
        })}
      </div>

      <div className="sidebar-footer">
        <div className="health-row">
          <span className={`health-dot ${healthDot}`} />
          <span>{healthLabel}</span>
        </div>
        <div className="sidebar-meta">
          SentinelML Platform v1.0<br />
          Capstone · {new Date().getFullYear()}
        </div>
      </div>
    </aside>
  )
}
