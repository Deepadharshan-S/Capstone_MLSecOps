import { useState } from 'react'
import { usersApi } from '../api/endpoints.js'
import { api } from '../api/client.js'
import { useFetch, useDebounced } from '../hooks/useFetch.js'
import { useAuth } from '../auth/AuthContext.jsx'
import {
  PageHeader, Panel, Button, DataTable, Badge, StatusBadge, Tabs,
  EmptyState, LoadingState, ErrorState, SearchInput, StatCard, Field,
  Select, Alert, useToast, useConfirm, Modal, TextInput, CopyButton,
} from '../components/ui/index.jsx'
import {
  ShieldCheck, Users, History, RefreshCw, KeyRound, Shield,
  UserCheck, UserPlus, Info,
} from '../components/icons.jsx'

const ROLES = ['admin', 'data_scientist', 'ml_engineer', 'viewer']

const ROLE_META = {
  admin: {
    tone: 'accent',
    desc: 'Full access: datasets, training, deployments and user management.',
    scopes: ['datasets:view', 'datasets:upload', 'datasets:delete', 'models:train', 'models:view', 'models:deploy', 'deployments:manage', 'users:manage'],
  },
  data_scientist: {
    tone: 'info',
    desc: 'Upload datasets, train models, deploy and manage deployments.',
    scopes: ['datasets:view', 'datasets:upload', 'datasets:delete', 'models:train', 'models:view', 'models:deploy', 'deployments:manage'],
  },
  ml_engineer: {
    tone: 'purple',
    desc: 'Train and deploy models; read-only on datasets.',
    scopes: ['datasets:view', 'models:train', 'models:view', 'models:deploy', 'deployments:manage'],
  },
  viewer: {
    tone: 'outline',
    desc: 'Read-only access to models and registry contents.',
    scopes: ['models:view'],
  },
}

export default function Admin() {
  const toast = useToast()
  const confirm = useConfirm()
  const { user: me } = useAuth()

  const [tab, setTab] = useState('users')
  const [query, setQuery] = useState('')
  const [acting, setActing] = useState(null)
  const [inviting, setInviting] = useState(false)
  const debounced = useDebounced(query, 200)

  const users = useFetch(() => usersApi.list(), [])
  const logs = useFetch(() => usersApi.auditLogs(), [])

  const userList = users.data?.users || (Array.isArray(users.data) ? users.data : [])
  const logList = logs.data?.logs || logs.data?.audit_logs || (Array.isArray(logs.data) ? logs.data : [])

  const filteredUsers = userList.filter((u) =>
    !debounced
    || [u.username, u.email, u.role].some((v) => String(v || '').toLowerCase().includes(debounced.toLowerCase())),
  )

  const filteredLogs = logList.filter((l) =>
    !debounced
    || [l.action, l.username, l.details, l.ip_address].some((v) => String(v || '').toLowerCase().includes(debounced.toLowerCase())),
  )

  async function changeRole(u, role) {
    if (u.role === role) return
    const id = u.user_id || u.id
    const ok = await confirm({
      title: `Change ${u.username} to ${role}?`,
      text: `Their effective permissions change immediately. The backend re-validates the new scope on every subsequent request.`,
      confirmLabel: 'Change role',
      tone: 'info',
    })
    if (!ok) return
    setActing(id)
    try {
      await usersApi.updateRole(id, role)
      toast.success('Role updated', `${u.username} is now ${role}.`)
      users.refetch()
    } catch (err) {
      toast.error('Role update failed', err?.message)
    } finally { setActing(null) }
  }

  const counts = {
    total: userList.length,
    admins: userList.filter((u) => u.role === 'admin').length,
    active: userList.filter((u) => u.is_active !== false).length,
    events: logList.length,
  }

  return (
    <div className="page">
      <PageHeader
        icon={ShieldCheck}
        title="Admin"
        subtitle="Users, roles and the security audit trail"
        actions={
          <>
            <Button variant="secondary" size="sm" icon={RefreshCw} onClick={() => { users.refetch(); logs.refetch() }} loading={users.loading}>
              Refresh
            </Button>
            <Button variant="primary" size="sm" icon={UserPlus} onClick={() => setInviting(true)}>
              Add user
            </Button>
          </>
        }
      />

      <div className="stat-grid">
        <StatCard icon={Users} tone="accent" value={counts.total} label="Registered users" sub="all accounts" />
        <StatCard icon={Shield} tone="warning" value={counts.admins} label="Administrators" sub="full access" />
        <StatCard icon={UserCheck} tone="success" value={counts.active} label="Active" sub="not disabled" />
        <StatCard icon={History} tone="info" value={counts.events} label="Audit events" sub="recorded actions" />
      </div>

      <div className="flex items-center justify-between gap-3 flex-wrap">
        <Tabs
          active={tab}
          onChange={setTab}
          tabs={[
            { id: 'users', label: 'Users', icon: Users },
            { id: 'roles', label: 'Role matrix', icon: Shield },
            { id: 'audit', label: 'Audit logs', icon: History },
          ]}
        />
        <SearchInput
          value={query}
          onChange={setQuery}
          placeholder={tab === 'audit' ? 'Search events…' : 'Search users…'}
        />
      </div>

      {/* ── Users ───────────────────────────────── */}
      {tab === 'users' && (
        <Panel icon={Users} title="Users" subtitle={`${filteredUsers.length} of ${userList.length}`} bodyClass={filteredUsers.length ? 'tight' : ''}>
          {users.loading ? (
            <LoadingState label="Loading users…" />
          ) : users.error ? (
            <ErrorState error={users.error} title="Could not load users" onRetry={users.refetch} />
          ) : filteredUsers.length === 0 ? (
            <EmptyState icon={Users} title={userList.length ? 'No match' : 'No users'} desc="Accounts appear here after registration." />
          ) : (
            <DataTable
              caption="Registered users"
              keyOf={(u) => u.user_id || u.id}
              rows={filteredUsers}
              pageSize={10}
              columns={[
                {
                  key: 'username', strong: true, header: 'User',
                  render: (u) => (
                    <span className="flex items-center gap-2">
                      <span className="avatar avatar-sm">{String(u.username).slice(0, 2)}</span>
                      {u.username}
                      {(u.user_id || u.id) === (me?.user_id || me?.id) && <Badge tone="accent" sm>you</Badge>}
                    </span>
                  ),
                },
                { key: 'email', header: 'Email', render: (u) => <span className="muted text-xs">{u.email || '—'}</span> },
                {
                  key: 'role', header: 'Role',
                  width: 210,
                  render: (u) => (
                    <Select
                      value={u.role}
                      disabled={acting === (u.user_id || u.id)}
                      onChange={(e) => changeRole(u, e.target.value)}
                      aria-label={`Role for ${u.username}`}
                      style={{ maxWidth: 200 }}
                    >
                      {ROLES.map((r) => <option key={r} value={r}>{r}</option>)}
                    </Select>
                  ),
                },
                {
                  key: 'is_active', width: 110, header: 'Status',
                  render: (u) => <StatusBadge status={u.is_active === false ? 'inactive' : 'active'} sm />,
                },
                {
                  key: 'scopes', width: 150, header: 'Access',
                  render: (u) => <Badge tone="outline" sm>{(ROLE_META[u.role]?.scopes || []).length} scopes</Badge>,
                },
              ]}
            />
          )}
        </Panel>
      )}

      {/* ── Role matrix ─────────────────────────── */}
      {tab === 'roles' && (
        <Panel icon={Shield} title="Role permission matrix" subtitle="What each role can do — the API enforces every scope">
          <div className="grid-2">
            {ROLES.map((r) => {
              const meta = ROLE_META[r]
              const count = userList.filter((u) => u.role === r).length
              return (
                <div key={r} className="row-item is-top">
                  <span className="row-glyph is-accent"><Shield size={15} /></span>
                  <span className="row-info">
                    <span className="row-title flex items-center gap-2">
                      {r} <Badge tone={meta.tone} sm>{count} user{count === 1 ? '' : 's'}</Badge>
                    </span>
                    <span className="row-sub">{meta.desc}</span>
                  </span>
                </div>
              )
            })}
          </div>

          <div className="mt-4">
            <div className="section-title">Scope reference</div>
            <div className="pill-row">
              {['datasets:view', 'datasets:upload', 'datasets:delete', 'models:view', 'models:train', 'models:deploy', 'deployments:manage', 'users:manage'].map((s) => (
                <span key={s} className="ref-chip"><KeyRound size={11} /> {s}</span>
              ))}
            </div>
          </div>
        </Panel>
      )}

      {/* ── Audit logs ──────────────────────────── */}
      {tab === 'audit' && (
        <Panel
          icon={History}
          title="Audit logs"
          subtitle={`${filteredLogs.length} of ${logList.length} events`}
          actions={<Button variant="ghost" size="sm" icon={RefreshCw} onClick={logs.refetch}>Refresh</Button>}
          bodyClass={filteredLogs.length ? 'tight' : ''}
        >
          {logs.loading ? (
            <LoadingState label="Loading audit logs…" />
          ) : logs.error ? (
            <ErrorState error={logs.error} title="Could not load audit logs" onRetry={logs.refetch} />
          ) : filteredLogs.length === 0 ? (
            <EmptyState icon={History} title="No audit events" desc="Security-relevant actions are recorded here as they happen." />
          ) : (
            <DataTable
              caption="Security audit log"
              keyOf={(l, i) => l.id || `${l.timestamp}-${i}`}
              rows={filteredLogs}
              pageSize={15}
              columns={[
                {
                  key: 'timestamp', width: 190, header: 'When',
                  render: (l) => <span className="text-xs muted">{l.timestamp ? new Date(l.timestamp).toLocaleString() : '—'}</span>,
                },
                {
                  key: 'action', strong: true, header: 'Action',
                  render: (l) => <Badge tone="outline">{l.action}</Badge>,
                },
                { key: 'username', header: 'Actor', render: (l) => <span className="text-xs">{l.username || '—'}</span> },
                { key: 'ip_address', header: 'IP', render: (l) => <code className="mono text-xs">{l.ip_address || '—'}</code> },
                {
                  key: 'details', header: 'Details',
                  render: (l) => <span className="muted text-xs truncate" style={{ maxWidth: 320, display: 'inline-block' }} title={String(l.details || '')}>{String(l.details || '—').slice(0, 120)}</span>,
                },
              ]}
            />
          )}
        </Panel>
      )}

      <Panel icon={Info} title="About role changes" subtitle="What happens when you promote or demote a user">
        <Alert tone="warning">
          Roles are enforced server-side. Changing a role here updates the user record, and the backend
          applies the new scope list on their very next request — no re-login is required, but any
          tokens already issued keep working until they expire.
        </Alert>
        <div className="mt-3 flex items-center gap-2">
          <code className="mono text-xs muted">PUT /users/&#123;user_id&#125;/role &#123; "role": "data_scientist" &#125;</code>
          <CopyButton text='PUT /users/{user_id}/role {"role":"data_scientist"}' />
        </div>
      </Panel>

      {inviting && <AddUserModal onClose={() => setInviting(false)} onDone={() => { setInviting(false); users.refetch() }} />}
    </div>
  )
}

/* ── Register a new account ─────────────────────── */
function AddUserModal({ onClose, onDone }) {
  const toast = useToast()
  const [form, setForm] = useState({ username: '', email: '', password: '' })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  async function submit(e) {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      // Registration is public; the seeded roles are assigned by an admin afterwards.
      await api.post('/auth/register', {
        username: form.username.trim(),
        email: form.email.trim() || `${form.username.trim()}@example.com`,
        password: form.password,
      }, { skipAuth: true })
      toast.success('Account created', `${form.username} can now sign in. Assign a role from the list.`)
      onDone()
    } catch (err) {
      setError(err?.message || 'Registration failed.')
      toast.error('Registration failed', err?.message)
    } finally { setBusy(false) }
  }

  return (
    <Modal
      open
      onClose={onClose}
      title="Add a user"
      subtitle="Creates an account with the default viewer role — promote it afterwards"
      footer={
        <>
          <Button variant="secondary" onClick={onClose}>Cancel</Button>
          <Button variant="primary" icon={UserPlus} loading={busy} onClick={submit}>Create account</Button>
        </>
      }
    >
      <form onSubmit={submit} className="flex flex-col gap-4">
        {error && <Alert tone="error">{error}</Alert>}
        <Field label="Username" required htmlFor="nu-user">
          <TextInput id="nu-user" value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })} autoComplete="off" />
        </Field>
        <Field label="Email" htmlFor="nu-email">
          <TextInput id="nu-email" type="email" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} />
        </Field>
        <Field label="Password" htmlFor="nu-pass" hint="8+ chars, upper, lower, digit and special character">
          <TextInput id="nu-pass" type="password" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} autoComplete="new-password" />
        </Field>
        <button type="submit" hidden />
      </form>
    </Modal>
  )
}
