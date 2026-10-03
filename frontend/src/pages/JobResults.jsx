import {
  AreaChart, Area, LineChart, Line, RadarChart, Radar,
  PolarGrid, PolarAngleAxis, XAxis, YAxis, CartesianGrid, Tooltip,
  ResponsiveContainer, BarChart, Bar,
} from 'recharts'
import {
  BarChart3, Activity, Target,
} from '../components/icons.jsx'
import { Panel, Progress, Badge, StatusBadge, EmptyState } from '../components/ui/index.jsx'
import { formatAccuracy } from '../lib/models.js'

const C = {
  1: 'var(--c-1)', 2: 'var(--c-2)', 3: 'var(--c-3)',
  4: 'var(--c-4)', 5: 'var(--c-5)', 6: 'var(--c-6)',
}

/* ── Custom Recharts tooltip ─────────────────────── */
export const ChartTip = ({ active, payload, label }) => {
  if (!active || !payload?.length) return null
  return (
    <div className="ct">
      <p>{label}</p>
      {payload.map((p, i) => (
        <p key={i} style={{ color: p.color, fontWeight: 600 }}>
          {p.name}: {typeof p.value === 'number' ? p.value.toFixed(4) : p.value}
        </p>
      ))}
    </div>
  )
}

/* ── Training console (live log stream) ─────────────
   Moved to ./JobPanels.jsx so this module's recharts import stays out of
   first paint — see the note at the top of that file.                   */

/* ── Confusion matrix ─────────────────────────────── */
/**
 * The backend reports confusion_matrix as a dict ({tn,fp,fn,tp}) for binary
 * problems and falls back to {matrix: [[..],[..]]} for multiclass. Older jobs
 * stored a bare 2x2 array — accept all three.
 */
function normalizeCM(cm) {
  if (!cm) return null
  if (Array.isArray(cm)) {
    return cm.length === 2 && Array.isArray(cm[0]) ? cm : null
  }
  if (Array.isArray(cm.matrix)) return normalizeCM(cm.matrix)
  const { tn, fp, fn, tp } = cm
  if ([tn, fp, fn, tp].every((v) => typeof v === 'number')) return [[tn, fp], [fn, tp]]
  return null
}

function ConfusionMatrix({ cm }) {
  const m = normalizeCM(cm)
  if (!m) return null
  const [[tn, fp], [fn, tp]] = m
  const total = (tn + fp + fn + tp) || 1
  const cell = (v, cls) => (
    <div className={`cm-cell ${cls}`}>
      {v}
      <div style={{ fontSize: 10, fontWeight: 400, opacity: .7, marginTop: 2 }}>
        {((v / total) * 100).toFixed(1)}%
      </div>
    </div>
  )
  return (
    <div className="cm-grid">
      <div />
      <div className="cm-h">Pred. Negative</div>
      <div className="cm-h">Pred. Positive</div>
      <div className="cm-rh">Actual Negative</div>
      {cell(tn, 'cm-tn')}
      {cell(fp, 'cm-fp')}
      <div className="cm-rh">Actual Positive</div>
      {cell(fn, 'cm-fn')}
      {cell(tp, 'cm-tp')}
    </div>
  )
}

/** Class support derived from the confusion matrix row sums. */
function supportFromCM(cm) {
  const m = normalizeCM(cm)
  if (!m) return null
  const [[tn, fp], [fn, tp]] = m
  return [
    { label: 'Negative', support: tn + fp },
    { label: 'Positive', support: fn + tp },
  ]
}

/* ── Feature importance ───────────────────────────── */
function FeatureImportance({ features }) {
  if (!features?.length) return null
  const max = Math.max(...features.map((f) => Math.abs(f.importance ?? f.value ?? 0)), 1e-9)
  return (
    <div className="fi-list">
      {features.slice(0, 14).map((f, i) => {
        const name = f.feature ?? f.name ?? `f${i}`
        const val = Number(f.importance ?? f.value ?? 0)
        return (
          <div key={name}>
            <div className="fi-top">
              <span className="fi-name">{name}</span>
              <span className="fi-val">{val.toFixed(4)}</span>
            </div>
            <div className="fi-track">
              <div className="fi-fill" style={{ width: `${(Math.abs(val) / max) * 100}%` }} />
            </div>
          </div>
        )
      })}
    </div>
  )
}

/* ── Metric tiles ─────────────────────────────────── */
function MetricTiles({ results }) {
  const tiles = [
    ['Accuracy', results.accuracy, 't-accent'],
    ['Precision', results.precision_score],
    ['Recall', results.recall_score],
    ['F1 Score', results.f1_score, 't-success'],
    ['ROC AUC', results.roc_auc],
    // JobStatusResponse calls this training_duration (seconds).
    ['Train time', results.training_duration ?? results.training_time ?? results.duration_s, null, true],
  ].filter(([, v]) => v !== undefined && v !== null)

  if (!tiles.length) return null
  return (
    <div className="metrics-grid">
      {tiles.map(([label, val, tone, raw]) => (
        <div key={label} className={`metric-tile ${tone || ''}`}>
          <div className="metric-tile-lbl">{label}</div>
          <div className="metric-tile-val">
            {raw ? `${Number(val).toFixed(2)}s` : formatAccuracy(val)}
          </div>
        </div>
      ))}
    </div>
  )
}

/* ══════════════════════════════════════════════════════
   JobResults — metrics, curves, matrix, importance.
   Used by Pipeline (live) and Jobs (historical).
   ══════════════════════════════════════════════════════ */
export default function JobResults({ results, isTraining = false, progress = 0, jobId }) {
  if (isTraining) {
    return (
      <Panel icon={Activity} title="Metrics" subtitle="Waiting for the run to produce metrics">
        <div className="empty-state">
          <div className="spinner spinner-lg" />
          <div className="empty-state-title mt-3">Training in progress</div>
          <div className="empty-state-desc">
            Metrics, learning curves and the confusion matrix will appear here
            as soon as the estimator finishes fitting.
          </div>
          <div style={{ width: 'min(420px, 100%)', marginTop: 'var(--sp-3)' }}>
            <Progress value={progress} label="Overall progress" />
          </div>
        </div>
      </Panel>
    )
  }

  if (!results) {
    return (
      <Panel icon={BarChart3} title="Metrics" subtitle="Evaluation results">
        <EmptyState
          icon={BarChart3}
          title="No results yet"
          desc="Run the pipeline to train a model and generate evaluation metrics."
        />
      </Panel>
    )
  }

  const history = results.history || []
  const roc = results.roc_curve || []
  const cm = results.confusion_matrix || null
  const features = results.feature_importance || []
  // The API doesn't return a per-class report, but the confusion matrix row
  // sums give exact class support — use that for the bar chart.
  const support = supportFromCM(cm) || []
  const radar = [
    { metric: 'Accuracy', value: Math.round((results.accuracy || 0) * 100) },
    { metric: 'Precision', value: Math.round((results.precision_score || 0) * 100) },
    { metric: 'Recall', value: Math.round((results.recall_score || 0) * 100) },
    { metric: 'F1', value: Math.round((results.f1_score || 0) * 100) },
  ]

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--sp-4)' }}>
      {/* Headline metrics */}
      <Panel
        icon={Target}
        title="Evaluation metrics"
        subtitle={jobId ? `Job ${jobId}` : 'Hold-out test set'}
        actions={
          results.status
            ? <StatusBadge status={results.status} sm />
            : results.accuracy >= 0.85
              ? <Badge tone="success" dot>Strong fit</Badge>
              : <Badge tone="warning" dot>Review fit</Badge>
        }
      >
        <MetricTiles results={results} />
        {results.dataset_id || results.model_type ? (
          <div className="mt-4 pill-row">
            {results.dataset_id && <Badge tone="outline">dataset · {results.dataset_id}</Badge>}
            {results.model_type && <Badge tone="accent">{results.model_type}</Badge>}
            {results.rows_used && <Badge tone="outline">{results.rows_used} rows</Badge>}
            {results.target_column && <Badge tone="outline">target · {results.target_column}</Badge>}
          </div>
        ) : null}
      </Panel>

      {/* Curves */}
      <div className="charts-grid-2">
        <div className="chart-panel">
          <div className="chart-header">
            <div>
              <div className="chart-title">Learning curve</div>
              <div className="chart-sub">Training vs. validation score per epoch/round</div>
            </div>
            <div className="chart-legend">
              <span className="legend-item"><span className="legend-dot" style={{ background: C[1] }} /> Train</span>
              <span className="legend-item"><span className="legend-dot" style={{ background: C[2] }} /> Val</span>
            </div>
          </div>
          {history.length ? (
            <ResponsiveContainer width="100%" height={210}>
              <LineChart data={history}>
                <CartesianGrid strokeDasharray="3 3" stroke="var(--grid-line)" />
                <XAxis dataKey="epoch" tick={{ fontSize: 10, fill: 'var(--text-tertiary)' }} />
                <YAxis tick={{ fontSize: 10, fill: 'var(--text-tertiary)' }} width={34} />
                <Tooltip content={<ChartTip />} />
                <Line type="monotone" dataKey="train" stroke={C[1]} strokeWidth={2} dot={false} />
                <Line type="monotone" dataKey="val" stroke={C[2]} strokeWidth={2} dot={false} strokeDasharray="4 3" />
              </LineChart>
            </ResponsiveContainer>
          ) : (
            <div className="chart-empty">No epoch history reported</div>
          )}
        </div>

        <div className="chart-panel">
          <div className="chart-header">
            <div>
              <div className="chart-title">ROC curve</div>
              <div className="chart-sub">
                {results.roc_auc != null ? `AUC ${results.roc_auc.toFixed(4)}` : 'True vs. false positive rate'}
              </div>
            </div>
            <div className="chart-legend">
              <span className="legend-item"><span className="legend-dot" style={{ background: C[3] }} /> ROC</span>
            </div>
          </div>
          {roc.length ? (
            <ResponsiveContainer width="100%" height={210}>
              <AreaChart data={roc}>
                <defs>
                  <linearGradient id="rocFill" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor={C[3]} stopOpacity={0.32} />
                    <stop offset="100%" stopColor={C[3]} stopOpacity={0.02} />
                  </linearGradient>
                </defs>
                <CartesianGrid strokeDasharray="3 3" stroke="var(--grid-line)" />
                <XAxis dataKey="fpr" tick={{ fontSize: 10, fill: 'var(--text-tertiary)' }} />
                <YAxis tick={{ fontSize: 10, fill: 'var(--text-tertiary)' }} width={34} />
                <Tooltip content={<ChartTip />} />
                <Area type="monotone" dataKey="tpr" stroke={C[3]} strokeWidth={2} fill="url(#rocFill)" />
              </AreaChart>
            </ResponsiveContainer>
          ) : (
            <div className="chart-empty">No ROC data reported</div>
          )}
        </div>
      </div>

      {/* Matrix + importance + radar */}
      <div className="charts-grid-2">
        <div className="chart-panel">
          <div className="chart-header">
            <div>
              <div className="chart-title">Confusion matrix</div>
              <div className="chart-sub">Predicted vs. actual class</div>
            </div>
          </div>
          {cm ? <ConfusionMatrix cm={cm} /> : <div className="chart-empty">Not available for this run</div>}
        </div>

        <div className="chart-panel">
          <div className="chart-header">
            <div>
              <div className="chart-title">Feature importance</div>
              <div className="chart-sub">Top contributing features</div>
            </div>
          </div>
          {features.length ? <FeatureImportance features={features} /> : <div className="chart-empty">Not available for this run</div>}
        </div>
      </div>

      <div className="charts-grid-2">
        <div className="chart-panel">
          <div className="chart-header">
            <div>
              <div className="chart-title">Class balance</div>
              <div className="chart-sub">Per-class support in the evaluation set</div>
            </div>
          </div>
          {support.length ? (
            <ResponsiveContainer width="100%" height={200}>
              <BarChart data={support}>
                <CartesianGrid strokeDasharray="3 3" stroke="var(--grid-line)" />
                <XAxis dataKey="label" tick={{ fontSize: 10, fill: 'var(--text-tertiary)' }} />
                <YAxis tick={{ fontSize: 10, fill: 'var(--text-tertiary)' }} width={34} />
                <Tooltip content={<ChartTip />} />
                <Bar dataKey="support" fill={C[1]} radius={[4, 4, 0, 0]} />
              </BarChart>
            </ResponsiveContainer>
          ) : (
            <div className="chart-empty">No confusion matrix for this run</div>
          )}
        </div>

        <div className="chart-panel">
          <div className="chart-header">
            <div>
              <div className="chart-title">Score profile</div>
              <div className="chart-sub">Normalized headline metrics</div>
            </div>
          </div>
          <ResponsiveContainer width="100%" height={200}>
            <RadarChart data={radar} outerRadius="72%">
              <PolarGrid stroke="var(--grid-line)" />
              <PolarAngleAxis dataKey="metric" tick={{ fontSize: 10, fill: 'var(--text-tertiary)' }} />
              <Radar name="score" dataKey="value" stroke={C[1]} fill={C[1]} fillOpacity={0.22} />
            </RadarChart>
          </ResponsiveContainer>
        </div>
      </div>
    </div>
  )
}

/* ── Job detail summary used by Jobs drawer ─────────
   Moved to ./JobPanels.jsx — see the note there.                       */
