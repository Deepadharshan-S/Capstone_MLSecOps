import { useState, useEffect, useCallback, createContext, useContext, useRef } from 'react'
import {
  X, AlertCircle, CheckCircle2, Info, AlertTriangle, ChevronLeft, ChevronRight,
  ChevronsUpDown, Search, Inbox, ArrowUp, ArrowDown, Loader2, RefreshCw, Copy,
} from '../icons.jsx'

/* ══════════════════════════════════════════════════════
   Buttons
   ══════════════════════════════════════════════════════ */
export function Button({
  children, variant = 'secondary', size, icon: Icon, loading = false,
  block = false, iconOnly = false, className = '', ...rest
}) {
  const cls = [
    'btn',
    `btn-${variant}`,
    size ? `btn-${size}` : '',
    block ? 'btn-block' : '',
    iconOnly ? 'btn-icon' : '',
    loading ? 'btn-loading' : '',
    className,
  ].filter(Boolean).join(' ')
  return (
    <button className={cls} disabled={loading || rest.disabled} {...rest}>
      {Icon && <Icon size={size === 'xs' ? 12 : size === 'sm' ? 13 : 15} strokeWidth={2} />}
      {!iconOnly && children}
    </button>
  )
}

/* ══════════════════════════════════════════════════════
   Form fields
   ══════════════════════════════════════════════════════ */
export function Field({ label, hint, error, required, children, htmlFor }) {
  return (
    <div className="input-group">
      {label && (
        <label className="input-label" htmlFor={htmlFor}>
          {label} {required && <span className="req">*</span>}
        </label>
      )}
      {children}
      {error
        ? <span className="input-error"><AlertCircle size={11} /> {error}</span>
        : hint ? <span className="input-hint">{hint}</span> : null}
    </div>
  )
}

export function TextInput({ invalid, className = '', ...rest }) {
  return <input className={`input-field ${invalid ? 'is-invalid' : ''} ${className}`} {...rest} />
}

export function Select({ className = '', children, ...rest }) {
  return <select className={`select-field ${className}`} {...rest}>{children}</select>
}

export function TextArea({ className = '', ...rest }) {
  return <textarea className={`textarea-field ${className}`} {...rest} />
}

export function SearchInput({ value, onChange, placeholder = 'Search…', className = '' }) {
  return (
    <div className={`search-box ${className}`}>
      <Search size={14} />
      <input
        className="input-field"
        type="search"
        value={value}
        placeholder={placeholder}
        aria-label={placeholder}
        onChange={(e) => onChange(e.target.value)}
      />
    </div>
  )
}

/* ══════════════════════════════════════════════════════
   Badges / chips / dots
   ══════════════════════════════════════════════════════ */
const BADGE_TONES = ['neutral', 'accent', 'success', 'warning', 'danger', 'info', 'purple', 'outline']

export function Badge({ children, tone = 'neutral', dot = false, sm = false, className = '' }) {
  const t = BADGE_TONES.includes(tone) ? tone : 'neutral'
  return (
    <span className={`badge badge-${t} ${dot ? 'badge-dot' : ''} ${sm ? 'badge-sm' : ''} ${className}`}>
      {children}
    </span>
  )
}

/** Shared status → badge tone mapping so every table agrees. */
export function statusTone(status) {
  const s = String(status || '').toLowerCase()
  // Terminal success states.
  if (['completed', 'complete', 'success', 'succeeded', 'active', 'healthy', 'ready', 'ok', 'deployed', 'passed'].includes(s)) return 'success'
  if (['failed', 'error', 'unhealthy', 'down', 'revoked', 'crashed', 'killed', 'rejected'].includes(s)) return 'danger'
  // In-progress states (including a job that is still 'running').
  if (['running', 'training', 'pending', 'queued', 'in_progress', 'starting', 'deploying', 'degraded', 'warning'].includes(s)) return 'warning'
  if (['stopped', 'inactive', 'disabled', 'cancelled', 'canceled', 'draft', 'archived', 'none'].includes(s)) return 'outline'
  return 'info'
}

export function StatusBadge({ status, sm = false }) {
  if (!status) return null
  return <Badge tone={statusTone(status)} dot sm={sm}>{String(status).replace(/_/g, ' ')}</Badge>
}

/* ══════════════════════════════════════════════════════
   Panels & stat cards
   ══════════════════════════════════════════════════════ */
export function Panel({ icon: Icon, iconTone, title, subtitle, actions, children, className = '', bodyClass = '', ...rest }) {
  return (
    <section className={`panel ${className}`} {...rest}>
      {(title || actions) && (
        <header className="panel-header">
          <div className="panel-title-row">
            {Icon && (
              <div className={`panel-icon ${iconTone ? `is-${iconTone}` : ''}`}><Icon size={16} /></div>
            )}
            <div style={{ minWidth: 0 }}>
              {title && <div className="panel-title">{title}</div>}
              {subtitle && <div className="panel-subtitle">{subtitle}</div>}
            </div>
          </div>
          {actions && <div className="panel-actions">{actions}</div>}
        </header>
      )}
      <div className={`panel-body ${bodyClass}`}>{children}</div>
    </section>
  )
}

export function StatCard({ icon: Icon, tone = 'accent', value, label, sub, onClick }) {
  const Tag = onClick ? 'button' : 'div'
  return (
    <Tag className="stat-card" onClick={onClick} style={onClick ? { textAlign: 'left', width: '100%', cursor: 'pointer' } : undefined}>
      {Icon && <div className={`stat-icon t-${tone}`}><Icon size={17} /></div>}
      <div className="stat-body">
        <div className="stat-value">{value}</div>
        <div className="stat-label">{label}</div>
        {sub && <div className="stat-sub">{sub}</div>}
      </div>
    </Tag>
  )
}

/* ══════════════════════════════════════════════════════
   Tabs (pill + underline variants)
   ══════════════════════════════════════════════════════ */
export function Tabs({ tabs, active, onChange, variant = 'pill', className = '' }) {
  return (
    <div className={`${variant === 'underline' ? 'tabs-underline' : 'tabs-row'} ${className}`} role="tablist">
      {tabs.map((t) => {
        const id = typeof t === 'string' ? t : t.id
        const label = typeof t === 'string' ? t : t.label
        const Icon = typeof t === 'string' ? null : t.icon
        return (
          <button
            key={id}
            role="tab"
            aria-selected={active === id}
            className={`tab-btn ${active === id ? 'tab-active' : ''}`}
            onClick={() => onChange(id)}
            type="button"
          >
            {Icon && <Icon size={14} />}
            {label}
          </button>
        )
      })}
    </div>
  )
}

export function Segmented({ options, value, onChange }) {
  return (
    <div className="seg">
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          className={`seg-btn ${value === o.value ? 'is-active' : ''}`}
          onClick={() => onChange(o.value)}
        >
          {o.icon && <o.icon size={13} />}
          {o.label}
        </button>
      ))}
    </div>
  )
}

/* ══════════════════════════════════════════════════════
   Modal & drawer
   ══════════════════════════════════════════════════════ */
export function Modal({ open, onClose, title, subtitle, children, footer, size = '', closeOnBackdrop = true }) {
  useEffect(() => {
    if (!open) return undefined
    const onKey = (e) => { if (e.key === 'Escape') onClose?.() }
    document.addEventListener('keydown', onKey)
    const prev = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      document.removeEventListener('keydown', onKey)
      document.body.style.overflow = prev
    }
  }, [open, onClose])

  if (!open) return null
  return (
    <div
      className="overlay"
      role="dialog"
      aria-modal="true"
      aria-label={typeof title === 'string' ? title : undefined}
      onMouseDown={(e) => { if (closeOnBackdrop && e.target === e.currentTarget) onClose?.() }}
    >
      <div className={`modal-card ${size ? `modal-${size}` : ''}`}>
        <div className="modal-header">
          <div style={{ minWidth: 0 }}>
            <div className="modal-title">{title}</div>
            {subtitle && <div className="modal-sub">{subtitle}</div>}
          </div>
          <button className="modal-close" onClick={onClose} aria-label="Close dialog"><X size={16} /></button>
        </div>
        <div className="modal-body">{children}</div>
        {footer && <div className="modal-footer">{footer}</div>}
      </div>
    </div>
  )
}

export function Drawer({ open, onClose, title, subtitle, children, footer, wide = false }) {
  useEffect(() => {
    if (!open) return undefined
    const onKey = (e) => { if (e.key === 'Escape') onClose?.() }
    document.addEventListener('keydown', onKey)
    const prev = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      document.removeEventListener('keydown', onKey)
      document.body.style.overflow = prev
    }
  }, [open, onClose])

  if (!open) return null
  return (
    <div
      className="overlay drawer-overlay"
      role="dialog"
      aria-modal="true"
      onMouseDown={(e) => { if (e.target === e.currentTarget) onClose?.() }}
    >
      <div className={`drawer-card ${wide ? 'drawer-wide' : ''}`}>
        <div className="modal-header">
          <div style={{ minWidth: 0 }}>
            <div className="modal-title">{title}</div>
            {subtitle && <div className="modal-sub">{subtitle}</div>}
          </div>
          <button className="modal-close" onClick={onClose} aria-label="Close panel"><X size={16} /></button>
        </div>
        <div className="modal-body">{children}</div>
        {footer && <div className="modal-footer">{footer}</div>}
      </div>
    </div>
  )
}

/* ══════════════════════════════════════════════════════
   Toasts
   ══════════════════════════════════════════════════════ */
const ToastCtx = createContext(null)
let toastSeq = 0

export function ToastProvider({ children }) {
  const [toasts, setToasts] = useState([])

  const dismiss = useCallback((id) => setToasts((t) => t.filter((x) => x.id !== id)), [])

  const push = useCallback((type, title, message) => {
    const id = ++toastSeq
    setToasts((t) => [...t, { id, type, title, message }])
    setTimeout(() => dismiss(id), type === 'error' ? 7000 : 4500)
    return id
  }, [dismiss])

  const api = useRef({
    success: (title, msg) => push('success', title, msg),
    error: (title, msg) => push('error', title, msg),
    info: (title, msg) => push('info', title, msg),
    warning: (title, msg) => push('warning', title, msg),
    dismiss,
  }).current
  api.dismiss = dismiss

  const ICONS = { success: CheckCircle2, error: AlertCircle, warning: AlertTriangle, info: Info }

  return (
    <ToastCtx.Provider value={api}>
      {children}
      <div className="toast-host" aria-live="polite">
        {toasts.map((t) => {
          const Icon = ICONS[t.type] || Info
          return (
            <div key={t.id} className={`toast toast-${t.type}`}>
              <span className="toast-icon"><Icon size={16} /></span>
              <div className="toast-body">
                <div className="toast-title">{t.title}</div>
                {t.message && <div className="toast-msg">{t.message}</div>}
              </div>
              <button className="toast-close" onClick={() => dismiss(t.id)} aria-label="Dismiss"><X size={13} /></button>
            </div>
          )
        })}
      </div>
    </ToastCtx.Provider>
  )
}

export function useToast() {
  const ctx = useContext(ToastCtx)
  if (!ctx) {
    // Fallback no-ops so components stay usable in isolation (tests, stories).
    return { success() {}, error() {}, info() {}, warning() {}, dismiss() {} }
  }
  return ctx
}

/* ══════════════════════════════════════════════════════
   Confirm dialog
   ══════════════════════════════════════════════════════ */
const ConfirmCtx = createContext(null)

export function ConfirmProvider({ children }) {
  const [state, setState] = useState(null) // {title,text,confirmLabel,tone,onResolve}

  const confirm = useCallback((opts) => new Promise((resolve) => {
    setState({ confirmLabel: 'Confirm', tone: 'danger', ...opts, onResolve: resolve })
  }), [])

  const close = (result) => {
    state?.onResolve?.(result)
    setState(null)
  }

  return (
    <ConfirmCtx.Provider value={confirm}>
      {children}
      {state && (
        <div className="overlay" onMouseDown={(e) => { if (e.target === e.currentTarget) close(false) }}>
          <div className="modal-card" style={{ maxWidth: 440 }} role="alertdialog" aria-modal="true">
            <div className="modal-body">
              <div className={`confirm-icon ${state.tone === 'danger' ? '' : `is-${state.tone}`}`}>
                {state.tone === 'danger' ? <AlertTriangle size={19} /> : state.tone === 'warning' ? <AlertTriangle size={19} /> : <Info size={19} />}
              </div>
              <div className="confirm-title">{state.title}</div>
              <div className="confirm-text">{state.text}</div>
            </div>
            <div className="modal-footer">
              <Button variant="secondary" onClick={() => close(false)}>{state.cancelLabel || 'Cancel'}</Button>
              <Button variant={state.tone === 'danger' ? 'danger' : 'primary'} onClick={() => close(true)}>
                {state.confirmLabel}
              </Button>
            </div>
          </div>
        </div>
      )}
    </ConfirmCtx.Provider>
  )
}

export function useConfirm() {
  const ctx = useContext(ConfirmCtx)
  if (!ctx) return async () => true
  return ctx
}

/* ══════════════════════════════════════════════════════
   Alert (inline banner)
   ══════════════════════════════════════════════════════ */
const ALERT_ICONS = { error: AlertCircle, success: CheckCircle2, warning: AlertTriangle, info: Info }

export function Alert({ tone = 'info', children, actions, icon: Icon }) {
  const ResolvedIcon = Icon || ALERT_ICONS[tone] || Info
  return (
    <div className={`alert alert-${tone}`} role={tone === 'error' ? 'alert' : undefined}>
      <ResolvedIcon size={15} />
      <div className="alert-body">{children}</div>
      {actions && <div className="alert-actions">{actions}</div>}
    </div>
  )
}

/* ══════════════════════════════════════════════════════
   Progress
   ══════════════════════════════════════════════════════ */
export function Progress({ value = 0, label, tone, showValue = true }) {
  const v = Math.max(0, Math.min(100, Number(value) || 0))
  return (
    <div>
      {(label || showValue) && (
        <div className="prog-header">
          <span>{label}</span>
          {showValue && <span className="prog-value tnum">{Math.round(v)}%</span>}
        </div>
      )}
      <div
        className="prog-track"
        role="progressbar"
        aria-label={label || 'Progress'}
        aria-valuenow={Math.round(v)}
        aria-valuemin={0}
        aria-valuemax={100}
      >
        <div className={`prog-fill ${tone ? `is-${tone}` : ''}`} style={{ width: `${v}%` }} />
      </div>
    </div>
  )
}

/* ══════════════════════════════════════════════════════
   Loading / empty / skeleton
   ══════════════════════════════════════════════════════ */
export function Spinner({ lg = false }) {
  return <span className={`spinner ${lg ? 'spinner-lg' : ''}`} role="status" aria-label="Loading" />
}

export function LoadingState({ label = 'Loading…', lg = false }) {
  return (
    <div className="empty-state">
      <Spinner lg={lg} />
      <div className="empty-state-desc" style={{ marginTop: 8 }}>{label}</div>
    </div>
  )
}

export function EmptyState({ icon: Icon = Inbox, title, desc, actions }) {
  return (
    <div className="empty-state">
      <div className="empty-state-icon"><Icon size={22} /></div>
      {title && <div className="empty-state-title">{title}</div>}
      {desc && <div className="empty-state-desc">{desc}</div>}
      {actions && <div className="empty-state-actions">{actions}</div>}
    </div>
  )
}

export function ErrorState({ error, onRetry, title = 'Something went wrong' }) {
  const msg = typeof error === 'string' ? error : error?.message || 'Unexpected error.'
  return (
    <div className="empty-state">
      <div className="empty-state-icon" style={{ background: 'var(--danger-soft)', color: 'var(--danger)' }}>
        <AlertCircle size={22} />
      </div>
      <div className="empty-state-title">{title}</div>
      <div className="empty-state-desc">{msg}</div>
      {onRetry && (
        <div className="empty-state-actions">
          <Button variant="secondary" icon={RefreshCw} onClick={onRetry}>Retry</Button>
        </div>
      )}
    </div>
  )
}

export function SkeletonRows({ rows = 5 }) {
  return (
    <div aria-hidden>
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="skeleton skeleton-row" style={{ width: `${94 - (i % 4) * 12}%` }} />
      ))}
    </div>
  )
}

export function SkeletonCards({ count = 4 }) {
  return (
    <div className="stat-grid" aria-hidden>
      {Array.from({ length: count }, (_, i) => <div key={i} className="skeleton skeleton-card" />)}
    </div>
  )
}

/* ══════════════════════════════════════════════════════
   Key / value list
   ══════════════════════════════════════════════════════ */
export function KeyValue({ items }) {
  return (
    <div className="kv-list">
      {items.filter(Boolean).map(([k, v]) => (
        <div className="kv-row" key={k}>
          <span className="kv-key">{k}</span>
          <span className="kv-val">{v == null || v === '' ? '—' : v}</span>
        </div>
      ))}
    </div>
  )
}

/* ══════════════════════════════════════════════════════
   Tooltip (hover / focus)
   ══════════════════════════════════════════════════════ */
export function Tooltip({ label, children }) {
  const [show, setShow] = useState(false)
  return (
    <span
      className="tooltip-wrap"
      onMouseEnter={() => setShow(true)}
      onMouseLeave={() => setShow(false)}
      onFocus={() => setShow(true)}
      onBlur={() => setShow(false)}
    >
      {children}
      {show && <span className="tooltip-bubble" role="tooltip">{label}</span>}
    </span>
  )
}

/* Fallback header label — an empty <th> gives screen readers no column name
   and trips axe's td-has-header rule. Derive one from the column key. */
function humanizeHeader(key) {
  if (!key) return ''
  return String(key)
    .replace(/_/g, ' ')
    .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
    .replace(/\b\w/g, (c) => c.toUpperCase())
}

/* ══════════════════════════════════════════════════════
   Data table with optional client-side sort + pagination
   ══════════════════════════════════════════════════════ */
export function DataTable({
  columns, rows, keyOf, onRowClick, sort, onSortChange,
  pageSize = 0, page = 0, onPageChange, empty, caption,
}) {
  // Internal fallback pagination when the caller doesn't control it.
  const [internalPage, setInternalPage] = useState(0)
  const effectivePage = pageSize ? (onPageChange ? page : internalPage) : 0
  const pageCount = pageSize ? Math.max(1, Math.ceil((rows?.length || 0) / pageSize)) : 1
  const visible = pageSize
    ? (rows || []).slice(effectivePage * pageSize, effectivePage * pageSize + pageSize)
    : (rows || [])

  const setPage = (p) => {
    if (onPageChange) onPageChange(p)
    else setInternalPage(p)
  }

  if (!rows?.length && empty) return empty

  const handleSort = (col) => {
    if (!col.sortKey || !onSortChange) return
    const nextDir = sort?.key === col.sortKey && sort.dir === 'asc' ? 'desc' : 'asc'
    onSortChange({ key: col.sortKey, dir: nextDir })
  }

  return (
    <>
      <div className="table-wrap">
        <table className="data-table">
          {caption && <caption className="sr-only">{caption}</caption>}
          <thead>
            <tr>
              {columns.map((col, i) => {
                // `active` requires both a sortKey and an active sort —
                // comparing two undefined values would otherwise be true.
                const active = Boolean(col.sortKey && sort && sort.key === col.sortKey)
                return (
                  <th
                    key={col.key || i}
                    className={col.sortKey && onSortChange ? 'sortable' : ''}
                    style={col.width ? { width: col.width } : undefined}
                    onClick={() => handleSort(col)}
                    aria-sort={active ? (sort.dir === 'asc' ? 'ascending' : 'descending') : undefined}
                  >
                    <span className="th-inner">
                      {col.header ?? humanizeHeader(col.key)}
                      {col.sortKey && onSortChange && (
                        active
                          ? (sort.dir === 'asc' ? <ArrowUp size={11} /> : <ArrowDown size={11} />)
                          : <ChevronsUpDown size={11} style={{ opacity: .45 }} />
                      )}
                    </span>
                  </th>
                )
              })}
            </tr>
          </thead>
          <tbody>
            {visible.map((row, ri) => (
              <tr
                // keyOf may take (row, index) — pass the index so fallbacks
                // like `${timestamp}-${i}` stay unique across equal timestamps.
                key={keyOf ? keyOf(row, ri) : ri}
                className={`${onRowClick ? 'row-clickable' : ''} ${row.__selected ? 'row-selected' : ''}`}
                onClick={onRowClick ? () => onRowClick(row) : undefined}
              >
                {columns.map((col, ci) => (
                  <td
                    key={col.key || ci}
                    className={`${col.strong ? 'cell-strong' : ''} ${col.actions ? 'cell-actions' : ''}`}
                    onClick={col.actions ? (e) => e.stopPropagation() : undefined}
                  >
                    {col.render ? col.render(row) : row[col.key]}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {pageSize > 0 && (
        <div className="table-footer">
          <span>
            {rows?.length
              ? `${effectivePage * pageSize + 1}–${Math.min((effectivePage + 1) * pageSize, rows.length)} of ${rows.length}`
              : '0 results'}
          </span>
          {pageCount > 1 && (
            <div className="pagination">
              <button className="page-btn" disabled={effectivePage === 0} onClick={() => setPage(effectivePage - 1)} aria-label="Previous page">
                <ChevronLeft size={14} />
              </button>
              {Array.from({ length: pageCount }, (_, i) => i)
                .filter((i) => i === 0 || i === pageCount - 1 || Math.abs(i - effectivePage) <= 1)
                .map((i, idx, arr) => (
                  <span key={i} className="flex items-center gap-1">
                    {idx > 0 && arr[idx - 1] !== i - 1 && <span className="muted" style={{ padding: '0 2px' }}>…</span>}
                    <button className={`page-btn ${i === effectivePage ? 'is-active' : ''}`} onClick={() => setPage(i)}>
                      {i + 1}
                    </button>
                  </span>
                ))}
              <button className="page-btn" disabled={effectivePage >= pageCount - 1} onClick={() => setPage(effectivePage + 1)} aria-label="Next page">
                <ChevronRight size={14} />
              </button>
            </div>
          )}
        </div>
      )}
    </>
  )
}

/* ══════════════════════════════════════════════════════
   Page header
   ══════════════════════════════════════════════════════ */
export function PageHeader({ icon: Icon, title, subtitle, actions, badge, children }) {
  return (
    <header className="page-header">
      <div className="page-title-row">
        {Icon && <div className="page-icon"><Icon size={19} /></div>}
        <div style={{ minWidth: 0 }}>
          <div className="flex items-center gap-3">
            <h1 className="page-title">{title}</h1>
            {badge}
          </div>
          {subtitle && <div className="page-subtitle">{subtitle}</div>}
        </div>
      </div>
      <div className="page-actions">{actions}{children}</div>
    </header>
  )
}

/* ══════════════════════════════════════════════════════
   Copy-to-clipboard text
   ══════════════════════════════════════════════════════ */
export function CopyButton({ text, size = 'xs' }) {
  const [copied, setCopied] = useState(false)
  return (
    <Button
      variant="ghost"
      size={size}
      icon={copied ? CheckCircle2 : Copy}
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(text)
          setCopied(true)
          setTimeout(() => setCopied(false), 1400)
        } catch { /* clipboard unavailable */ }
      }}
      aria-label="Copy"
    />
  )
}

/* ══════════════════════════════════════════════════════
   Loading button helper
   ══════════════════════════════════════════════════════ */
export function BusyButton({ busy, children, ...rest }) {
  return <Button loading={busy} {...rest}>{children}</Button>
}

export function InlineLoader() {
  return <Loader2 size={13} className="spin-inline" style={{ animation: 'spin .8s linear infinite' }} />
}
