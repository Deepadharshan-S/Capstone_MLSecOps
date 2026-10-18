import { useEffect, useState } from 'react'
import { threatHuntingApi, usersApi } from '../../api/endpoints.js'
import { useAuth } from '../../auth/AuthContext.jsx'
import { useFetch, usePolling, useDebounced } from '../../hooks/useFetch.js'
import {
  Panel, Button, DataTable, Drawer, Badge, StatCard, Modal,
  EmptyState, LoadingState, ErrorState, SearchInput, Select, Field,
  TextArea, KeyValue, Segmented, useToast,
} from '../../components/ui/index.jsx'
import {
  Bell, RefreshCw, Activity, AlertTriangle, Zap, Clock, Eye,
  CheckCircle2, XCircle, Inbox,
} from '../../components/icons.jsx'
import {
  severityTone, alertStatusTone, STATUS_TRANSITIONS,
  ALL_SEVERITIES, fmtTime, relTime, fmtValue,
} from './shared.jsx'

const ALERT_POLL_MS = 20000 // detection worker cycles every ~30s backend-side

const STATUS_TABS = [
  { value: '', label: 'All' },
  { value: 'OPEN', label: 'Open' },
  { value: 'INVESTIGATING', label: 'Investigating' },
  { value: 'RESOLVED', label: 'Resolved' },
  { value: 'FALSE_POSITIVE', label: 'False positive' },
]

const TRANSITION_LABELS = {
  INVESTIGATING: 'Start investigation',
  RESOLVED: 'Resolve…',
  FALSE_POSITIVE: 'False positive…',
}

function SeverityBadge({ severity }) {
  return <Badge tone={severityTone(severity)}>{severity}</Badge>
}

/* ── Resolution modal (RESOLVED / FALSE_POSITIVE require a reason) ───── */
function ResolveModal({ open, onClose, targetStatus, onSubmit, submitting }) {
  const [reason, setReason] = useState('')
  useEffect(() => { if (open) setReason('') }, [open, targetStatus])

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={targetStatus === 'FALSE_POSITIVE' ? 'Mark as false positive' : 'Resolve alert'}
      subtitle="The reason is recorded in the investigation history and audit log"
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={submitting}>Cancel</Button>
          <Button
            variant="primary"
            loading={submitting}
            onClick={() => onSubmit(reason.trim())}
          >
            {targetStatus === 'FALSE_POSITIVE' ? 'Mark false positive' : 'Resolve'}
          </Button>
        </>
      }
    >
      <Field label="Resolution reason" hint="Optional — a default note is recorded if left blank">
        <TextArea
          rows={3}
          maxLength={500}
          value={reason}
          placeholder="e.g. Expected batch job — allowlisted service account"
          onChange={(e) => setReason(e.target.value)}
        />
      </Field>
    </Modal>
  )
}

/* ── Alert detail drawer: evidence, history, events, lifecycle actions ─ */
function AlertDrawer({ alertId, onClose, onChanged }) {
  const { hasPermission } = useAuth()
  const toast = useToast()
  const canManage = hasPermission('alerts:manage')

  const detail = useFetch(
    () => (alertId ? threatHuntingApi.getAlert(alertId) : Promise.resolve(null)),
    [alertId],
    { enabled: !!alertId },
  )
  const events = useFetch(
    () => (alertId ? threatHuntingApi.alertEvents(alertId, 50) : Promise.resolve(null)),
    [alertId],
    { enabled: !!alertId },
  )
  // Assignee picker — only loaded for users who can actually manage alerts.
  const users = useFetch(
    () => (canManage && alertId ? usersApi.list() : Promise.resolve([])),
    [alertId, canManage],
    { enabled: canManage && !!alertId },
  )

  const [saving, setSaving] = useState(false)
  const [resolveTarget, setResolveTarget] = useState(null) // 'RESOLVED' | 'FALSE_POSITIVE'
  const [note, setNote] = useState('')

  const alert = detail.data

  const patch = async (body, successMsg) => {
    setSaving(true)
    try {
      await threatHuntingApi.updateAlert(alertId, body)
      toast.success(successMsg || 'Alert updated')
      await detail.refetch()
      onChanged?.()
    } catch (err) {
      toast.error('Update failed', err?.message)
    } finally {
      setSaving(false)
      setResolveTarget(null)
    }
  }

  const transitions = alert ? STATUS_TRANSITIONS[alert.status] || [] : []
  const evidence = alert?.supporting_evidence || {}
  const history = alert?.history || []
  const eventItems = events.data?.items || []

  return (
    <>
      <Drawer
        open={!!alertId}
        onClose={onClose}
        wide
        title={alert ? alert.title : 'Alert'}
        subtitle={alert ? `Alert ${String(alert.id).slice(0, 8)} · ${alert.detection_source}` : 'Loading…'}
        footer={
          alert && canManage ? (
            <div className="flex items-center gap-2 flex-wrap">
              {transitions.map((s) => (
                <Button
                  key={s}
                  size="sm"
                  variant={s === 'RESOLVED' || s === 'FALSE_POSITIVE' ? 'secondary' : 'primary'}
                  icon={s === 'FALSE_POSITIVE' ? XCircle : s === 'RESOLVED' ? CheckCircle2 : Activity}
                  disabled={saving}
                  onClick={() => {
                    if (s === 'RESOLVED' || s === 'FALSE_POSITIVE') setResolveTarget(s)
                    else patch({ status: s }, 'Alert moved to investigating')
                  }}
                >
                  {TRANSITION_LABELS[s] || s}
                </Button>
              ))}
            </div>
          ) : null
        }
      >
        {detail.loading ? (
          <LoadingState label="Loading alert…" />
        ) : detail.error ? (
          <ErrorState error={detail.error} title="Could not load alert" onRetry={detail.refetch} />
        ) : alert ? (
          <div className="flex flex-col gap-4">
            <div className="flex items-center gap-2 flex-wrap">
              <SeverityBadge severity={alert.severity} />
              <Badge tone={alertStatusTone(alert.status)} dot>{String(alert.status).replace(/_/g, ' ')}</Badge>
              {(alert.mitre_attack_id || alert.mitre_atlas_id) && (
                <>
                  {alert.mitre_attack_id && <Badge tone="purple">MITRE {alert.mitre_attack_id}</Badge>}
                  {alert.mitre_atlas_id && <Badge tone="purple">ATLAS {alert.mitre_atlas_id}</Badge>}
                </>
              )}
            </div>

            <p className="muted" style={{ margin: 0 }}>{alert.description}</p>

            <KeyValue
              items={[
                ['Status', String(alert.status).replace(/_/g, ' ')],
                ['Severity', alert.severity],
                ['Detection source', alert.detection_source],
                ['Rule', alert.rule_id ? `${String(alert.rule_id).slice(0, 8)} (v${alert.rule_version ?? '—'})` : '—'],
                ['Dedup key', alert.dedup_key],
                ['First seen', fmtTime(alert.first_seen)],
                ['Last seen', fmtTime(alert.last_seen)],
                ['Event count', alert.event_count],
                ['Assignee', alert.assignee_id ? String(alert.assignee_id).slice(0, 8) : 'Unassigned'],
                ['Resolved at', fmtTime(alert.resolved_at)],
                ['Resolution reason', alert.resolution_reason],
              ]}
            />

            {/* ── Lifecycle controls (alerts:manage only) ───────────── */}
            {canManage && (
              <Panel title="Investigation controls" icon={Eye} bodyClass="tight">
                <div className="grid-2">
                  <Field label="Severity" hint="Re-scored severity takes effect on the next sweep">
                    <Select
                      value={alert.severity}
                      disabled={saving}
                      onChange={(e) => patch({ severity: e.target.value }, 'Severity updated')}
                    >
                      {ALL_SEVERITIES.map((s) => <option key={s} value={s}>{s}</option>)}
                    </Select>
                  </Field>
                  <Field label="Assignee" hint="Users with accounts on this platform">
                    <Select
                      value={alert.assignee_id || ''}
                      disabled={saving}
                      onChange={(e) => e.target.value && patch({ assignee_id: e.target.value }, 'Alert assigned')}
                    >
                      <option value="">Unassigned</option>
                      {(users.data || []).filter((u) => u.is_active !== false).map((u) => (
                        <option key={u.id} value={u.id}>{u.username}</option>
                      ))}
                    </Select>
                  </Field>
                </div>
                <div className="mt-3">
                  <Field label="Add investigation note" hint="Recorded in the history timeline">
                    <TextArea
                      rows={2}
                      maxLength={1000}
                      value={note}
                      placeholder="e.g. Correlated with incident INC-1042"
                      disabled={saving}
                      onChange={(e) => setNote(e.target.value)}
                    />
                  </Field>
                  <Button
                    size="sm"
                    icon={Bell}
                    disabled={saving || !note.trim()}
                    onClick={async () => {
                      await patch({ notes: note.trim() }, 'Note added')
                      setNote('')
                    }}
                  >
                    Add note
                  </Button>
                </div>
              </Panel>
            )}

            {/* ── Supporting evidence from the detection engine ─────── */}
            <Panel title="Supporting evidence" icon={Zap} bodyClass={Object.keys(evidence).length ? 'tight' : ''}>
              {Object.keys(evidence).length === 0 ? (
                <EmptyState icon={Inbox} title="No evidence recorded" />
              ) : (
                <KeyValue items={Object.entries(evidence).map(([k, v]) => [k, fmtValue(v)])} />
              )}
            </Panel>

            {/* ── Investigation history timeline ────────────────────── */}
            <Panel title="Investigation history" icon={Clock} bodyClass={history.length ? 'tight' : ''}>
              {history.length === 0 ? (
                <EmptyState icon={Inbox} title="No history yet" />
              ) : (
                <ol className="flex flex-col gap-3" style={{ listStyle: 'none', margin: 0, padding: 0 }}>
                  {history.map((h) => (
                    <li key={h.id} style={{ borderLeft: '2px solid var(--border)', paddingLeft: 12 }}>
                      <div className="flex items-center gap-2 flex-wrap">
                        <strong>{String(h.new_status || '').replace(/_/g, ' ')}</strong>
                        {h.previous_status && (
                          <span className="muted">
                            from {String(h.previous_status).replace(/_/g, ' ')}
                          </span>
                        )}
                        <span className="muted" style={{ fontSize: 12 }}>{fmtTime(h.created_at)}</span>
                      </div>
                      <div className="muted" style={{ fontSize: 13 }}>
                        {h.actor_id ? `${h.actor_id}: ` : ''}{h.notes || '—'}
                      </div>
                    </li>
                  ))}
                </ol>
              )}
            </Panel>

            {/* ── Triggering security events ────────────────────────── */}
            <Panel
              title="Triggering events"
              icon={AlertTriangle}
              subtitle={events.data ? `${events.data.items?.length ?? 0} events linked to this alert` : 'Loading…'}
              actions={
                <Button size="xs" variant="ghost" icon={RefreshCw} onClick={events.refetch} loading={events.loading}>
                  Refresh
                </Button>
              }
              bodyClass={eventItems.length ? 'tight' : ''}
            >
              {events.loading ? (
                <LoadingState label="Loading events…" />
              ) : events.error ? (
                <ErrorState error={events.error} title="Could not load events" onRetry={events.refetch} />
              ) : eventItems.length === 0 ? (
                <EmptyState icon={Inbox} title="No linked events" />
              ) : (
                <DataTable
                  caption="Security events that triggered this alert"
                  keyOf={(r) => r.id}
                  pageSize={10}
                  columns={[
                    { key: 'severity', render: (r) => <SeverityBadge severity={r.severity} /> },
                    { key: 'event_type', strong: true },
                    { key: 'source' },
                    { key: 'actor_id', render: (r) => r.actor_id || '—' },
                    { key: 'resource_id', render: (r) => r.resource_id || '—' },
                    { key: 'occurrence_time', render: (r) => <span className="tnum">{fmtTime(r.occurrence_time)}</span> },
                  ]}
                  rows={eventItems}
                />
              )}
            </Panel>
          </div>
        ) : null}
      </Drawer>

      <ResolveModal
        open={!!resolveTarget}
        targetStatus={resolveTarget}
        onClose={() => setResolveTarget(null)}
        submitting={saving}
        onSubmit={(reason) => patch(
          { status: resolveTarget, resolution_reason: reason || undefined },
          resolveTarget === 'FALSE_POSITIVE' ? 'Marked as false positive' : 'Alert resolved',
        )}
      />
    </>
  )
}

/* ── Main alerts tab ─────────────────────────────────────────────────── */
export default function AlertsTab() {
  const [statusFilter, setStatusFilter] = useState('')
  const [severityFilter, setSeverityFilter] = useState('')
  const [query, setQuery] = useState('')
  const [selectedId, setSelectedId] = useState(null)
  const debounced = useDebounced(query, 200)

  // Live feed: newest 200 alerts, refreshed on the poll interval so alerts
  // surface in the UI as the backend detection worker generates them.
  // `deps` makes the feed refetch the moment a filter changes — otherwise the
  // new closure is only picked up on the next 20s tick, and switching
  // severity/status looked like it needed a manual Refresh.
  const alerts = usePolling(
    () => threatHuntingApi.listAlerts({
      status: statusFilter || undefined,
      severity: severityFilter || undefined,
      limit: 200,
    }),
    ALERT_POLL_MS,
    { immediate: true, deps: [statusFilter, severityFilter] },
  )

  // Exact counts from the server (limit=1 requests that only read `total`).
  const stats = usePolling(async () => {
    const [total, open, critOpen, highOpen] = await Promise.all([
      threatHuntingApi.listAlerts({ limit: 1 }),
      threatHuntingApi.listAlerts({ status: 'OPEN', limit: 1 }),
      threatHuntingApi.listAlerts({ status: 'OPEN', severity: 'CRITICAL', limit: 1 }),
      threatHuntingApi.listAlerts({ status: 'OPEN', severity: 'HIGH', limit: 1 }),
    ])
    return { total: total.total, open: open.total, critOpen: critOpen.total, highOpen: highOpen.total }
  }, ALERT_POLL_MS, { immediate: true })

  const items = alerts.data?.items || []
  const filtered = debounced
    ? items.filter((a) =>
      [a.title, a.description, a.dedup_key, a.detection_source]
        .some((f) => String(f || '').toLowerCase().includes(debounced.toLowerCase())))
    : items

  const columns = [
    {
      key: 'severity',
      width: '100px',
      render: (r) => <SeverityBadge severity={r.severity} />,
    },
    {
      key: 'title',
      strong: true,
      render: (r) => (
        <div style={{ minWidth: 0 }}>
          <div style={{ fontWeight: 600 }}>{r.title}</div>
          <div className="muted" style={{ fontSize: 12 }}>
            {r.mitre_attack_id || r.mitre_atlas_id
              ? `MITRE ${r.mitre_attack_id || r.mitre_atlas_id} · `
              : ''}{r.detection_source}
          </div>
        </div>
      ),
    },
    {
      key: 'status',
      width: '140px',
      render: (r) => (
        <Badge tone={alertStatusTone(r.status)} dot sm>
          {String(r.status).replace(/_/g, ' ')}
        </Badge>
      ),
    },
    { key: 'event_count', width: '80px', render: (r) => <span className="tnum">{r.event_count}</span> },
    { key: 'last_seen', width: '120px', render: (r) => <span title={fmtTime(r.last_seen)}>{relTime(r.last_seen)}</span> },
  ]

  return (
    <div className="flex flex-col gap-4">
      <div className="stat-grid">
        <StatCard
          icon={Bell} tone="danger"
          value={stats.data ? stats.data.total : '—'}
          label="Total alerts"
          sub="all time"
        />
        <StatCard
          icon={AlertTriangle} tone="warning"
          value={stats.data ? stats.data.open : '—'}
          label="Open"
          sub="awaiting triage"
        />
        <StatCard
          icon={Zap} tone="purple"
          value={stats.data ? stats.data.critOpen + stats.data.highOpen : '—'}
          label="Open critical / high"
          sub="priority queue"
        />
        <StatCard
          icon={Activity} tone="info"
          value={stats.data ? Math.max(0, stats.data.total - stats.data.open) : '—'}
          label="Closed out"
          sub="resolved or false positive"
        />
      </div>

      <div className="flex items-center justify-between gap-3 flex-wrap">
        <Segmented
          options={STATUS_TABS}
          value={statusFilter}
          onChange={(v) => setStatusFilter(v)}
        />
        <div className="flex items-center gap-2 flex-wrap">
          <Select
            value={severityFilter}
            aria-label="Filter by severity"
            onChange={(e) => setSeverityFilter(e.target.value)}
          >
            <option value="">All severities</option>
            {ALL_SEVERITIES.map((s) => <option key={s} value={s}>{s}</option>)}
          </Select>
          <SearchInput value={query} onChange={setQuery} placeholder="Search alerts…" />
          <Button
            variant="secondary" size="sm" icon={RefreshCw}
            onClick={() => { alerts.refresh(); stats.refresh() }}
            loading={alerts.loading}
          >
            Refresh
          </Button>
        </div>
      </div>

      <Panel
        icon={Bell}
        title="Security alerts"
        subtitle={
          alerts.data
            ? `${filtered.length} of ${items.length} loaded · ${alerts.data.total} total${alerts.lastUpdated ? ` · updated ${relTime(alerts.lastUpdated.toISOString())}` : ''}`
            : 'Loading…'
        }
        bodyClass={filtered.length ? 'tight' : ''}
      >
        {alerts.loading && !alerts.data ? (
          <LoadingState label="Loading alerts…" />
        ) : alerts.error && !alerts.data ? (
          <ErrorState error={alerts.error} title="Could not load alerts" onRetry={alerts.refetch} />
        ) : (
          <DataTable
            caption="Security alerts, newest first"
            keyOf={(r) => r.id}
            rows={filtered}
            columns={columns}
            pageSize={25}
            onRowClick={(r) => setSelectedId(r.id)}
            empty={
              <EmptyState
                icon={Inbox}
                title="No alerts match this view"
                desc="The detection engine creates alerts from security events — try widening the filters, or run a detection-rule test."
              />
            }
          />
        )}
      </Panel>

      <AlertDrawer
        alertId={selectedId}
        onClose={() => setSelectedId(null)}
        onChanged={() => { alerts.refresh(); stats.refresh() }}
      />
    </div>
  )
}
