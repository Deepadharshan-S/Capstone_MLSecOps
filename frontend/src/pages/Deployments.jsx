import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { mlOpsApi } from '../api/endpoints.js'
import { useFetch, useDebounced } from '../hooks/useFetch.js'
import { useAuth } from '../auth/AuthContext.jsx'
import {
  PageHeader, Panel, Button, DataTable, Badge, StatusBadge,
  EmptyState, LoadingState, ErrorState, SearchInput, StatCard,
  Segmented, useToast, useConfirm, CopyButton, Select, Alert,
} from '../components/ui/index.jsx'
import {
  Rocket, RefreshCw, Square, RotateCcw, Search, Terminal,
  CheckCircle2, Layers, Plus, Info,
} from '../components/icons.jsx'

const ACTIVE_STATUSES = ['running', 'active', 'starting', 'deploying']

export default function Deployments() {
  const navigate = useNavigate()
  const toast = useToast()
  const confirm = useConfirm()
  const { hasPermission } = useAuth()
  const canManage = hasPermission('deployments:manage')

  const [statusFilter, setStatusFilter] = useState('')
  const [envFilter, setEnvFilter] = useState('')
  const [activeOnly, setActiveOnly] = useState(false)
  const [query, setQuery] = useState('')
  const [acting, setActing] = useState(null)
  const debounced = useDebounced(query, 200)

  const deployments = useFetch(
    () => mlOpsApi.listDeployments({
      status: statusFilter || undefined,
      environment: envFilter || undefined,
      active_only: activeOnly || undefined,
    }),
    [statusFilter, envFilter, activeOnly],
  )

  const all = deployments.data?.deployments || []
  const rows = all.filter((d) =>
    !debounced
    || [d.model_name, d.deployment_id, d.environment, d.status, d.deployed_by]
      .some((v) => String(v || '').toLowerCase().includes(debounced.toLowerCase())),
  )

  const active = all.filter((d) => ACTIVE_STATUSES.includes(String(d.status).toLowerCase()) || d.is_active)
  const prod = all.filter((d) => d.environment === 'production')
  const stopped = all.filter((d) => String(d.status).toLowerCase() === 'stopped')

  async function manage(deployment_id, action) {
    if (action === 'stop') {
      const ok = await confirm({
        title: `Stop deployment ${deployment_id}?`,
        text: 'The RayService is scaled to zero and the deployment is marked stopped. You can restart it afterwards.',
        confirmLabel: 'Stop deployment',
        tone: 'danger',
      })
      if (!ok) return
    }
    setActing(`${deployment_id}:${action}`)
    try {
      const res = await mlOpsApi.manageDeployment(deployment_id, action)
      toast.success(`Deployment ${action}d`, res.message || `${deployment_id} · ${action}`)
      deployments.refetch()
    } catch (err) {
      toast.error(`${action} failed`, err?.message)
    } finally { setActing(null) }
  }

  return (
    <div className="page">
      <PageHeader
        icon={Rocket}
        title="Deployments"
        subtitle="KubeRay RayService deployments tracked in MLflow — restart, rollback and stop"
        actions={
          <>
            <Button variant="secondary" size="sm" icon={RefreshCw} onClick={deployments.refetch} loading={deployments.loading}>
              Refresh
            </Button>
            {canManage && hasPermission('models:deploy') && (
              <Button variant="primary" size="sm" icon={Plus} onClick={() => navigate('/registry')}>
                Deploy a model
              </Button>
            )}
          </>
        }
      />

      <div className="stat-grid">
        <StatCard icon={Rocket} tone="success" value={active.length} label="Active" sub="running or starting" />
        <StatCard icon={Layers} tone="accent" value={all.length} label="Total tracked" sub="all environments" />
        <StatCard icon={CheckCircle2} tone="info" value={prod.length} label="Production" sub="customer-facing" />
        <StatCard icon={Square} tone="danger" value={stopped.length} label="Stopped" sub="scaled to zero" />
      </div>

      {!canManage && (
        <Alert tone="info">
          You can view deployments, but the <strong>deployments:manage</strong> scope is required to restart, roll back or stop them.
        </Alert>
      )}

      <Panel
        icon={Search}
        title="Deployments"
        subtitle={`${rows.length} of ${all.length}`}
        actions={
          <div className="flex items-center gap-2" style={{ flexWrap: 'wrap' }}>
            <SearchInput value={query} onChange={setQuery} placeholder="Search…" />
            <Select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)} aria-label="Filter deployments by status" style={{ width: 140 }}>
              <option value="">All statuses</option>
              <option value="running">running</option>
              <option value="stopped">stopped</option>
              <option value="starting">starting</option>
            </Select>
            <Select value={envFilter} onChange={(e) => setEnvFilter(e.target.value)} aria-label="Filter deployments by environment" style={{ width: 150 }}>
              <option value="">All environments</option>
              <option value="staging">staging</option>
              <option value="production">production</option>
            </Select>
            <Segmented
              value={activeOnly ? 'active' : 'all'}
              onChange={(v) => setActiveOnly(v === 'active')}
              options={[
                { value: 'all', label: 'All' },
                { value: 'active', label: 'Active only' },
              ]}
            />
          </div>
        }
        bodyClass={rows.length ? 'tight' : ''}
      >
        {deployments.loading && !all.length ? (
          <LoadingState label="Loading deployments…" />
        ) : deployments.error ? (
          <ErrorState error={deployments.error} title="Could not load deployments" onRetry={deployments.refetch} />
        ) : rows.length === 0 ? (
          <EmptyState
            icon={Rocket}
            title={all.length ? 'No match' : 'No deployments yet'}
            desc={all.length ? 'Nothing matches the current filters.' : 'Deploy a model version from the Registry to see it here.'}
            actions={!all.length && hasPermission('models:deploy')
              ? <Button variant="primary" icon={Rocket} onClick={() => navigate('/registry')}>Open registry</Button>
              : null}
          />
        ) : (
          <DataTable
            caption="Deployments"
            keyOf={(d) => d.deployment_id}
            rows={rows}
            pageSize={10}
            columns={[
              {
                key: 'model_name', strong: true, header: 'Model',
                render: (d) => (
                  <span className="flex items-center gap-2">
                    <span className="row-glyph is-success" style={{ width: 26, height: 26 }}><Rocket size={13} /></span>
                    <span className="truncate" style={{ maxWidth: 180 }}>{d.model_name || d.deployment_id}</span>
                  </span>
                ),
              },
              { key: 'version', width: 70, header: 'Version', render: (d) => <Badge tone="outline">v{d.version}</Badge> },
              { key: 'environment', width: 120, header: 'Env', render: (d) => <Badge tone={d.environment === 'production' ? 'purple' : 'info'} sm>{d.environment}</Badge> },
              { key: 'status', width: 130, header: 'Status', render: (d) => <StatusBadge status={d.status} sm /> },
              {
                key: 'k8s_status', width: 120, header: 'RayService',
                render: (d) => <span className="text-xs muted">{d.k8s_status || '—'}</span>,
              },
              { key: 'deployed_by', width: 130, header: 'By', render: (d) => <span className="text-xs muted">{d.deployed_by || '—'}</span> },
              {
                key: 'endpoint', width: 150, header: 'Endpoint',
                render: (d) => d.endpoint_url
                  ? <code className="mono text-xs truncate" style={{ maxWidth: 140, display: 'inline-block' }} title={d.endpoint_url}>{d.endpoint_url}</code>
                  : <span className="muted">—</span>,
              },
              {
                key: 'predict', actions: true, width: 250, header: <span className="sr-only">Actions</span>,
                render: (d) => (
                  <span className="flex items-center" style={{ justifyContent: 'flex-end', gap: 4 }}>
                    <Link
                      className="btn btn-ghost btn-xs"
                      to={`/predict?deployment=${encodeURIComponent(d.deployment_id)}`}
                    >
                      <Terminal size={12} /> Predict
                    </Link>
                    {canManage && ['restart', 'rollback', 'stop'].map((a) => (
                      <Button
                        key={a}
                        variant={a === 'stop' ? 'danger-ghost' : 'ghost'}
                        size="xs"
                        icon={a === 'restart' ? RotateCcw : a === 'rollback' ? Layers : Square}
                        loading={acting === `${d.deployment_id}:${a}`}
                        onClick={() => manage(d.deployment_id, a)}
                      >
                        {a}
                      </Button>
                    ))}
                  </span>
                ),
              },
            ]}
          />
        )}
      </Panel>

      <Panel icon={Info} title="Actions" subtitle="What each management operation does">
        <div className="grid-3">
          {[
            ['Restart', 'Recreates the RayService pods while keeping the same model version and replicas.', RotateCcw],
            ['Rollback', 'Promotes the previously deployed version of this model and restarts traffic on it.', Layers],
            ['Stop', 'Scales the service to zero and marks the deployment stopped in MLflow.', Square],
          ].map(([t, d, Icon]) => (
            <div key={t} className="row-item" style={{ alignItems: 'flex-start' }}>
              <span className="row-glyph is-accent"><Icon size={15} /></span>
              <span className="row-info">
                <span className="row-title">{t}</span>
                <span className="row-sub" style={{ whiteSpace: 'normal' }}>{d}</span>
              </span>
            </div>
          ))}
        </div>
        <div className="mt-4 flex items-center gap-2">
          <code className="mono text-xs muted">POST /deployments/manage &#123; deployment_id, action &#125;</code>
          <CopyButton text='POST /deployments/manage {"deployment_id":"...","action":"restart"}' />
        </div>
      </Panel>
    </div>
  )
}
