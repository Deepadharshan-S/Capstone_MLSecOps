import { useState, useEffect, useMemo, Fragment } from 'react'
import { mlOpsApi, datasetsApi } from '../api/endpoints.js'
import { api } from '../api/client.js'
import { useFetch } from '../hooks/useFetch.js'
import { parseCsvTable } from '../lib/csv.js'
import { computeClassificationMetrics } from '../lib/metrics.js'
import { formatAccuracy } from '../lib/models.js'
import {
  PageHeader, Panel, Button, Field, TextInput, Select, Badge,
  EmptyState, LoadingState, ErrorState, Alert, Segmented, KeyValue,
  DataTable, useToast,
} from '../components/ui/index.jsx'
import {
  Scale, Play, Rocket, Boxes, Database, FileText, Target, Zap,
  Info, CheckCircle2, XCircle,
} from '../components/icons.jsx'

// Same read cap the dataset preview uses — a test run only needs the head of
// the file, and the download stream hands over the whole object.
const READ_BYTES = 512 * 1024
const SAMPLE_SIZES = [10, 25, 50, 100, 200]
const PREVIEW_ROWS = 10

/**
 * CSV cells arrive as strings, but `pd.DataFrame(records)` must infer the
 * same dtypes `pd.read_csv` produced at training time — a column of quoted
 * floats would otherwise reach an sklearn pipeline as object dtype and fail.
 * (pandas also ignores quotes when inferring type, so this matches it.)
 */
function coerce(raw) {
  if (raw === '' || raw === null || raw === undefined) return raw
  if (/^(true|false)$/i.test(raw)) return raw.toLowerCase() === 'true'
  const n = Number(raw)
  return Number.isFinite(n) ? n : raw
}

/* ── Confusion matrix (N×N, labels from the run) ───── */
function Matrix({ metrics }) {
  const { labels, confusion } = metrics
  const n = labels.length
  const total = metrics.total || 1
  return (
    <div style={{ overflowX: 'auto' }}>
      {/* Reuses the .cm-* styling from the Jobs drawer, which is pinned to a
          2×2 grid — override the template so multiclass fits too. */}
      <div
        className="cm-grid"
        style={{ gridTemplateColumns: `auto repeat(${n}, minmax(46px, 1fr))`, minWidth: n > 4 ? 420 : 0 }}
      >
        <div />
        {labels.map((l) => <div className="cm-h" key={`c-${l}`} title={`Predicted ${l}`}>Pred. {l}</div>)}
        {confusion.map((row, i) => (
          <Fragment key={`r-${labels[i]}`}>
            <div className="cm-rh" title={`Actual ${labels[i]}`}>Actual {labels[i]}</div>
            {row.map((v, j) => {
              const pct = (v / total) * 100
              const pctStr = v > 0 && pct < 0.05 ? '<0.1%' : `${pct.toFixed(1)}%`
              return (
                <div className={`cm-cell ${i === j ? 'cm-tp' : 'cm-fp'}`} key={`cell-${i}-${j}`}>
                  {v}
                  <div style={{ fontSize: 10, fontWeight: 400, opacity: .7, marginTop: 2 }}>{pctStr}</div>
                </div>
              )
            })}
          </Fragment>
        ))}
      </div>
    </div>
  )
}

/* ══════════════════════════════════════════════════════
   Testing — run a registry model or a live deployment against held-out
   rows from a dataset and score the predictions against the real labels.

   Predict already sends hand-written JSON; this page is about *measured*
   behaviour: it reads the same CSV the model was trained from, drops the
   target column, sends one batched request and reports metrics that use the
   backend's own weighting (ray_wrapper.py → average="weighted").
   ══════════════════════════════════════════════════════ */
export default function Testing() {
  const toast = useToast()

  /* ── Sample selection ────────────────────────────── */
  const [dataset, setDataset] = useState('')
  const [refInput, setRefInput] = useState('')
  const [path, setPath] = useState('')
  const [targetCol, setTargetCol] = useState('')
  const [excluded, setExcluded] = useState('')
  const [sampleSize, setSampleSize] = useState('50')

  /* ── Target selection ────────────────────────────── */
  const [tab, setTab] = useState('deployment')
  const [target, setTarget] = useState('')
  const [version, setVersion] = useState('')

  /* ── Run state ───────────────────────────────────── */
  const [running, setRunning] = useState(false)
  const [report, setReport] = useState(null)
  const [error, setError] = useState(null)

  const datasets = useFetch(() => datasetsApi.list(), [])
  const models = useFetch(() => mlOpsApi.listModels(), [])
  const deployments = useFetch(() => mlOpsApi.listDeployments({ active_only: true }), [])

  const datasetList = useMemo(
    () => (Array.isArray(datasets.data) ? datasets.data : []),
    [datasets.data],
  )
  const selectedDs = datasetList.find((d) => d.name === dataset) || null
  const defaultBranch = selectedDs?.default_branch || 'main'
  const ref = (refInput || '').trim() || defaultBranch
  const suggestedTarget = selectedDs?.metadata_info?.target_column || ''

  const files = useFetch(
    () => (dataset ? datasetsApi.files(dataset, ref) : Promise.resolve([])),
    [dataset, ref],
  )

  // Read the file once per dataset@ref@path — the same read feeds the column
  // picker, the row preview and the run itself.
  const table = useFetch(async () => {
    if (!dataset || !path) return null
    const blob = await api.download(datasetsApi.downloadUrl(dataset, path, ref))
    const byteTruncated = blob.size > READ_BYTES
    let text = await blob.slice(0, READ_BYTES).text()
    if (byteTruncated) text = text.slice(0, Math.max(0, text.lastIndexOf('\n')))
    return { ...parseCsvTable(text), byteTruncated }
  }, [dataset, ref, path])

  const modelList = useMemo(() => models.data?.models || [], [models.data])
  const depList = useMemo(() => deployments.data?.deployments || [], [deployments.data])
  const options = tab === 'model' ? modelList : depList

  /* ── Auto-selection (guarded so they can't loop) ─── */
  useEffect(() => {
    if (datasets.loading || datasets.error || !datasetList.length) return
    if (dataset && datasetList.some((d) => d.name === dataset)) return
    setDataset(datasetList[0].name)
  }, [datasets.loading, datasets.error, datasetList, dataset])

  const filePaths = useMemo(
    () => (Array.isArray(files.data) ? files.data : []).map((f) => f?.path).filter(Boolean),
    [files.data],
  )
  const csvPaths = useMemo(
    () => filePaths.filter((p) => p.toLowerCase().endsWith('.csv')),
    [filePaths],
  )
  useEffect(() => {
    if (files.loading || files.error || !filePaths.length) return
    if (path && filePaths.includes(path)) return
    setPath(csvPaths[0] || filePaths[0])
  }, [files.loading, files.error, filePaths, csvPaths, path])

  const columns = useMemo(() => table.data?.columns || [], [table.data])
  useEffect(() => {
    if (!columns.length) return
    if (targetCol && columns.includes(targetCol)) return
    const byMetadata = suggestedTarget && columns.includes(suggestedTarget) ? suggestedTarget : ''
    const byName = columns.find((c) => ['label', 'target', 'class'].includes(String(c).toLowerCase())) || ''
    setTargetCol(byMetadata || byName || columns[columns.length - 1])
  }, [columns, targetCol, suggestedTarget])

  useEffect(() => {
    if (target && options.some((o) => (o.deployment_id || o.name) === target)) return
    if (!options.length) return
    setTarget(options[0].deployment_id || options[0].name)
  }, [options, target])

  /* ── Build the payload ───────────────────────────── */
  const excludedList = useMemo(
    () => excluded.split(',').map((s) => s.trim()).filter(Boolean),
    [excluded],
  )

  const sample = useMemo(() => {
    const cols = table.data?.columns
    const rows = table.data?.rows
    if (!cols?.length || !rows?.length || !targetCol || !cols.includes(targetCol)) return null

    const drop = new Set([...excludedList, targetCol])
    const featureCols = cols.filter((c) => c && !drop.has(c))
    const idx = new Map(cols.map((c, i) => [c, i]))
    const targetIdx = idx.get(targetCol)
    const featureIdx = featureCols.map((c) => idx.get(c))
    const limit = Math.max(1, Number(sampleSize) || 50)

    const records = []
    const expected = []
    let skipped = 0
    for (const row of rows) {
      if (records.length >= limit) break
      const label = row[targetIdx]
      // Rows without a usable label or with holes in the features are dropped
      // rather than sent as nulls — a null column would change dtypes and the
      // model would reject or silently mangle the batch.
      if (label === undefined || label === '') { skipped += 1; continue }
      const rec = {}
      let complete = true
      for (let i = 0; i < featureCols.length; i++) {
        const raw = row[featureIdx[i]]
        if (raw === undefined || raw === '') { complete = false; break }
        rec[featureCols[i]] = coerce(raw)
      }
      if (!complete) { skipped += 1; continue }
      records.push(rec)
      expected.push(coerce(label))
    }

    return {
      records,
      expected,
      featureCols,
      skipped,
      rowsRead: rows.length,
      rowTruncated: Boolean(table.data.rowTruncated),
      byteTruncated: Boolean(table.data.byteTruncated),
    }
  }, [table.data, targetCol, excludedList, sampleSize])

  const canRun = Boolean(sample?.records.length && target && !running)

  async function run() {
    setError(null)
    setReport(null)
    if (!sample || !sample.records.length) {
      setError('No usable rows — pick a dataset, file and target column with labelled data.')
      return
    }
    if (!target) { setError('Select a model or deployment target first.'); return }

    setRunning(true)
    const startedAt = Date.now()
    try {
      // One batched request: both predict endpoints are rate-limited to
      // 60/min and reject payloads over 10 000 rows, so per-row calls would
      // throttle immediately while adding nothing — the server answers with a
      // single `latency_ms` for the whole batch regardless.
      const envelope = { dataframe_records: sample.records }
      const res = tab === 'deployment'
        ? await mlOpsApi.predictDeployment(target, envelope)
        : await mlOpsApi.predictModel(target, envelope, version || undefined)
      const wallMs = Date.now() - startedAt
      const predictions = Array.isArray(res?.predictions) ? res.predictions : []
      const metrics = predictions.length === sample.expected.length
        ? computeClassificationMetrics(sample.expected, predictions)
        : null

      setReport({ res, metrics, wallMs, predictions, at: new Date(), rows: sample.records.length })
      if (!metrics) {
        setError(`The response returned ${predictions.length} prediction(s) for ${sample.expected.length} input row(s) — metrics were not computed.`)
      } else {
        toast.success('Test run finished', `${metrics.correct}/${metrics.total} correct · ${wallMs}ms`)
      }
    } catch (err) {
      setError(err?.message || 'Prediction failed.')
      toast.error('Test run failed', err?.message)
    } finally {
      setRunning(false)
    }
  }

  /* ── Derived UI data ─────────────────────────────── */
  const previewRows = useMemo(
    () => (sample ? sample.records.slice(0, PREVIEW_ROWS).map((rec, i) => ({ i, rec, label: sample.expected[i] })) : []),
    [sample],
  )
  const previewCols = sample ? sample.featureCols.slice(0, 5) : []
  const dep = depList.find((d) => d.deployment_id === target)
  const mdl = modelList.find((m) => m.name === target)
  const datasetDenied = datasets.error?.status === 403

  return (
    <div className="page">
      <PageHeader
        icon={Scale}
        title="Model testing"
        subtitle="Score a registered model or a live deployment against held-out rows from a dataset"
        actions={
          <>
            <Badge tone="outline"><Database size={11} /> {datasetList.length || '—'} datasets</Badge>
            <Button variant="primary" size="sm" icon={Play} loading={running} disabled={!canRun} onClick={run}>
              Run test
            </Button>
          </>
        }
      />

      {datasetDenied && (
        <Alert tone="warning">
          Your role does not have the <code className="mono">datasets:view</code> scope, so this page
          cannot read datasets or download samples. Predictions against hand-written payloads still
          work on the <strong>Predict</strong> page.
        </Alert>
      )}

      <div className="grid-2">
        {/* ── Setup ─────────────────────────────────── */}
        <div className="flex flex-col gap-4">
          <Panel icon={Database} title="Test sample" subtitle="Held-out rows read straight from the dataset">
            {datasets.loading ? (
              <LoadingState label="Loading datasets…" />
            ) : datasets.error ? (
              <ErrorState error={datasets.error} title="Could not load datasets" onRetry={datasets.refetch} />
            ) : !datasetList.length ? (
              <EmptyState
                icon={Database}
                title="No datasets yet"
                desc="Register a dataset first — its labelled rows are the expected values this page scores against."
              />
            ) : (
              <div className="flex flex-col gap-4">
                <Field label="Dataset" htmlFor="ts-dataset">
                  <Select id="ts-dataset" value={dataset} onChange={(e) => { setDataset(e.target.value); setPath(''); setTargetCol('') }}>
                    {datasetList.map((d) => <option key={d.name} value={d.name}>{d.name}</option>)}
                  </Select>
                </Field>

                <Field label="Ref" htmlFor="ts-ref" hint="Branch, commit or tag — defaults to the dataset's default branch">
                  <TextInput id="ts-ref" value={refInput} onChange={(e) => setRefInput(e.target.value)} placeholder={defaultBranch} />
                </Field>

                <Field label="File" htmlFor="ts-file" hint={files.loading ? 'Loading objects…' : `${filePaths.length} object${filePaths.length === 1 ? '' : 's'} on ${ref}`}>
                  <Select id="ts-file" value={path} onChange={(e) => { setPath(e.target.value); setTargetCol('') }}>
                    {!filePaths.length && <option value="">No files found</option>}
                    {filePaths.map((p) => <option key={p} value={p}>{p}</option>)}
                  </Select>
                </Field>

                {/* Listing objects is a lakeFS call — without this the select
                    above just says "No files found" and the failure is
                    indistinguishable from an empty repository. */}
                {files.error && (
                  <Alert
                    tone="warning"
                    actions={<Button size="xs" variant="secondary" onClick={files.refetch}>Retry</Button>}
                  >
                    Could not list files on <code className="mono">{ref}</code>: {files.error.message}
                  </Alert>
                )}

                <Field label="Target column" htmlFor="ts-target" hint="Dropped from the payload; its values become the expected predictions">
                  <Select id="ts-target" value={targetCol} onChange={(e) => setTargetCol(e.target.value)}>
                    {!columns.length && <option value="">Load a tabular file first</option>}
                    {columns.map((c) => <option key={c} value={c}>{c}</option>)}
                  </Select>
                </Field>

                <Field label="Exclude columns" htmlFor="ts-exclude" hint="Comma-separated — use this for ID or leakage columns the model wasn't trained on">
                  <TextInput id="ts-exclude" value={excluded} onChange={(e) => setExcluded(e.target.value)} placeholder="id, index, fold" />
                </Field>

                <Field label="Rows to score" htmlFor="ts-size" hint={sample?.rowTruncated || sample?.byteTruncated ? 'The file holds more rows than were read — only the first N usable rows are used.' : undefined}>
                  <Select id="ts-size" value={sampleSize} onChange={(e) => setSampleSize(e.target.value)}>
                    {SAMPLE_SIZES.map((n) => <option key={n} value={String(n)}>{n} rows</option>)}
                  </Select>
                </Field>
              </div>
            )}
          </Panel>

          <Panel
            icon={FileText}
            title="Rows to send"
            subtitle={
              table.loading ? 'Reading file…'
                : sample ? `${sample.records.length} of ${sample.rowsRead} rows ready${sample.skipped ? ` · ${sample.skipped} skipped (unlabelled or incomplete)` : ''}`
                : 'Waiting for a file and target column'
            }
            actions={sample ? <Badge tone="outline">{sample.featureCols.length} feature{sample.featureCols.length === 1 ? '' : 's'}</Badge> : null}
            bodyClass={previewRows.length ? 'tight' : ''}
          >
            {table.loading ? (
              <LoadingState label="Reading the file…" />
            ) : table.error ? (
              <ErrorState error={table.error} title="Could not read this file" onRetry={table.refetch} />
            ) : !previewRows.length ? (
              <EmptyState
                icon={FileText}
                title="Nothing to score yet"
                desc={table.data?.isText ? 'This file is not tabular CSV.' : 'Choose a file with a labelled target column.'}
              />
            ) : (
              <>
                <DataTable
                  caption="Rows that will be sent"
                  keyOf={(r) => r.i}
                  rows={previewRows}
                  columns={[
                    { key: '__i', header: '#', width: 46, render: (r) => <span className="tnum muted">{r.i + 1}</span> },
                    ...previewCols.map((c) => ({
                      key: c,
                      header: c,
                      render: (r) => <span className="mono text-xs">{String(r.rec[c])}</span>,
                    })),
                    { key: '__expected', header: 'Expected', strong: true, render: (r) => <span className="mono text-xs">{String(r.label)}</span> },
                  ]}
                />
                {/* The body is `tight` (padding: 0) so the table can bleed to
                    the panel edges — this action bar still needs its own
                    inset, otherwise the label and the button sit flush against
                    the border. .panel-footer supplies that inset plus the
                    separator that keeps it distinct from the table. */}
                {/* One Run test lives in the page header and is the only CTA
                    when no preview exists, so this bar just reports how much
                    of the sample will be sent. It used to repeat the button. */}
                <div className="panel-footer">
                  <span className="muted text-xs">
                    Previewing {previewRows.length} of {sample.records.length} rows
                    {sample.records.length > previewRows.length ? ` · ${sample.records.length - previewRows.length} more will be sent` : ''}
                  </span>
                </div>
              </>
            )}
          </Panel>
        </div>

        {/* ── Target & result ───────────────────────── */}
        <div className="flex flex-col gap-4">
          <Panel icon={Target} title="Target" subtitle={tab === 'deployment' ? 'Live RayService endpoint' : 'Model version from the registry'}>
            <div className="flex flex-col gap-4">
              <Segmented
                className="is-even"
                value={tab}
                onChange={(v) => { setTab(v); setTarget('') }}
                options={[
                  { value: 'deployment', label: 'Deployment', icon: Rocket },
                  { value: 'model', label: 'Registry model', icon: Boxes },
                ]}
              />

              {tab === 'deployment' && deployments.error ? (
                <ErrorState error={deployments.error} title="Could not load deployments" onRetry={deployments.refetch} />
              ) : tab === 'model' && models.error ? (
                <ErrorState error={models.error} title="Could not load models" onRetry={models.refetch} />
              ) : options.length === 0 ? (
                <EmptyState
                  icon={Target}
                  title={tab === 'deployment' ? 'No active deployments' : 'No registered models'}
                  desc={tab === 'deployment'
                    ? 'Deploy a model version first — inference is served exclusively through a live RayService endpoint.'
                    : 'Train or upload a model to make it available here.'}
                />
              ) : (
                <>
                  <Field label={tab === 'deployment' ? 'Deployment' : 'Model'} htmlFor="ts-target-sel">
                    <Select id="ts-target-sel" value={target} onChange={(e) => setTarget(e.target.value)}>
                      {options.map((o) => (
                        <option key={o.deployment_id || o.name} value={o.deployment_id || o.name}>
                          {tab === 'deployment'
                            ? `${o.model_name || o.deployment_id} · ${o.environment} · ${o.status}`
                            : `${o.name}${o.accuracy != null ? ` · acc ${Number(o.accuracy).toFixed(3)}` : ''}`}
                        </option>
                      ))}
                    </Select>
                  </Field>

                  {tab === 'model' && (
                    <Field label="Version (optional)" htmlFor="ts-ver" hint="Leave blank for the latest version">
                      <TextInput id="ts-ver" value={version} onChange={(e) => setVersion(e.target.value)} placeholder="latest" />
                    </Field>
                  )}

                  {/* A deployment has a concrete URL; a model is addressed by
                      name, so its row spells out the real path rather than
                      leaving the template placeholder. "Type" drops out when
                      the registry has no model_type instead of rendering a
                      dead dash. */}
                  <KeyValue
                    items={tab === 'deployment'
                      ? [
                          ['Endpoint', dep?.endpoint_url],
                          ['Model', dep?.model_name],
                          ['Version', dep?.version],
                          ['Environment', dep?.environment],
                          ['Status', dep?.status],
                        ]
                      : [
                          ['Endpoint', mdl?.name ? `POST /api/models/${mdl.name}/predict` : 'POST /api/models/{name}/predict'],
                          ['Model', mdl?.name],
                          mdl?.model_type ? ['Type', mdl.model_type] : null,
                          ['Registry accuracy', mdl?.accuracy != null ? formatAccuracy(mdl.accuracy) : undefined],
                        ]}
                  />
                </>
              )}
            </div>
          </Panel>

          <Panel
            icon={Zap}
            title="Result"
            subtitle={report ? `scored at ${report.at.toLocaleTimeString()}` : error ? 'Run failed' : 'Awaiting a run'}
            actions={
              report?.metrics
                ? <Badge tone={report.metrics.accuracy >= 0.85 ? 'success' : 'warning'} dot>
                    {formatAccuracy(report.metrics.accuracy)}
                  </Badge>
                : error ? <Badge tone="danger" dot>error</Badge> : <Badge tone="outline">idle</Badge>
            }
          >
            {error && <div className="mb-3"><Alert tone="error">{error}</Alert></div>}

            {!report && !error && (
              <EmptyState
                icon={Zap}
                title="No test run yet"
                desc="Pick a sample and a target, then press Run test — one batched request goes out and the response is scored against the target column."
              />
            )}

            {report && !report.metrics && !error && (
              <EmptyState icon={Zap} title="No predictions returned" desc="The endpoint responded without a usable prediction list." />
            )}

            {report?.metrics && (
              <>
                <div className="metrics-grid cols-3" style={{ marginBottom: 'var(--sp-4)' }}>
                  <div className="metric-tile t-accent">
                    <div className="metric-tile-lbl">Accuracy</div>
                    <div className="metric-tile-val">{formatAccuracy(report.metrics.accuracy)}</div>
                  </div>
                  <div className="metric-tile">
                    <div className="metric-tile-lbl">Precision</div>
                    <div className="metric-tile-val">{formatAccuracy(report.metrics.precision)}</div>
                  </div>
                  <div className="metric-tile">
                    <div className="metric-tile-lbl">Recall</div>
                    <div className="metric-tile-val">{formatAccuracy(report.metrics.recall)}</div>
                  </div>
                  <div className="metric-tile t-success">
                    {/* Labelled to match JobResults' tiles. "F1 (weighted)" and
                        "Server latency" both wrapped to a second line in a
                        121px tile, making this row taller than the one above —
                        the weighting is spelled out in "How this works". */}
                    <div className="metric-tile-lbl">F1 Score</div>
                    <div className="metric-tile-val">{formatAccuracy(report.metrics.f1)}</div>
                  </div>
                  <div className="metric-tile">
                    <div className="metric-tile-lbl">Latency</div>
                    <div className="metric-tile-val">
                      {report.res?.latency_ms != null ? `${Number(report.res.latency_ms).toFixed(1)}ms` : '—'}
                    </div>
                  </div>
                  {/* Kept to a bare duration: appending "· N rows" wrapped onto
                      a second line, leaving this tile row taller than the one
                      above it. The row count rides in the badge row below. */}
                  <div className="metric-tile">
                    <div className="metric-tile-lbl">Wall time</div>
                    <div className="metric-tile-val">{report.wallMs}ms</div>
                  </div>
                </div>

                <div className="pill-row" style={{ marginBottom: 'var(--sp-3)' }}>
                  <Badge tone="outline">target · {target}</Badge>
                  {report.res?.model_name && <Badge tone="accent">{report.res.model_name}</Badge>}
                  {report.res?.model_version && <Badge tone="outline">v{report.res.model_version}</Badge>}
                  <Badge tone="outline">{report.rows} row{report.rows === 1 ? '' : 's'} scored</Badge>
                  <Badge tone="outline">dataset · {dataset}</Badge>
                  <Badge tone="outline">label · {targetCol}</Badge>
                </div>

                <div className="chart-panel" style={{ marginBottom: 'var(--sp-4)' }}>
                  <div className="chart-header">
                    <div>
                      <div className="chart-title">Confusion matrix</div>
                      <div className="chart-sub">Rows are actual labels, columns are predictions</div>
                    </div>
                    <div className="chart-legend">
                      <span className="legend-item"><span className="legend-dot" style={{ background: 'var(--accent)' }} /> correct</span>
                      <span className="legend-item"><span className="legend-dot" style={{ background: 'var(--danger)' }} /> wrong</span>
                    </div>
                  </div>
                  <Matrix metrics={report.metrics} />
                </div>

                <div className="section-title">Per-class scores (support-weighted)</div>
                <DataTable
                  caption="Per-class metrics"
                  keyOf={(r) => r.label}
                  rows={report.metrics.perClass}
                  pageSize={Math.max(5, Math.min(report.metrics.perClass.length, 10))}
                  columns={[
                    { key: 'label', strong: true, header: 'Class', render: (r) => <span className="mono text-xs">{r.label}</span> },
                    { key: 'support', header: 'Support', render: (r) => <span className="tnum">{r.support}</span> },
                    { key: 'precision', header: 'Precision', render: (r) => <span className="tnum">{formatAccuracy(r.precision)}</span> },
                    { key: 'recall', header: 'Recall', render: (r) => <span className="tnum">{formatAccuracy(r.recall)}</span> },
                    { key: 'f1', header: 'F1', render: (r) => <span className="tnum">{formatAccuracy(r.f1)}</span> },
                  ]}
                />

                <div className="mt-4">
                  {report.metrics.mismatches.length === 0 ? (
                    <div className="flex items-center gap-2" style={{ color: 'var(--success-text)' }}>
                      <CheckCircle2 size={15} />
                      <span className="text-sm">Every row matched its expected label.</span>
                    </div>
                  ) : (
                    <>
                      <div className="section-title flex items-center gap-2">
                        <XCircle size={13} /> {report.metrics.mismatches.length} disagreement
                        {report.metrics.mismatches.length === 1 ? '' : 's'}
                      </div>
                      <DataTable
                        caption="Rows the model scored incorrectly"
                        keyOf={(r) => `${r.index}-${r.predicted}`}
                        rows={report.metrics.mismatches}
                        pageSize={8}
                        columns={[
                          { key: 'index', header: '#', width: 56, render: (r) => <span className="tnum muted">{r.index + 1}</span> },
                          { key: 'expected', strong: true, header: 'Expected', render: (r) => <span className="mono text-xs">{String(r.expected)}</span> },
                          { key: 'predicted', header: 'Predicted', render: (r) => <span className="mono text-xs" style={{ color: 'var(--danger-text)' }}>{String(r.predicted)}</span> },
                        ]}
                      />
                    </>
                  )}
                </div>
              </>
            )}
          </Panel>

          <Panel icon={Info} title="How this works" subtitle="What the run actually does with your selection">
            <div className="flex flex-col gap-3 text-sm muted">
              <div>
                The first <strong>{sampleSize}</strong> usable rows of <code className="mono">{path || '—'}</code> are
                read (capped at {Math.round(READ_BYTES / 1024)} KB), the <code className="mono">{targetCol || 'target'}</code> column
                is removed, and the remainder is sent as <code className="mono">{'{ dataframe_records: [...] }'}</code> in a
                single request — both predict endpoints allow 60 requests/minute, so batching is required, not optional.
              </div>
              <div>
                Metric weighting matches the trainer so the numbers are comparable with a job record:{' '}
                <code className="mono">average="weighted"</code>, <code className="mono">zero_division=0</code>,
                with the confusion matrix ordered by sorted unique labels.
              </div>
              <div>
                Inference is served by a live RayService endpoint. If the cluster is offline the request returns 503 —
                the message below the form will say so.
              </div>
            </div>
          </Panel>
        </div>
      </div>
    </div>
  )
}
