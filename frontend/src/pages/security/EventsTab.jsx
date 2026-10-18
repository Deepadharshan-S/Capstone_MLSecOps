import { useState } from 'react'
import { threatHuntingApi } from '../../api/endpoints.js'
import { usePolling, useDebounced } from '../../hooks/useFetch.js'
import {
  Panel, Button, DataTable, Drawer, Badge, EmptyState, LoadingState,
  ErrorState, SearchInput, Select, KeyValue, TextInput,
} from '../../components/ui/index.jsx'
import {
  Database, RefreshCw, Inbox,
} from '../../components/icons.jsx'
import {
  severityTone, ALL_SEVERITIES, fmtTime, relTime, fmtValue,
} from './shared.jsx'

const EVENT_POLL_MS = 30000
const LOAD_WINDOW = 200 // newest N events; backend max is 500

export default function EventsTab() {
  const [sourceFilter, setSourceFilter] = useState('')
  const [severityFilter, setSeverityFilter] = useState('')
  const [query, setQuery] = useState('')
  const [selected, setSelected] = useState(null)
  const debounced = useDebounced(query, 200)

  // `deps` refetches the moment a filter changes rather than leaving the
  // previous result on screen until the next 30s tick.
  const events = usePolling(
    () => threatHuntingApi.listSecurityEvents({
      source: sourceFilter || undefined,
      severity: severityFilter || undefined,
      limit: LOAD_WINDOW,
    }),
    EVENT_POLL_MS,
    { immediate: true, deps: [sourceFilter, severityFilter] },
  )

  const items = events.data?.items || []
  const filtered = debounced
    ? items.filter((e) =>
      [e.event_type, e.source, e.actor_id, e.resource_id, e.correlation_id]
        .some((f) => String(f || '').toLowerCase().includes(debounced.toLowerCase())))
    : items

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div className="flex items-center gap-2 flex-wrap">
          <Select
            value={severityFilter}
            aria-label="Filter by severity"
            onChange={(e) => setSeverityFilter(e.target.value)}
          >
            <option value="">All severities</option>
            {ALL_SEVERITIES.map((s) => <option key={s} value={s}>{s}</option>)}
          </Select>
          <TextInput
            style={{ width: 180 }}
            value={sourceFilter}
            placeholder="Source (e.g. auth_service)"
            aria-label="Filter by source"
            onChange={(e) => setSourceFilter(e.target.value)}
          />
        </div>
        <div className="flex items-center gap-2 flex-wrap">
          <SearchInput value={query} onChange={setQuery} placeholder="Search events…" />
          <Button variant="secondary" size="sm" icon={RefreshCw} onClick={events.refetch} loading={events.loading}>
            Refresh
          </Button>
        </div>
      </div>

      <Panel
        icon={Database}
        title="Normalized security events"
        subtitle={
          events.data
            ? `${filtered.length} of ${items.length} loaded · ${events.data.total} total in store${events.lastUpdated ? ` · updated ${relTime(events.lastUpdated.toISOString())}` : ''}`
            : 'Loading…'
        }
        bodyClass={filtered.length ? 'tight' : ''}
      >
        {events.loading && !events.data ? (
          <LoadingState label="Loading events…" />
        ) : events.error && !events.data ? (
          <ErrorState error={events.error} title="Could not load events" onRetry={events.refetch} />
        ) : (
          <DataTable
            caption="Normalized security events, newest first"
            keyOf={(r) => r.id}
            rows={filtered}
            pageSize={25}
            onRowClick={(r) => setSelected(r)}
            columns={[
              { key: 'severity', width: '100px', render: (r) => <Badge tone={severityTone(r.severity)}>{r.severity}</Badge> },
              { key: 'event_type', strong: true },
              { key: 'source' },
              { key: 'actor_id', render: (r) => r.actor_id || '—' },
              { key: 'resource_id', render: (r) => r.resource_id || '—' },
              { key: 'occurrence_time', width: '120px', render: (r) => <span title={fmtTime(r.occurrence_time)}>{relTime(r.occurrence_time)}</span> },
            ]}
            empty={
              <EmptyState
                icon={Inbox}
                title="No security events match this view"
                desc="Events are ingested from auth activity, ML operations, and the lakeFS poisoning gate."
              />
            }
          />
        )}
      </Panel>

      <Drawer
        open={!!selected}
        onClose={() => setSelected(null)}
        title={selected?.event_type || 'Event'}
        subtitle={selected ? `Event ${String(selected.id).slice(0, 8)} · ${selected.source}` : ''}
      >
        {selected && (
          <div className="flex flex-col gap-4">
            <div className="flex items-center gap-2 flex-wrap">
              <Badge tone={severityTone(selected.severity)}>{selected.severity}</Badge>
              {selected.correlation_id && <Badge tone="outline">correlation {selected.correlation_id}</Badge>}
            </div>
            <KeyValue
              items={[
                ['Event type', selected.event_type],
                ['Source', selected.source],
                ['Actor', selected.actor_id],
                ['Resource', selected.resource_id ? `${selected.resource_type || 'resource'}: ${selected.resource_id}` : null],
                ['Severity', selected.severity],
                ['Occurrence time', fmtTime(selected.occurrence_time)],
                ['Ingestion time', fmtTime(selected.ingestion_time)],
                ['Dedup hash', selected.dedup_hash],
                ['Correlation ID', selected.correlation_id],
              ]}
            />
            <Panel title="Attributes" bodyClass="tight">
              <KeyValue
                items={Object.entries(selected.attributes || {}).map(([k, v]) => [k, fmtValue(v)])}
              />
            </Panel>
            <p className="muted" style={{ margin: 0, fontSize: 12 }}>
              Sensitive values are redacted at ingestion server-side; raw payloads are never stored.
            </p>
          </div>
        )}
      </Drawer>
    </div>
  )
}
