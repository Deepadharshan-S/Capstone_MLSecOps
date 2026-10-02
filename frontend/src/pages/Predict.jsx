import { useState, useEffect } from 'react'
import { useSearchParams } from 'react-router-dom'
import { mlOpsApi } from '../api/endpoints.js'
import { useFetch } from '../hooks/useFetch.js'
import {
  PageHeader, Panel, Button, Field, TextInput, TextArea, Badge,
  EmptyState, LoadingState, Alert, Segmented, KeyValue,
  useToast, Select,
} from '../components/ui/index.jsx'
import {
  Terminal, Play, Rocket, Boxes, Send, History, Zap, Info, Check,
  AlertCircle, Layers, Target,
} from '../components/icons.jsx'

const SAMPLE = '[\n  {"feature1": 1.0, "feature2": 0.42}\n]'

export default function Predict() {
  const [searchParams] = useSearchParams()
  const toast = useToast()

  const [tab, setTab] = useState(searchParams.get('model') ? 'model' : 'deployment')
  const [target, setTarget] = useState(
    searchParams.get('deployment') || searchParams.get('model') || '',
  )
  const [version, setVersion] = useState('')
  const [payload, setPayload] = useState(SAMPLE)
  const [result, setResult] = useState(null)
  const [error, setError] = useState(null)
  const [running, setRunning] = useState(false)
  const [history, setHistory] = useState([])

  const models = useFetch(() => mlOpsApi.listModels(), [])
  const deployments = useFetch(() => mlOpsApi.listDeployments({ active_only: true }), [])
  const supported = useFetch(() => mlOpsApi.supportedModels(), [])

  const modelList = models.data?.models || []
  const depList = deployments.data?.deployments || []

  // Keep the target valid when the query param isn't in either list yet.
  useEffect(() => {
    if (!target && modelList.length) setTarget(modelList[0].name)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [modelList.length, depList.length])

  async function run() {
    setError(null)
    setResult(null)

    let parsed
    try {
      parsed = JSON.parse(payload)
    } catch (err) {
      setError(`Payload is not valid JSON: ${err.message}`)
      return
    }
    if (!target) { setError('Select a target first.'); return }

    // PredictionRequestSchema requires an envelope — it accepts either
    // { dataframe_records: [ {...}, ... ] } or { inputs: [[...], ...] }.
    let envelope = parsed
    if (Array.isArray(parsed)) envelope = { dataframe_records: parsed }
    else if (parsed && typeof parsed === 'object' && !('dataframe_records' in parsed) && !('inputs' in parsed)) {
      envelope = { dataframe_records: [parsed] }
    } else if (parsed && typeof parsed === 'object' && Array.isArray(parsed.dataframe_records)) {
      envelope = parsed
    } else {
      setError("Payload must be an object, an array of objects, or use 'dataframe_records' / 'inputs'.")
      return
    }

    setRunning(true)
    const startedAt = Date.now()
    try {
      const res = tab === 'deployment'
        ? await mlOpsApi.predictDeployment(target, envelope)
        : await mlOpsApi.predictModel(target, envelope, version || undefined)
      setResult(res)
      const entry = {
        id: `${startedAt}`,
        tab, target, version,
        ms: Date.now() - startedAt,
        at: new Date(),
        ok: true,
        preview: JSON.stringify(res).slice(0, 160),
      }
      setHistory((h) => [entry, ...h].slice(0, 12))
      toast.success('Prediction returned', `${target} · ${entry.ms}ms`)
    } catch (err) {
      setError(err?.message || 'Prediction failed.')
      setHistory((h) => [{
        id: `${startedAt}`, tab, target, ms: Date.now() - startedAt,
        at: new Date(), ok: false, preview: err?.message || 'error',
      }, ...h].slice(0, 12))
      toast.error('Prediction failed', err?.message)
    } finally { setRunning(false) }
  }

  const options = tab === 'model' ? modelList : depList

  return (
    <div className="page">
      <PageHeader
        icon={Terminal}
        title="Predict"
        subtitle="Send a JSON payload to a registered model or a live deployment"
        actions={
          <>
            <Badge tone="outline"><Layers size={11} /> {supported.data?.models?.length || '—'} supported algorithms</Badge>
            <Button variant="primary" size="sm" icon={Send} loading={running} onClick={run}>Send request</Button>
          </>
        }
      />

      <div className="flex items-center gap-3" style={{ flexWrap: 'wrap' }}>
        <Segmented
          value={tab}
          onChange={(v) => { setTab(v); setTarget(''); setResult(null); setError(null) }}
          options={[
            { value: 'deployment', label: 'Deployment', icon: Rocket },
            { value: 'model', label: 'Registry model', icon: Boxes },
          ]}
        />
        <span className="muted text-xs">
          <code className="mono">{tab === 'deployment' ? 'POST /deployments/{id}/predict' : 'POST /models/{name}/predict'}</code>
        </span>
      </div>

      <div className="grid-2">
        {/* ── Request ────────────────────────────── */}
        <div className="flex flex-col gap-4">
          <Panel icon={Target} title="Target" subtitle={tab === 'deployment' ? 'Live RayService endpoint' : 'Model version from the registry'}>
            {(tab === 'deployment' ? deployments.loading : models.loading) ? (
              <LoadingState label="Loading targets…" />
            ) : options.length === 0 ? (
              <EmptyState
                icon={Rocket}
                title={tab === 'deployment' ? 'No active deployments' : 'No registered models'}
                desc={tab === 'deployment'
                  ? 'Deploy a model version first — only active deployments can serve predictions.'
                  : 'Train or upload a model to make it available here.'}
              />
            ) : (
              <div className="flex flex-col gap-4">
                <Field label={tab === 'deployment' ? 'Deployment' : 'Model'} htmlFor="pr-target">
                  <Select id="pr-target" value={target} onChange={(e) => setTarget(e.target.value)}>
                    <option value="">Select…</option>
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
                  <Field label="Version (optional)" htmlFor="pr-ver" hint="Leave blank for the latest version">
                    <TextInput id="pr-ver" value={version} onChange={(e) => setVersion(e.target.value)} placeholder="latest" />
                  </Field>
                )}

                {tab === 'deployment' && target && (() => {
                  const d = depList.find((x) => x.deployment_id === target)
                  return d ? (
                    <KeyValue
                      items={[
                        ['Model', d.model_name],
                        ['Version', d.version],
                        ['Environment', d.environment],
                        ['Endpoint', d.endpoint_url],
                        ['Status', d.status],
                      ]}
                    />
                  ) : null
                })()}
              </div>
            )}
          </Panel>

          <Panel
            icon={Send}
            title="Request payload"
            subtitle="JSON array of feature objects, or a single object"
            actions={
              <div className="flex items-center gap-2">
                <Button variant="ghost" size="xs" onClick={() => setPayload(SAMPLE)}>Sample</Button>
                <Button variant="ghost" size="xs" onClick={() => {
                  try { setPayload(JSON.stringify(JSON.parse(payload), null, 2)) } catch { /* ignore */ }
                }}>Format</Button>
              </div>
            }
          >
            <TextArea
              value={payload}
              onChange={(e) => setPayload(e.target.value)}
              rows={12}
              spellCheck={false}
              aria-label="Request payload"
            />
            <div className="mt-3 flex items-center justify-between">
              <span className="muted text-xs">Parsed rows · {safeCount(payload)}</span>
              <Button variant="primary" size="sm" icon={Play} loading={running} onClick={run} disabled={!target}>
                Send request
              </Button>
            </div>
          </Panel>
        </div>

        {/* ── Response ───────────────────────────── */}
        <div className="flex flex-col gap-4">
          <Panel
            icon={Zap}
            title="Response"
            subtitle={result ? '200 OK' : error ? 'Request failed' : 'Awaiting a request'}
            actions={result ? <Badge tone="success" dot>success</Badge> : error ? <Badge tone="danger" dot>error</Badge> : <Badge tone="outline">idle</Badge>}
          >
            {error && <div className="mb-3"><Alert tone="error">{error}</Alert></div>}
            {!result && !error && (
              <EmptyState
                icon={Terminal}
                title="No response yet"
                desc="Choose a target, edit the payload and press Send request."
              />
            )}
            {result && (
              <>
                <div className="metrics-grid" style={{ marginBottom: 14 }}>
                  <div className="metric-tile t-success">
                    <div className="metric-tile-lbl">Status</div>
                    <div className="metric-tile-val">200</div>
                  </div>
                  <div className="metric-tile t-accent">
                    <div className="metric-tile-lbl">Predictions</div>
                    <div className="metric-tile-val">{countResults(result)}</div>
                  </div>
                  <div className="metric-tile">
                    <div className="metric-tile-lbl">Server latency</div>
                    <div className="metric-tile-val">
                      {result?.latency_ms != null ? `${Number(result.latency_ms).toFixed(1)}ms` : '—'}
                    </div>
                  </div>
                </div>
                {result?.model_name && (
                  <div className="pill-row" style={{ marginBottom: 12 }}>
                    <Badge tone="outline">model · {result.model_name}</Badge>
                    <Badge tone="accent">v{result.model_version || '?'}</Badge>
                  </div>
                )}
                <pre className="code-block" style={{ maxHeight: 380, overflow: 'auto' }}>
                  {JSON.stringify(result, null, 2)}
                </pre>
              </>
            )}
          </Panel>

          <Panel
            icon={History}
            title="Request history"
            subtitle={`${history.length} request${history.length === 1 ? '' : 's'} this session`}
            bodyClass={history.length ? 'tight' : ''}
          >
            {history.length === 0 ? (
              <EmptyState icon={History} title="Nothing yet" desc="Your recent inference requests will be listed here." />
            ) : (
              <div style={{ padding: 'var(--sp-4) var(--sp-5)' }}>
                <div className="row-list">
                  {history.map((h) => (
                    <div key={h.id} className="row-item">
                      <span className={`row-glyph ${h.ok ? 'is-success' : 'is-danger'}`}>
                        {h.ok ? <Check size={14} /> : <AlertCircle size={14} />}
                      </span>
                      <span className="row-info">
                        <span className="row-title">{h.target}</span>
                        <span className="row-sub">
                          {h.tab} · {h.ms}ms · {h.at.toLocaleTimeString()}
                        </span>
                        <span className="row-sub mono truncate" title={h.preview}>{h.preview}</span>
                      </span>
                      <Badge tone={h.ok ? 'success' : 'danger'} sm>{h.ok ? '200' : 'ERR'}</Badge>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </Panel>
        </div>
      </div>

      <Panel icon={Info} title="Payload formats" subtitle="What you type is wrapped automatically for the API">
        <div className="mb-3">
          <Alert tone="info">
            Both prediction endpoints expect an envelope object. A bare object is
            sent as <code className="mono">{'{ dataframe_records: [obj] }'}</code> and a bare array as
            <code className="mono">{'{ dataframe_records: [...] }'}</code> — you can also write the
            envelope yourself using <code className="mono">dataframe_records</code> or
            <code className="mono">inputs</code>.
          </Alert>
        </div>
        <div className="grid-2">
          <div>
            <div className="section-title">Single record</div>
            <pre className="code-block">{`{
  "feature1": 1.0,
  "feature2": 0.42
}`}</pre>
          </div>
          <div>
            <div className="section-title">Batch records</div>
            <pre className="code-block">{`[
  { "feature1": 1.0, "feature2": 0.42 },
  { "feature1": 0.3, "feature2": 0.87 }
]`}</pre>
          </div>
        </div>
      </Panel>
    </div>
  )
}

function safeCount(text) {
  try {
    const v = JSON.parse(text)
    return Array.isArray(v) ? v.length : 1
  } catch { return '—' }
}

function countResults(res) {
  if (Array.isArray(res)) return res.length
  if (Array.isArray(res?.predictions)) return res.predictions.length
  if (Array.isArray(res?.results)) return res.results.length
  return 1
}
