import { lazy, Suspense, useState, useRef, useCallback, useEffect, useMemo } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  datasetsApi, mlOpsApi,
} from '../api/endpoints.js'
import { useFetch } from '../hooks/useFetch.js'
import {
  AVAILABLE_ALGORITHMS, toRepoName, formatAccuracy, fmtUnixDate,
} from '../lib/models.js'
import {
  PageHeader, Panel, Button, Field, TextInput, TextArea, Badge,
  Alert, Progress, EmptyState, LoadingState, ErrorState, Segmented,
  KeyValue, useToast, useConfirm, StatCard, CopyButton,
} from '../components/ui/index.jsx'
import { TrainingConsole } from './JobPanels.jsx'
import { useAuth } from '../auth/AuthContext.jsx'
import {
  Database, GitBranch, BrainCircuit, Play, BarChart3, Upload, Check,
  ChevronRight, ChevronLeft, RefreshCw, Rocket, ClipboardList, Sparkles,
  Code2, FileText, Target, Layers, Zap, GitCommitHorizontal,
  Tag, Boxes, RotateCcw, Save, Copy,
} from '../components/icons.jsx'

// recharts lives in JobResults — defer it until the Results stage renders.
const JobResults = lazy(() => import('./JobResults.jsx'))

/* ══════════════════════════════════════════════════════
   Step model
   ══════════════════════════════════════════════════════ */
const STEPS = [
  { id: 0, label: 'Dataset',        desc: 'Choose or upload training data', icon: Database },
  { id: 1, label: 'Version',        desc: 'Pin the ref to train against',   icon: GitBranch },
  { id: 2, label: 'Model',          desc: 'Algorithm & hyperparameters',     icon: BrainCircuit },
  { id: 3, label: 'Train',          desc: 'Submit and watch the run',        icon: Play },
  { id: 4, label: 'Results',        desc: 'Metrics & visual analytics',      icon: BarChart3 },
]

// The backend contract (ray_wrapper.py) is:
//     instance = <first class with a callable .train>()   # no-arg __init__
//     model    = instance.train(data_path, epochs, **hyperparameters)
//     metrics  = calculate_metrics(model, data_path, target_col=None)
// so the template must expose a zero-arg constructor and a train() that takes
// a CSV *path* (not a DataFrame) and returns a fitted estimator.
const DEFAULT_CODE = `# Custom training entrypoint.
#
# The server loads this module, picks the first class exposing a callable
# \`train\` method, then executes:
#
#     model = Trainer().train(data_path, epochs, **hyperparameters)
#     metrics = calculate_metrics(model, data_path, target_col=None)
#
# - data_path is a local path to a CSV file (return a fitted estimator).
# - Hyperparameters arrive as **kwargs; unknown keys must be filtered out.
# - Metrics are computed by the backend: the target column is auto-detected
#   as "label", "target", or the last column of the CSV.
import pandas as pd
from sklearn.ensemble import RandomForestClassifier


class Trainer:
    def train(self, data_path: str, epochs: int = 10, **kwargs):
        df = pd.read_csv(data_path)
        target = next((c for c in ("label", "target") if c in df.columns), df.columns[-1])
        y = df[target]
        X = df.drop(columns=[target])

        allowed = {
            k: v for k, v in kwargs.items()
            if k in ("n_estimators", "max_depth", "min_samples_split", "min_samples_leaf")
        }
        # \`epochs\` maps onto forest size so the setting is never a no-op.
        allowed.setdefault("n_estimators", int(epochs) or 10)
        model = RandomForestClassifier(random_state=42, n_jobs=-1, **allowed)
        model.fit(X, y)
        return model
`

/* ══════════════════════════════════════════════════════
   Stepper
   ══════════════════════════════════════════════════════ */
function Stepper({ step, maxReached, onJump, state }) {
  return (
    <div className="stepper" role="tablist" aria-label="Pipeline stages">
      {STEPS.map((s) => {
        const st = state(s.id)
        const reachable = s.id <= maxReached
        return (
          <button
            key={s.id}
            type="button"
            role="tab"
            aria-selected={step === s.id}
            disabled={!reachable}
            className={`step-item ${step === s.id ? 'is-active' : ''} ${st === 'done' ? 'is-done' : ''}`}
            onClick={() => reachable && onJump(s.id)}
          >
            <span className="step-circle">
              {st === 'done' ? <Check size={14} /> : <s.icon size={14} />}
            </span>
            <span className="step-body">
              <span className="step-label">{s.label}</span>
              <span className="step-desc">{s.desc}</span>
            </span>
          </button>
        )
      })}
    </div>
  )
}

/* ══════════════════════════════════════════════════════
   Step 0 — Dataset
   ══════════════════════════════════════════════════════ */
function DatasetStep({ datasets, loading, error, selected, onSelect, onUploaded, canUpload }) {
  const toast = useToast()
  const fileRef = useRef(null)
  const [drag, setDrag] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [uploadErr, setUploadErr] = useState(null)
  const [filter, setFilter] = useState('')

  const visible = useMemo(
    () => datasets.filter((d) => (d.name || '').toLowerCase().includes(filter.toLowerCase())),
    [datasets, filter],
  )

  async function uploadFile(file) {
    if (!file) return
    if (!/\.csv$/i.test(file.name)) {
      setUploadErr(`Only .csv files are supported for training. Got "${file.name}".`)
      return
    }
    const repoName = toRepoName(file.name)
    const body = new FormData()
    body.append('name', repoName)
    body.append('description', `Uploaded via pipeline · ${file.name}`)
    body.append('file', file)
    setUploadErr(null)
    setUploading(true)
    try {
      const res = await datasetsApi.register(body)
      toast.success('Dataset registered', `${res.name || repoName} created in lakeFS.`)
      const fresh = await onUploaded()
      const created = (fresh || []).find((d) => d.name === res.name || d.name === repoName)
      if (created) onSelect(created)
    } catch (err) {
      setUploadErr(err?.message || 'Upload failed.')
      toast.error('Upload failed', err?.message)
    } finally {
      setUploading(false)
    }
  }

  if (loading) return <LoadingState label="Loading datasets…" />
  if (error) return <ErrorState error={error} title="Could not load datasets" onRetry={onUploaded} />

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--sp-4)' }}>
      <Panel
        icon={Upload}
        title="Upload a new dataset"
        subtitle="CSV files are registered into lakeFS and versioned as a repository"
        actions={uploading ? <Badge tone="warning" dot>Uploading…</Badge> : null}
      >
        <div
          className={`file-drop ${drag ? 'is-drag' : ''}`}
          onClick={() => fileRef.current?.click()}
          onKeyDown={(e) => {
            if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); fileRef.current?.click() }
          }}
          onDragOver={(e) => { e.preventDefault(); setDrag(true) }}
          onDragLeave={() => setDrag(false)}
          onDrop={(e) => { e.preventDefault(); setDrag(false); uploadFile(e.dataTransfer.files?.[0]) }}
          role="button"
          tabIndex={0}
        >
          <span className="file-drop-icon"><Upload size={19} /></span>
          <span className="file-drop-title">Drop a CSV here, or click to browse</span>
          <span className="file-drop-sub">
            {canUpload ? 'Max 100 MB · registered as a versioned lakeFS repository' : 'Your role cannot upload datasets'}
          </span>
          <span className="pill-row" style={{ marginTop: 6, justifyContent: 'center' }}>
            <span className="format-chip">csv</span>
            <span className="format-chip">header row</span>
            <span className="format-chip">numeric + categorical</span>
          </span>
        </div>
        <input
          ref={fileRef}
          type="file"
          accept=".csv"
          hidden
          onChange={(e) => { uploadFile(e.target.files?.[0]); e.target.value = '' }}
        />
        {uploadErr && <div className="mt-3"><Alert tone="error">{uploadErr}</Alert></div>}
        {!canUpload && <div className="mt-3"><Alert tone="warning">You need the <strong>datasets:upload</strong> scope to add data.</Alert></div>}
      </Panel>

      <Panel
        icon={Database}
        title="Or pick an existing dataset"
        subtitle={`${datasets.length} dataset${datasets.length === 1 ? '' : 's'} available`}
        actions={<TextInput placeholder="Filter…" value={filter} onChange={(e) => setFilter(e.target.value)} style={{ width: 200 }} />}
        bodyClass={visible.length ? 'tight' : ''}
      >
        {visible.length === 0 ? (
          <EmptyState
            icon={Database}
            title={datasets.length ? 'No match' : 'No datasets yet'}
            desc={datasets.length ? 'Nothing matches that filter.' : 'Upload a CSV above to get started.'}
          />
        ) : (
          <div style={{ padding: 'var(--sp-4) var(--sp-5)' }}>
            <div className="row-list">
              {visible.map((d) => {
                const isSel = selected?.name === d.name
                return (
                  <button
                    key={d.name}
                    type="button"
                    className="row-item"
                    onClick={() => onSelect(d)}
                    style={{
                      cursor: 'pointer', textAlign: 'left', width: '100%',
                      borderColor: isSel ? 'var(--accent)' : undefined,
                      background: isSel ? 'var(--accent-softer)' : undefined,
                      boxShadow: isSel ? 'var(--sh-focus)' : undefined,
                    }}
                  >
                    <span className={`row-glyph ${isSel ? 'is-accent' : ''}`}>
                      {isSel ? <Check size={15} /> : <Database size={15} />}
                    </span>
                    <span className="row-info">
                      <span className="row-title">{d.name}</span>
                      <span className="row-sub">
                        {d.default_branch || 'main'} branch
                        {d.description ? ` · ${d.description}` : ''}
                      </span>
                    </span>
                    {isSel && <Badge tone="accent">Selected</Badge>}
                  </button>
                )
              })}
            </div>
          </div>
        )}
      </Panel>
    </div>
  )
}

/* ══════════════════════════════════════════════════════
   Step 1 — Version / ref
   ══════════════════════════════════════════════════════ */
function VersionStep({ dataset, ref, onRefChange }) {
  const name = dataset?.name
  const branches = useFetch(() => datasetsApi.branches(name), [name], { enabled: !!name })
  const tags = useFetch(() => datasetsApi.tags(name), [name], { enabled: !!name })
  const commits = useFetch(() => datasetsApi.commits(name, ref), [name, ref], { enabled: !!name })

  const branchList = Array.isArray(branches.data) ? branches.data : branches.data?.branches || []
  const tagList = Array.isArray(tags.data) ? tags.data : tags.data?.tags || []
  const commitList = Array.isArray(commits.data) ? commits.data : commits.data?.commits || []

  if (!name) {
    return <EmptyState icon={GitBranch} title="Pick a dataset first" desc="A dataset is required before choosing a version ref." />
  }

  const options = [
    ...branchList.map((b) => ({ kind: 'branch', value: b.name, meta: b.head_commit_id })),
    ...tagList.map((t) => ({ kind: 'tag', value: t.name, meta: t.commit_id })),
    ...commitList.slice(0, 12).map((c) => ({ kind: 'commit', value: c.id, meta: c.message })),
  ]

  const loading = branches.loading || tags.loading

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--sp-4)' }}>
      <Panel
        icon={GitBranch}
        title="Training ref"
        subtitle={`Branch, tag or commit of ${name} to read the data from`}
        actions={<Badge tone="outline">{ref || 'main'}</Badge>}
      >
        {loading ? (
          <LoadingState label="Loading refs…" />
        ) : (
          <>
            <div className="pill-row" style={{ gap: 8 }}>
              {options.length === 0 && <span className="muted">No branches, tags or commits found (is lakeFS up?).</span>}
              {options.map((o) => (
                <button
                  key={`${o.kind}:${o.value}`}
                  type="button"
                  className={`ref-chip ${ref === o.value ? 'is-selected' : ''}`}
                  onClick={() => onRefChange(o.value)}
                  title={o.meta || o.value}
                >
                  {o.kind === 'branch' ? <GitBranch size={12} /> : o.kind === 'tag' ? <Tag size={12} /> : <GitCommitHorizontal size={12} />}
                  {o.kind === 'commit' ? String(o.value).slice(0, 10) : o.value}
                </button>
              ))}
            </div>

            <div className="mt-4">
              <Field label="Or type an explicit ref" hint="Any branch, tag name, or full/short commit id accepted by lakeFS.">
                <TextInput value={ref} onChange={(e) => onRefChange(e.target.value)} placeholder="main" />
              </Field>
            </div>
          </>
        )}
      </Panel>

      <Panel icon={GitCommitHorizontal} title="Recent commits" subtitle={`On ref "${ref || 'main'}"`} bodyClass={commitList.length ? 'tight' : ''}>
        {commits.loading ? (
          <LoadingState label="Loading commits…" />
        ) : commitList.length === 0 ? (
          <EmptyState icon={GitCommitHorizontal} title="No commits" desc="Commit your data to create a version." />
        ) : (
          <div style={{ padding: 'var(--sp-4) var(--sp-5)' }}>
            <div className="row-list">
              {commitList.slice(0, 8).map((c) => (
                <div key={c.id} className="row-item">
                  <span className="row-glyph"><GitCommitHorizontal size={15} /></span>
                  <span className="row-info">
                    <span className="row-title">{c.message || '(no message)'}</span>
                    <span className="row-sub">
                      {String(c.id).slice(0, 10)} · {c.committer || 'unknown'} ·{' '}
                      {fmtUnixDate(c.creation_date)}
                    </span>
                  </span>
                  <CopyButton text={c.id} />
                </div>
              ))}
            </div>
          </div>
        )}
      </Panel>
    </div>
  )
}

/* ══════════════════════════════════════════════════════
   Step 2 — Model & hyperparameters
   ══════════════════════════════════════════════════════ */
function ModelStep({
  mode, onModeChange, modelId, onModelChange, hyperparams, onHyperparams,
  meta, onMeta, supported, supportedLoading, code, onCode, epochs, onEpochs,
}) {
  const catalogue = useMemo(() => {
    const supportedNames = supported?.models?.map((m) => m.name) || null
    const optional = new Set(supported?.models?.filter((m) => m.optional_dependency).map((m) => m.name) || [])
    let list = AVAILABLE_ALGORITHMS
    if (supportedNames) list = list.filter((a) => supportedNames.includes(a.id))
    return list.map((a) => ({ ...a, optional: optional.has(a.id) }))
  }, [supported])

  const selected = AVAILABLE_ALGORITHMS.find((a) => a.id === modelId) || null

  function setParam(k, v) {
    onHyperparams({ ...hyperparams, [k]: v })
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--sp-4)' }}>
      <Panel
        icon={Sparkles}
        title="Training mode"
        subtitle="Automated sklearn pipeline, or bring your own training class"
      >
        <Segmented
          value={mode}
          onChange={onModeChange}
          options={[
            { value: 'pipeline', label: 'Automated pipeline', icon: Zap },
            { value: 'custom', label: 'Custom training code', icon: Code2 },
          ]}
        />
        <div className="mt-3">
          {mode === 'pipeline' ? (
            <Alert tone="info">
              Sends <code className="mono">POST /models/train-pipeline</code>. The backend selects the
              estimator, splits the data, and reports metrics back through the job record.
            </Alert>
          ) : (
            <Alert tone="warning">
              Sends <code className="mono">POST /models/train</code> with your Python source. The class is
              executed server-side — only run code you trust.
            </Alert>
          )}
        </div>
      </Panel>

      {mode === 'pipeline' && (
        <>
          <Panel
            icon={BrainCircuit}
            title="Algorithm"
            subtitle={supportedLoading ? 'Loading canonical list from GET /models/supported…' : `${catalogue.length} algorithms accepted by the API`}
            actions={selected ? <Badge tone="accent">{selected.name}</Badge> : null}
          >
            {supportedLoading ? (
              <LoadingState label="Loading supported models…" />
            ) : (
              <div className="model-grid">
                {catalogue.map((a) => (
                  <button
                    key={a.id}
                    type="button"
                    className={`model-card is-multi ${modelId === a.id ? 'is-selected' : ''}`}
                    onClick={() => onModelChange(a.id)}
                  >
                    <div className="model-card-top">
                      <span className="model-glyph" style={{ background: 'var(--accent-soft)' }}>
                        <Layers size={16} />
                      </span>
                      {a.optional && <Badge tone="warning" sm>optional dep</Badge>}
                    </div>
                    <div className="model-name">{a.name}</div>
                    <div className="model-type">{a.type}</div>
                    <div className="model-desc">{a.desc}</div>
                    <div className="model-stats">
                      <div className="model-stat">
                        <div className="model-stat-val">{a.stats?.speed || '—'}</div>
                        <div className="model-stat-lbl">Speed</div>
                      </div>
                      <div className="model-stat">
                        <div className="model-stat-val">{a.stats?.memory || '—'}</div>
                        <div className="model-stat-lbl">Memory</div>
                      </div>
                    </div>
                  </button>
                ))}
              </div>
            )}
            {!supportedLoading && catalogue.length === 0 && (
              <EmptyState icon={BrainCircuit} title="No algorithms" desc="GET /models/supported returned an empty list." />
            )}
          </Panel>

          <Panel
            icon={Target}
            title="Hyperparameters"
            subtitle={selected ? `Defaults seeded from the ${selected.name} preset` : 'Select an algorithm to edit its parameters'}
          >
            {!selected ? (
              <EmptyState icon={Target} title="Pick an algorithm" desc="Hyperparameter fields appear once an algorithm is selected." />
            ) : Object.keys(hyperparams).length === 0 ? (
              <EmptyState icon={Target} title="No tunable parameters" desc={`${selected.name} uses estimator defaults.`} />
            ) : (
              <div className="hyperparam-grid">
                {Object.entries(hyperparams).map(([k, v]) => (
                  <Field
                    key={k}
                    label={k}
                    htmlFor={`hp-${k}`}
                    hint={typeof v === 'number' ? 'numeric' : typeof v === 'boolean' ? 'boolean' : 'string'}
                  >
                    <TextInput
                      id={`hp-${k}`}
                      value={String(v)}
                      inputMode={typeof v === 'number' ? 'decimal' : undefined}
                      onChange={(e) => {
                        const raw = e.target.value
                        setParam(k, typeof v === 'number'
                          ? (raw === '' ? '' : Number(raw))
                          : raw)
                      }}
                    />
                  </Field>
                ))}
              </div>
            )}
          </Panel>
        </>
      )}

      {mode === 'custom' && (
        <>
          <Panel icon={Code2} title="Training code" subtitle="Python source defining the Trainer class" actions={<Badge tone="outline">server-side execution</Badge>}>
            <TextArea
              value={code}
              onChange={(e) => onCode(e.target.value)}
              rows={16}
              spellCheck={false}
              aria-label="Custom training code"
            />
            <div className="mt-3 flex items-center gap-2">
              <Button variant="ghost" size="sm" icon={RotateCcw} onClick={() => onCode(DEFAULT_CODE)}>Restore template</Button>
              <Button variant="ghost" size="sm" icon={Copy} onClick={() => navigator.clipboard?.writeText(code)}>Copy code</Button>
              <span className="muted text-xs">{code.split('\n').length} lines</span>
            </div>
          </Panel>

          <Panel icon={Layers} title="Run settings" subtitle="Applies to the custom-code path">
            <div className="field-row field-row-3">
              <Field label="Epochs" htmlFor="epochs">
                <TextInput id="epochs" type="number" min={1} max={1000} value={epochs} onChange={(e) => onEpochs(e.target.value)} />
              </Field>
              <Field label="Hyperparameters JSON" hint='e.g. {"n_estimators": 200}'>
                <TextInput
                  value={typeof hyperparams.__json === 'string' ? hyperparams.__json : JSON.stringify(hyperparams)}
                  onChange={(e) => {
                    let parsed = {}
                    try { parsed = e.target.value.trim() ? JSON.parse(e.target.value) : {} } catch { /* live typing */ }
                    onHyperparams({ ...parsed, __json: e.target.value })
                  }}
                  placeholder='{"n_estimators": 200}'
                />
              </Field>
              <div />
            </div>
          </Panel>
        </>
      )}

      <Panel icon={Save} title="Run identity" subtitle="Optional names recorded in MLflow">
        <div className="field-row field-row-3">
          <Field label="Experiment name" htmlFor="exp-name" hint="Created if it does not exist">
            <TextInput id="exp-name" value={meta.experiment_name} onChange={(e) => onMeta({ ...meta, experiment_name: e.target.value })} placeholder="capstone-experiments" />
          </Field>
          <Field label="Registered model name" htmlFor="model-name" hint="Leave blank to derive from the algorithm">
            <TextInput id="model-name" value={meta.model_name} onChange={(e) => onMeta({ ...meta, model_name: e.target.value })} placeholder="auto" />
          </Field>
          <Field label="Target column" htmlFor="target-col" hint="Used by the automated pipeline">
            <TextInput id="target-col" value={meta.target_column} onChange={(e) => onMeta({ ...meta, target_column: e.target.value })} placeholder="label" />
          </Field>
        </div>
      </Panel>
    </div>
  )
}

/* ══════════════════════════════════════════════════════
   Page root
   ══════════════════════════════════════════════════════ */
export default function Pipeline() {
  const navigate = useNavigate()
  const toast = useToast()
  const confirm = useConfirm()
  const { hasPermission } = useAuth()

  const [step, setStep] = useState(0)
  const [maxReached, setMaxReached] = useState(0)

  const [datasets, setDatasets] = useState([])
  const [dsLoading, setDsLoading] = useState(true)
  const [dsError, setDsError] = useState(null)
  const [selectedDs, setSelectedDs] = useState(null)
  const [ref, setRef] = useState('main')

  const [mode, setMode] = useState('pipeline')
  const [modelId, setModelId] = useState('')
  const [hyperparams, setHyperparams] = useState({})
  const [meta, setMeta] = useState({ experiment_name: '', model_name: '', target_column: 'label' })
  const [code, setCode] = useState(DEFAULT_CODE)
  const [epochs, setEpochs] = useState(10)

  const supported = useFetch(() => mlOpsApi.supportedModels(), [])
  const registered = useFetch(() => mlOpsApi.listModels(), [])

  const [job, setJob] = useState(null)        // { job_id, status }
  const [progress, setProgress] = useState(0)
  const [logs, setLogs] = useState([])
  const [results, setResults] = useState(null)
  const [submitting, setSubmitting] = useState(false)
  const [submitError, setSubmitError] = useState(null)

  const pollRef = useRef(null)
  const pollFails = useRef(0)

  const canRun = hasPermission('models:train')
  const canUpload = hasPermission('datasets:upload')

  /* ── datasets ─────────────────────────────────── */
  const loadDatasets = useCallback(async () => {
    setDsLoading(true)
    setDsError(null)
    try {
      const data = await datasetsApi.list()
      const list = Array.isArray(data) ? data : data?.datasets || []
      setDatasets(list)
      return list
    } catch (err) {
      setDsError(err)
      return []
    } finally {
      setDsLoading(false)
    }
  }, [])

  useEffect(() => { loadDatasets() }, [loadDatasets])

  /* ── seed hyperparams when the model changes ──── */
  useEffect(() => {
    const preset = AVAILABLE_ALGORITHMS.find((a) => a.id === modelId)
    setHyperparams({ ...(preset?.params || {}) })
  }, [modelId])

  /* ── log helper ───────────────────────────────── */
  const addLog = useCallback((level, msg) => {
    const time = new Date().toLocaleTimeString('en', { hour12: false })
    setLogs((p) => [...p, { level, msg, time }])
  }, [])

  /* ── job polling ──────────────────────────────── */
  const stopPolling = useCallback(() => {
    if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null }
  }, [])

  const pollJob = useCallback((jobId) => {
    stopPolling()
    pollFails.current = 0
    pollRef.current = setInterval(async () => {
      let j
      try {
        j = await mlOpsApi.getJob(jobId)
        pollFails.current = 0
      } catch (err) {
        pollFails.current += 1
        if (err?.status === 404 || pollFails.current >= 15) {
          stopPolling()
          setSubmitting(false)
          addLog('error', `Training failed: ${err?.message || 'Job not found.'}`)
        }
        return
      }
      setProgress(j.progress || 0)
      if (j.status === 'completed') {
        stopPolling()
        setSubmitting(false)
        setProgress(100)
        setResults(j)
        setJob({ ...j, status: 'completed' })
        addLog('success', 'Training complete — model registered in MLflow.')
        toast.success('Training complete', 'Metrics are ready in the Results step.')
        registered.refetch()
        setStep(4); setMaxReached((m) => Math.max(m, 4))
      } else if (j.status === 'failed') {
        stopPolling()
        setSubmitting(false)
        addLog('error', `Training failed: ${j.error_message || 'Unknown error'}`)
        toast.error('Training failed', j.error_message || 'See the console for details.')
      } else {
        setJob((prev) => ({ ...(prev || {}), ...j }))
      }
    }, 2000)
  }, [addLog, registered, stopPolling, toast])

  useEffect(() => () => stopPolling(), [stopPolling])

  /* ── validation ───────────────────────────────── */
  const validation = useMemo(() => {
    if (step === 0 && !selectedDs) return 'Select or upload a dataset to continue.'
    if (step === 2 && mode === 'pipeline' && !modelId) return 'Choose an algorithm to continue.'
    if (step === 2 && mode === 'custom' && !code.trim()) return 'Provide custom training code to continue.'
    return null
  }, [step, selectedDs, mode, modelId, code])

  const stepState = (id) => {
    if (id === 0) return selectedDs ? 'done' : step === 0 ? 'active' : 'todo'
    if (id === 1) return selectedDs && ref ? 'done' : step === 1 ? 'active' : 'todo'
    if (id === 2) return modelId || mode === 'custom' ? 'done' : step === 2 ? 'active' : 'todo'
    if (id === 3) return job?.status === 'completed' ? 'done' : step === 3 ? 'active' : 'todo'
    if (id === 4) return results ? 'done' : step === 4 ? 'active' : 'todo'
    return 'todo'
  }

  /* ── submit ───────────────────────────────────── */
  async function submitRun() {
    if (!selectedDs) return
    setSubmitting(true)
    setSubmitError(null)
    setLogs([])
    setProgress(0)
    setResults(null)
    setJob(null)

    const cleanParams = () => {
      const out = {}
      for (const [k, v] of Object.entries(hyperparams)) {
        if (k === '__json') continue
        if (v === '' || v === null || v === undefined) continue
        if (typeof v === 'string') {
          const t = v.trim()
          if (t === '') continue
          out[k] = /^-?\d+(\.\d+)?$/.test(t) ? (t.includes('.') ? parseFloat(t) : parseInt(t, 10)) : t
        } else {
          out[k] = v
        }
      }
      return out
    }

    const common = {
      dataset_id: selectedDs.name,
      ref: ref || 'main',
      hyperparameters: cleanParams(),
      experiment_name: meta.experiment_name || undefined,
      model_name: meta.model_name || undefined,
    }

    try {
      addLog('info', `Dataset: ${common.dataset_id} @ ${common.ref}`)
      addLog('info', `Mode: ${mode === 'pipeline' ? 'automated pipeline' : 'custom training code'}`)

      let res
      if (mode === 'pipeline') {
        addLog('info', `Algorithm: ${modelId} · target column: ${meta.target_column || 'label'}`)
        addLog('info', 'Submitting to POST /models/train-pipeline…')
        res = await mlOpsApi.trainPipeline({
          ...common,
          target_column: meta.target_column || 'label',
          model_type: modelId,
        })
      } else {
        addLog('info', `Epochs: ${epochs}`)
        addLog('info', 'Submitting to POST /models/train…')
        res = await mlOpsApi.train({
          ...common,
          epochs: Number(epochs) || 10,
          code,
        })
      }

      addLog('info', `Job accepted: ${res.job_id} (${res.status})`)
      setJob({ job_id: res.job_id, status: res.status })
      setStep(3); setMaxReached((m) => Math.max(m, 3))
      pollJob(res.job_id)
    } catch (err) {
      const msg = err?.message || 'Failed to submit the training job.'
      addLog('error', msg)
      setSubmitError(msg)
      setSubmitting(false)
      toast.error('Submission failed', msg)
    }
  }

  async function resetAll() {
    const ok = await confirm({
      title: 'Reset the pipeline?',
      text: 'This clears the selected dataset, model, hyperparameters and any run output.',
      confirmLabel: 'Reset',
      tone: 'warning',
    })
    if (!ok) return
    stopPolling()
    setStep(0); setMaxReached(0)
    setSelectedDs(null); setRef('main')
    setModelId(''); setHyperparams({})
    setMeta({ experiment_name: '', model_name: '', target_column: 'label' })
    setJob(null); setResults(null); setLogs([]); setProgress(0)
    setSubmitError(null); setSubmitting(false)
  }

  const goNext = () => {
    if (validation) { toast.warning('Not ready', validation); return }
    const next = Math.min(step + 1, 4)
    setStep(next); setMaxReached((m) => Math.max(m, next))
  }
  const goBack = () => setStep((s) => Math.max(0, s - 1))

  const activeStep = STEPS[step]

  return (
    <div className="page">
      <PageHeader
        icon={activeStep.icon}
        title={`Pipeline — ${activeStep.label}`}
        subtitle={activeStep.desc}
        actions={
          <>
            <Button variant="ghost" size="sm" icon={RotateCcw} onClick={resetAll}>Reset</Button>
            {step < 4 && (
              <Button
                variant="primary"
                size="sm"
                icon={step === 3 ? Play : ChevronRight}
                onClick={step === 3 ? submitRun : goNext}
                disabled={!canRun || (step === 3 && submitting) || (!!validation && step !== 3)}
              >
                {step === 3 ? (submitting ? 'Training…' : 'Start training') : 'Continue'}
              </Button>
            )}
          </>
        }
      />

      {!canRun && (
        <Alert tone="warning">
          Your role does not have the <strong>models:train</strong> scope, so this pipeline is read-only.
        </Alert>
      )}
      {submitError && <Alert tone="error" actions={<Button size="xs" variant="secondary" onClick={() => setSubmitError(null)}>Dismiss</Button>}>{submitError}</Alert>}

      <Stepper step={step} maxReached={maxReached} onJump={setStep} state={stepState} />

      {/* ── Step 0 ───────────────────────────────── */}
      {step === 0 && (
        <DatasetStep
          datasets={datasets}
          loading={dsLoading}
          error={dsError}
          selected={selectedDs}
          canUpload={canUpload}
          onSelect={(d) => { setSelectedDs(d); setRef(d.default_branch || 'main') }}
          onUploaded={async () => { const list = await loadDatasets(); return list }}
        />
      )}

      {/* ── Step 1 ───────────────────────────────── */}
      {step === 1 && (
        <VersionStep dataset={selectedDs} ref={ref} onRefChange={setRef} />
      )}

      {/* ── Step 2 ───────────────────────────────── */}
      {step === 2 && (
        <ModelStep
          mode={mode} onModeChange={setMode}
          modelId={modelId} onModelChange={setModelId}
          hyperparams={hyperparams} onHyperparams={setHyperparams}
          meta={meta} onMeta={setMeta}
          supported={supported.data} supportedLoading={supported.loading}
          code={code} onCode={setCode}
          epochs={epochs} onEpochs={setEpochs}
        />
      )}

      {/* ── Step 3 ───────────────────────────────── */}
      {step === 3 && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--sp-4)' }}>
          <div className="stat-grid">
            <StatCard icon={Database} tone="accent" value={selectedDs?.name || '—'} label="Dataset" sub={`ref · ${ref}`} />
            <StatCard
              icon={BrainCircuit} tone="info"
              value={mode === 'pipeline' ? (AVAILABLE_ALGORITHMS.find((a) => a.id === modelId)?.name || '—') : 'Custom code'}
              label="Algorithm" sub={mode === 'pipeline' ? modelId : `${epochs} epochs`}
            />
            <StatCard icon={Target} tone="purple" value={meta.target_column || 'label'} label="Target column" sub={mode === 'pipeline' ? 'automated split' : 'provided by code'} />
            <StatCard
              icon={Zap} tone={submitting ? 'warning' : results ? 'success' : 'accent'}
              value={job ? `${progress}%` : 'Idle'} label="Pipeline status"
              sub={job?.job_id ? `job ${String(job.job_id).slice(0, 8)}` : 'not submitted'}
            />
          </div>

          <Panel icon={FileText} title="Request preview" subtitle="Exact payload sent to the API" actions={job?.job_id ? <Badge tone="success" dot>submitted</Badge> : <Badge tone="outline">draft</Badge>}>
            <div className="code-block">{JSON.stringify(
              mode === 'pipeline'
                ? { dataset_id: selectedDs?.name, ref, target_column: meta.target_column || 'label', model_type: modelId, hyperparameters: hyperparams, experiment_name: meta.experiment_name || null, model_name: meta.model_name || null }
                : { dataset_id: selectedDs?.name, ref, epochs: Number(epochs) || 10, hyperparameters: hyperparams, code: `# ${code.split('\n').length} lines omitted`, experiment_name: meta.experiment_name || null, model_name: meta.model_name || null },
              null, 2,
            )}</div>
            <div className="mt-3 flex items-center justify-between">
              <span className="muted text-xs">
                Endpoint · <span className="mono">{mode === 'pipeline' ? 'POST /models/train-pipeline' : 'POST /models/train'}</span>
              </span>
              <span className="muted text-xs">Rate limit · 2 requests / minute</span>
            </div>
          </Panel>

          <TrainingConsole logs={logs} isTraining={submitting} empty="Press “Start training” to submit the job and stream logs." />

          {progress > 0 && (
            <Panel icon={RefreshCw} title="Progress" subtitle={submitting ? 'Polling GET /jobs/{job_id} every 2s' : 'Run finished'}>
              <Progress value={progress} label="Overall" />
            </Panel>
          )}
        </div>
      )}

      {/* ── Step 4 ───────────────────────────────── */}
      {step === 4 && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--sp-4)' }}>
          {!results ? (
            <Panel icon={BarChart3} title="Results" subtitle="Evaluation output">
              <EmptyState
                icon={BarChart3}
                title="No results yet"
                desc="Submit the training job to generate metrics, curves and a confusion matrix."
                actions={<Button variant="primary" icon={ChevronLeft} onClick={() => setStep(3)}>Back to Train</Button>}
              />
            </Panel>
          ) : (
            <>
              <Panel
                icon={Check}
                iconTone="success"
                title="Run complete"
                subtitle={results.job_id ? `Job ${results.job_id}` : ''}
                actions={
                  <>
                    <Button variant="secondary" size="sm" icon={ClipboardList} onClick={() => navigate('/jobs')}>All jobs</Button>
                    <Button variant="secondary" size="sm" icon={Boxes} onClick={() => navigate('/registry')}>Registry</Button>
                    {hasPermission('models:deploy') && (
                      <Button variant="primary" size="sm" icon={Rocket} onClick={() => navigate('/deployments')}>Deploy</Button>
                    )}
                  </>
                }
              >
                <div className="metrics-grid">
                  {[
                    ['Accuracy', formatAccuracy(results.accuracy)],
                    ['Precision', results.precision_score != null ? results.precision_score.toFixed(3) : '—'],
                    ['Recall', results.recall_score != null ? results.recall_score.toFixed(3) : '—'],
                    ['F1 Score', results.f1_score != null ? results.f1_score.toFixed(3) : '—'],
                    ['Duration', results.training_duration != null ? `${Number(results.training_duration).toFixed(1)}s` : '—'],
                    ['Status', results.status || 'completed'],
                  ].map(([l, v]) => (
                    <div key={l} className="metric-tile t-accent">
                      <div className="metric-tile-lbl">{l}</div>
                      <div className="metric-tile-val">{v}</div>
                    </div>
                  ))}
                </div>
                <div className="mt-4 quick-actions">
                  <Button variant="ghost" size="sm" icon={RotateCcw} onClick={resetAll}>New run</Button>
                  <Button variant="ghost" size="sm" icon={ChevronLeft} onClick={() => setStep(2)}>Tweak hyperparameters</Button>
                </div>
              </Panel>

              <Suspense fallback={<LoadingState label="Loading results…" />}>
                <JobResults results={results} jobId={results.job_id} />
              </Suspense>
            </>
          )}
        </div>
      )}

      {/* ── Nav footer ───────────────────────────── */}
      <div className="flex items-center justify-between gap-3" style={{ paddingTop: 4 }}>
        <Button variant="secondary" size="sm" icon={ChevronLeft} onClick={goBack} disabled={step === 0}>
          Back
        </Button>
        {validation && <span className="muted text-xs">{validation}</span>}
        {step < 4 && (
          <Button
            variant="primary"
            size="sm"
            icon={step === 3 ? Play : ChevronRight}
            onClick={step === 3 ? submitRun : goNext}
            disabled={!canRun || (step === 3 && submitting) || (!!validation && step !== 3)}
          >
            {step === 3 ? (submitting ? 'Training…' : 'Start training') : 'Continue'}
          </Button>
        )}
      </div>

      {step === 4 && registered.data && (
        <Panel icon={Layers} title="Registered models" subtitle={`${registered.data.models?.length || 0} in MLflow registry`} bodyClass="tight">
          <KeyValue items={(registered.data.models || []).slice(0, 6).map((m) => [m.name, m.accuracy != null ? formatAccuracy(m.accuracy) : '—'])} />
        </Panel>
      )}
    </div>
  )
}
