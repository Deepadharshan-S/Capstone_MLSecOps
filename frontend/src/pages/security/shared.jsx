// Shared vocabulary for the Security Center tabs: severity/status badge
// tones, time formatting, and the alert lifecycle rules mirrored from
// backend/app/services/threat_hunting/alert_service.py.

/** Severity → Badge tone (BADGE_TONES in components/ui). */
export function severityTone(severity) {
  switch (String(severity || '').toUpperCase()) {
    case 'CRITICAL': return 'danger'
    case 'HIGH': return 'warning'
    case 'MEDIUM': return 'accent'
    case 'LOW': return 'info'
    default: return 'neutral' // INFO and anything unknown
  }
}

/**
 * Alert status → Badge tone. Deliberately NOT the shared statusTone():
 * an OPEN security alert is an unresolved incident (needs attention),
 * while RESOLVED is the good terminal state — the inverse of job statuses.
 */
export function alertStatusTone(status) {
  switch (String(status || '').toUpperCase()) {
    case 'OPEN': return 'danger'
    case 'INVESTIGATING': return 'warning'
    case 'RESOLVED': return 'success'
    case 'FALSE_POSITIVE': return 'outline'
    default: return 'neutral'
  }
}

// Mirrors VALID_STATUS_TRANSITIONS in backend alert_service.py. FALSE_POSITIVE
// is terminal; RESOLVED may be reopened into INVESTIGATING only.
export const STATUS_TRANSITIONS = {
  OPEN: ['INVESTIGATING', 'RESOLVED', 'FALSE_POSITIVE'],
  INVESTIGATING: ['RESOLVED', 'FALSE_POSITIVE'],
  RESOLVED: ['INVESTIGATING'],
  FALSE_POSITIVE: [],
}

export const ALL_STATUSES = ['OPEN', 'INVESTIGATING', 'RESOLVED', 'FALSE_POSITIVE']
export const ALL_SEVERITIES = ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW', 'INFO']

/** ISO timestamp → locale string; renders '—' for null. */
export function fmtTime(iso) {
  if (!iso) return '—'
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? '—' : d.toLocaleString()
}

/** Short, relative age for polled feeds: "just now", "2m ago", "3h ago". */
export function relTime(iso) {
  if (!iso) return '—'
  const t = new Date(iso).getTime()
  if (Number.isNaN(t)) return '—'
  const s = Math.max(0, Math.floor((Date.now() - t) / 1000))
  if (s < 10) return 'just now'
  if (s < 60) return `${s}s ago`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m ago`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h ago`
  return `${Math.floor(h / 24)}d ago`
}

/**
 * Compact, safe rendering of supporting_evidence / attributes objects.
 * Values can be nested arrays (violations, sample ids) — JSON.stringify
 * those with indentation instead of rendering "[object Object]".
 */
export function fmtValue(v) {
  if (v == null || v === '') return '—'
  if (typeof v === 'boolean') return v ? 'yes' : 'no'
  if (typeof v === 'object') {
    try { return JSON.stringify(v, null, 1) } catch { return String(v) }
  }
  return String(v)
}

/** Convert a datetime-local input value ("2026-10-17T13:45") to ISO-8601. */
export function localInputToIso(v) {
  if (!v) return undefined
  const d = new Date(v)
  return Number.isNaN(d.getTime()) ? undefined : d.toISOString()
}

/** Default hunt window: last 24h, expressed as datetime-local values. */
export function defaultHuntWindow() {
  const fmt = (d) => {
    const p = (n) => String(n).padStart(2, '0')
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`
  }
  const now = new Date()
  return { start: fmt(new Date(now.getTime() - 24 * 3600 * 1000)), end: fmt(now) }
}
