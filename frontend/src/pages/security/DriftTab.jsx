import { useState } from 'react'
import { driftApi, datasetsApi, mlOpsApi } from '../../api/endpoints.js'
import { useFetch, usePolling } from '../../hooks/useFetch.js'
import {
  Panel, Button, DataTable, Drawer, Modal, Badge, EmptyState, LoadingState,
  ErrorState, SearchInput, Select, KeyValue, TextInput, Field, Segmented,
  Alert, useToast,
} from '../../components/ui/index.jsx'
import {
  LineChart, RefreshCw, Inbox, FlaskConical, Gauge, Activity, Plus, Play,
} from '../../components/icons.jsx'
import {
  severityTone, ALL_SEVERITIES, fmtTime, relTime, fmtValue,
} from './shared.jsx'

const FINDING_POLL_MS = 60000

// Detection type → label + tone. Kept in one map so the list, the drawer and
// the filter never disagree about the vocabulary (mirrors
// backend/app/services/drift/findings.py).
const DETECTIONS = {
  data_drift: { label: 'Data drift', desc: 'Input feature distributions vs the reference dataset', icon: Activity },
  prediction_drift: { label: 'Prediction drift', desc: 'Predicted class proportions vs the reference', icon: LineChart },
  confidence_drift: { label: 'Confidence drift', desc: 'Model confidence distribution vs the reference', icon: Gauge },
}
const DETECTION_TONE = {
  data_drift: 'accent',
  prediction_drift: 'info',
  confidence_drift: 'warning',
}

const VIEWS = [
  { value: 'findings', label: 'Findings' },
  { value: 'baselines', label: 'Baselines' },
  { value: 'telemetry', label: 'Telemetry' },
]

export default function DriftTab({ refreshToken = 0 }) {
  const [view, setView] = useState('findings')
  const toast = useToast()

  // Detector configuration, fetched once — the UI quotes these numbers when
  // explaining why a check was skipped ("only 12 observations; at least 50").
  const config = useFetch(() => driftApi.config(), [refreshToken])

  const [checkOpen, setCheckOpen] = useState(false)

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <Segmented options={VIEWS} value={view} onChange={setView} className="is-even" />
        <div className="flex items-center gap-2 flex-wrap">
          <ConfigChips config={config.data} />
          <Button size="sm" icon={Play} onClick={() => setCheckOpen(true)}>
            Run drift check
          </Button>
          <Button
            variant="secondary"
            size="sm"
            icon={RefreshCw}
            onClick={config.refetch}
            loading={config.loading}
          >
            Refresh
          </Button>
        </div>
      </div>

      {view === 'findings' && <FindingsView refreshToken={refreshToken} config={config.data} />}
      {view === 'baselines' && <BaselinesView refreshToken={refreshToken} />}
      {view === 'telemetry' && <TelemetryView refreshToken={refreshToken} />}

      <CheckModal
        open={checkOpen}
        onClose={() => setCheckOpen(false)}
        onRan={() => { toast.success('Drift check completed'); setCheckOpen(false) }}
      />
    </div>
  )
}

/** Detector thresholds/window, so a skipped check can be explained on screen. */
function ConfigChips({ config }) {
  if (!config) return null
  const t = config.thresholds || {}
  return (
    <div className="flex items-center gap-1.5 flex-wrap" data-testid="drift-config">
      <Badge tone={config.enabled ? 'success' : 'neutral'} dot>
        {config.enabled ? 'sweeping' : 'paused'}
      </Badge>
      <Badge tone="outline" title="Observation window compared against the baseline">
        window {Math.round((config.observation_window_seconds || 0) / 60)}m
      </Badge>
      <Badge tone="outline" title="Checks stay silent below this many observations">
        min {config.min_observations} obs
      </Badge>
      <Badge tone="outline" title={`PSI ≥ ${t.psi_threshold} · KS D ≥ ${t.ks_d_threshold} · JSD ≥ ${t.jsd_threshold}`}>
        PSI {t.psi_threshold} · KS {t.ks_d_threshold}
      </Badge>
    </div>
  )
}

/* ══════════════════════════════════════════════════════
   Findings
   ══════════════════════════════════════════════════════ */
function FindingsView({ refreshToken }) {
  const [typeFilter, setTypeFilter] = useState('')
  const [severityFilter, setSeverityFilter] = useState('')
  const [modelFilter, setModelFilter] = useState('')
  const [query, setQuery] = useState('')
  const [selected, setSelected] = useState(null)

  const findings = usePolling(
    () => driftApi.listFindings({
      detection_type: typeFilter || undefined,
      severity: severityFilter || undefined,
      model_name: modelFilter || undefined,
      limit: 100,
    }),
    FINDING_POLL_MS,
    { immediate: true, deps: [typeFilter, severityFilter, modelFilter, refreshToken] },
  )

  const items = findings.data?.items || []
  const needle = query.trim().toLowerCase()
  const filtered = needle
    ? items.filter((f) =>
      [f.title, f.summary, f.model_name, f.model_version, ...(f.affected_features || [])]
        .some((v) => String(v || '').toLowerCase().includes(needle)))
    : items

  return (
    <>
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div className="flex items-center gap-2 flex-wrap">
          <Select
            value={typeFilter}
            aria-label="Filter by detection type"
            onChange={(e) => setTypeFilter(e.target.value)}
          >
            <option value="">All detections</option>
            {Object.entries(DETECTIONS).map(([k, v]) => <option key={k} value={k}>{v.label}</option>)}
          </Select>
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
            value={modelFilter}
            placeholder="Model name"
            aria-label="Filter by model"
            onChange={(e) => setModelFilter(e.target.value)}
          />
        </div>
        <SearchInput value={query} onChange={setQuery} placeholder="Search findings…" />
      </div>

      <Panel
        icon={LineChart}
        title="Drift findings"
        subtitle={
          findings.data
            ? `${filtered.length} shown · ${findings.data.total} open/closed${findings.lastUpdated ? ` · updated ${relTime(findings.lastUpdated.toISOString())}` : ''}`
            : 'Loading…'
        }
        bodyClass={filtered.length ? 'tight' : ''}
      >
        {findings.loading && !findings.data ? (
          <LoadingState label="Loading drift findings…" />
        ) : findings.error && !findings.data ? (
          <ErrorState error={findings.error} title="Could not load drift findings" onRetry={findings.refresh} />
        ) : (
          <DataTable
            caption="Drift findings, most recently seen first"
            keyOf={(r) => r.alert_id}
            rows={filtered}
            pageSize={25}
            onRowClick={(r) => setSelected(r)}
            columns={[
              {
                key: 'severity',
                width: '100px',
                render: (r) => <Badge tone={severityTone(r.severity)}>{r.severity}</Badge>,
              },
              {
                key: 'detection_type',
                width: '150px',
                render: (r) => (
                  <Badge tone={DETECTION_TONE[r.detection_type] || 'neutral'}>
                    {DETECTIONS[r.detection_type]?.label || r.detection_type}
                  </Badge>
                ),
              },
              { key: 'model_name', strong: true, render: (r) => r.model_name || '—' },
              {
                key: 'affected_features',
                render: (r) => {
                  const feats = r.affected_features || []
                  if (!feats.length) return '—'
                  const shown = feats.slice(0, 3).join(', ')
                  return <span title={feats.join(', ')}>{shown}{feats.length > 3 ? ` +${feats.length - 3}` : ''}</span>
                },
              },
              { key: 'status', width: '120px', render: (r) => <Badge tone={r.status === 'OPEN' ? 'danger' : 'neutral'}>{r.status}</Badge> },
              {
                key: 'last_seen',
                width: '120px',
                render: (r) => <span title={fmtTime(r.last_seen)}>{relTime(r.last_seen)}</span>,
              },
            ]}
            empty={
              <EmptyState
                icon={Inbox}
                title="No drift findings yet"
                desc="Findings appear once a model has a baseline and enough inference traffic to test against."
              />
            }
          />
        )}
      </Panel>

      <FindingDrawer finding={selected} onClose={() => setSelected(null)} />
    </>
  )
}

/**
 * The finding detail: what was observed, against which reference, with which
 * thresholds — plus an explicit limitations block, because a statistical shift
 * is a signal and not a confirmed incident.
 */
function FindingDrawer({ finding, onClose }) {
  const evidence = finding || {}
  const measurements = evidence.measurements || {}
  const limitations = evidence.limitations || []

  return (
    <Drawer
      open={!!finding}
      onClose={onClose}
      title={finding?.title || 'Drift finding'}
      subtitle={finding ? `${DETECTIONS[finding.detection_type]?.label || finding.detection_type} · ${finding.model_name}${finding.model_version ? ` @ ${finding.model_version}` : ''}` : ''}
      wide
    >
      {finding && (
        <div className="flex flex-col gap-4">
          <div className="flex items-center gap-2 flex-wrap">
            <Badge tone={severityTone(finding.severity)}>{finding.severity}</Badge>
            <Badge tone={DETECTION_TONE[finding.detection_type] || 'neutral'}>
              {DETECTIONS[finding.detection_type]?.label || finding.detection_type}
            </Badge>
            <Badge tone="outline">alert {String(finding.alert_id).slice(0, 8)}</Badge>
          </div>

          <Alert tone="info" icon={Activity}>
            {finding.summary}
          </Alert>

          <KeyValue
            items={[
              ['Model', finding.model_name ? `${finding.model_name}${finding.model_version ? ` @ ${finding.model_version}` : ''}` : null],
              ['Observed window', finding.observation_start ? `${fmtTime(finding.observation_start)} → ${fmtTime(finding.observation_end)}` : null],
              ['Observations', evidence.observation_samples],
              ['Reference samples', evidence.baseline_samples],
              ['Reference dataset', evidence.baseline_dataset_ref],
              ['Severity', finding.severity],
              ['Status', finding.status],
              ['First seen', fmtTime(finding.first_seen)],
              ['Last seen', fmtTime(finding.last_seen)],
            ]}
          />

          {/* Per-feature measurements: baseline vs observed with the tests run. */}
          <Panel title="Measurements" subtitle="Reference compared against the observation window" bodyClass="tight">
            {Object.keys(measurements).length === 0 ? (
              <p className="muted" style={{ margin: 0, fontSize: 12 }}>
                No per-feature detail was recorded with this finding.
              </p>
            ) : (
              <div className="flex flex-col gap-3">
                {Object.entries(measurements).map(([feature, detail]) => (
                  <FeatureBlock key={feature} feature={feature} detail={detail} />
                ))}
              </div>
            )}
          </Panel>

          <Panel title="Thresholds applied" bodyClass="tight">
            <KeyValue items={Object.entries(evidence.thresholds || {}).map(([k, v]) => [k, fmtValue(v)])} />
          </Panel>

          {/* What this finding does NOT establish — never hidden. */}
          <Panel
            title="Limitations"
            subtitle="What this finding does not establish"
            bodyClass="tight"
          >
            <ul className="flex flex-col gap-1.5" style={{ margin: 0, paddingLeft: 18 }}>
              {limitations.map((l) => (
                <li key={l} style={{ fontSize: 12 }} className="muted">{l}</li>
              ))}
            </ul>
          </Panel>
        </div>
      )}
    </Drawer>
  )
}

function FeatureBlock({ feature, detail }) {
  const kind = detail.kind === 'numeric' || detail.kind === 'categorical'
    ? detail.kind
    : detail.issue ? 'schema change' : detail.kind
  const tests = detail.tests || []

  return (
    <div className="flex flex-col gap-1.5" data-feature={feature}>
      <div className="flex items-center gap-2 flex-wrap">
        <strong style={{ fontSize: 13 }}>{feature}</strong>
        <Badge tone="outline">{kind}</Badge>
        <Badge tone={detail.flagged ? 'danger' : 'success'}>
          {detail.flagged ? 'shifted' : 'stable'}
        </Badge>
      </div>

      {detail.issue && <p className="muted" style={{ margin: 0, fontSize: 12 }}>{detail.issue}</p>}
      {detail.missing_rate_shift && (
        <p className="muted" style={{ margin: 0, fontSize: 12 }}>
          Missing rate {fmtValue(detail.missing_rate_shift.baseline)} → {fmtValue(detail.missing_rate_shift.observed)}
          {' '}(Δ {fmtValue(detail.missing_rate_shift.delta)})
        </p>
      )}
      {detail.unseen_categories?.length > 0 && (
        <p className="muted" style={{ margin: 0, fontSize: 12 }}>
          Categories not present in the reference: {detail.unseen_categories.join(', ')}
        </p>
      )}

      {tests.length > 0 && (
        <KeyValue
          items={tests.map((t) => [
            t.test,
            t.p_value != null
              ? `${t.statistic != null ? Number(t.statistic).toFixed(4) : '—'} (p=${Number(t.p_value).toExponential(2)}, gate ${t.threshold ?? '—'}) ${t.significant ? '· significant' : ''}`
              : `${t.statistic != null ? Number(t.statistic).toFixed(4) : '—'} (gate ${t.threshold ?? '—'}) ${t.significant ? '· significant' : ''}`,
          ])}
        />
      )}

      {(detail.baseline || detail.observed) && (
        <KeyValue
          items={[
            ...(detail.baseline ? [['baseline', fmtValue({ ...detail.baseline, sample: undefined })]] : []),
            ...(detail.observed ? [['observed', fmtValue({ ...detail.observed, sample: undefined })]] : []),
          ]}
        />
      )}
    </div>
  )
}

/* ══════════════════════════════════════════════════════
   Baselines
   ══════════════════════════════════════════════════════ */
function BaselinesView({ refreshToken }) {
  const toast = useToast()
  const [query, setQuery] = useState('')
  const [selected, setSelected] = useState(null)
  const [creating, setCreating] = useState(false)

  const baselines = usePolling(
    () => driftApi.listBaselines({ limit: 100 }),
    FINDING_POLL_MS,
    { immediate: true, deps: [refreshToken] },
  )

  const items = baselines.data?.items || []
  const needle = query.trim().toLowerCase()
  const filtered = needle
    ? items.filter((b) => [b.model_name, b.model_version, b.dataset_id, b.dataset_ref]
      .some((v) => String(v || '').toLowerCase().includes(needle)))
    : items

  return (
    <>
      <div className="flex items-center justify-end gap-2 flex-wrap">
        <SearchInput value={query} onChange={setQuery} placeholder="Search baselines…" />
        <Button size="sm" variant="secondary" icon={Plus} onClick={() => setCreating(true)}>
          Compute baseline
        </Button>
      </div>

      <Panel
        icon={FlaskConical}
        title="Reference baselines"
        subtitle={baselines.data ? `${filtered.length} of ${items.length} shown` : 'Loading…'}
        bodyClass={filtered.length ? 'tight' : ''}
      >
        {baselines.loading && !baselines.data ? (
          <LoadingState label="Loading baselines…" />
        ) : baselines.error && !baselines.data ? (
          <ErrorState error={baselines.error} title="Could not load baselines" onRetry={baselines.refresh} />
        ) : (
          <DataTable
            caption="One baseline per model version"
            keyOf={(r) => r.id}
            rows={filtered}
            pageSize={25}
            onRowClick={(r) => setSelected(r)}
            columns={[
              { key: 'model_name', strong: true },
              { key: 'model_version', width: '90px', render: (r) => r.model_version || '—' },
              { key: 'status', width: '110px', render: (r) => <Badge tone={r.status === 'active' ? 'success' : 'neutral'}>{r.status}</Badge> },
              {
                key: 'feature_names',
                render: (r) => {
                  const n = (r.feature_names || []).length
                  return `${n} feature${n === 1 ? '' : 's'}`
                },
              },
              { key: 'sample_count', width: '110px', render: (r) => (r.sample_count != null ? r.sample_count : '—') },
              {
                key: 'coverage',
                width: '150px',
                render: (r) => (
                  <span className="flex items-center gap-1 flex-wrap">
                    {r.has_prediction_dist ? <Badge sm tone="info">pred</Badge> : null}
                    {r.has_confidence_stats ? <Badge sm tone="accent">conf</Badge> : null}
                    {!r.has_prediction_dist && !r.has_confidence_stats ? 'features only' : null}
                  </span>
                ),
              },
              { key: 'created_at', width: '120px', render: (r) => <span title={fmtTime(r.created_at)}>{relTime(r.created_at)}</span> },
            ]}
            empty={
              <EmptyState
                icon={FlaskConical}
                title="No baselines computed"
                desc="A baseline stores the reference distributions a drift check compares against. Compute one from a model's training dataset."
                actions={<Button size="sm" icon={Plus} onClick={() => setCreating(true)}>Compute baseline</Button>}
              />
            }
          />
        )}
      </Panel>

      <BaselineDrawer baseline={selected} onClose={() => setSelected(null)} />

      <ComputeModal
        open={creating}
        onClose={() => setCreating(false)}
        onDone={(name) => { toast.success(`Baseline computed for ${name}`); setCreating(false); baselines.refresh() }}
      />
    </>
  )
}

function BaselineDrawer({ baseline, onClose }) {
  const detail = useFetch(
    () => (baseline ? driftApi.getBaselineDetail(baseline.model_name, baseline.model_version) : Promise.resolve(null)),
    [baseline?.id],
  )

  const stats = detail.data?.feature_stats || {}

  return (
    <Drawer
      open={!!baseline}
      onClose={onClose}
      title={baseline ? `${baseline.model_name} @ ${baseline.model_version}` : 'Baseline'}
      subtitle={baseline ? `baseline ${String(baseline.id).slice(0, 8)} · ${baseline.status}` : ''}
      wide
    >
      {baseline && (
        <div className="flex flex-col gap-4">
          <KeyValue
            items={[
              ['Model', `${baseline.model_name} @ ${baseline.model_version}`],
              ['Status', baseline.status],
              ['Reference samples', baseline.sample_count],
              ['Dataset', baseline.dataset_id],
              ['lakeFS ref', baseline.dataset_ref],
              ['Schema hash', baseline.schema_hash],
              ['Created', fmtTime(baseline.created_at)],
              ['Created by', baseline.created_by],
              // Training-time metrics — shown as context, never compared against
              // production: that would require ground-truth outcomes (D4).
              ['Training metrics', baseline.reference_metrics],
            ]}
          />

          <Panel title="Per-feature reference statistics" bodyClass="tight">
            {detail.loading && !detail.data ? (
              <LoadingState label="Loading reference statistics…" />
            ) : detail.error ? (
              <ErrorState error={detail.error} title="Could not load baseline detail" onRetry={detail.refetch} />
            ) : Object.keys(stats).length === 0 ? (
              <p className="muted" style={{ margin: 0, fontSize: 12 }}>No feature statistics recorded.</p>
            ) : (
              <div className="flex flex-col gap-3">
                {Object.entries(stats).map(([name, s]) => (
                  <div key={name} className="flex flex-col gap-1">
                    <div className="flex items-center gap-2 flex-wrap">
                      <strong style={{ fontSize: 13 }}>{name}</strong>
                      <Badge tone="outline">{s.kind}</Badge>
                      {s.missing_rate > 0 && <Badge sm tone="warning">missing {Math.round(s.missing_rate * 100)}%</Badge>}
                    </div>
                    <KeyValue
                      items={s.kind === 'numeric'
                        ? [
                          ['mean', s.mean?.toFixed?.(4)],
                          ['std', s.std?.toFixed?.(4)],
                          ['min / max', s.min != null && s.max != null ? `${s.min} / ${s.max}` : null],
                          ['p25 / p50 / p75', s.quantiles
                            ? `${s.quantiles.p25?.toFixed?.(3)} / ${s.quantiles.p50?.toFixed?.(3)} / ${s.quantiles.p75?.toFixed?.(3)}`
                            : null],
                          ['reference sample', s.sample ? `${s.sample.length} values` : null],
                        ]
                        : [
                          ['distinct', s.distinct_count],
                          ['top categories', s.category_counts
                            ? Object.entries(s.category_counts).slice(0, 6).map(([k, v]) => `${k}:${v}`).join(', ')
                            : null],
                        ]}
                    />
                  </div>
                ))}
              </div>
            )}
          </Panel>
        </div>
      )}
    </Drawer>
  )
}

/* ══════════════════════════════════════════════════════
   Telemetry
   ══════════════════════════════════════════════════════ */
function TelemetryView({ refreshToken }) {
  const [modelFilter, setModelFilter] = useState('')

  const telemetry = usePolling(
    () => driftApi.listTelemetry({ model_name: modelFilter || undefined, limit: 50 }),
    FINDING_POLL_MS,
    { immediate: true, deps: [modelFilter, refreshToken] },
  )

  const items = telemetry.data?.items || []

  return (
    <>
      <div className="flex items-center justify-end gap-2 flex-wrap">
        <TextInput
          style={{ width: 200 }}
          value={modelFilter}
          placeholder="Model name"
          aria-label="Filter telemetry by model"
          onChange={(e) => setModelFilter(e.target.value)}
        />
      </div>

      <Panel
        icon={Gauge}
        title="Inference telemetry"
        subtitle={telemetry.data ? `${items.length} of ${telemetry.data.total} rows${items.some((r) => r.has_probabilities) ? '' : ' · no probabilities captured yet'}` : 'Loading…'}
        bodyClass={items.length ? 'tight' : ''}
      >
        {telemetry.loading && !telemetry.data ? (
          <LoadingState label="Loading telemetry…" />
        ) : telemetry.error && !telemetry.data ? (
          <ErrorState error={telemetry.error} title="Could not load telemetry" onRetry={telemetry.refresh} />
        ) : (
          <DataTable
            caption="Raw per-request rows the drift detectors measure against"
            keyOf={(r) => r.id}
            rows={items}
            pageSize={25}
            columns={[
              { key: 'model_name', strong: true },
              { key: 'model_version', width: '90px', render: (r) => r.model_version || '—' },
              {
                key: 'has_probabilities',
                width: '130px',
                render: (r) => (
                  <Badge tone={r.has_probabilities ? 'success' : 'neutral'}>
                    {r.has_probabilities ? 'probabilities' : 'labels only'}
                  </Badge>
                ),
              },
              { key: 'confidence', width: '110px', render: (r) => (r.confidence != null ? Number(r.confidence).toFixed(3) : '—') },
              { key: 'feature_count', width: '100px', render: (r) => (r.feature_count ?? '—') },
              {
                key: 'features_truncated',
                width: '110px',
                render: (r) => (r.features_truncated ? <Badge tone="warning">clipped</Badge> : '—'),
              },
              { key: 'latency_ms', width: '100px', render: (r) => (r.latency_ms != null ? `${Math.round(r.latency_ms)}ms` : '—') },
              {
                key: 'recorded_at',
                width: '120px',
                render: (r) => <span title={fmtTime(r.recorded_at)}>{relTime(r.recorded_at)}</span>,
              },
            ]}
            empty={
              <EmptyState
                icon={Gauge}
                title="No inference telemetry yet"
                desc="Rows are written on every prediction served. Once a model has a baseline and enough rows, drift checks start comparing them."
              />
            }
          />
        )}
      </Panel>
    </>
  )
}

/* ══════════════════════════════════════════════════════
   Run check modal
   ══════════════════════════════════════════════════════ */
function CheckModal({ open, onClose, onRan }) {
  const toast = useToast()
  const [model, setModel] = useState('')
  const [version, setVersion] = useState('')
  const [windowSecs, setWindowSecs] = useState('')
  const [publish, setPublish] = useState(true)
  const [busy, setBusy] = useState(false)
  const [report, setReport] = useState(null)
  const [err, setErr] = useState(null)

  const models = useFetch(
    () => (open ? mlOpsApi.listModels().catch(() => ({ items: [] })) : Promise.resolve(null)),
    [open],
  )

  const close = () => { setReport(null); setErr(null); onClose() }

  const run = async () => {
    if (!model.trim()) { setErr('Choose a model.'); return }
    setBusy(true); setErr(null); setReport(null)
    try {
      const res = await driftApi.runCheck({
        model_name: model.trim(),
        model_version: version.trim() || undefined,
        window_seconds: windowSecs ? Number(windowSecs) : undefined,
        publish,
      })
      setReport(res)
      if (res.findings?.length) onRan?.()
      else toast.info('No drift detected in this window')
    } catch (e) {
      setErr(e.message || 'Drift check failed')
    } finally {
      setBusy(false)
    }
  }

  const modelOptions = models.data?.items || models.data?.models || []

  return (
    <Modal
      open={open}
      onClose={close}
      title="Run drift check"
      subtitle="Compares the latest observation window against the stored baseline"
      footer={(
        <>
          <Button variant="secondary" onClick={close}>Close</Button>
          <Button icon={Play} onClick={run} loading={busy}>Run check</Button>
        </>
      )}
    >
      <div className="flex flex-col gap-3">
        <Field label="Model" required hint="Must match the model name recorded on its telemetry rows">
          {modelOptions.length > 0 ? (
            <Select value={model} onChange={(e) => setModel(e.target.value)} aria-label="Model">
              <option value="">Select a model…</option>
              {modelOptions.map((m) => (
                <option key={m.name || m.model_name} value={m.name || m.model_name}>
                  {m.name || m.model_name}
                </option>
              ))}
            </Select>
          ) : (
            <TextInput value={model} placeholder="e.g. titanic-classifier" onChange={(e) => setModel(e.target.value)} />
          )}
        </Field>

        <div className="flex gap-3 flex-wrap">
          <Field label="Version" hint="Optional — defaults to the latest">
            <TextInput style={{ width: 120 }} value={version} placeholder="latest" onChange={(e) => setVersion(e.target.value)} />
          </Field>
          <Field label="Window (seconds)" hint="Defaults to the configured observation window">
            <TextInput style={{ width: 160 }} value={windowSecs} placeholder="3600" onChange={(e) => setWindowSecs(e.target.value)} />
          </Field>
        </div>

        <Field label="Publish findings" hint="Off = preview the result without creating a security alert">
          <Select value={publish ? 'yes' : 'no'} onChange={(e) => setPublish(e.target.value === 'yes')} aria-label="Publish findings">
            <option value="yes">Create events and alerts</option>
            <option value="no">Preview only (no alerts)</option>
          </Select>
        </Field>

        {err && <Alert tone="danger">{err}</Alert>}

        {report && (
          <div className="flex flex-col gap-3" data-testid="check-report">
            <KeyValue
              items={[
                ['Window', `${fmtTime(report.window_start)} → ${fmtTime(report.window_end)}`],
                ['Telemetry rows', report.records],
                ['Observations', report.observations],
                ['Baseline', report.baseline_found ? `found (${String(report.baseline_id || '').slice(0, 8)})` : 'none'],
                ['Findings', report.findings?.length || 0],
              ]}
            />

            {(report.findings || []).map((f, i) => (
              <Alert key={`${f.detection_type}-${i}`} tone={f.severity === 'HIGH' ? 'danger' : 'warning'} icon={LineChart}>
                <strong>{DETECTIONS[f.detection_type]?.label || f.detection_type}</strong>
                {' — '}
                {f.summary}
              </Alert>
            ))}

            {(report.skipped || []).length > 0 && (
              <Panel title="Not measured" subtitle="Why a detector produced nothing" bodyClass="tight">
                <ul className="flex flex-col gap-1.5" style={{ margin: 0, paddingLeft: 18 }}>
                  {report.skipped.map((s, i) => (
                    <li key={`${s.reason}-${i}`} style={{ fontSize: 12 }} className="muted">
                      <strong>{s.reason}</strong> — {s.detail}
                    </li>
                  ))}
                </ul>
              </Panel>
            )}
          </div>
        )}
      </div>
    </Modal>
  )
}

/* ══════════════════════════════════════════════════════
   Compute baseline modal
   ══════════════════════════════════════════════════════ */
function ComputeModal({ open, onClose, onDone }) {
  const [model, setModel] = useState('')
  const [version, setVersion] = useState('')
  const [datasetId, setDatasetId] = useState('')
  const [dataPath, setDataPath] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState(null)

  const models = useFetch(
    () => (open ? mlOpsApi.listModels().catch(() => ({ items: [] })) : Promise.resolve(null)),
    [open],
  )
  const datasets = useFetch(
    () => (open ? datasetsApi.list().catch(() => ({ items: [] })) : Promise.resolve(null)),
    [open],
  )

  const close = () => { setErr(null); onClose() }

  const submit = async () => {
    if (!model.trim() || !version.trim()) { setErr('Model and version are required.'); return }
    setBusy(true); setErr(null)
    try {
      const created = await driftApi.createBaseline({
        model_name: model.trim(),
        model_version: version.trim(),
        dataset_id: datasetId.trim() || undefined,
        data_path: dataPath.trim() || undefined,
      })
      onDone?.(created.model_name)
    } catch (e) {
      // The backend reports the readable cause (lakeFS unreachable, no
      // dataset recorded on the run) rather than a stack trace.
      setErr(e.message || 'Baseline computation failed')
    } finally {
      setBusy(false)
    }
  }

  const modelOptions = models.data?.items || models.data?.models || []
  const datasetOptions = datasets.data?.items || datasets.data?.datasets || []

  return (
    <Modal
      open={open}
      onClose={close}
      title="Compute baseline"
      subtitle="Builds reference statistics from the training dataset for one exact model version"
      footer={(
        <>
          <Button variant="secondary" onClick={close}>Cancel</Button>
          <Button icon={FlaskConical} onClick={submit} loading={busy}>Compute</Button>
        </>
      )}
    >
      <div className="flex flex-col gap-3">
        <div className="flex gap-3 flex-wrap">
          <Field label="Model" required>
            {modelOptions.length > 0 ? (
              <Select value={model} onChange={(e) => setModel(e.target.value)} aria-label="Model">
                <option value="">Select a model…</option>
                {modelOptions.map((m) => (
                  <option key={m.name || m.model_name} value={m.name || m.model_name}>
                    {m.name || m.model_name}
                  </option>
                ))}
              </Select>
            ) : (
              <TextInput value={model} placeholder="model name" onChange={(e) => setModel(e.target.value)} />
            )}
          </Field>
          <Field label="Version" required hint="A baseline is bound to one version">
            <TextInput style={{ width: 110 }} value={version} placeholder="1" onChange={(e) => setVersion(e.target.value)} />
          </Field>
        </div>

        <Field
          label="Dataset"
          hint="Leave empty to resolve it from the model's MLflow run tags"
        >
          {datasetOptions.length > 0 ? (
            <Select value={datasetId} onChange={(e) => setDatasetId(e.target.value)} aria-label="Dataset">
              <option value="">Resolve from MLflow run…</option>
              {datasetOptions.map((d) => (
                <option key={d.id || d.name} value={d.id || d.name}>
                  {d.name || d.id}
                </option>
              ))}
            </Select>
          ) : (
            <TextInput value={datasetId} placeholder="dataset id (optional)" onChange={(e) => setDatasetId(e.target.value)} />
          )}
        </Field>

        <Field label="File path" hint="Optional file inside the lakeFS repository (defaults to the first CSV/Parquet)">
          <TextInput value={dataPath} placeholder="data/train.csv" onChange={(e) => setDataPath(e.target.value)} />
        </Field>

        {err && <Alert tone="danger">{err}</Alert>}
      </div>
    </Modal>
  )
}
