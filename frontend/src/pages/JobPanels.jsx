/* ══════════════════════════════════════════════════════
   Chart-free job panels.

   These used to live in JobResults.jsx, which statically imports recharts
   (~420 kB). Pipeline and Jobs only need the console / summary on first
   paint, so keeping them separate lets those routes defer the chart bundle
   until results are actually rendered.
   ══════════════════════════════════════════════════════ */
import { useEffect, useRef } from 'react'
import { Panel, Badge } from '../components/ui/index.jsx'
import { Terminal } from '../components/icons.jsx'
import { formatAccuracy } from '../lib/models.js'

// Human labels for the `source` field of GET /models/jobs/{id}/logs. Saying
// which one answered beats leaving the reader to guess whether the lines
// they are looking at are live or a replay of a finished run.
const SOURCE_LABEL = {
  kubernetes: 'from the live head pod',
  minio: 'from the MinIO archive',
  unavailable: 'no archive found',
}

/* ── Training console (live log stream) ───────────── */
export function TrainingConsole({ logs, isTraining, source = null, empty = 'No output yet.' }) {
  const boxRef = useRef(null)
  // Follow the tail. `.console` is a 320px scroll box, so without this the
  // newest line lands below the fold and you have to scroll by hand to watch
  // a run progress. Only follow when the reader is already at the bottom —
  // yanking the view away from someone reading earlier output is worse than
  // not scrolling at all.
  const stick = useRef(true)
  useEffect(() => {
    const el = boxRef.current
    if (el && stick.current) el.scrollTop = el.scrollHeight
  }, [logs])

  const sourceLabel = SOURCE_LABEL[source]

  return (
    <Panel
      icon={Terminal}
      title="Training console"
      subtitle={
        `${isTraining ? 'Streaming live' : 'Real-time'} pipeline logs`
        + (sourceLabel ? ` · ${sourceLabel}` : '')
      }
      actions={
        isTraining
          ? <Badge tone="warning" dot>Live</Badge>
          : <Badge tone="neutral">Idle</Badge>
      }
      bodyClass="tight"
    >
      <div style={{ padding: 'var(--sp-4) var(--sp-5)' }}>
        <div
          ref={boxRef}
          className="console"
          role="log"
          aria-live="polite"
          onScroll={(e) => {
            const el = e.currentTarget
            stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 32
          }}
        >
          {!logs?.length ? (
            <span style={{ color: '#6b7688' }}>{empty}</span>
          ) : (
            logs.map((l, i) => (
              <div key={i} className={`log-line log-${l.level}`}>
                <span className="log-ts">{l.time}</span>
                <span className="log-level">{String(l.level).toUpperCase()}</span>
                <span className="log-msg">{l.msg}</span>
              </div>
            ))
          )}
        </div>
      </div>
    </Panel>
  )
}

/* ── Job detail summary used by the Jobs drawer ───── */
export function JobSummary({ job }) {
  if (!job) return null
  // Backend `progress` is a fixed stage marker (pending=10, training=50,
  // terminal=100) — JobService.update_progress() is never called, so printing
  // it as a percentage implies measurement that doesn't exist.
  const staged = ['pending', 'training'].includes(String(job.status || '').toLowerCase())
  const items = [
    ['Job ID', job.job_id],
    ['Status', job.status],
    ['Model', job.model_type || job.model_name || '—'],
    ['Dataset', job.dataset_id || '—'],
    [staged ? 'Stage' : 'Progress', staged ? String(job.status) : `${job.progress || 0}%`],
    ['Accuracy', formatAccuracy(job.accuracy)],
    ['Precision', job.precision_score != null ? Number(job.precision_score).toFixed(4) : '—'],
    ['Recall', job.recall_score != null ? Number(job.recall_score).toFixed(4) : '—'],
    ['F1 Score', job.f1_score != null ? Number(job.f1_score).toFixed(4) : '—'],
    ['Duration', job.training_duration != null ? `${Number(job.training_duration).toFixed(2)}s` : '—'],
    ['Started', job.started_at ? new Date(job.started_at).toLocaleString() : '—'],
    ['Completed', job.completed_at ? new Date(job.completed_at).toLocaleString() : '—'],
    ['Error', job.error_message || '—'],
  ]
  return (
    <div className="kv-list" style={{ marginBottom: 'var(--sp-4)' }}>
      {items.map(([k, v]) => (
        <div className="kv-row" key={k}>
          <span className="kv-key">{k}</span>
          <span className="kv-val" style={k === 'Error' && v !== '—' ? { color: 'var(--danger-text)' } : undefined}>{v}</span>
        </div>
      ))}
    </div>
  )
}
