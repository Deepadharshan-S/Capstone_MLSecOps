import { lazy, Suspense, useState, useEffect, useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import { mlOpsApi } from '../api/endpoints.js'
import { useFetch, useDebounced, usePolling } from '../hooks/useFetch.js'
import { formatAccuracy } from '../lib/models.js'
import { parseJobLogText } from '../lib/joblogs.js'
import {
  PageHeader, Panel, Button, DataTable, Drawer, Badge, StatusBadge,
  EmptyState, LoadingState, ErrorState, SearchInput, Select,
  Progress, StatCard, Alert, useToast,
} from '../components/ui/index.jsx'
import { JobSummary, TrainingConsole } from './JobPanels.jsx'
import {
  ClipboardList, RefreshCw, Play, CheckCircle2, XCircle,
  Activity, ArrowRight, Filter, Terminal, TrendingUp,
} from '../components/icons.jsx'

// recharts lives in JobResults — the drawer is the only place it renders.
const JobResults = lazy(() => import('./JobResults.jsx'))

const ACTIVE = ['running', 'training', 'pending', 'queued', 'in_progress', 'starting']

/** "2m 14s" style elapsed time, used because the backend reports no real progress. */
function fmtElapsed(ms) {
  if (!Number.isFinite(ms) || ms < 0) return null
  const s = Math.floor(ms / 1000)
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m ${s % 60}s`
  return `${Math.floor(m / 60)}h ${m % 60}m`
}

export default function Jobs() {
  const navigate = useNavigate()
  const toast = useToast()

  const [query, setQuery] = useState('')
  const [status, setStatus] = useState('all')
  const [selected, setSelected] = useState(null)
  // List rows are built with include_logs=False / include_artifacts=False
  // (job_service.py), so opening a job always needs a follow-up GET /jobs/{id}
  // — without it the drawer shows headline metrics but no charts and no logs.
  const [detailLoading, setDetailLoading] = useState(false)
  const [detailError, setDetailError] = useState(null)
  // GET /jobs/{id} only embeds the last 200 lines, and only when its own log
  // lookup happens to succeed — which for a finished job whose head pod is
  // gone means the drawer's console stays empty. The drawer reads the
  // dedicated log endpoint instead: it tails the live Ray head pod while the
  // job runs and the MinIO archive once it is done, and reports which via
  // `source`. Kept separate from `selected` so the status poll (which
  // replaces that object wholesale) can't wipe the console mid-stream.
  const [jobLogs, setJobLogs] = useState([])
  const [logSource, setLogSource] = useState(null)
  const [logError, setLogError] = useState(null)
  // Which job the console currently belongs to, for dropping late responses.
  const logsJobRef = useRef(null)
  const [live, setLive] = useState(true)
  const debounced = useDebounced(query, 200)

  // Poll the list while any job is active so the status column and stat cards
  // stay current without a manual refresh.
  const jobs = useFetch(() => mlOpsApi.listJobs(100), [])
  const list = jobs.data?.jobs || (Array.isArray(jobs.data) ? jobs.data : [])
  const anyActive = list.some((j) => ACTIVE.includes(String(j.status).toLowerCase()))

  // The polling result is stored straight into `jobs`, so its own `data` is
  // unused — but its `error` must not be discarded, otherwise a failing
  // refresh silently freezes the table behind a "Live: on" badge.
  const listPoll = usePolling(
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

  function resetLogs() {
    logsJobRef.current = null
    setJobLogs([])
    setLogSource(null)
    setLogError(null)
  }

  function closeDrawer() {
    setSelected(null)
    setDetailError(null)
    setDetailLoading(false)
    resetLogs()
  }

  /**
   * Load a job's logs from GET /models/jobs/{id}/logs.
   *
   * `silent` defaults to true because a lookup that comes back "unavailable"
   * is the normal state of a historical job whose pod has been reclaimed and
   * whose archive was never written — the endpoint still returns the reason
   * as text, so there is nothing to toast about.
   */
  async function loadLogs(jobId, { silent = true } = {}) {
    try {
      const d = await mlOpsApi.jobLogs(jobId)
      // A response landing after the drawer switched jobs or closed must not
      // take over whichever job is open now.
      if (logsJobRef.current !== jobId) return null
      setJobLogs(parseJobLogText(d?.logs))
      setLogSource(d?.source || null)
      setLogError(null)
      return d
    } catch (err) {
      if (logsJobRef.current !== jobId) return null
      setLogError(err)
      if (!silent) toast.error('Could not load logs', err?.message || 'Request failed.')
      return null
    }
  }

  /**
   * Open a job in the drawer. The list row renders instantly so there's no
   * flash, but it carries no logs/artifacts — fetch the full record right
   * after, including for completed/failed jobs (which previously had no way
   * to load results at all, because "Refresh status" only rendered while a
   * job was still active).
   */
  async function openJob(job) {
    if (!job?.job_id) return
    setSelected(job)
    setDetailError(null)
    setDetailLoading(true)
    // Clear first so the previous job's output never sits under this one's
    // title while the new fetch is in flight, then start the log lookup in
    // parallel — a failing log endpoint must not hold up the metrics.
    resetLogs()
    logsJobRef.current = job.job_id
    loadLogs(job.job_id)
    try {
      const full = await mlOpsApi.getJob(job.job_id)
      // Guard against the drawer having been closed or switched meanwhile.
      setSelected((cur) => (cur && cur.job_id === job.job_id ? full : cur))
    } catch (err) {
      // Keep the list-row snapshot: headline metrics still render, and the
      // panels below fall back to their empty states. Surface why here.
      setDetailError(err)
    } finally {
      setDetailLoading(false)
    }
  }

  /**
   * Refresh the open drawer's job. `silent` is for the interval below —
   * toasting every 4s on a flaky backend would bury the UI in errors.
   */
  async function refreshSelected(jobId, { silent = false } = {}) {
    try {
      const j = await mlOpsApi.getJob(jobId)
      // A response landing after the drawer closed must not re-open it, nor
      // overwrite a different job the user has since selected.
      setSelected((cur) => (cur && cur.job_id === jobId ? j : cur))
      if (j.status === 'completed' || j.status === 'failed') jobs.refetch()
      return j
    } catch (err) {
      if (silent) return null
      // Previously swallowed every failure, so "Refresh status" looked broken
      // with no feedback at all.
      toast.error('Could not refresh this job', err?.status === 404
        ? 'This job no longer exists on the server.'
        : (err?.message || 'Request failed.'))
      return null
    }
  }

  const selectedActive = Boolean(selected) && ACTIVE.includes(String(selected.status).toLowerCase())
  const stageLabel = selected ? String(selected.status || '').replace(/_/g, ' ') : ''

  // The list poll updates the table only — `selected` is a separate snapshot,
  // so without this the open drawer froze while its own badge claimed
  // "polling" and the copy promised "refreshes every few seconds".
  usePolling(
    async () => {
      if (!selected) return
      await refreshSelected(selected.job_id, { silent: true })
      // While a job runs its head pod is still emitting, so the tail grows —
      // refetch it on this same tick rather than opening a second interval
      // that competes for the same 60 req/min budget.
      await loadLogs(selected.job_id)
    },
    4000,
    { enabled: live && selectedActive },
  )

  // Backend progress is stage-based (10/50/100), never a real percentage, so
  // the drawer pairs the indeterminate bar with a ticking elapsed time.
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!selectedActive) return undefined
    const t = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(t)
  }, [selectedActive])
  const elapsed = selected?.started_at
    ? fmtElapsed(now - new Date(selected.started_at).getTime())
    : null

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

      {listPoll.error && (
        <Alert
          tone="warning"
          actions={<Button size="xs" variant="secondary" onClick={listPoll.refresh}>Retry</Button>}
        >
          Live refresh failed: {listPoll.error.message} — the table may be stale.
        </Alert>
      )}

      <div className="stat-grid">
        <StatCard
          icon={ClipboardList}
          tone="accent"
          value={jobs.loading && !jobs.data ? '…' : counts.total}
          label="Total jobs"
          sub={jobs.loading && !jobs.data ? 'Loading…' : 'all time'}
        />
        <StatCard
          icon={Activity}
          tone="warning"
          value={jobs.loading && !jobs.data ? '…' : counts.active}
          label="Active"
          sub={jobs.loading && !jobs.data ? 'Loading…' : (live ? 'auto-refreshing' : 'live paused')}
        />
        <StatCard
          icon={CheckCircle2}
          tone="success"
          value={jobs.loading && !jobs.data ? '…' : counts.done}
          label="Completed"
          sub={jobs.loading && !jobs.data ? 'Loading…' : 'successfully finished'}
        />
        <StatCard
          icon={XCircle}
          tone="danger"
          value={jobs.loading && !jobs.data ? '…' : counts.failed}
          label="Failed"
          sub={jobs.loading && !jobs.data ? 'Loading…' : 'needs attention'}
        />
      </div>

      <Panel
        icon={Filter}
        title="Job history"
        subtitle={jobs.loading && !jobs.data ? 'Loading jobs…' : `${rows.length} of ${list.length} jobs`}
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
              <option value="training">Training</option>
              <option value="failed">Failed</option>
              <option value="pending">Pending</option>
              {/* No "Running" option: the backend maps RUNNING → "training"
                  (job_service.py), so a `running` filter never matched a row. */}
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
            onRowClick={openJob}
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
                render: (j) => {
                  // Backend progress is a fixed stage marker (pending=10,
                  // training=50), not a measured percentage — rendering it as
                  // one promised motion that can never happen.
                  const st = String(j.status).toLowerCase()
                  const staged = st === 'pending' || st === 'training'
                  return (
                    <div style={{ minWidth: 130 }}>
                      <Progress value={j.progress || 0} showValue={false} indeterminate={staged} />
                      <span className="text-xs muted">
                        {staged ? (st === 'training' ? 'Training' : 'Pending') : `${j.progress || 0}%`}
                      </span>
                    </div>
                  )
                },
              },
              {
                key: 'started_at', width: 170, header: 'Started',
                render: (j) => <span className="text-xs muted">{j.started_at ? new Date(j.started_at).toLocaleString() : '—'}</span>,
              },
              {
                key: 'open', actions: true, width: 90, header: <span className="sr-only">Details</span>,
                render: (j) => <Button variant="ghost" size="xs" icon={ArrowRight} onClick={() => openJob(j)}>Open</Button>,
              },
            ]}
          />
        )}
      </Panel>

      {selected && (
        <Drawer
          open
          wide
          onClose={closeDrawer}
          title={`Job ${String(selected.job_id).slice(0, 12)}`}
          subtitle={selected.model_type || selected.model_name || 'training run'}
          footer={
            <>
              <Button variant="secondary" onClick={closeDrawer}>Close</Button>
              {detailError && (
                <Button variant="primary" icon={RefreshCw} loading={detailLoading} onClick={() => openJob(selected)}>
                  Retry details
                </Button>
              )}
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

            {detailLoading && (
              <div className="flex items-center gap-2 muted text-xs" role="status">
                <span className="spinner" /> Loading full job results — charts, metrics and logs…
              </div>
            )}

            {detailError && (
              <Alert
                tone="warning"
                actions={<Button size="xs" variant="secondary" onClick={() => openJob(selected)}>Retry</Button>}
              >
                Could not load the full record for this job
                {detailError.status ? ` (HTTP ${detailError.status})` : ''} — charts and logs below may be
                incomplete. Headline metrics come from the job list.
              </Alert>
            )}

            {selected.error_message && <Alert tone="error">{selected.error_message}</Alert>}

            {selectedActive && (
              <Panel icon={Activity} title="Live progress" actions={<Badge tone="warning" dot>polling</Badge>}>
                <Progress
                  value={selected.progress || 0}
                  label="Stage"
                  indeterminate
                  status={stageLabel || 'Working'}
                />
                <div className="mt-3 text-xs muted">
                  {stageLabel || 'Working'}
                  {elapsed ? ` · running for ${elapsed}` : ''}
                  {' · '}status refreshes every few seconds while the job is active.
                  The backend reports a stage, not a measured percentage.
                </div>
              </Panel>
            )}

            <TrainingConsole
              // The dedicated endpoint is preferred; `selected.logs` remains
              // as the fallback for a viewer whose log lookup was denied.
              logs={jobLogs.length ? jobLogs : (selected.logs || [])}
              isTraining={selectedActive}
              source={logSource}
              empty={logError
                ? `Could not load logs — ${logError.message || 'the request failed.'}`
                : 'No logs archived for this job — the backend found nothing to stream from the pod or MinIO.'}
            />

            <Suspense fallback={<LoadingState label="Loading charts…" />}>
              <JobResults
                results={String(selected.status).toLowerCase() === 'completed' ? selected : null}
                isTraining={selectedActive}
                jobId={selected.job_id}
              />
            </Suspense>
          </div>
        </Drawer>
      )}
    </div>
  )
}
