/* ══════════════════════════════════════════════════════
   Chart-free job panels.

   These used to live in JobResults.jsx, which statically imports recharts
   (~420 kB). Pipeline and Jobs only need the console / summary on first
   paint, so keeping them separate lets those routes defer the chart bundle
   until results are actually rendered.
   ══════════════════════════════════════════════════════ */
import { Panel, Badge } from '../components/ui/index.jsx'
import { Terminal } from '../components/icons.jsx'
import { formatAccuracy } from '../lib/models.js'

/* ── Training console (live log stream) ───────────── */
export function TrainingConsole({ logs, isTraining, empty = 'No output yet.' }) {
  return (
    <Panel
      icon={Terminal}
      title="Training console"
      subtitle={isTraining ? 'Streaming live pipeline logs' : 'Real-time pipeline execution logs'}
      actions={
        isTraining
          ? <Badge tone="warning" dot>Live</Badge>
          : <Badge tone="neutral">Idle</Badge>
      }
      bodyClass="tight"
    >
      <div style={{ padding: 'var(--sp-4) var(--sp-5)' }}>
        <div className="console" role="log" aria-live="polite">
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
  const items = [
    ['Job ID', job.job_id],
    ['Status', job.status],
    ['Model', job.model_type || job.model_name || '—'],
    ['Dataset', job.dataset_id || '—'],
    ['Progress', `${job.progress || 0}%`],
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
