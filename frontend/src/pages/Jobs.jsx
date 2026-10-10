import { lazy, Suspense, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { mlOpsApi } from '../api/endpoints.js'
import { useFetch, useDebounced, usePolling } from '../hooks/useFetch.js'
import { formatAccuracy } from '../lib/models.js'
import {
  PageHeader, Panel, Button, DataTable, Drawer, Badge, StatusBadge,
  EmptyState, LoadingState, ErrorState, SearchInput, Select,
  Progress, StatCard, Alert,
} from '../components/ui/index.jsx'
import { JobSummary, TrainingConsole } from './JobPanels.jsx'
import {
  ClipboardList, RefreshCw, Play, CheckCircle2, XCircle,
  Activity, ArrowRight, Filter, Terminal, TrendingUp,
} from '../components/icons.jsx'

// recharts lives in JobResults — the drawer is the only place it renders.
const JobResults = lazy(() => import('./JobResults.jsx'))

const ACTIVE = ['running', 'training', 'pending', 'queued', 'in_progress', 'starting']

export default function Jobs() {
  const navigate = useNavigate()

  const [query, setQuery] = useState('')
  const [status, setStatus] = useState('all')
  const [selected, setSelected] = useState(null)
  const [live, setLive] = useState(true)
  const debounced = useDebounced(query, 200)

  // Poll the list while any job is active so progress bars move on their own.
  const jobs = useFetch(() => mlOpsApi.listJobs(100), [])
  const list = jobs.data?.jobs || (Array.isArray(jobs.data) ? jobs.data : [])
  const anyActive = list.some((j) => ACTIVE.includes(String(j.status).toLowerCase()))

  usePolling(
    async () => { const d = await mlOpsApi.listJobs(100); jobs.setData(d); return d },
    4000,
    { enabled: live && anyActive },
  )

  const rows = list.filter((j) => {
    const q = debounced.toLowerCase()
    if (status !== 'all' && String(j.status).toLowerCase() !== status) return false
    if (!q) return true
    return [j.job_id, j.model_type, j.model_name, j.dataset_id, j.status]
      .some((v) => String(v || '').toLowerCase().includes(q))
  })

  const counts = {
    total: list.length,
    active: list.filter((j) => ACTIVE.includes(String(j.status).toLowerCase())).length,
    done: list.filter((j) => String(j.status).toLowerCase() === 'completed').length,
    failed: list.filter((j) => String(j.status).toLowerCase() === 'failed').length,
  }

  async function refreshSelected(jobId) {
    try {
      const j = await mlOpsApi.getJob(jobId)
      setSelected(j)
      if (j.status === 'completed' || j.status === 'failed') jobs.refetch()
    } catch { /* job may be gone */ }
  }

  return (
    <div className="page">
      <PageHeader
        icon={ClipboardList}
        title="Training jobs"
        subtitle="Every automated and custom-code run submitted through the pipeline"
        actions={
          <>
            <Button variant="secondary" size="sm" icon={Terminal} onClick={() => setLive((v) => !v)}>
              {live ? 'Live: on' : 'Live: off'}
            </Button>
            <Button variant="secondary" size="sm" icon={RefreshCw} onClick={jobs.refetch} loading={jobs.loading}>
              Refresh
            </Button>
            <Button variant="primary" size="sm" icon={Play} onClick={() => navigate('/pipeline')}>
              New run
            </Button>
          </>
        }
      />

      <div className="stat-grid">
        <StatCard icon={ClipboardList} tone="accent" value={counts.total} label="Total jobs" sub="all time" />
        <StatCard icon={Activity} tone="warning" value={counts.active} label="Active" sub={live ? 'auto-refreshing' : 'live paused'} />
        <StatCard icon={CheckCircle2} tone="success" value={counts.done} label="Completed" sub="successfully finished" />
        <StatCard icon={XCircle} tone="danger" value={counts.failed} label="Failed" sub="needs attention" />
      </div>

      <Panel
        icon={Filter}
        title="Job history"
        subtitle={`${rows.length} of ${list.length} jobs`}
        actions={
          <div className="flex items-center gap-2">
            <SearchInput value={query} onChange={setQuery} placeholder="Search jobs…" />
            <Select
              value={status}
              onChange={(e) => setStatus(e.target.value)}
              aria-label="Filter jobs by status"
              style={{ width: 150 }}
            >
              <option value="all">All statuses</option>
              <option value="completed">Completed</option>
              <option value="running">Running</option>
              <option value="training">Training</option>
              <option value="failed">Failed</option>
              <option value="pending">Pending</option>
            </Select>
          </div>
        }
        bodyClass={rows.length ? 'tight' : ''}
      >
        {jobs.loading && !list.length ? (
          <LoadingState label="Loading jobs…" />
        ) : jobs.error ? (
          <ErrorState error={jobs.error} title="Could not load jobs" onRetry={jobs.refetch} />
        ) : rows.length === 0 ? (
          <EmptyState
            icon={ClipboardList}
            title={list.length ? 'No match' : 'No training jobs yet'}
            desc={list.length ? 'Nothing matches the current filter.' : 'Run the pipeline to create your first training job.'}
            actions={!list.length ? <Button variant="primary" icon={Play} onClick={() => navigate('/pipeline')}>Start pipeline</Button> : null}
          />
        ) : (
          <DataTable
            caption="Training jobs"
            keyOf={(j) => j.job_id}
            rows={rows}
            pageSize={12}
            onRowClick={setSelected}
            columns={[
              {
                key: 'job_id', strong: true, header: 'Job',
                render: (j) => (
                  <span className="flex items-center gap-2">
                    <code className="mono text-xs">{String(j.job_id).slice(0, 8)}</code>
                    <span className="truncate" style={{ maxWidth: 160 }} title={j.model_name || j.model_type}>
                      {j.model_name || j.model_type || '—'}
                    </span>
                  </span>
                ),
              },
              { key: 'dataset_id', header: 'Dataset', render: (j) => <span className="muted text-xs">{j.dataset_id || '—'}</span> },
              { key: 'status', header: 'Status', render: (j) => <StatusBadge status={j.status} sm /> },
              {
                key: 'accuracy', header: 'Accuracy',
                render: (j) => <span className="tnum">{formatAccuracy(j.accuracy)}</span>,
              },
              {
                key: 'progress', width: 170, header: 'Progress',
                render: (j) => (
                  <div style={{ minWidth: 130 }}>
                    <Progress value={j.progress || 0} showValue={false} />
                    <span className="text-xs muted">{j.progress || 0}%</span>
                  </div>
                ),
              },
              {
                key: 'started_at', width: 170, header: 'Started',
                render: (j) => <span className="text-xs muted">{j.started_at ? new Date(j.started_at).toLocaleString() : '—'}</span>,
              },
              {
                key: 'open', actions: true, width: 90, header: <span className="sr-only">Details</span>,
                render: (j) => <Button variant="ghost" size="xs" icon={ArrowRight} onClick={() => setSelected(j)}>Open</Button>,
              },
            ]}
          />
        )}
      </Panel>

      {selected && (
        <Drawer
          open
          wide
          onClose={() => setSelected(null)}
          title={`Job ${String(selected.job_id).slice(0, 12)}`}
          subtitle={selected.model_type || selected.model_name || 'training run'}
          footer={
            <>
              <Button variant="secondary" onClick={() => setSelected(null)}>Close</Button>
              {ACTIVE.includes(String(selected.status).toLowerCase()) && (
                <Button variant="primary" icon={RefreshCw} onClick={() => refreshSelected(selected.job_id)}>Refresh status</Button>
              )}
              {String(selected.status).toLowerCase() === 'completed' && (
                <Button variant="primary" icon={TrendingUp} onClick={() => navigate('/experiments')}>View experiment</Button>
              )}
            </>
          }
        >
          <div className="flex flex-col gap-4">
            <JobSummary job={selected} />

            {selected.error_message && <Alert tone="error">{selected.error_message}</Alert>}

            {ACTIVE.includes(String(selected.status).toLowerCase()) && (
              <Panel icon={Activity} title="Live progress" actions={<Badge tone="warning" dot>polling</Badge>}>
                <Progress value={selected.progress || 0} label="Progress" />
                <div className="mt-3 text-xs muted">
                  Status refreshes every few seconds while the job is active.
                </div>
              </Panel>
            )}

            <TrainingConsole
              logs={selected.logs || []}
              isTraining={ACTIVE.includes(String(selected.status).toLowerCase())}
              empty="Structured logs are not persisted for this job — progress above reflects the backend record."
            />

            <Suspense fallback={<LoadingState label="Loading charts…" />}>
              <JobResults
                results={String(selected.status).toLowerCase() === 'completed' ? selected : null}
                isTraining={ACTIVE.includes(String(selected.status).toLowerCase())}
                progress={selected.progress || 0}
                jobId={selected.job_id}
              />
            </Suspense>
          </div>
        </Drawer>
      )}
    </div>
  )
}
