import { useState, useRef } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { mlOpsApi } from '../api/endpoints.js'
import { useFetch, useDebounced } from '../hooks/useFetch.js'
import { formatAccuracy, resolveModelPreset } from '../lib/models.js'
import { useAuth } from '../auth/AuthContext.jsx'
import {
  PageHeader, Panel, Button, DataTable, Modal, Field, TextInput,
  Badge, StatusBadge, EmptyState, LoadingState, ErrorState, SearchInput,
  StatCard, Alert, KeyValue, CopyButton, Drawer, useToast,
} from '../components/ui/index.jsx'
import {
  Boxes, Upload, RefreshCw, Rocket, Search, GitBranch, Target,
  Activity, Layers, ExternalLink, Play,
} from '../components/icons.jsx'

export default function Registry() {
  const navigate = useNavigate()
  const { hasPermission } = useAuth()
  const canTrain = hasPermission('models:train')
  const canDeploy = hasPermission('models:deploy')

  const [params, setParams] = useSearchParams()
  const [query, setQuery] = useState('')
  const [showUpload, setShowUpload] = useState(false)
  const [deployTarget, setDeployTarget] = useState(null) // { name, version }
  const debounced = useDebounced(query, 200)

  const models = useFetch(() => mlOpsApi.listModels(), [])
  const list = models.data?.models || []

  // Deep link ?model=<name> opens the drawer straight from the dashboard.
  const openName = params.get('model')
  const selected = list.find((m) => m.name === openName) || null

  const versions = useFetch(
    () => (selected ? mlOpsApi.modelVersions(selected.name) : Promise.resolve(null)),
    [selected?.name],
    { enabled: !!selected },
  )

  const rows = list.filter((m) =>
    !debounced
    || String(m.name).toLowerCase().includes(debounced.toLowerCase())
    || String(m.experiment_name || '').toLowerCase().includes(debounced.toLowerCase()),
  )

  const avgAcc = list.length && list.some((m) => m.accuracy != null)
    ? list.reduce((n, m) => n + (m.accuracy || 0), 0) / list.filter((m) => m.accuracy != null).length
    : null

  function open(m) { setParams({ model: m.name }) }
  function close() { setParams({}) }

  return (
    <div className="page">
      <PageHeader
        icon={Boxes}
        title="Model registry"
        subtitle="MLflow registered models, versions, uploads and deployments"
        actions={
          <>
            {canTrain && <Button variant="secondary" size="sm" icon={Upload} onClick={() => setShowUpload(true)}>Upload .pkl</Button>}
            <Button variant="secondary" size="sm" icon={RefreshCw} onClick={models.refetch} loading={models.loading}>Refresh</Button>
            <Button variant="primary" size="sm" icon={Play} onClick={() => navigate('/pipeline')}>Train new model</Button>
          </>
        }
      />

      <div className="stat-grid">
        <StatCard icon={Boxes} tone="accent" value={list.length} label="Registered models" sub="MLflow registry" />
        <StatCard
          icon={Target} tone="success"
          value={avgAcc != null ? formatAccuracy(avgAcc) : '—'}
          label="Mean accuracy" sub="across all models"
        />
        <StatCard
          icon={GitBranch} tone="info"
          value={list.filter((m) => m.experiment_name && m.experiment_name !== 'unknown').length}
          label="Models from runs" sub="models with a source run"
        />
        <StatCard
          icon={Layers} tone="purple"
          value={new Set(list.map((m) => m.experiment_name).filter((n) => n && n !== 'unknown')).size}
          label="Experiments used" sub="distinct training origins"
        />
      </div>

      <Panel
        icon={Search}
        title="Registry"
        subtitle={`${rows.length} of ${list.length} models`}
        actions={<SearchInput value={query} onChange={setQuery} placeholder="Search models…" />}
        bodyClass={rows.length ? 'tight' : ''}
      >
        {models.loading ? (
          <LoadingState label="Loading models…" />
        ) : models.error ? (
          <ErrorState error={models.error} title="Could not load the registry" onRetry={models.refetch} />
        ) : rows.length === 0 ? (
          <EmptyState
            icon={Boxes}
            title={list.length ? 'No match' : 'Registry is empty'}
            desc={list.length ? 'Nothing matches your search.' : 'Train a pipeline or upload a .pkl to register your first model.'}
            actions={!list.length && canTrain ? (
              <>
                <Button variant="primary" icon={Play} onClick={() => navigate('/pipeline')}>Run pipeline</Button>
                <Button variant="secondary" icon={Upload} onClick={() => setShowUpload(true)}>Upload .pkl</Button>
              </>
            ) : null}
          />
        ) : (
          <DataTable
            caption="Registered models"
            keyOf={(m) => m.id || m.name}
            rows={rows}
            pageSize={10}
            onRowClick={open}
            columns={[
              {
                key: 'name', strong: true, sortKey: 'name', header: 'Model',
                render: (m) => (
                  <span className="flex items-center gap-2">
                    <span className="row-glyph is-accent sm"><Boxes size={13} /></span>
                    <span className="truncate" style={{ maxWidth: 220 }}>{m.name}</span>
                  </span>
                ),
              },
              {
                key: 'experiment_name', header: 'Experiment',
                render: (m) => m.experiment_name && m.experiment_name !== 'unknown'
                  ? <Badge tone="outline">{m.experiment_name}</Badge>
                  : <span className="muted">uploaded</span>,
              },
              {
                key: 'precision', width: 110, header: 'Precision',
                render: (m) => <span className="tnum">{m.precision ? Number(m.precision).toFixed(3) : '—'}</span>,
              },
              {
                key: 'recall', width: 100, header: 'Recall',
                render: (m) => <span className="tnum">{m.recall ? Number(m.recall).toFixed(3) : '—'}</span>,
              },
              {
                key: 'accuracy', width: 120, sortKey: 'accuracy', header: 'Accuracy',
                render: (m) => <span className="tnum">{formatAccuracy(m.accuracy)}</span>,
              },
              {
                key: 'f1_score', width: 100, header: 'F1',
                render: (m) => <span className="tnum">{m.f1_score != null ? Number(m.f1_score).toFixed(3) : '—'}</span>,
              },
              {
                key: 'created_at', width: 170, header: 'Registered',
                render: (m) => (
                  <span className="text-xs muted">
                    {m.created_at && m.created_at !== 'unknown' ? new Date(m.created_at).toLocaleString() : '—'}
                  </span>
                ),
              },
              {
                key: 'actions', actions: true, width: 130, header: <span className="sr-only">Actions</span>,
                render: (m) => (
                  <span className="flex items-center" style={{ justifyContent: 'flex-end' }}>
                    {canDeploy && (
                      <Button variant="ghost" size="xs" icon={Rocket} onClick={() => setDeployTarget({ name: m.name, version: 'latest' })}>
                        Deploy
                      </Button>
                    )}
                    <Button variant="ghost" size="xs" icon={ExternalLink} onClick={() => open(m)}>Open</Button>
                  </span>
                ),
              },
            ]}
          />
        )}
      </Panel>

      {/* ── Model detail drawer ─────────────────── */}
      <ModelDrawer
        model={selected}
        versions={versions}
        onClose={close}
        canDeploy={canDeploy}
        onDeploy={(v) => setDeployTarget({ name: selected.name, version: v })}
        onGoExperiments={() => navigate('/experiments')}
      />

      {showUpload && (
        <UploadModal onClose={() => setShowUpload(false)} onUploaded={() => { setShowUpload(false); models.refetch() }} />
      )}

      {deployTarget && (
        <DeployModal
          target={deployTarget}
          onClose={() => setDeployTarget(null)}
          onDone={() => { setDeployTarget(null); navigate('/deployments') }}
        />
      )}
    </div>
  )
}

/* ── Detail drawer ──────────────────────────────── */
function ModelDrawer({ model, versions, onClose, canDeploy, onDeploy, onGoExperiments }) {
  const data = versions.data
  const versionList = data?.versions || []
  const preset = model ? resolveModelPreset(model.name) : null

  return (
    <Drawer
      open={!!model}
      wide
      onClose={onClose}
      title={model?.name || ''}
      subtitle={model?.experiment_name ? `Experiment · ${model.experiment_name}` : 'Custom / uploaded model'}
      footer={
        <>
          <Button variant="secondary" onClick={onClose}>Close</Button>
          <Button variant="secondary" icon={Play} onClick={onGoExperiments}>Experiments</Button>
          {canDeploy && (
            <Button variant="primary" icon={Rocket} onClick={() => onDeploy('latest')}>Deploy latest</Button>
          )}
        </>
      }
    >
      {!model ? null : (
        <div className="flex flex-col gap-4">
          <div className="stat-grid is-3">
            <div className="metric-tile t-accent">
              <div className="metric-tile-lbl">Accuracy</div>
              <div className="metric-tile-val">{formatAccuracy(model.accuracy)}</div>
            </div>
            <div className="metric-tile">
              <div className="metric-tile-lbl">F1 Score</div>
              <div className="metric-tile-val">{model.f1_score != null ? Number(model.f1_score).toFixed(3) : '—'}</div>
            </div>
            <div className="metric-tile t-success">
              <div className="metric-tile-lbl">Versions</div>
              <div className="metric-tile-val">{versionList.length || model.versions?.length || 1}</div>
            </div>
          </div>

          {preset && (
            <Alert tone="info">
              Matched pipeline preset <strong>{preset.name}</strong> · {preset.type} · {preset.desc}
            </Alert>
          )}

          <Panel icon={GitBranch} title="Versions" subtitle="Stages, aliases and per-version metrics" bodyClass={versionList.length ? 'tight' : ''}>
            {versions.loading ? (
              <LoadingState label="Loading versions…" />
            ) : versions.error ? (
              <ErrorState error={versions.error} onRetry={versions.refetch} />
            ) : versionList.length === 0 ? (
              <EmptyState icon={GitBranch} title="No versions" desc={data?.error || 'This model has no recorded versions.'} />
            ) : (
              <DataTable
                keyOf={(v) => String(v.version)}
                rows={versionList}
                columns={[
                  { key: 'version', strong: true, header: 'Version', render: (v) => <Badge tone="accent">v{v.version}</Badge> },
                  { key: 'stage', header: 'Stage', render: (v) => <Badge tone="outline">{v.stage || '—'}</Badge> },
                  {
                    key: 'aliases', header: 'Aliases',
                    render: (v) => (v.aliases || []).length
                      ? <span className="pill-row">{v.aliases.map((a) => <Badge key={a} tone="info" sm>{a}</Badge>)}</span>
                      : <span className="muted">—</span>,
                  },
                  { key: 'status', header: 'Status', render: (v) => <StatusBadge status={v.status} sm /> },
                  {
                    key: 'metrics', header: 'Metrics',
                    render: (v) => (
                      <span className="mono text-xs">
                        acc {v.metrics?.accuracy != null ? Number(v.metrics.accuracy).toFixed(3) : '—'} ·
                        f1 {v.metrics?.f1_score != null ? Number(v.metrics.f1_score).toFixed(3) : '—'}
                      </span>
                    ),
                  },
                  {
                    key: 'actions', actions: true, header: <span className="sr-only">Actions</span>,
                    render: (v) => canDeploy
                      ? <Button variant="ghost" size="xs" icon={Rocket} onClick={() => onDeploy(String(v.version))}>Deploy</Button>
                      : null,
                  },
                ]}
              />
            )}
          </Panel>

          {model.parameters && Object.keys(model.parameters).length > 0 && (
            <Panel icon={Target} title="Parameters" subtitle="Hyperparameters recorded at training time">
              <KeyValue items={Object.entries(model.parameters).map(([k, v]) => [k, String(v).slice(0, 80)])} />
            </Panel>
          )}

          <Panel icon={Activity} title="Record" subtitle="Raw registry metadata">
            <KeyValue
              items={[
                ['Model ID', model.id],
                ['Name', model.name],
                ['Experiment', model.experiment_name],
                ['Algorithm', model.algorithm || model.model_type],
                ['Created', model.created_at && model.created_at !== 'unknown' ? new Date(model.created_at).toLocaleString() : undefined],
                ['Run ID', model.run_id],
              ]}
            />
            <div className="mt-3 flex items-center gap-2">
              <code className="mono text-xs muted truncate">GET /models/{model.name}/versions</code>
              <CopyButton text={`/models/${model.name}/versions`} />
            </div>
          </Panel>
        </div>
      )}
    </Drawer>
  )
}

/* ── Upload .pkl ────────────────────────────────── */
function UploadModal({ onClose, onUploaded }) {
  const toast = useToast()
  const fileRef = useRef(null)
  const [file, setFile] = useState(null)
  const [drag, setDrag] = useState(false)
  const [form, setForm] = useState({ model_name: '', experiment_name: '', metadata: '', metrics: '' })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  async function submit(e) {
    e.preventDefault()
    if (!file) { setError('Choose a .pkl file.'); return }
    // Validate optional JSON before hitting the API.
    for (const k of ['metadata', 'metrics']) {
      if (form[k].trim()) {
        try { JSON.parse(form[k]) } catch { setError(`${k} must be valid JSON.`); return }
      }
    }
    setBusy(true)
    setError(null)
    const body = new FormData()
    body.append('file', file)
    for (const [k, v] of Object.entries(form)) if (v.trim()) body.append(k, v.trim())
    try {
      const res = await mlOpsApi.uploadModel(body)
      toast.success('Model uploaded', `${res.model_name} (v${res.version || '?'}) registered.`)
      onUploaded()
    } catch (err) {
      setError(err?.message || 'Upload failed.')
      toast.error('Upload failed', err?.message)
    } finally { setBusy(false) }
  }

  // Drag-and-drop must land in the same state as the hidden input, otherwise
  // the form submits with no file and the user only sees a generic error.
  const pick = (f) => {
    if (!f) return
    if (!f.name.toLowerCase().endsWith('.pkl')) {
      const msg = `Only .pkl files can be registered — got “${f.name}”.`
      setError(msg)
      toast.error('Unsupported file', msg)
      return
    }
    setError(null)
    setFile(f)
  }

  return (
    <Modal
      open
      onClose={onClose}
      title="Upload a .pkl model"
      subtitle="Registers an existing scikit-learn pickle into the MLflow registry"
      footer={
        <>
          <Button variant="secondary" onClick={onClose}>Cancel</Button>
          <Button variant="primary" icon={Upload} loading={busy} onClick={submit}>Upload &amp; register</Button>
        </>
      }
    >
      <form onSubmit={submit} className="flex flex-col gap-4">
        {error && <Alert tone="error">{error}</Alert>}

        <div
          className={`file-drop ${drag ? 'is-drag' : ''}`}
          onClick={() => fileRef.current?.click()}
          onKeyDown={(e) => {
            if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); fileRef.current?.click() }
          }}
          onDragOver={(e) => { e.preventDefault(); setDrag(true) }}
          onDragLeave={() => setDrag(false)}
          onDrop={(e) => { e.preventDefault(); setDrag(false); pick(e.dataTransfer.files?.[0]) }}
          role="button"
          tabIndex={0}
        >
          <span className="file-drop-icon"><Upload size={19} /></span>
          <span className="file-drop-title">{file ? file.name : 'Choose a .pkl file'}</span>
          <span className="file-drop-sub">{file ? `${(file.size / 1024).toFixed(1)} KB` : 'Pickle files only'}</span>
        </div>
        <input ref={fileRef} type="file" accept=".pkl" hidden onChange={(e) => { setFile(e.target.files?.[0]); e.target.value = '' }} />

        <div className="field-row field-row-2">
          <Field label="Model name" htmlFor="up-model" hint="Defaults to the filename">
            <TextInput id="up-model" value={form.model_name} onChange={(e) => setForm({ ...form, model_name: e.target.value })} />
          </Field>
          <Field label="Experiment" htmlFor="up-exp">
            <TextInput id="up-exp" value={form.experiment_name} onChange={(e) => setForm({ ...form, experiment_name: e.target.value })} placeholder="uploaded-models" />
          </Field>
        </div>

        <div className="field-row field-row-2">
          <Field label="Metadata JSON" htmlFor="up-meta" hint='e.g. {"owner":"team"}'>
            <TextInput id="up-meta" value={form.metadata} onChange={(e) => setForm({ ...form, metadata: e.target.value })} placeholder="{}" />
          </Field>
          <Field label="Metrics JSON" htmlFor="up-metrics" hint='e.g. {"accuracy":0.92}'>
            <TextInput id="up-metrics" value={form.metrics} onChange={(e) => setForm({ ...form, metrics: e.target.value })} placeholder="{}" />
          </Field>
        </div>

        <button type="submit" hidden />
      </form>
    </Modal>
  )
}

/* ── Deploy ─────────────────────────────────────── */
function DeployModal({ target, onClose, onDone }) {
  const toast = useToast()
  const [form, setForm] = useState({ environment: 'staging', version: target.version || 'latest', replicas: 1 })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  async function submit(e) {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      const res = await mlOpsApi.deploy({
        model_id: target.name,
        environment: form.environment,
        version: form.version || 'latest',
        replicas: Number(form.replicas) || 1,
      })
      toast.success('Deployment created', `${res.model_id} v${res.version} → ${res.environment}`)
      onDone()
    } catch (err) {
      setError(err?.message || 'Deploy failed.')
      toast.error('Deploy failed', err?.message)
    } finally { setBusy(false) }
  }

  return (
    <Modal
      open
      onClose={onClose}
      title={`Deploy ${target.name}`}
      subtitle="Creates a KubeRay RayService tracked in MLflow"
      footer={
        <>
          <Button variant="secondary" onClick={onClose}>Cancel</Button>
          <Button variant="primary" icon={Rocket} loading={busy} onClick={submit}>Deploy</Button>
        </>
      }
    >
      <form onSubmit={submit} className="flex flex-col gap-4">
        {error && <Alert tone="error">{error}</Alert>}

        <div className="field-row field-row-3">
          <Field label="Version" htmlFor="dp-ver">
            <TextInput id="dp-ver" value={form.version} onChange={(e) => setForm({ ...form, version: e.target.value })} />
          </Field>
          <Field label="Environment" htmlFor="dp-env">
            <select id="dp-env" className="select-field" value={form.environment} onChange={(e) => setForm({ ...form, environment: e.target.value })}>
              <option value="staging">staging</option>
              <option value="production">production</option>
            </select>
          </Field>
          <Field label="Replicas" htmlFor="dp-rep" hint="1–32">
            <TextInput id="dp-rep" type="number" min={1} max={32} value={form.replicas} onChange={(e) => setForm({ ...form, replicas: e.target.value })} />
          </Field>
        </div>

        <Alert tone="info">
          POST /models/deploy · rate limit 5 requests / minute. The deployment appears in
          <strong> Deployments</strong> as soon as MLflow records it.
        </Alert>

        <button type="submit" hidden />
      </form>
    </Modal>
  )
}
