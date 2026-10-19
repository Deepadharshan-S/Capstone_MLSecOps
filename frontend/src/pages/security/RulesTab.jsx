import { useEffect, useState } from 'react'
import { threatHuntingApi } from '../../api/endpoints.js'
import { useAuth } from '../../auth/AuthContext.jsx'
import { useFetch } from '../../hooks/useFetch.js'
import {
  Panel, Button, DataTable, Badge, Modal, EmptyState, LoadingState,
  ErrorState, Select, Field, TextInput, TextArea, useToast,
} from '../../components/ui/index.jsx'
import {
  SlidersHorizontal, RefreshCw, Plus, Pencil, Play, Inbox, Trash2, X,
} from '../../components/icons.jsx'
import { severityTone, ALL_SEVERITIES, fmtTime } from './shared.jsx'

const GROUP_BY_OPTIONS = ['actor_id', 'source', 'resource_id', 'correlation_id', 'none']

const EMPTY_FORM = {
  name: '',
  description: '',
  rule_type: 'threshold',
  is_enabled: true,
  severity: 'MEDIUM',
  mitre_attack_id: '',
  mitre_atlas_id: '',
  event_type: '',
  threshold: '5',
  window_seconds: '300',
  group_by: 'actor_id',
  match_attrs: [], // [{ key, value }]
}

/** Validate the form against the same bounds as the Pydantic schemas. */
function validate(form) {
  const errs = {}
  if (form.name.trim().length < 3) errs.name = 'Name must be at least 3 characters'
  if (form.description.trim().length < 5) errs.description = 'Description must be at least 5 characters'
  if (form.event_type.trim().length < 2) errs.event_type = 'Event type is required'
  const th = Number(form.threshold)
  if (!Number.isInteger(th) || th < 1 || th > 10000) errs.threshold = 'Threshold must be 1–10000'
  const win = Number(form.window_seconds)
  if (!Number.isInteger(win) || win < 10 || win > 86400) errs.window_seconds = 'Window must be 10–86400 seconds'
  if (form.mitre_attack_id.length > 50) errs.mitre_attack_id = 'Max 50 characters'
  if (form.mitre_atlas_id.length > 50) errs.mitre_atlas_id = 'Max 50 characters'
  return errs
}

function formToPayload(form) {
  const match_attributes = {}
  for (const { key, value } of form.match_attrs) {
    if (key.trim()) match_attributes[key.trim()] = value
  }
  return {
    name: form.name.trim(),
    description: form.description.trim(),
    rule_type: form.rule_type,
    is_enabled: form.is_enabled,
    severity: form.severity,
    mitre_attack_id: form.mitre_attack_id.trim() || null,
    mitre_atlas_id: form.mitre_atlas_id.trim() || null,
    config: {
      event_type: form.event_type.trim(),
      threshold: Number(form.threshold),
      window_seconds: Number(form.window_seconds),
      group_by: form.group_by,
      match_attributes,
    },
  }
}

function ruleToForm(rule) {
  const cfg = rule.config || {}
  return {
    name: rule.name || '',
    description: rule.description || '',
    rule_type: rule.rule_type || 'threshold',
    is_enabled: rule.is_enabled !== false,
    severity: rule.severity || 'MEDIUM',
    mitre_attack_id: rule.mitre_attack_id || '',
    mitre_atlas_id: rule.mitre_atlas_id || '',
    event_type: cfg.event_type || '',
    threshold: String(cfg.threshold ?? 5),
    window_seconds: String(cfg.window_seconds ?? 300),
    group_by: cfg.group_by || 'actor_id',
    match_attrs: Object.entries(cfg.match_attributes || {}).map(([key, value]) => ({
      key,
      value: typeof value === 'object' ? JSON.stringify(value) : String(value),
    })),
  }
}

/* ── Create / edit rule modal ────────────────────────────────────────── */
function RuleModal({ open, rule, onClose, onSaved }) {
  const toast = useToast()
  const [form, setForm] = useState(EMPTY_FORM)
  const [errs, setErrs] = useState({})
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    if (open) {
      setForm(rule ? ruleToForm(rule) : EMPTY_FORM)
      setErrs({})
    }
  }, [open, rule])

  const set = (patch) => setForm((f) => ({ ...f, ...patch }))

  const submit = async () => {
    const e = validate(form)
    setErrs(e)
    if (Object.keys(e).length) return
    setSaving(true)
    try {
      const payload = formToPayload(form)
      if (rule) {
        await threatHuntingApi.updateRule(rule.id, payload)
        toast.success('Rule updated', `“${payload.name}” saved`)
      } else {
        await threatHuntingApi.createRule(payload)
        toast.success('Rule created', `“${payload.name}” is now active`)
      }
      onSaved()
      onClose()
    } catch (err) {
      toast.error(rule ? 'Update failed' : 'Creation failed', err?.message)
    } finally {
      setSaving(false)
    }
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      size="wide"
      title={rule ? `Edit rule — ${rule.name}` : 'New detection rule'}
      subtitle="Threshold rules fire when N matching events occur inside a sliding window"
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={saving}>Cancel</Button>
          <Button variant="primary" loading={saving} onClick={submit}>
            {rule ? 'Save changes' : 'Create rule'}
          </Button>
        </>
      }
    >
      <form
        className="flex flex-col gap-4"
        onSubmit={(e) => { e.preventDefault(); submit() }}
      >
        <div className="grid-2">
          <Field label="Name" required error={errs.name}>
            <TextInput
              value={form.name}
              invalid={!!errs.name}
              placeholder="Repeated login failures"
              onChange={(e) => set({ name: e.target.value })}
            />
          </Field>
          <Field label="Severity" required>
            <Select value={form.severity} onChange={(e) => set({ severity: e.target.value })}>
              {ALL_SEVERITIES.map((s) => <option key={s} value={s}>{s}</option>)}
            </Select>
          </Field>
        </div>

        <Field label="Description" required error={errs.description}>
          <TextArea
            rows={2}
            value={form.description}
            invalid={!!errs.description}
            placeholder="Detects brute-force style login failure bursts per account"
            onChange={(e) => set({ description: e.target.value })}
          />
        </Field>

        <div className="grid-2">
          <Field label="Event type" required error={errs.event_type} hint="e.g. auth.login_failed">
            <TextInput
              value={form.event_type}
              invalid={!!errs.event_type}
              placeholder="auth.login_failed"
              onChange={(e) => set({ event_type: e.target.value })}
            />
          </Field>
          <Field label="Group by" hint="Entity the threshold counts per">
            <Select value={form.group_by} onChange={(e) => set({ group_by: e.target.value })}>
              {GROUP_BY_OPTIONS.map((g) => <option key={g} value={g}>{g}</option>)}
            </Select>
          </Field>
        </div>

        <div className="grid-2">
          <Field label="Threshold" required error={errs.threshold} hint="1–10000 matching events">
            <TextInput
              type="number"
              min={1}
              max={10000}
              value={form.threshold}
              invalid={!!errs.threshold}
              onChange={(e) => set({ threshold: e.target.value })}
            />
          </Field>
          <Field label="Window (seconds)" required error={errs.window_seconds} hint="10–86400 seconds">
            <TextInput
              type="number"
              min={10}
              max={86400}
              value={form.window_seconds}
              invalid={!!errs.window_seconds}
              onChange={(e) => set({ window_seconds: e.target.value })}
            />
          </Field>
        </div>

        {/* match_attributes — optional exact-match filters on event attributes */}
        <Field label="Attribute filters" hint="Optional — only events whose attributes match every pair are counted">
          <div className="flex flex-col gap-2">
            {form.match_attrs.map((ma, i) => (
              <div key={i} className="flex items-center gap-2">
                <TextInput
                  placeholder="attribute key"
                  value={ma.key}
                  onChange={(e) => set({
                    match_attrs: form.match_attrs.map((x, j) => (i === j ? { ...x, key: e.target.value } : x)),
                  })}
                />
                <TextInput
                  placeholder="value"
                  value={ma.value}
                  onChange={(e) => set({
                    match_attrs: form.match_attrs.map((x, j) => (i === j ? { ...x, value: e.target.value } : x)),
                  })}
                />
                <Button
                  size="sm" variant="ghost" iconOnly icon={X} aria-label="Remove filter"
                  onClick={() => set({ match_attrs: form.match_attrs.filter((_, j) => j !== i) })}
                />
              </div>
            ))}
            <Button
              size="sm" variant="secondary" icon={Plus}
              onClick={() => set({ match_attrs: [...form.match_attrs, { key: '', value: '' }] })}
            >
              Add filter
            </Button>
          </div>
        </Field>

        <div className="grid-2">
          <Field label="MITRE ATT&CK ID" error={errs.mitre_attack_id} hint="Optional, e.g. T1110">
            <TextInput
              value={form.mitre_attack_id}
              invalid={!!errs.mitre_attack_id}
              onChange={(e) => set({ mitre_attack_id: e.target.value })}
            />
          </Field>
          <Field label="MITRE ATLAS ID" error={errs.mitre_atlas_id} hint="Optional, e.g. AML.T0024">
            <TextInput
              value={form.mitre_atlas_id}
              invalid={!!errs.mitre_atlas_id}
              onChange={(e) => set({ mitre_atlas_id: e.target.value })}
            />
          </Field>
        </div>
      </form>
    </Modal>
  )
}

/* ── Dry-run test modal ──────────────────────────────────────────────── */
function TestModal({ rule, onClose }) {
  const toast = useToast()
  const [result, setResult] = useState(null)
  const [running, setRunning] = useState(false)

  useEffect(() => {
    if (!rule) return
    setResult(null)
    setRunning(true)
    threatHuntingApi.testRule(rule.id)
      .then(setResult)
      .catch((err) => toast.error('Test failed', err?.message))
      .finally(() => setRunning(false))
  }, [rule, toast])

  const candidates = result?.candidate_alerts || []

  return (
    <Modal
      open={!!rule}
      onClose={onClose}
      size="wide"
      title={rule ? `Test — ${rule.name}` : 'Test'}
      subtitle="Dry run against recent events — no alerts are created"
      footer={<Button variant="secondary" onClick={onClose}>Close</Button>}
    >
      {running ? (
        <LoadingState label="Evaluating rule against recent events…" />
      ) : result ? (
        <div className="flex flex-col gap-4">
          <div className="stat-grid is-3">
            <div className="stat-card">
              <div className="stat-body">
                <div className="stat-value">{result.matches_found}</div>
                <div className="stat-label">Matches found</div>
              </div>
            </div>
            <div className="stat-card">
              <div className="stat-body">
                <div className="stat-value">{result.evaluation_window_seconds}s</div>
                <div className="stat-label">Evaluation window</div>
              </div>
            </div>
            <div className="stat-card">
              <div className="stat-body">
                <div className="stat-value">{result.duration_ms}</div>
                <div className="stat-label">Duration (ms)</div>
              </div>
            </div>
          </div>

          {candidates.length === 0 ? (
            <EmptyState
              icon={Inbox}
              title="No matches in the current window"
              desc="The rule is valid — it simply found no qualifying events recently. Widen the window or ingest test events to see candidates."
            />
          ) : (
            <Panel title={`Candidate alerts (${candidates.length})`} bodyClass="tight">
              <DataTable
                caption="Candidate alerts the rule would produce"
                keyOf={(c, i) => `${c.title}-${i}`}
                rows={candidates}
                pageSize={10}
                columns={[
                  { key: 'severity', width: '100px', render: (c) => <Badge tone={severityTone(c.severity)}>{c.severity}</Badge> },
                  { key: 'title', strong: true },
                  { key: 'event_count', width: '90px' },
                  { key: 'actor_id', render: (c) => c.actor_id || '—' },
                  { key: 'resource_id', render: (c) => c.resource_id || '—' },
                  { key: 'first_seen', render: (c) => fmtTime(c.first_seen) },
                ]}
              />
            </Panel>
          )}
        </div>
      ) : null}
    </Modal>
  )
}

/* ── Main rules tab ──────────────────────────────────────────────────── */
export default function RulesTab() {
  const { hasPermission } = useAuth()
  const toast = useToast()
  const canManage = hasPermission('rules:manage')

  // Rules change only when someone edits them, so this is a one-shot fetch
  // with a manual Refresh — unlike the other tabs, nothing polls here.
  const rules = useFetch(() => threatHuntingApi.listRules(), [])
  const [modalOpen, setModalOpen] = useState(false)
  const [editing, setEditing] = useState(null) // rule being edited, or null = create
  const [testing, setTesting] = useState(null)

  const items = rules.data || []

  const toggleEnabled = async (rule) => {
    try {
      await threatHuntingApi.updateRule(rule.id, { is_enabled: !rule.is_enabled })
      toast.success(rule.is_enabled ? 'Rule disabled' : 'Rule enabled', rule.name)
      rules.refetch()
    } catch (err) {
      toast.error('Update failed', err?.message)
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <span className="muted">
          {items.length} detection rule{items.length === 1 ? '' : 's'} · the background sweep evaluates enabled rules every ~30s
        </span>
        <div className="flex items-center gap-2">
          {canManage && (
            <Button
              variant="primary" size="sm" icon={Plus}
              onClick={() => { setEditing(null); setModalOpen(true) }}
            >
              New rule
            </Button>
          )}
          <Button variant="secondary" size="sm" icon={RefreshCw} onClick={rules.refetch} loading={rules.loading}>
            Refresh
          </Button>
        </div>
      </div>

      {!canManage && (
        <span className="muted" style={{ fontSize: 13 }}>
          Viewing is read-only — the <strong>rules:manage</strong> scope is required to create, edit, or test rules.
        </span>
      )}

      <Panel
        icon={SlidersHorizontal}
        title="Detection rules"
        subtitle="Deterministic threshold rules — no arbitrary expressions"
        bodyClass={items.length ? 'tight' : ''}
      >
        {/* `&& !rules.data` matches the other tabs: a refresh keeps the rows
            on screen instead of swapping them for a skeleton. */}
        {rules.loading && !rules.data ? (
          <LoadingState label="Loading rules…" />
        ) : rules.error ? (
          <ErrorState error={rules.error} title="Could not load rules" onRetry={rules.refetch} />
        ) : (
          <DataTable
            caption="Detection rules"
            keyOf={(r) => r.id}
            rows={items}
            pageSize={25}
            columns={[
              {
                key: 'name',
                strong: true,
                render: (r) => (
                  <div style={{ minWidth: 0 }}>
                    <div style={{ fontWeight: 600 }}>{r.name}</div>
                    <div className="muted" style={{ fontSize: 12 }}>{r.description}</div>
                  </div>
                ),
              },
              { key: 'severity', width: '100px', render: (r) => <Badge tone={severityTone(r.severity)}>{r.severity}</Badge> },
              {
                key: 'config',
                render: (r) => (
                  <span className="muted" style={{ fontSize: 12 }}>
                    {r.config?.event_type || '—'} · ≥{r.config?.threshold} in {r.config?.window_seconds}s · per {r.config?.group_by}
                  </span>
                ),
              },
              {
                key: 'is_enabled',
                width: '110px',
                render: (r) => (
                  <Badge tone={r.is_enabled ? 'success' : 'outline'} dot sm>
                    {r.is_enabled ? 'Enabled' : 'Disabled'}
                  </Badge>
                ),
              },
              { key: 'version', width: '70px', render: (r) => <span className="tnum">v{r.version}</span> },
              {
                key: 'actions',
                actions: true,
                width: '200px',
                render: (r) => (
                  canManage ? (
                    <div className="flex items-center gap-2">
                      <Button size="xs" variant="ghost" icon={Play} onClick={() => setTesting(r)}>
                        Test
                      </Button>
                      <Button size="xs" variant="ghost" icon={Pencil} onClick={() => { setEditing(r); setModalOpen(true) }}>
                        Edit
                      </Button>
                      <Button
                        size="xs"
                        variant={r.is_enabled ? 'ghost' : 'secondary'}
                        icon={r.is_enabled ? Trash2 : Plus}
                        onClick={() => toggleEnabled(r)}
                      >
                        {r.is_enabled ? 'Disable' : 'Enable'}
                      </Button>
                    </div>
                  ) : null
                ),
              },
            ]}
            empty={
              <EmptyState
                icon={Inbox}
                title="No detection rules yet"
                desc="Create a threshold rule to turn security-event bursts into alerts."
                actions={canManage ? (
                  <Button variant="primary" icon={Plus} onClick={() => { setEditing(null); setModalOpen(true) }}>
                    New rule
                  </Button>
                ) : null}
              />
            }
          />
        )}
      </Panel>

      <RuleModal
        open={modalOpen}
        rule={editing}
        onClose={() => setModalOpen(false)}
        onSaved={rules.refetch}
      />
      <TestModal rule={testing} onClose={() => setTesting(null)} />
    </div>
  )
}
