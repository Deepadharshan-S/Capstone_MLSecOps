import { useState, useEffect } from 'react'
import { mlOpsApi } from '../api/endpoints.js'
import { useFetch, useDebounced } from '../hooks/useFetch.js'
import {
  PageHeader, Panel, Button, DataTable, Drawer, Badge, StatusBadge,
  EmptyState, LoadingState, ErrorState, SearchInput, StatCard,
  Tabs, KeyValue, CopyButton,
} from '../components/ui/index.jsx'
import {
  FlaskConical, RefreshCw, ArrowLeft, Play, Layers,
  Activity,
} from '../components/icons.jsx'

export default function Experiments() {
  const [selected, setSelected] = useState(null)
  const [query, setQuery] = useState('')
  const [tab, setTab] = useState('experiments') // experiments | runs
  const debounced = useDebounced(query, 200)

  const experiments = useFetch(() => mlOpsApi.listExperiments(), [])
  const expList = experiments.data?.experiments || []

  const runs = useFetch(
    () => (selected ? mlOpsApi.experimentRuns(selected.experiment_id, 100) : Promise.resolve(null)),
    [selected?.experiment_id],
    { enabled: !!selected },
  )
  const runList = runs.data?.runs || []

  // "All runs" aggregates GET /experiments/{id}/runs across every experiment.
  const [allRuns, setAllRuns] = useState([])
  const [allRunsLoading, setAllRunsLoading] = useState(false)
  const expKey = expList.map((e) => `${e.experiment_id}::${e.name}`).join('|')
  useEffect(() => {
    if (tab !== 'runs' || !expKey) return undefined
    const pairs = expKey.split('|').map((p) => p.split('::'))
    let cancelled = false
    setAllRunsLoading(true)
    Promise.all(
      pairs.map(([id, name]) => mlOpsApi.experimentRuns(id, 100)
        .then((d) => (d?.runs || []).map((r) => ({ ...r, _experiment: name })))
        .catch(() => [])),
    ).then((groups) => {
      if (cancelled) return
      setAllRuns(groups.flat().sort((a, b) => (b.start_time || 0) - (a.start_time || 0)))
      setAllRunsLoading(false)
    })
    return () => { cancelled = true }
  }, [tab, expKey])

  const filteredExp = expList.filter((e) =>
    !debounced || String(e.name).toLowerCase().includes(debounced.toLowerCase()),
  )

  const runRows = tab === 'runs'
    ? allRuns.filter((r) => !debounced || JSON.stringify(r).toLowerCase().includes(debounced.toLowerCase()))
    : runList

  return (
    <div className="page">
      <PageHeader
        icon={FlaskConical}
        title="Experiments"
        subtitle="MLflow experiments and their runs, tracked automatically by every training job"
        badge={selected ? <Badge tone="accent">{selected.name}</Badge> : null}
        actions={
          selected ? (
            <Button variant="secondary" size="sm" icon={ArrowLeft} onClick={() => { setSelected(null); runs.setData(null) }}>
              All experiments
            </Button>
          ) : (
            <Button variant="secondary" size="sm" icon={RefreshCw} onClick={experiments.refetch} loading={experiments.loading}>
              Refresh
            </Button>
          )
        }
      />

      <div className="stat-grid">
        <StatCard icon={FlaskConical} tone="accent" value={expList.length} label="Experiments" sub="MLflow tracking" />
        <StatCard
          icon={Play} tone="info"
          value={tab === 'runs' ? allRuns.length : '—'}
          label="Runs across experiments"
          sub={tab === 'runs' ? 'aggregated, newest first' : 'open the All runs tab'}
        />
        <StatCard icon={Layers} tone="purple" value={expList.filter((e) => e.lifecycle_stage === 'active').length} label="Active" sub="lifecycle stage" />
        <StatCard icon={Activity} tone="success" value={runRows?.length || 0} label={tab === 'runs' ? 'Visible runs' : 'Runs in view'} sub="current filter" />
      </div>

      <div className="flex items-center justify-between gap-3" style={{ flexWrap: 'wrap' }}>
        <Tabs
          active={tab}
          onChange={setTab}
          tabs={[
            { id: 'experiments', label: 'Experiments', icon: FlaskConical },
            { id: 'runs', label: 'All runs', icon: Play },
          ]}
        />
        <SearchInput value={query} onChange={setQuery} placeholder={tab === 'runs' ? 'Search runs…' : 'Search experiments…'} />
      </div>

      {tab === 'experiments' ? (
        <Panel
          icon={FlaskConical}
          title="Experiments"
          subtitle={`${filteredExp.length} of ${expList.length}`}
          bodyClass={filteredExp.length ? 'tight' : ''}
        >
          {experiments.loading ? (
            <LoadingState label="Loading experiments…" />
          ) : experiments.error ? (
            <ErrorState error={experiments.error} title="Could not load experiments" onRetry={experiments.refetch} />
          ) : filteredExp.length === 0 ? (
            <EmptyState
              icon={FlaskConical}
              title={expList.length ? 'No match' : 'No experiments yet'}
              desc={expList.length ? 'Nothing matches your search.' : 'Run the pipeline — every training job creates or reuses an MLflow experiment.'}
            />
          ) : (
            <DataTable
              caption="MLflow experiments"
              keyOf={(e) => e.experiment_id}
              rows={filteredExp}
              pageSize={10}
              onRowClick={setSelected}
              columns={[
                { key: 'name', strong: true, header: 'Experiment', render: (e) => <span className="flex items-center gap-2"><FlaskConical size={14} /> {e.name}</span> },
                { key: 'experiment_id', header: 'ID', render: (e) => <code className="mono text-xs">{e.experiment_id}</code> },
                {
                  key: 'lifecycle_stage', header: 'Stage',
                  render: (e) => <Badge tone={e.lifecycle_stage === 'active' ? 'success' : 'outline'} sm>{e.lifecycle_stage || 'active'}</Badge>,
                },
                {
                  key: 'runs', width: 130, header: 'Browse',
                  render: (e) => (
                    <Button
                      variant="ghost"
                      size="xs"
                      onClick={() => { setTab('runs'); setQuery(e.name) }}
                      title="Jump to the All runs tab, filtered to this experiment"
                    >
                      All runs
                    </Button>
                  ),
                },
                {
                  key: 'open', actions: true, width: 110, header: <span className="sr-only">Details</span>,
                  render: (e) => <Button variant="ghost" size="xs" onClick={() => setSelected(e)}>Open</Button>,
                },
              ]}
            />
          )}
        </Panel>
      ) : (
        <Panel
          icon={Play}
          title="All runs"
          subtitle={`${runRows?.length || 0} runs`}
          actions={<Button variant="ghost" size="sm" icon={RefreshCw} onClick={() => setTab('runs')}>Refresh</Button>}
          bodyClass={runRows?.length ? 'tight' : ''}
        >
          {allRunsLoading ? (
            <LoadingState label="Loading runs…" />
          ) : !runRows?.length ? (
            <EmptyState icon={Play} title="No runs" desc="Runs appear here after a training job finishes." />
          ) : (
            <RunsTable rows={runRows} />
          )}
        </Panel>
      )}

      <Drawer
        open={!!selected}
        wide
        onClose={() => { setSelected(null); runs.setData(null) }}
        title={selected?.name || 'Experiment'}
        subtitle={`Experiment ${selected?.experiment_id || ''} · ${runList.length} run${runList.length === 1 ? '' : 's'}`}
        footer={<Button variant="secondary" onClick={() => { setSelected(null); runs.setData(null) }}>Close</Button>}
      >
        <div className="flex flex-col gap-4">
          <KeyValue
            items={[
              ['Experiment ID', selected?.experiment_id],
              ['Lifecycle', selected?.lifecycle_stage || 'active'],
              ['Runs', runList.length],
            ]}
          />

          {runs.loading ? (
            <LoadingState label="Loading runs…" />
          ) : runs.error ? (
            <ErrorState error={runs.error} onRetry={runs.refetch} />
          ) : runList.length === 0 ? (
            <EmptyState icon={Play} title="No runs in this experiment" desc="Training a model against this experiment will populate it." />
          ) : (
            <RunsTable rows={runList} />
          )}
        </div>
      </Drawer>
    </div>
  )
}

function RunsTable({ rows }) {
  return (
    <DataTable
      caption="MLflow runs"
      keyOf={(r) => r.run_id}
      rows={rows}
      pageSize={10}
      columns={[
        {
          key: 'run', strong: true, header: 'Run',
          render: (r) => (
            <span className="flex items-center gap-2">
              <span className="truncate" style={{ maxWidth: 150 }}>{r.run_name || String(r.run_id).slice(0, 8)}</span>
              <CopyButton text={r.run_id} />
            </span>
          ),
        },
        { key: 'status', header: 'Status', render: (r) => <StatusBadge status={r.status} sm /> },
        {
          key: 'experiment', header: 'Experiment',
          render: (r) => r._experiment ? <Badge tone="outline" sm>{r._experiment}</Badge> : <span className="muted">—</span>,
        },
        {
          key: 'metrics', header: 'Metrics',
          render: (r) => {
            const entries = Object.entries(r.metrics || {}).slice(0, 4)
            return entries.length
              ? <span className="mono text-xs">{entries.map(([k, v]) => `${k}=${Number(v).toFixed(3)}`).join('  ')}</span>
              : <span className="muted">—</span>
          },
        },
        {
          key: 'params', header: 'Params',
          render: (r) => {
            const entries = Object.entries(r.params || {}).slice(0, 3)
            return entries.length
              ? <span className="muted text-xs">{entries.map(([k, v]) => `${k}=${v}`).join(', ')}</span>
              : <span className="muted">—</span>
          },
        },
        {
          key: 'start_time',
          width: 170,
          header: 'Started',
          render: (r) => <span className="text-xs muted">{r.start_time ? new Date(r.start_time).toLocaleString() : '—'}</span>,
        },
      ]}
    />
  )
}
