import { useState, useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import { datasetsApi } from '../api/endpoints.js'
import { useFetch, useDebounced } from '../hooks/useFetch.js'
import { toRepoName } from '../lib/models.js'
import { useAuth } from '../auth/AuthContext.jsx'
import {
  PageHeader, Panel, Button, DataTable, Field, TextInput, TextArea, Modal,
  SearchInput, EmptyState, LoadingState, ErrorState, Badge, Alert,
  useToast, useConfirm,
} from '../components/ui/index.jsx'
import {
  Database, Upload, RefreshCw, Plus, Trash2, ArrowRight, GitBranch,
  FileSpreadsheet, HardDrive, FolderTree, Info,
} from '../components/icons.jsx'

export default function Datasets() {
  const navigate = useNavigate()
  const toast = useToast()
  const confirm = useConfirm()
  const { hasPermission } = useAuth()
  const canUpload = hasPermission('datasets:upload')
  const canDelete = hasPermission('datasets:delete')

  const list = useFetch(() => datasetsApi.list(), [])
  const [query, setQuery] = useState('')
  const [showCreate, setShowCreate] = useState(false)
  const [deleting, setDeleting] = useState(null)

  const debounced = useDebounced(query, 200)
  const all = Array.isArray(list.data) ? list.data : list.data?.datasets || []

  const rows = all.filter((d) => {
    const q = debounced.toLowerCase()
    return !q || (d.name || '').toLowerCase().includes(q) || (d.description || '').toLowerCase().includes(q)
  })

  async function handleDelete(d) {
    const ok = await confirm({
      title: `Delete "${d.name}"?`,
      text: 'This removes the lakeFS repository and the database record. Every branch, tag and commit is destroyed and cannot be recovered.',
      confirmLabel: 'Delete dataset',
      tone: 'danger',
    })
    if (!ok) return
    setDeleting(d.name)
    try {
      await datasetsApi.remove(d.name)
      toast.success('Dataset deleted', `${d.name} removed.`)
      list.refetch()
    } catch (err) {
      toast.error('Delete failed', err?.message)
    } finally {
      setDeleting(null)
    }
  }

  return (
    <div className="page">
      <PageHeader
        icon={Database}
        title="Datasets"
        subtitle="lakeFS-backed versioned datasets — branches, tags, diffs and rollback"
        actions={
          <>
            <Button variant="secondary" size="sm" icon={RefreshCw} onClick={list.refetch} loading={list.loading}>
              Refresh
            </Button>
            {canUpload && (
              <Button variant="primary" size="sm" icon={Plus} onClick={() => setShowCreate(true)}>
                New dataset
              </Button>
            )}
          </>
        }
      />

      <div className="stat-grid">
        <div className="stat-card">
          <div className="stat-icon t-accent"><Database size={17} /></div>
          <div className="stat-body">
            <div className="stat-value">{all.length}</div>
            <div className="stat-label">Datasets</div>
            <div className="stat-sub">registered repositories</div>
          </div>
        </div>
        <div className="stat-card">
          <div className="stat-icon t-info"><FileSpreadsheet size={17} /></div>
          <div className="stat-body">
            <div className="stat-value">{all.filter((d) => d.description).length}</div>
            <div className="stat-label">Documented</div>
            <div className="stat-sub">with a description</div>
          </div>
        </div>
        <div className="stat-card">
          <div className="stat-icon t-purple"><GitBranch size={17} /></div>
          <div className="stat-body">
            <div className="stat-value">{all.filter((d) => (d.default_branch || 'main') !== 'main').length}</div>
            <div className="stat-label">Custom default branch</div>
            <div className="stat-sub">not the main branch</div>
          </div>
        </div>
        <div className="stat-card">
          <div className="stat-icon t-success"><HardDrive size={17} /></div>
          <div className="stat-body">
            <div className="stat-value">lakeFS</div>
            <div className="stat-label">Storage backend</div>
            <div className="stat-sub">object-store versioning</div>
          </div>
        </div>
      </div>

      <Panel
        icon={FolderTree}
        title="Catalog"
        subtitle={`${rows.length} of ${all.length} datasets`}
        actions={
          <div className="flex items-center gap-2">
            <SearchInput value={query} onChange={setQuery} placeholder="Search datasets…" className="" />
          </div>
        }
        bodyClass="tight"
      >
        {list.loading ? (
          <LoadingState label="Loading datasets…" />
        ) : list.error ? (
          <ErrorState error={list.error} title="Could not load datasets" onRetry={list.refetch} />
        ) : rows.length === 0 ? (
          <EmptyState
            icon={Database}
            title={all.length ? 'No match' : 'No datasets yet'}
            desc={all.length
              ? 'Nothing matches your search.'
              : 'Register a CSV to create a lakeFS repository with branches, tags and full commit history.'}
            actions={canUpload ? <Button variant="primary" icon={Plus} onClick={() => setShowCreate(true)}>New dataset</Button> : null}
          />
        ) : (
          <DataTable
            caption="Registered datasets"
            keyOf={(d) => d.id || d.name}
            rows={rows}
            pageSize={10}
            onRowClick={(d) => navigate(`/datasets/${encodeURIComponent(d.name)}`)}
            columns={[
              {
                key: 'name', strong: true, sortKey: 'name', header: 'Dataset',
                render: (d) => (
                  <span className="flex items-center gap-2">
                    <span className="row-glyph is-accent" style={{ width: 26, height: 26 }}><Database size={13} /></span>
                    {d.name}
                  </span>
                ),
              },
              {
                key: 'description', header: 'Description',
                render: (d) => <span className="muted">{d.description || '—'}</span>,
              },
              {
                key: 'default_branch', width: 140, header: 'Default branch',
                render: (d) => <Badge tone="outline"><GitBranch size={11} /> {d.default_branch || 'main'}</Badge>,
              },
              {
                key: 'created_at', width: 170, sortKey: 'created_at', header: 'Created',
                render: (d) => (
                  <span className="text-xs muted">
                    {d.created_at ? new Date(d.created_at).toLocaleString() : '—'}
                  </span>
                ),
              },
              {
                key: 'actions', actions: true, width: 190, header: 'Actions',
                render: (d) => (
                  <span className="flex items-center" style={{ justifyContent: 'flex-end', gap: 4 }}>
                    <Button variant="ghost" size="xs" icon={ArrowRight} onClick={() => navigate(`/datasets/${encodeURIComponent(d.name)}`)}>
                      Open
                    </Button>
                    {canDelete && (
                      <Button
                        variant="danger-ghost"
                        size="xs"
                        icon={Trash2}
                        loading={deleting === d.name}
                        onClick={() => handleDelete(d)}
                        aria-label={`Delete ${d.name}`}
                      >
                        Delete
                      </Button>
                    )}
                  </span>
                ),
              },
            ]}
          />
        )}
      </Panel>

      <Panel icon={Info} title="How datasets work here" subtitle="Version control semantics applied to tabular data">
        <div className="grid-3">
          {[
            ['Branches', 'Branch a dataset to experiment on a copy without touching main. Merge-free by design — training reads one ref.', GitBranch],
            ['Commits', 'Every upload and mutation is committed with a message, author and timestamp.', FolderTree],
            ['Rollback', 'Compare two refs and restore an earlier state with a single action.', RefreshCw],
          ].map(([t, d, Icon]) => (
            <div key={t} className="row-item" style={{ alignItems: 'flex-start' }}>
              <span className="row-glyph is-accent"><Icon size={15} /></span>
              <span className="row-info">
                <span className="row-title">{t}</span>
                <span className="row-sub" style={{ whiteSpace: 'normal' }}>{d}</span>
              </span>
            </div>
          ))}
        </div>
      </Panel>

      {showCreate && (
        <CreateDatasetModal
          onClose={() => setShowCreate(false)}
          onCreated={(name) => {
            setShowCreate(false)
            list.refetch()
            if (name) navigate(`/datasets/${encodeURIComponent(name)}`)
          }}
        />
      )}
    </div>
  )
}

/* ── Create (register + upload) modal ────────────── */
function CreateDatasetModal({ onClose, onCreated }) {
  const toast = useToast()
  const fileRef = useRef(null)
  const [file, setFile] = useState(null)
  const [name, setName] = useState('')
  const [desc, setDesc] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [drag, setDrag] = useState(false)

  function pick(f) {
    if (!f) return
    if (!/\.csv$/i.test(f.name)) { setError(`Only .csv files are supported. Got "${f.name}".`); return }
    setError(null)
    setFile(f)
    if (!name) setName(toRepoName(f.name))
  }

  async function submit(e) {
    e.preventDefault()
    if (!file) { setError('Choose a CSV file.'); return }
    if (!name.trim()) { setError('Dataset name is required.'); return }
    setBusy(true)
    setError(null)
    const body = new FormData()
    body.append('name', name.trim())
    if (desc.trim()) body.append('description', desc.trim())
    body.append('file', file)
    try {
      await datasetsApi.register(body)
      toast.success('Dataset created', `${name} registered and committed to lakeFS.`)
      onCreated(name.trim())
    } catch (err) {
      setError(err?.message || 'Registration failed.')
      toast.error('Registration failed', err?.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      open
      onClose={onClose}
      title="Register a dataset"
      subtitle="Creates a lakeFS repository, uploads the file, and commits it to the default branch"
      footer={
        <>
          <Button variant="secondary" onClick={onClose}>Cancel</Button>
          <Button variant="primary" icon={Plus} loading={busy} onClick={submit}>Create dataset</Button>
        </>
      }
    >
      <form onSubmit={submit} style={{ display: 'flex', flexDirection: 'column', gap: 'var(--sp-4)' }}>
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
          <span className="file-drop-title">{file ? file.name : 'Drop a CSV or click to browse'}</span>
          <span className="file-drop-sub">
            {file ? `${(file.size / 1024).toFixed(1)} KB` : 'Only .csv is accepted by the training pipeline'}
          </span>
        </div>
        <input ref={fileRef} type="file" accept=".csv" hidden onChange={(e) => { pick(e.target.files?.[0]); e.target.value = '' }} />

        <Field label="Dataset name" required htmlFor="ds-name" hint="Lowercase, hyphens only — becomes the lakeFS repository name.">
          <TextInput id="ds-name" value={name} onChange={(e) => setName(e.target.value)} placeholder="churn-data" required />
        </Field>

        <Field label="Description" htmlFor="ds-desc">
          <TextArea id="ds-desc" rows={3} value={desc} onChange={(e) => setDesc(e.target.value)} placeholder="What this dataset contains and where it came from." />
        </Field>

        <button type="submit" hidden aria-hidden />
      </form>
    </Modal>
  )
}
