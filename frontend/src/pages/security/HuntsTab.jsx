import { useEffect, useState } from 'react'
import { threatHuntingApi } from '../../api/endpoints.js'
import { useAuth } from '../../auth/AuthContext.jsx'
import { usePolling } from '../../hooks/useFetch.js'
import {
  Panel, Button, DataTable, Drawer, Badge, StatusBadge, EmptyState,
  LoadingState, ErrorState, Field, TextInput, KeyValue, useToast,
} from '../../components/ui/index.jsx'
import {
  Target, RefreshCw, Inbox, Play, Eye,
} from '../../components/icons.jsx'
import {
  severityTone, ALL_SEVERITIES, fmtTime, relTime, localInputToIso, defaultHuntWindow,
} from './shared.jsx'

const HUNT_POLL_MS = 30000

const csv = (s) => s.split(',').map((x) => x.trim()).filter(Boolean)

function HuntForm({ onCreated }) {
  const { hasPermission } = useAuth()
  const toast = useToast()
  const canHunt = hasPermission('hunts:execute')
  const win = defaultHuntWindow()

  const [name, setName] = useState('')
  const [eventTypes, setEventTypes] = useState('')
  const [sources, setSources] = useState('')
  const [actorId, setActorId] = useState('')
  const [resourceId, setResourceId] = useState('')
  const [severities, setSeverities] = useState('')
  const [start, setStart] = useState(win.start)
  const [end, setEnd] = useState(win.end)
  const [limit, setLimit] = useState('100')
  const [err, setErr] = useState('')
  const [submitting, setSubmitting] = useState(false)

  const submit = async (e) => {
    e.preventDefault()
    setErr('')
    if (name.trim().length < 3) return setErr('Name must be at least 3 characters')
    const startIso = localInputToIso(start)
    const endIso = localInputToIso(end)
    if (!startIso || !endIso) return setErr('Start and end time are required')
    if (new Date(startIso) >= new Date(endIso)) return setErr('Start time must be before end time')
    const lim = Number(limit)
    if (!Number.isInteger(lim) || lim < 1 || lim > 1000) return setErr('Limit must be 1–1000')

    const sevs = csv(severities).map((s) => s.toUpperCase())
    const badSev = sevs.find((s) => !ALL_SEVERITIES.includes(s))
    if (badSev) return setErr(`Unknown severity: ${badSev}`)

    setSubmitting(true)
    try {
      const job = await threatHuntingApi.createHunt({
        name: name.trim(),
        hunt_definition: {
          event_types: csv(eventTypes).length ? csv(eventTypes) : undefined,
          sources: csv(sources).length ? csv(sources) : undefined,
          actor_id: actorId.trim() || undefined,
          resource_id: resourceId.trim() || undefined,
          severities: sevs.length ? sevs : undefined,
          start_time: startIso,
          end_time: endIso,
          limit: lim,
        },
      })
      toast.success('Hunt completed', `${job.result_count} matching events`)
      setName('')
      onCreated()
    } catch (err2) {
      toast.error('Hunt failed', err2?.message)
      setErr(err2?.message || 'Hunt failed')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Panel icon={Target} title="Run a threat hunt" subtitle="Queries historical security events — never creates alerts">
      {!canHunt && (
        <p className="muted" style={{ marginTop: 0 }}>
          Read-only — the <strong>hunts:execute</strong> scope is required to run hunts.
        </p>
      )}
      {canHunt && (
        <form onSubmit={submit} className="flex flex-col gap-3">
          <div className="grid-2">
            <Field label="Hunt name" required>
              <TextInput
                value={name}
                placeholder="Failed logins last night"
                onChange={(e) => setName(e.target.value)}
              />
            </Field>
            <Field label="Max results" hint="1–1000">
              <TextInput type="number" min={1} max={1000} value={limit} onChange={(e) => setLimit(e.target.value)} />
            </Field>
          </div>
          <div className="grid-2">
            <Field label="Event types" hint="Comma-separated, e.g. auth.login_failed">
              <TextInput value={eventTypes} onChange={(e) => setEventTypes(e.target.value)} />
            </Field>
            <Field label="Sources" hint="Comma-separated, e.g. auth_service">
              <TextInput value={sources} onChange={(e) => setSources(e.target.value)} />
            </Field>
          </div>
          <div className="grid-2">
            <Field label="Actor ID" hint="Optional exact match">
              <TextInput value={actorId} onChange={(e) => setActorId(e.target.value)} />
            </Field>
            <Field label="Resource ID" hint="Optional exact match">
              <TextInput value={resourceId} onChange={(e) => setResourceId(e.target.value)} />
            </Field>
          </div>
          <div className="grid-2">
            <Field label="Start" required>
              <TextInput type="datetime-local" value={start} onChange={(e) => setStart(e.target.value)} />
            </Field>
            <Field label="End" required>
              <TextInput type="datetime-local" value={end} onChange={(e) => setEnd(e.target.value)} />
            </Field>
          </div>
          <Field label="Severities" hint="Comma-separated: INFO, LOW, MEDIUM, HIGH, CRITICAL">
            <TextInput value={severities} onChange={(e) => setSeverities(e.target.value)} />
          </Field>
          {err && <span className="input-error">{err}</span>}
          <div>
            <Button type="submit" variant="primary" icon={Play} loading={submitting}>
              Run hunt
            </Button>
          </div>
        </form>
      )}
    </Panel>
  )
}

function HuntResultsDrawer({ hunt, onClose }) {
  const [results, setResults] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(false)

  // Load results whenever a different hunt is selected.
  useEffect(() => {
    if (!hunt) return undefined
    let cancelled = false
    setResults(null)
    setError(null)
    setLoading(true)
    threatHuntingApi.getHuntResults(hunt.id)
      .then((r) => { if (!cancelled) setResults(r) })
      .catch((e) => { if (!cancelled) setError(e) })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [hunt?.id]) // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <Drawer
      open={!!hunt}
      onClose={onClose}
      wide
      title={hunt?.name || 'Hunt results'}
      subtitle={hunt ? `Hunt ${String(hunt.id).slice(0, 8)} · ${hunt.status}` : ''}
    >
      {hunt && (
        <div className="flex flex-col gap-4">
          <KeyValue
            items={[
              ['Status', hunt.status],
              ['Result count', hunt.result_count],
              ['Started', fmtTime(hunt.started_at)],
              ['Completed', fmtTime(hunt.completed_at)],
              ['Error', hunt.error_message],
            ]}
          />
          {hunt.results_summary && Object.keys(hunt.results_summary).length > 0 && (
            <Panel title="Summary" bodyClass="tight">
              <KeyValue
                items={Object.entries(hunt.results_summary).map(([k, v]) => [k, typeof v === 'object' ? JSON.stringify(v) : String(v)])}
              />
            </Panel>
          )}
          <Panel title="Matched events" bodyClass={results?.events?.length ? 'tight' : ''}>
            {loading ? (
              <LoadingState label="Loading results…" />
            ) : error ? (
              <ErrorState error={error} title="Could not load results" />
            ) : results?.events?.length ? (
              <DataTable
                caption="Events matched by the hunt"
                keyOf={(r) => r.id}
                rows={results.events}
                pageSize={10}
                columns={[
                  { key: 'severity', width: '100px', render: (r) => <Badge tone={severityTone(r.severity)}>{r.severity}</Badge> },
                  { key: 'event_type', strong: true },
                  { key: 'source' },
                  { key: 'actor_id', render: (r) => r.actor_id || '—' },
                  { key: 'resource_id', render: (r) => r.resource_id || '—' },
                  { key: 'occurrence_time', render: (r) => fmtTime(r.occurrence_time) },
                ]}
              />
            ) : (
              <EmptyState icon={Inbox} title="No matching events" desc="The hunt ran cleanly but found nothing in its window." />
            )}
          </Panel>
        </div>
      )}
    </Drawer>
  )
}

export default function HuntsTab({ refreshToken = 0 }) {
  const [selected, setSelected] = useState(null)

  const hunts = usePolling(
    () => threatHuntingApi.listHunts({ limit: 100 }),
    HUNT_POLL_MS,
    { immediate: true, deps: [refreshToken] },
  )

  const items = hunts.data?.items || []

  return (
    <div className="flex flex-col gap-4">
      <HuntForm onCreated={hunts.refetch} />

      <div className="flex items-center justify-between gap-3 flex-wrap">
        <span className="muted">
          {items.length} hunt{items.length === 1 ? '' : 's'} · newest first
        </span>
        <Button variant="secondary" size="sm" icon={RefreshCw} onClick={hunts.refetch} loading={hunts.loading}>
          Refresh
        </Button>
      </div>

      <Panel
        icon={Target}
        title="Threat hunt history"
        subtitle="Historical searches over normalized security events"
        bodyClass={items.length ? 'tight' : ''}
      >
        {hunts.loading && !hunts.data ? (
          <LoadingState label="Loading hunts…" />
        ) : hunts.error && !hunts.data ? (
          <ErrorState error={hunts.error} title="Could not load hunts" onRetry={hunts.refetch} />
        ) : (
          <DataTable
            caption="Threat hunt jobs"
            keyOf={(r) => r.id}
            rows={items}
            pageSize={25}
            onRowClick={(r) => setSelected(r)}
            columns={[
              { key: 'name', strong: true },
              { key: 'status', width: '130px', render: (r) => <StatusBadge status={r.status} sm /> },
              { key: 'result_count', width: '100px', render: (r) => <span className="tnum">{r.result_count}</span> },
              {
                key: 'window',
                render: (r) => {
                  const d = r.hunt_definition || {}
                  return (
                    <span className="muted" style={{ fontSize: 12 }}>
                      {d.start_time ? fmtTime(d.start_time) : '—'} → {d.end_time ? fmtTime(d.end_time) : '—'}
                    </span>
                  )
                },
              },
              { key: 'created_at', width: '120px', render: (r) => <span title={fmtTime(r.created_at)}>{relTime(r.created_at)}</span> },
              {
                key: 'actions',
                actions: true,
                width: '90px',
                render: () => <Badge tone="outline" sm><Eye size={11} /> View</Badge>,
              },
            ]}
            empty={
              <EmptyState
                icon={Inbox}
                title="No hunts yet"
                desc="Run a hunt above to search historical security events."
              />
            }
          />
        )}
      </Panel>

      <HuntResultsDrawer hunt={selected} onClose={() => setSelected(null)} />
    </div>
  )
}
