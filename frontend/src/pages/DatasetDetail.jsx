import { useState, useRef, useEffect } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { datasetsApi } from '../api/endpoints.js'
import { useFetch } from '../hooks/useFetch.js'
import { fmtUnixDate } from '../lib/models.js'
import { api } from '../api/client.js'
import {
  PageHeader, Panel, Button, DataTable, Field, TextInput, TextArea, Badge,
  Tabs, EmptyState, LoadingState, ErrorState, Alert, KeyValue, Modal,
  useToast, useConfirm, CopyButton, StatCard,
} from '../components/ui/index.jsx'
import {
  Database, GitBranch, GitCommitHorizontal, Tag, GitCompareArrows, Settings,
  ArrowLeft, RefreshCw, Upload, Download, Plus, Trash2, Check, Layers,
  Info, Save, Undo2, FolderTree, HardDrive, RotateCcw,
} from '../components/icons.jsx'

const TABS = [
  { id: 'overview', label: 'Overview', icon: Info },
  { id: 'files', label: 'Files', icon: FolderTree },
  { id: 'commits', label: 'Commits', icon: GitCommitHorizontal },
  { id: 'branches', label: 'Branches', icon: GitBranch },
  { id: 'tags', label: 'Tags', icon: Tag },
  { id: 'compare', label: 'Compare', icon: GitCompareArrows },
  { id: 'manage', label: 'Manage', icon: Settings },
]

export default function DatasetDetail() {
  const { name } = useParams()
  const navigate = useNavigate()

  const [tab, setTab] = useState('overview')

  const meta = useFetch(() => datasetsApi.metadata(name), [name])
  // GET /datasets/{name} only returns lakeFS + custom metadata; the human
  // fields (description, default branch, storage namespace, timestamps) live
  // on the catalog record from GET /datasets. Merge both.
  const catalog = useFetch(() => datasetsApi.list(), [])
  const branches = useFetch(() => datasetsApi.branches(name), [name])
  const tags = useFetch(() => datasetsApi.tags(name), [name])
  const commits = useFetch(() => datasetsApi.commits(name, 'main', 100), [name])

  const branchList = norm(branches.data, 'branches')
  const tagList = norm(tags.data, 'tags')
  const commitList = norm(commits.data, 'commits')

  const listed = (Array.isArray(catalog.data) ? catalog.data : [])
    .find((d) => d.name === name) || {}
  const dbMeta = { ...listed, ...(meta.data?.db_metadata || {}) }
  const lfMeta = meta.data?.lakefs_metadata || {}
  if (!dbMeta.description) dbMeta.description = lfMeta.description || undefined

  const refreshAll = () => {
    meta.refetch(); catalog.refetch(); branches.refetch(); tags.refetch(); commits.refetch()
  }

  if (meta.loading || catalog.loading) {
    return <div className="page"><LoadingState label={`Loading ${name}…`} lg /></div>
  }
  if (meta.error) {
    return (
      <div className="page">
        <PageHeader icon={Database} title={name} subtitle="Dataset workspace" />
        <ErrorState error={meta.error} title="Could not load dataset metadata" onRetry={meta.refetch} />
        <div><Button variant="secondary" icon={ArrowLeft} onClick={() => navigate('/datasets')}>Back to datasets</Button></div>
      </div>
    )
  }

  return (
    <div className="page">
      <PageHeader
        icon={Database}
        title={meta.data?.dataset_name || name}
        subtitle={dbMeta.description || 'lakeFS-backed versioned dataset'}
        badge={<Badge tone="success" dot>registered</Badge>}
        actions={
          <>
            <Button variant="secondary" size="sm" icon={ArrowLeft} onClick={() => navigate('/datasets')}>All datasets</Button>
            <Button variant="secondary" size="sm" icon={RefreshCw} onClick={refreshAll}>Refresh</Button>
          </>
        }
      />

      <div className="stat-grid">
        <StatCard icon={GitBranch} tone="accent" value={branchList.length} label="Branches" sub={dbMeta.default_branch || 'main'} />
        <StatCard icon={Tag} tone="info" value={tagList.length} label="Tags" sub="named refs" />
        <StatCard icon={GitCommitHorizontal} tone="purple" value={commitList.length} label="Commits" sub="on default ref" />
        <StatCard icon={Layers} tone="success" value={meta.data?.dataset_name ? 'lakeFS' : '—'} label="Backend" sub="object storage" />
      </div>

      <Tabs tabs={TABS} active={tab} onChange={setTab} variant="underline" />

      {tab === 'overview' && <OverviewTab meta={meta.data} dbMeta={dbMeta} lfMeta={lfMeta} />}
      {tab === 'files' && <FilesTab name={name} defaultBranch={dbMeta.default_branch || 'main'} branches={branchList} />}
      {tab === 'commits' && <CommitsTab name={name} commits={commitList} loading={commits.loading} error={commits.error} onRefresh={commits.refetch} />}
      {tab === 'branches' && <BranchesTab name={name} branches={branchList} loading={branches.loading} error={branches.error} onRefresh={branches.refetch} />}
      {tab === 'tags' && <TagsTab name={name} tags={tagList} commits={commitList} loading={tags.loading} error={tags.error} onRefresh={tags.refetch} />}
      {tab === 'compare' && <CompareTab name={name} refs={[...branchList.map((b) => b.name), ...tagList.map((t) => t.name)]} commits={commitList} />}
      {tab === 'manage' && <ManageTab name={name} dbMeta={dbMeta} onSaved={meta.refetch} onDeleted={() => navigate('/datasets')} />}
    </div>
  )
}

/* ── helpers ─────────────────────────────────────── */
function norm(data, key) {
  if (Array.isArray(data)) return data
  return data?.[key] || []
}

function serializeMeta(dbMeta) {
  return dbMeta?.metadata_info ? JSON.stringify(dbMeta.metadata_info, null, 2) : '{}'
}

/* ══════════════════════════════════════════════════════
   Overview
   ══════════════════════════════════════════════════════ */
function OverviewTab({ meta, dbMeta, lfMeta }) {
  return (
    <div className="grid-2">
      <Panel icon={Database} title="Database record" subtitle="PostgreSQL registration">
        <KeyValue
          items={[
            ['Dataset name', meta?.dataset_name],
            ['Default branch', dbMeta.default_branch],
            ['Storage namespace', dbMeta.storage_namespace],
            ['Created', dbMeta.created_at ? new Date(dbMeta.created_at).toLocaleString() : undefined],
            ['Updated', dbMeta.updated_at ? new Date(dbMeta.updated_at).toLocaleString() : undefined],
            ['Description', dbMeta.description],
          ]}
        />
      </Panel>

      <Panel icon={HardDrive} title="lakeFS metadata" subtitle="Repository state reported by lakeFS">
        {Object.keys(lfMeta).length === 0 ? (
          <EmptyState icon={HardDrive} title="No lakeFS metadata" desc="The repository may not be initialized yet." />
        ) : (
          <KeyValue
            items={Object.entries(lfMeta).slice(0, 12).map(([k, v]) => [
              k, typeof v === 'object' ? JSON.stringify(v) : String(v),
            ])}
          />
        )}
      </Panel>

      <Panel icon={Layers} title="Custom metadata" subtitle="Key/value pairs stored with the dataset" className="grid-span-2">
        {dbMeta.metadata_info && Object.keys(dbMeta.metadata_info).length ? (
          <KeyValue items={Object.entries(dbMeta.metadata_info).map(([k, v]) => [k, String(v)])} />
        ) : (
          <EmptyState icon={Layers} title="No custom metadata" desc="Add key/value pairs from the Manage tab." />
        )}
      </Panel>
    </div>
  )
}

/* ══════════════════════════════════════════════════════
   Files — upload + download
   ══════════════════════════════════════════════════════ */
function FilesTab({ name, defaultBranch, branches }) {
  const toast = useToast()
  const fileRef = useRef(null)
  const [branch, setBranch] = useState(defaultBranch)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [drag, setDrag] = useState(false)
  const [log, setLog] = useState([])

  async function upload(file) {
    if (!file) return
    setBusy(true)
    setError(null)
    try {
      const body = new FormData()
      body.append('file', file)
      body.append('branch', branch)
      await datasetsApi.uploadFile(name, body)
      toast.success('File uploaded', `${file.name} staged on ${branch}. Commit to publish it.`)
      setLog((l) => [{ file: file.name, at: new Date(), branch }, ...l])
    } catch (err) {
      setError(err?.message || 'Upload failed.')
      toast.error('Upload failed', err?.message)
    } finally {
      setBusy(false)
    }
  }

  async function download(path) {
    try {
      const blob = await api.download(datasetsApi.downloadUrl(name, path, branch).replace(/^\//, ''))
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = path.split('/').pop() || 'file.csv'
      a.click()
      URL.revokeObjectURL(url)
      toast.success('Download started', path)
    } catch (err) {
      toast.error('Download failed', err?.message)
    }
  }

  // `branches` arrives as BranchResponse objects ({name, head_commit_id}).
  // Rendering the objects themselves in <option> throws — reduce to names.
  const branchNames = (Array.isArray(branches) ? branches : [])
    .map((b) => (typeof b === 'string' ? b : b?.name))
    .filter(Boolean)

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--sp-4)' }}>
      <Panel icon={Upload} title="Upload a file" subtitle="Staged on the selected branch until you commit">
        <div className="field-row field-row-2 mb-3">
          <Field label="Branch" htmlFor="up-branch">
            <select id="up-branch" className="select-field" value={branch} onChange={(e) => setBranch(e.target.value)}>
              {(branchNames.length ? branchNames : [defaultBranch]).map((b) => (
                <option key={b} value={b}>{b}</option>
              ))}
            </select>
          </Field>
          <Field label="Status">
            <div className="flex items-center gap-2" style={{ height: 37 }}>
              {busy ? <Badge tone="warning" dot>Uploading…</Badge> : <Badge tone="neutral">Idle</Badge>}
            </div>
          </Field>
        </div>

        <div
          className={`file-drop ${drag ? 'is-drag' : ''}`}
          onClick={() => fileRef.current?.click()}
          onKeyDown={(e) => {
            if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); fileRef.current?.click() }
          }}
          onDragOver={(e) => { e.preventDefault(); setDrag(true) }}
          onDragLeave={() => setDrag(false)}
          onDrop={(e) => { e.preventDefault(); setDrag(false); upload(e.dataTransfer.files?.[0]) }}
          role="button"
          tabIndex={0}
        >
          <span className="file-drop-icon"><Upload size={19} /></span>
          <span className="file-drop-title">Drop a file or click to browse</span>
          <span className="file-drop-sub">Uploaded to {name} @ {branch}</span>
        </div>
        <input ref={fileRef} type="file" hidden onChange={(e) => { upload(e.target.files?.[0]); e.target.value = '' }} />
        {error && <div className="mt-3"><Alert tone="error">{error}</Alert></div>}
      </Panel>

      <Panel icon={Download} title="Download" subtitle={`Fetch a file from ${name} @ ${branch}`}>
        <div className="field-row field-row-2">
          <Field label="File path" htmlFor="dl-path" hint="Path relative to the repository root">
            <TextInput id="dl-path" placeholder="data.csv" defaultValue="" onKeyDown={(e) => { if (e.key === 'Enter') download(e.currentTarget.value) }} />
          </Field>
          <Field label="Or use the API directly">
            <div className="flex items-center gap-2" style={{ height: 37 }}>
              <code className="mono text-xs muted truncate">
                GET /datasets/{name}/download?path=…&amp;ref={branch}
              </code>
              <CopyButton text={`/datasets/${name}/download?path=data.csv&ref=${branch}`} />
            </div>
          </Field>
        </div>
        <div className="mt-3">
          <Button variant="primary" size="sm" icon={Download} onClick={() => {
            const el = document.getElementById('dl-path')
            if (el?.value) download(el.value)
          }}>
            Download
          </Button>
        </div>
      </Panel>

      {log.length > 0 && (
        <Panel icon={Check} iconTone="success" title="Upload history (this session)">
          <div className="row-list">
            {log.map((l, i) => (
              <div key={i} className="row-item">
                <span className="row-glyph is-success"><Check size={14} /></span>
                <span className="row-info">
                  <span className="row-title">{l.file}</span>
                  <span className="row-sub">{l.branch} · {l.at.toLocaleTimeString()}</span>
                </span>
              </div>
            ))}
          </div>
        </Panel>
      )}
    </div>
  )
}

/* ══════════════════════════════════════════════════════
   Commits + rollback
   ══════════════════════════════════════════════════════ */
function CommitsTab({ name, commits, loading, error, onRefresh }) {
  const toast = useToast()
  const confirm = useConfirm()
  const [message, setMessage] = useState('')
  const [busy, setBusy] = useState(false)
  const [rollbackTarget, setRollbackTarget] = useState(null)

  async function commit(e) {
    e.preventDefault()
    if (!message.trim()) { toast.warning('Message required', 'Describe the change before committing.'); return }
    setBusy(true)
    try {
      await datasetsApi.commit(name, { message: message.trim() })
      toast.success('Committed', message)
      setMessage('')
      onRefresh()
    } catch (err) {
      toast.error('Commit failed', err?.message)
    } finally { setBusy(false) }
  }

  async function rollback() {
    const ok = await confirm({
      title: 'Roll back to this commit?',
      text: `A new commit will be written on main that restores ${String(rollbackTarget).slice(0, 10)} as the effective dataset state. History is never rewritten.`,
      confirmLabel: 'Roll back',
      tone: 'danger',
    })
    if (!ok) return
    setBusy(true)
    try {
      // RollbackRequest accepts only { branch, commit_id } — the backend
      // generates its own commit message.
      await datasetsApi.rollback(name, {
        branch: 'main',
        commit_id: rollbackTarget,
      })
      toast.success('Rolled back', `Reverted to ${String(rollbackTarget).slice(0, 10)}.`)
      setRollbackTarget(null)
      onRefresh()
    } catch (err) {
      toast.error('Rollback failed', err?.message)
    } finally { setBusy(false) }
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--sp-4)' }}>
      <Panel icon={Save} title="Commit pending changes" subtitle="Publish staged uploads to the branch head">
        <form onSubmit={commit} className="field-row" style={{ gridTemplateColumns: '1fr auto', alignItems: 'end' }}>
          <Field label="Commit message" htmlFor="commit-msg" required>
            <TextInput id="commit-msg" value={message} onChange={(e) => setMessage(e.target.value)} placeholder="Add churn features" />
          </Field>
          <Button type="submit" variant="primary" icon={GitCommitHorizontal} loading={busy}>Commit</Button>
        </form>
      </Panel>

      <Panel
        icon={GitCommitHorizontal}
        title="History"
        subtitle={`${commits.length} commits`}
        actions={<Button variant="ghost" size="sm" icon={RefreshCw} onClick={onRefresh}>Refresh</Button>}
        bodyClass={commits.length ? 'tight' : ''}
      >
        {loading ? <LoadingState label="Loading commits…" />
          : error ? <ErrorState error={error} onRetry={onRefresh} />
          : commits.length === 0 ? <EmptyState icon={GitCommitHorizontal} title="No commits yet" desc="Commit staged changes to start the history." />
          : (
            <DataTable
              keyOf={(c) => c.id}
              rows={commits}
              pageSize={10}
              columns={[
                {
                  key: 'id', strong: true, header: 'Commit',
                  render: (c) => (
                    <span className="flex items-center gap-2">
                      <code className="mono text-xs">{String(c.id).slice(0, 10)}</code>
                      <CopyButton text={c.id} />
                    </span>
                  ),
                },
                { key: 'message', header: 'Message', render: (c) => <span className="truncate" title={c.message}>{c.message || '(no message)'}</span> },
                { key: 'committer', header: 'Author', render: (c) => <span className="muted text-xs">{c.committer || '—'}</span> },
                {
                  key: 'creation_date', header: 'Date',
                  render: (c) => <span className="muted text-xs">{fmtUnixDate(c.creation_date)}</span>,
                },
                {
                  key: 'actions', actions: true, header: <span className="sr-only">Actions</span>,
                  render: (c) => (
                    <Button variant="danger-ghost" size="xs" icon={Undo2} onClick={() => setRollbackTarget(c.id)}>
                      Roll back
                    </Button>
                  ),
                },
              ]}
            />
          )}
      </Panel>

      <Modal
        open={!!rollbackTarget}
        onClose={() => setRollbackTarget(null)}
        title="Roll back dataset"
        subtitle={`Target commit ${String(rollbackTarget || '').slice(0, 10)}`}
        footer={
          <>
            <Button variant="secondary" onClick={() => setRollbackTarget(null)}>Cancel</Button>
            <Button variant="danger" icon={Undo2} loading={busy} onClick={rollback}>Roll back</Button>
          </>
        }
      >
        <div className="flex flex-col gap-3">
          <Alert tone="warning">
            Rolling back writes a <strong>new</strong> commit on main — existing history is never
            rewritten, so you can always roll forward again.
          </Alert>
          <KeyValue
            items={[
              ['Branch', 'main'],
              ['Restore commit', <code key="c" className="mono">{String(rollbackTarget || '')}</code>],
              ['Commits kept', 'all of them'],
            ]}
          />
        </div>
      </Modal>
    </div>
  )
}

/* ══════════════════════════════════════════════════════
   Branches
   ══════════════════════════════════════════════════════ */
function BranchesTab({ name, branches, loading, error, onRefresh }) {
  const toast = useToast()
  const confirm = useConfirm()
  const [open, setOpen] = useState(false)
  const [form, setForm] = useState({ branch_name: '', source_branch: 'main' })
  const [busy, setBusy] = useState(false)

  async function create(e) {
    e.preventDefault()
    setBusy(true)
    try {
      await datasetsApi.createBranch(name, form.branch_name.trim(), form.source_branch)
      toast.success('Branch created', form.branch_name)
      setOpen(false); setForm({ branch_name: '', source_branch: 'main' }); onRefresh()
    } catch (err) {
      toast.error('Branch creation failed', err?.message)
    } finally { setBusy(false) }
  }

  async function remove(b) {
    const ok = await confirm({
      title: `Delete branch "${b}"?`,
      text: 'All commits reachable only from this branch will be lost.',
      confirmLabel: 'Delete branch',
      tone: 'danger',
    })
    if (!ok) return
    try {
      await datasetsApi.deleteBranch(name, b)
      toast.success('Branch deleted', b)
      onRefresh()
    } catch (err) { toast.error('Delete failed', err?.message) }
  }

  return (
    <Panel
      icon={GitBranch}
      title="Branches"
      subtitle={`${branches.length} branch${branches.length === 1 ? '' : 'es'}`}
      actions={<Button variant="primary" size="sm" icon={Plus} onClick={() => setOpen(true)}>New branch</Button>}
      bodyClass={branches.length ? 'tight' : ''}
    >
      {loading ? <LoadingState label="Loading branches…" />
        : error ? <ErrorState error={error} onRetry={onRefresh} />
        : branches.length === 0 ? <EmptyState icon={GitBranch} title="No branches" desc="Create a branch to experiment on a copy of the data." />
        : (
          <DataTable
            keyOf={(b) => b.name}
            rows={branches}
            columns={[
              { key: 'name', strong: true, header: 'Branch', render: (b) => <span className="flex items-center gap-2"><GitBranch size={13} /> {b.name}</span> },
              { key: 'head_commit_id', header: 'HEAD', render: (b) => <code className="mono text-xs">{String(b.head_commit_id || '—').slice(0, 10)}</code> },
              { key: 'type', header: 'Type', render: (b) => <Badge tone="outline">{b.type || 'branch'}</Badge> },
              {
                key: 'actions', actions: true, header: <span className="sr-only">Actions</span>,
                render: (b) => b.name === 'main'
                  ? <Badge tone="success" sm>default</Badge>
                  : <Button variant="danger-ghost" size="xs" icon={Trash2} onClick={() => remove(b)}>Delete</Button>,
              },
            ]}
          />
        )}

      <Modal
        open={open}
        onClose={() => setOpen(false)}
        title="Create branch"
        subtitle="Fork the dataset at a source ref"
        footer={
          <>
            <Button variant="secondary" onClick={() => setOpen(false)}>Cancel</Button>
            <Button variant="primary" loading={busy} onClick={create}>Create</Button>
          </>
        }
      >
        <form onSubmit={create} className="flex flex-col gap-4">
          <Field label="Branch name" required htmlFor="br-name">
            <TextInput id="br-name" value={form.branch_name} onChange={(e) => setForm({ ...form, branch_name: e.target.value })} placeholder="experiment-dropout" />
          </Field>
          <Field label="Source branch" htmlFor="br-src">
            <TextInput id="br-src" value={form.source_branch} onChange={(e) => setForm({ ...form, source_branch: e.target.value })} />
          </Field>
          <button type="submit" hidden />
        </form>
      </Modal>
    </Panel>
  )
}

/* ══════════════════════════════════════════════════════
   Tags
   ══════════════════════════════════════════════════════ */
function TagsTab({ name, tags, commits, loading, error, onRefresh }) {
  const toast = useToast()
  const confirm = useConfirm()
  const [open, setOpen] = useState(false)
  const [form, setForm] = useState({ tag_name: '', target_ref: 'main' })
  const [busy, setBusy] = useState(false)

  async function create(e) {
    e.preventDefault()
    setBusy(true)
    try {
      await datasetsApi.createTag(name, form.tag_name.trim(), form.target_ref)
      toast.success('Tag created', form.tag_name)
      setOpen(false); setForm({ tag_name: '', target_ref: 'main' }); onRefresh()
    } catch (err) { toast.error('Tag creation failed', err?.message) }
    finally { setBusy(false) }
  }

  async function remove(t) {
    const ok = await confirm({
      title: `Delete tag "${t}"?`,
      text: 'The tag pointer is removed; the underlying commits remain reachable from branches.',
      confirmLabel: 'Delete tag',
      tone: 'danger',
    })
    if (!ok) return
    try {
      await datasetsApi.deleteTag(name, t)
      toast.success('Tag deleted', t)
      onRefresh()
    } catch (err) { toast.error('Delete failed', err?.message) }
  }

  return (
    <Panel
      icon={Tag}
      title="Tags"
      subtitle="Immutable pointers to a commit — ideal for marking dataset releases"
      actions={<Button variant="primary" size="sm" icon={Plus} onClick={() => setOpen(true)}>New tag</Button>}
      bodyClass={tags.length ? 'tight' : ''}
    >
      {loading ? <LoadingState label="Loading tags…" />
        : error ? <ErrorState error={error} onRetry={onRefresh} />
        : tags.length === 0 ? <EmptyState icon={Tag} title="No tags" desc="Tag a commit to create a stable dataset release." />
        : (
          <DataTable
            keyOf={(t) => t.name}
            rows={tags}
            columns={[
              { key: 'name', strong: true, header: 'Tag', render: (t) => <span className="flex items-center gap-2"><Tag size={13} /> {t.name}</span> },
              { key: 'commit_id', header: 'Commit', render: (t) => <code className="mono text-xs">{String(t.commit_id || '—').slice(0, 10)}</code> },
              { key: 'message', header: 'Message', render: (t) => <span className="muted">{t.message || '—'}</span> },
              {
                key: 'actions', actions: true, header: <span className="sr-only">Actions</span>,
                render: (t) => <Button variant="danger-ghost" size="xs" icon={Trash2} onClick={() => remove(t)}>Delete</Button>,
              },
            ]}
          />
        )}

      <Modal
        open={open}
        onClose={() => setOpen(false)}
        title="Create tag"
        subtitle="Point a named tag at a branch, tag or commit"
        footer={
          <>
            <Button variant="secondary" onClick={() => setOpen(false)}>Cancel</Button>
            <Button variant="primary" loading={busy} onClick={create}>Create tag</Button>
          </>
        }
      >
        <form onSubmit={create} className="flex flex-col gap-4">
          <Field label="Tag name" required htmlFor="tg-name">
            <TextInput id="tg-name" value={form.tag_name} onChange={(e) => setForm({ ...form, tag_name: e.target.value })} placeholder="v1.0-release" />
          </Field>
          <Field label="Target ref" htmlFor="tg-ref" hint="Branch name or commit id">
            <TextInput id="tg-ref" value={form.target_ref} onChange={(e) => setForm({ ...form, target_ref: e.target.value })} />
          </Field>
          {commits.length > 0 && (
            <div className="pill-row">
              {commits.slice(0, 6).map((c) => (
                <button key={c.id} type="button" className="ref-chip" onClick={() => setForm({ ...form, target_ref: c.id })}>
                  <GitCommitHorizontal size={11} /> {String(c.id).slice(0, 8)}
                </button>
              ))}
            </div>
          )}
          <button type="submit" hidden />
        </form>
      </Modal>
    </Panel>
  )
}

/* ══════════════════════════════════════════════════════
   Compare (three-dot diff)
   ══════════════════════════════════════════════════════ */
function CompareTab({ name, refs, commits }) {
  const toast = useToast()
  const [left, setLeft] = useState('main')
  const [right, setRight] = useState('')
  const [rows, setRows] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)

  const options = [...new Set([...refs, 'main', ...commits.map((c) => String(c.id).slice(0, 10))])].filter(Boolean)

  async function run() {
    if (!left || !right) { toast.warning('Pick both refs', 'Choose a left and right ref to compare.'); return }
    setLoading(true); setError(null)
    try {
      const res = await datasetsApi.compare(name, left, right, 'three_dot')
      setRows(Array.isArray(res) ? res : [])
    } catch (err) {
      setError(err); setRows(null)
    } finally { setLoading(false) }
  }

  const stats = rows
    ? rows.reduce((acc, r) => { acc[r.type] = (acc[r.type] || 0) + 1; return acc }, {})
    : {}

  // lakeFS three-dot diffs report 'added' | 'removed' | 'changed' | 'conflict'.
  const diffTone = (t) => {
    const v = String(t || '').toLowerCase()
    if (v.startsWith('add') || v.startsWith('new')) return 'success'
    if (v.startsWith('rem') || v.startsWith('del')) return 'danger'
    if (v.startsWith('conf')) return 'warning'
    return 'info'
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--sp-4)' }}>
      <Panel icon={GitCompareArrows} title="Compare refs" subtitle="Three-dot diff between two branches, tags or commits">
        <div className="field-row field-row-3">
          <Field label="Left ref" htmlFor="cmp-left">
            <TextInput id="cmp-left" list="ref-options" value={left} onChange={(e) => setLeft(e.target.value)} />
          </Field>
          <Field label="Right ref" htmlFor="cmp-right">
            <TextInput id="cmp-right" list="ref-options" value={right} onChange={(e) => setRight(e.target.value)} placeholder="v1.0-release" />
          </Field>
          <Field label="&nbsp;">
            <Button variant="primary" icon={GitCompareArrows} loading={loading} onClick={run} block>Compare</Button>
          </Field>
        </div>
        <datalist id="ref-options">
          {options.map((o) => <option key={o} value={o} />)}
        </datalist>
      </Panel>

      <Panel
        icon={Layers}
        title="Changes"
        subtitle={rows ? `${rows.length} changed path${rows.length === 1 ? '' : 's'}` : 'Run a comparison to see the diff'}
        bodyClass={rows?.length ? 'tight' : ''}
        actions={rows ? (
          <div className="pill-row">
            {Object.entries(stats).map(([k, v]) => <Badge key={k} tone={diffTone(k)}>{k} · {v}</Badge>)}
          </div>
        ) : null}
      >
        {loading ? <LoadingState label="Comparing refs…" />
          : error ? <ErrorState error={error} title="Compare failed" onRetry={run} />
          : !rows ? <EmptyState icon={GitCompareArrows} title="No comparison yet" desc="Select two refs above and run a comparison." />
          : rows.length === 0 ? <EmptyState icon={Check} title="No differences" desc="The two refs point at identical content." />
          : (
            <DataTable
              keyOf={(r, i) => `${r.path}-${i}`}
              rows={rows}
              pageSize={15}
              columns={[
                {
                  key: 'type', header: 'Change',
                  render: (r) => (
                    <Badge tone={diffTone(r.type)} sm>
                      {r.type}
                    </Badge>
                  ),
                },
                { key: 'path', strong: true, header: 'Path', render: (r) => <code className="mono text-xs">{r.path}</code> },
                { key: 'path_type', header: 'Kind', render: (r) => <Badge tone="outline" sm>{r.path_type}</Badge> },
                {
                  key: 'size_bytes', header: 'Size',
                  render: (r) => <span className="muted text-xs">{r.size_bytes != null ? `${r.size_bytes} B` : '—'}</span>,
                },
              ]}
            />
          )}
      </Panel>
    </div>
  )
}

/* ══════════════════════════════════════════════════════
   Manage (metadata update + delete)
   ══════════════════════════════════════════════════════ */
function ManageTab({ name, dbMeta, onSaved, onDeleted }) {
  const toast = useToast()
  const confirm = useConfirm()
  const [kvText, setKvText] = useState(serializeMeta(dbMeta))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  // The catalog fetch can land after mount — keep the editor in sync until the
  // user starts typing.
  const [pristine, setPristine] = useState(true)
  useEffect(() => {
    if (!pristine) return
    setKvText(serializeMeta(dbMeta))
  }, [dbMeta, pristine])

  async function save(e) {
    e.preventDefault()
    setError(null)
    let parsed
    try {
      parsed = kvText.trim() ? JSON.parse(kvText) : {}
    } catch {
      setError('Metadata must be valid JSON.')
      return
    }
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      setError('Metadata must be a JSON object of string keys to string values.')
      return
    }
    // MetadataUpdateRequest types metadata as dict[str, str] — coerce numbers
    // and booleans to strings so the request can never 422.
    const metadata = {}
    for (const [k, v] of Object.entries(parsed)) {
      metadata[String(k)] = typeof v === 'string' ? v : JSON.stringify(v)
    }
    setBusy(true)
    try {
      await datasetsApi.updateMetadata(name, metadata)
      toast.success('Metadata saved', `${name} updated.`)
      onSaved()
    } catch (err) {
      setError(err?.message || 'Update failed.')
      toast.error('Update failed', err?.message)
    } finally { setBusy(false) }
  }

  async function destroy() {
    const ok = await confirm({
      title: `Delete "${name}"?`,
      text: 'This permanently removes the lakeFS repository and database record, including every branch, tag and commit.',
      confirmLabel: 'Delete permanently',
      tone: 'danger',
    })
    if (!ok) return
    setBusy(true)
    try {
      await datasetsApi.remove(name)
      toast.success('Dataset deleted', name)
      onDeleted()
    } catch (err) {
      toast.error('Delete failed', err?.message)
      setBusy(false)
    }
  }

  const reset = () => { setKvText(serializeMeta(dbMeta)); setPristine(true); setError(null) }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--sp-4)' }}>
      <Panel icon={Info} title="Dataset record" subtitle="Fields set at registration time">
        <KeyValue
          items={[
            ['Name', dbMeta.name || name],
            ['Description', dbMeta.description],
            ['Default branch', dbMeta.default_branch],
            ['Storage namespace', dbMeta.storage_namespace],
            ['Created', dbMeta.created_at ? new Date(dbMeta.created_at).toLocaleString() : undefined],
            ['Updated', dbMeta.updated_at ? new Date(dbMeta.updated_at).toLocaleString() : undefined],
          ]}
        />
        <div className="mt-3">
          <Alert tone="info">
            The description and default branch are written at registration time and are
            immutable through the API — only custom metadata below can be updated with
            <code className="mono"> PUT /datasets/{'{name}'}</code>.
          </Alert>
        </div>
      </Panel>

      <Panel icon={Settings} title="Custom metadata" subtitle="Free-form key/value pairs stored alongside the dataset">
        <form onSubmit={save} className="flex flex-col gap-4">
          {error && <Alert tone="error">{error}</Alert>}
          <Field
            label="Metadata (JSON)"
            htmlFor="mg-kv"
            hint='dict[str, str] — numbers and booleans are stringified automatically. e.g. {"owner":"team-a","pii":"false"}'
          >
            <TextArea id="mg-kv" rows={8} value={kvText} onChange={(e) => { setKvText(e.target.value); setPristine(false) }} />
          </Field>
          <div className="flex gap-2">
            <Button type="submit" variant="primary" icon={Save} loading={busy}>Save metadata</Button>
            <Button variant="ghost" icon={RotateCcw} onClick={reset}>Revert</Button>
          </div>
        </form>
      </Panel>

      <Panel
        icon={Trash2}
        iconTone="danger"
        title="Danger zone"
        subtitle="Irreversible operations"
        actions={<Badge tone="danger">destructive</Badge>}
      >
        <div className="row-item" style={{ borderColor: 'var(--danger-border)', background: 'var(--danger-soft)' }}>
          <span className="row-glyph is-danger"><Trash2 size={15} /></span>
          <span className="row-info">
            <span className="row-title">Delete this dataset</span>
            <span className="row-sub" style={{ whiteSpace: 'normal' }}>
              Removes the lakeFS repository, all branches and tags, and the PostgreSQL record.
            </span>
          </span>
          <Button variant="danger" size="sm" icon={Trash2} loading={busy} onClick={destroy}>Delete dataset</Button>
        </div>
      </Panel>
    </div>
  )
}
