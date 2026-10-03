import { Link, useNavigate } from 'react-router-dom'
import { useAuth } from '../auth/AuthContext.jsx'
import {
  datasetsApi, mlOpsApi, usersApi, healthApi,
} from '../api/endpoints.js'
import { useFetch, usePolling } from '../hooks/useFetch.js'
import { formatAccuracy } from '../lib/models.js'
import {
  PageHeader, StatCard, Panel, Badge, StatusBadge, LoadingState, ErrorState,
  EmptyState, Button, KeyValue, SkeletonCards, DataTable,
} from '../components/ui/index.jsx'
import {
  Database, Boxes, ClipboardList, Rocket, Route, ArrowRight,
  Activity, Users, ShieldCheck, Clock, Play, Upload,
  RefreshCw, TrendingUp, Target,
} from '../components/icons.jsx'

function quickStart(hasPermission) {
  return [
    {
      to: '/datasets', icon: Database, title: 'Register a dataset',
      desc: 'Upload a CSV/Parquet file into a lakeFS-backed dataset with branches, tags and full version history.',
      scope: 'datasets:view',
    },
    {
      to: '/pipeline', icon: Route, title: 'Run a training pipeline',
      desc: 'Pick a dataset, choose an algorithm, tune hyperparameters and stream training logs in real time.',
      scope: 'models:train',
    },
    {
      to: '/registry', icon: Boxes, title: 'Browse the model registry',
      desc: 'Inspect registered models, compare versions and review MLflow run metrics side by side.',
      scope: 'models:view',
    },
    {
      to: '/deployments', icon: Rocket, title: 'Deploy a model',
      desc: 'Promote a model version to staging or production and monitor it for drift and errors.',
      scope: 'models:deploy',
    },
  ].filter((i) => !i.scope || hasPermission(i.scope))
}

export default function Dashboard() {
  const { user, hasPermission } = useAuth()
  const navigate = useNavigate()

  const health = usePolling(() => healthApi.check(), 60000, { immediate: true })
  const datasets = useFetch(() => datasetsApi.list(), [])
  const models = useFetch(() => mlOpsApi.listModels(), [])
  const jobs = useFetch(() => mlOpsApi.listJobs(50), [])
  const deployments = useFetch(() => mlOpsApi.listDeployments(), [])
  const experiments = useFetch(() => mlOpsApi.listExperiments(), [])

  const loading = datasets.loading && models.loading && jobs.loading && deployments.loading
  const firstError = datasets.error || models.error || jobs.error || deployments.error

  const dsList = datasets.data?.datasets || []
  const modelList = models.data?.models || []
  const jobList = jobs.data?.jobs || []
  const depList = deployments.data?.deployments || []
  const expList = experiments.data?.experiments || []

  const runningJobs = jobList.filter((j) => ['running', 'pending', 'queued', 'in_progress'].includes(String(j.status).toLowerCase()))
  const activeDeps = depList.filter((d) => d.is_active ?? d.active ?? String(d.status).toLowerCase() === 'active')
  const succeeded = jobList.filter((j) => ['completed', 'success', 'succeeded'].includes(String(j.status).toLowerCase()))

  const counts = { datasets: dsList.length, models: modelList.length, jobs: jobList.length, deployments: depList.length }

  if (loading) {
    return (
      <div className="page">
        <PageHeader icon={Activity} title="Dashboard" subtitle="Loading workspace overview…" />
        <SkeletonCards count={4} />
        <div className="grid-2">
          <div className="skeleton" style={{ height: 260, borderRadius: 'var(--r-xl)' }} />
          <div className="skeleton" style={{ height: 260, borderRadius: 'var(--r-xl)' }} />
        </div>
      </div>
    )
  }

  return (
    <div className="page">
      <PageHeader
        icon={Activity}
        title={`Welcome back, ${user?.username || 'there'}`}
        subtitle={`Signed in as ${user?.role?.replace(/_/g, ' ') || 'user'} · ${new Date().toLocaleDateString(undefined, { weekday: 'long', month: 'long', day: 'numeric' })}`}
        actions={
          <>
            <Button variant="secondary" icon={RefreshCw} onClick={() => { datasets.refetch(); models.refetch(); jobs.refetch(); deployments.refetch(); experiments.refetch(); }}>
              Refresh
            </Button>
            {hasPermission('models:train') && (
              <Button variant="primary" icon={Route} onClick={() => navigate('/pipeline')}>
                New pipeline run
              </Button>
            )}
          </>
        }
      />

      {firstError && (
        <ErrorState
          error={firstError}
          title="Some panels failed to load"
          onRetry={() => { datasets.refetch(); models.refetch(); jobs.refetch(); deployments.refetch() }}
        />
      )}

      {/* ── KPI tiles ─────────────────────────────── */}
      <div className="stat-grid">
        <StatCard
          icon={Database} tone="accent"
          value={counts.datasets} label="Datasets"
          sub={dsList.length ? `${dsList.length} lakeFS repositor${dsList.length === 1 ? 'y' : 'ies'}` : 'No data registered yet'}
          onClick={() => navigate('/datasets')}
        />
        <StatCard
          icon={Boxes} tone="info"
          value={counts.models} label="Registered models"
          sub={hasPermission('models:view') ? 'Open the registry' : 'View-only access'}
          onClick={() => navigate('/registry')}
        />
        <StatCard
          icon={ClipboardList} tone="purple"
          value={counts.jobs} label="Training jobs"
          sub={`${runningJobs.length} running · ${succeeded.length} succeeded`}
          onClick={() => navigate('/jobs')}
        />
        <StatCard
          icon={Rocket} tone="success"
          value={activeDeps.length} label="Active deployments"
          sub={`${depList.length} total tracked`}
          onClick={() => navigate('/deployments')}
        />
      </div>

      {/* ── Status + activity row ─────────────────── */}
      <div className="grid-2">
        <Panel
          icon={Activity}
          title="Platform status"
          subtitle="Live health of connected services"
          actions={<Badge tone={health.data?.status === 'healthy' ? 'success' : health.data ? 'warning' : 'danger'} dot>
            {health.data?.status === 'healthy' ? 'All systems operational' : health.data ? 'Degraded' : 'API unreachable'}
          </Badge>}
        >
          <KeyValue
            items={[
              ['API base', <span key="ab" className="mono">{import.meta.env.VITE_API_URL || '/api'}</span>],
              ['API status', <span key="as" className="mono">{health.data?.status || '—'}</span>],
              ['Last checked', health.lastUpdated ? health.lastUpdated.toLocaleTimeString() : '—'],
              ['Datasets (lakeFS)', dsList.length ? `${dsList.length} repositories` : 'None yet'],
              ['Experiments (MLflow)', expList.length ? `${expList.length} tracked` : 'None yet'],
              ['Session role', user?.role || '—'],
            ]}
          />
          <div className="mt-4 quick-actions">
            <Button variant="secondary" size="sm" icon={RefreshCw} onClick={health.refresh}>Re-check health</Button>
            {hasPermission('datasets:upload') && (
              <Button variant="secondary" size="sm" icon={Upload} onClick={() => navigate('/datasets')}>Upload dataset</Button>
            )}
            <Button variant="secondary" size="sm" icon={Target} onClick={() => navigate('/experiments')}>Experiments</Button>
          </div>
        </Panel>

        <Panel
          icon={Clock}
          title="Recent activity"
          subtitle="Latest training jobs across the workspace"
          actions={<Button variant="ghost" size="sm" icon={ArrowRight} onClick={() => navigate('/jobs')}>All jobs</Button>}
          bodyClass={jobList.length ? 'tight' : ''}
        >
          {jobList.length === 0 ? (
            <EmptyState
              icon={ClipboardList}
              title="No training jobs yet"
              desc="Run your first pipeline to start populating this feed."
              actions={hasPermission('models:train') ? <Button variant="primary" size="sm" icon={Play} onClick={() => navigate('/pipeline')}>Start pipeline</Button> : null}
            />
          ) : (
            <DataTable
              pageSize={6}
              keyOf={(r) => r.job_id || r.run_id}
              rows={jobList}
              columns={[
                {
                  key: 'name', strong: true, header: 'Job',
                  render: (r) => (
                    <span className="truncate" title={r.algorithm || r.name || r.job_id}>
                      {r.algorithm || r.name || r.job_id?.slice(0, 8) || 'job'}
                    </span>
                  ),
                },
                {
                  key: 'status', header: 'Status',
                  render: (r) => <StatusBadge status={r.status} sm />,
                },
                {
                  key: 'when', width: 140, header: 'Started',
                  render: (r) => (
                    <span className="text-xs muted">
                      {(r.started_at || r.created_at)
                        ? new Date(r.started_at || r.created_at).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
                        : '—'}
                    </span>
                  ),
                },
              ]}
            />
          )}
        </Panel>
      </div>

      {/* ── Model & deployment health ─────────────── */}
      <div className="grid-2">
        <Panel
          icon={TrendingUp}
          title="Model registry"
          subtitle={`${modelList.length} models · ${expList.length} experiments`}
          actions={<Button variant="ghost" size="sm" icon={ArrowRight} onClick={() => navigate('/registry')}>Open registry</Button>}
          bodyClass={modelList.length ? 'tight-padded' : ''}
        >
          {modelList.length === 0 ? (
            <EmptyState
              icon={Boxes}
              title="Registry is empty"
              desc="Train a model to register its first version."
            />
          ) : (
            <div>
              <div className="row-list">
                {modelList.slice(0, 5).map((m) => (
                  <Link key={m.name} to={`/registry?model=${encodeURIComponent(m.name)}`} className="row-item">
                    <span className="row-glyph is-accent"><Boxes size={15} /></span>
                    <span className="row-info">
                      <span className="row-title">{m.name}</span>
                      <span className="row-sub">
                        {m.accuracy != null ? `${formatAccuracy(m.accuracy)} accuracy` : 'metrics unavailable'}
                        {' · '}
                        {m.experiment_name && m.experiment_name !== 'unknown' ? m.experiment_name : 'uploaded'}
                      </span>
                    </span>
                    <ArrowRight size={14} style={{ color: 'var(--text-disabled)' }} />
                  </Link>
                ))}
              </div>
            </div>
          )}
        </Panel>

        <Panel
          icon={Rocket}
          title="Deployments"
          subtitle={`${activeDeps.length} active of ${depList.length} tracked`}
          actions={<Button variant="ghost" size="sm" icon={ArrowRight} onClick={() => navigate('/deployments')}>Manage</Button>}
          bodyClass={depList.length ? 'tight-padded' : ''}
        >
          {depList.length === 0 ? (
            <EmptyState
              icon={Rocket}
              title="No deployments yet"
              desc="Promote a registry model to staging or production."
            />
          ) : (
            <div>
              <div className="row-list">
                {depList.slice(0, 5).map((d) => (
                  <div key={d.deployment_id} className="row-item">
                    <span className={`row-glyph ${d.is_active || d.active ? 'is-success' : ''}`}>
                      <Rocket size={15} />
                    </span>
                    <span className="row-info">
                      <span className="row-title">{d.model_name || d.name || d.deployment_id}</span>
                      <span className="row-sub">
                        {d.environment || 'default'} · {d.endpoint?.replace(/^https?:\/\//, '') || d.url || 'local'}
                      </span>
                    </span>
                    <StatusBadge status={d.is_active || d.active ? 'active' : d.status} sm />
                  </div>
                ))}
              </div>
            </div>
          )}
        </Panel>
      </div>

      {/* ── Quick start ───────────────────────────── */}
      <Panel icon={ShieldCheck} title="Quick start" subtitle="Common next steps for your role">
        <div className="grid-4">
          {quickStart(hasPermission).map((q) => (
            <button
              key={q.to}
              type="button"
              className="model-card"
              onClick={() => navigate(q.to)}
              style={{ textAlign: 'left' }}
            >
              <div className="model-card-top">
                <span className="model-glyph"><q.icon size={17} /></span>
                <ArrowRight size={14} style={{ color: 'var(--text-disabled)' }} />
              </div>
              <div className="model-name">{q.title}</div>
              <div className="model-desc" style={{ minHeight: 0, marginTop: 4 }}>{q.desc}</div>
            </button>
          ))}
        </div>
      </Panel>

      {/* ── Workspace summary ─────────────────────── */}
      <Panel icon={Users} title="Workspace summary" subtitle="Counts pulled live from the API" bodyClass="tight">
        <div className="table-wrap">
          <table className="data-table">
            <caption className="sr-only">Workspace summary — counts by resource</caption>
            <thead>
              <tr>
                <th scope="col">Resource</th>
                <th scope="col">Count</th>
                <th scope="col" className="cell-actions"><span className="sr-only">Open</span></th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <td className="cell-strong">Datasets</td>
                <td>{dsList.length}</td>
                <td className="cell-actions">
                  <Button variant="ghost" size="xs" onClick={() => navigate('/datasets')} icon={ArrowRight}>Open</Button>
                </td>
              </tr>
              <tr>
                <td className="cell-strong">Registered models</td>
                <td>{modelList.length}</td>
                <td className="cell-actions">
                  <Button variant="ghost" size="xs" onClick={() => navigate('/registry')} icon={ArrowRight}>Open</Button>
                </td>
              </tr>
              <tr>
                <td className="cell-strong">Training jobs</td>
                <td>{jobList.length}{runningJobs.length ? ` (${runningJobs.length} running)` : ''}</td>
                <td className="cell-actions">
                  <Button variant="ghost" size="xs" onClick={() => navigate('/jobs')} icon={ArrowRight}>Open</Button>
                </td>
              </tr>
              <tr>
                <td className="cell-strong">Experiments</td>
                <td>{expList.length}</td>
                <td className="cell-actions">
                  <Button variant="ghost" size="xs" onClick={() => navigate('/experiments')} icon={ArrowRight}>Open</Button>
                </td>
              </tr>
              <tr>
                <td className="cell-strong">Deployments</td>
                <td>{depList.length}</td>
                <td className="cell-actions">
                  <Button variant="ghost" size="xs" onClick={() => navigate('/deployments')} icon={ArrowRight}>Open</Button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </Panel>

      {/* Users list is admin-only; show only what the role can see. */}
      {hasPermission('users:manage') && <AdminTeaser />}
    </div>
  )
}

function AdminTeaser() {
  const users = useFetch(() => usersApi.list(), [])
  const list = users.data?.users || []
  return (
    <Panel
      icon={ShieldCheck}
      iconTone="warning"
      title="Team access"
      subtitle={`${list.length} registered user${list.length === 1 ? '' : 's'}`}
      actions={<Link to="/admin"><Button variant="ghost" size="sm" icon={ArrowRight}>Manage users</Button></Link>}
      bodyClass="tight"
    >
      {users.loading ? (
        <LoadingState label="Loading users…" />
      ) : (
        <DataTable
          pageSize={5}
          keyOf={(r) => r.user_id || r.username}
          rows={list}
          columns={[
            { key: 'username', strong: true, header: 'User' },
            { key: 'role', header: 'Role', render: (r) => <Badge tone="accent">{r.role}</Badge> },
            { key: 'email', header: 'Email', render: (r) => <span className="muted text-xs">{r.email || '—'}</span> },
            { key: 'active', header: 'Status', render: (r) => <StatusBadge status={r.is_active === false ? 'inactive' : 'active'} sm /> },
          ]}
        />
      )}
    </Panel>
  )
}
