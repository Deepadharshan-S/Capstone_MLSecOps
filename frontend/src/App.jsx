import { useState, useRef, useCallback, useEffect } from 'react'
import {
  AreaChart, Area,
  LineChart, Line,
  RadarChart, Radar,
  PolarGrid, PolarAngleAxis,
  XAxis, YAxis, CartesianGrid, Tooltip,
  ResponsiveContainer,
} from 'recharts'
import './index.css'

/* ── DATA ───────────────────────────────── */

const MODELS = [
  { id: 'random_forest', name: 'Random Forest',           type: 'Ensemble',         icon: '🌲', color: '#22c55e', desc: 'Robust ensemble of decision trees for classification and regression with high accuracy.', stats: { accuracy: '94.2%', speed: 'Fast', memory: '256MB' }, params: { n_estimators: 100, max_depth: 10, min_samples_split: 2 } },
  { id: 'xgboost',       name: 'XGBoost',                 type: 'Gradient Boosting', icon: '⚡', color: '#3b82f6', desc: 'Optimized gradient boosting delivering state-of-the-art results on structured data.',   stats: { accuracy: '96.8%', speed: 'Medium', memory: '512MB' }, params: { n_estimators: 200, learning_rate: 0.1, max_depth: 6 } },
  { id: 'neural_net',    name: 'Neural Network',          type: 'Deep Learning',    icon: '🧠', color: '#8b5cf6', desc: 'Multi-layer perceptron for complex pattern recognition in high-dimensional feature spaces.', stats: { accuracy: '97.5%', speed: 'Slow', memory: '1 GB' }, params: { hidden_layers: 3, neurons: 256, dropout: 0.3 } },
  { id: 'svm',           name: 'Support Vector Machine',  type: 'Kernel Method',    icon: '📐', color: '#f59e0b', desc: 'Effective in high-dimensional spaces with a clear margin of separation.', stats: { accuracy: '91.3%', speed: 'Fast', memory: '128MB' }, params: { kernel: 'rbf', C: 1.0, gamma: 'scale' } },
  { id: 'logistic',      name: 'Logistic Regression',     type: 'Linear Model',     icon: '📊', color: '#06b6d4', desc: 'Simple, interpretable baseline ideal for binary classification with linear boundaries.', stats: { accuracy: '87.1%', speed: 'Very Fast', memory: '64MB' }, params: { C: 1.0, max_iter: 1000, solver: 'lbfgs' } },
  { id: 'lstm',          name: 'LSTM Network',            type: 'Recurrent DL',     icon: '🔄', color: '#ec4899', desc: 'Long Short-Term Memory network for sequential data and temporal pattern detection.', stats: { accuracy: '95.1%', speed: 'Slow', memory: '768MB' }, params: { units: 128, sequence_len: 50, epochs: 30 } },
]

const MODEL_ICON_BG = {
  random_forest: 'rgba(34,197,94,.14)',
  xgboost:       'rgba(59,130,246,.14)',
  neural_net:    'rgba(139,92,246,.14)',
  svm:           'rgba(245,158,11,.14)',
  logistic:      'rgba(6,182,212,.14)',
  lstm:          'rgba(236,72,153,.14)',
}

const genHistory = () => Array.from({ length: 20 }, (_, i) => ({
  epoch: i + 1,
  trainLoss: +(2.5 * Math.exp(-0.18 * i) + 0.05 * (Math.random() - .5)).toFixed(4),
  valLoss:   +(2.7 * Math.exp(-0.16 * i) + 0.08 * (Math.random() - .5)).toFixed(4),
  trainAcc:  +(100 - 60 * Math.exp(-0.2 * i) + 1.5 * (Math.random() - .5)).toFixed(2),
  valAcc:    +(100 - 65 * Math.exp(-0.18 * i) + 2 * (Math.random() - .5)).toFixed(2),
}))

const HISTORY = genHistory()

const ROC_DATA = Array.from({ length: 11 }, (_, i) => {
  const fpr = i / 10
  return { fpr: +fpr.toFixed(1), tpr: +(Math.min(1, fpr + 0.1 + (1 - fpr) * 0.85)).toFixed(3) }
})

const RADAR_DATA = [
  { metric: 'Accuracy', value: 97 },
  { metric: 'Precision', value: 95 },
  { metric: 'Recall', value: 92 },
  { metric: 'F1-Score', value: 94 },
  { metric: 'AUC-ROC', value: 98 },
  { metric: 'Specificity', value: 96 },
]

const FEATURES = [
  { name: 'Request Frequency', value: .89 },
  { name: 'Payload Size',      value: .76 },
  { name: 'Entropy Score',     value: .68 },
  { name: 'IP Reputation',     value: .61 },
  { name: 'Session Duration',  value: .54 },
  { name: 'Port Number',       value: .42 },
  { name: 'Protocol Type',     value: .38 },
]

const LOGS = [
  { level: 'info',    msg: 'Initializing MLSecOps pipeline…' },
  { level: 'info',    msg: 'Loading dataset into memory' },
  { level: 'info',    msg: 'Preprocessing — normalization applied' },
  { level: 'info',    msg: 'Train/Val split: 80% / 20%' },
  { level: 'info',    msg: 'Starting model training…' },
  { level: 'info',    msg: 'Epoch 5/20 — loss: 1.2341, val_loss: 1.4102' },
  { level: 'info',    msg: 'Epoch 10/20 — loss: 0.6892, val_loss: 0.7914' },
  { level: 'info',    msg: 'Epoch 15/20 — loss: 0.3214, val_loss: 0.4011' },
  { level: 'info',    msg: 'Epoch 20/20 — loss: 0.1023, val_loss: 0.1589' },
  { level: 'success', msg: 'Training complete! Evaluating on test set…' },
  { level: 'success', msg: 'Accuracy: 97.5%  |  F1: 0.944  |  AUC-ROC: 0.984' },
  { level: 'success', msg: 'Model saved → /models/checkpoint_final.pkl' },
]

const STEPS = [
  { id: 0, icon: '📁', label: 'Dataset',        desc: 'Upload & configure data sources' },
  { id: 1, icon: '🧠', label: 'Model',           desc: 'Select algorithm & hyperparameters' },
  { id: 2, icon: '📈', label: 'Output & Metrics', desc: 'View results and visual analytics' },
]

/* ── Custom Recharts tooltip ─────────────── */
const ChartTip = ({ active, payload, label }) => {
  if (!active || !payload?.length) return null
  return (
    <div className="ct">
      <p style={{ fontSize: 10, color: 'var(--text-500)', marginBottom: 4 }}>{label}</p>
      {payload.map((p, i) => (
        <p key={i} style={{ color: p.color, fontWeight: 600 }}>
          {p.name}: {typeof p.value === 'number' ? p.value.toFixed(3) : p.value}
        </p>
      ))}
    </div>
  )
}

/* ── SIDEBAR ─────────────────────────────── */
function Sidebar({ active, setActive, stepStates }) {
  return (
    <aside className="sidebar">
      {/* Brand */}
      <div className="sidebar-brand">
        <div className="sidebar-logo">M</div>
        <span className="sidebar-brand-name">ML<span>SecOps</span></span>
      </div>

      <span className="sidebar-section-label">Pipeline Workflow</span>

      {/* Steps */}
      <div className="pipeline-steps">
        {STEPS.map(s => {
          const state = stepStates[s.id] // 'idle' | 'active' | 'done'
          return (
            <button
              key={s.id}
              id={`sidebar-step-${s.id}`}
              className={`step-item${state === 'active' ? ' active-step-item' : ''}${state === 'done' ? ' done-step-item' : ''}`}
              onClick={() => setActive(s.id)}
            >
              <div className="step-circle">
                <div className="step-circle-inner">
                  {state === 'done' ? '✓' : s.id + 1}
                </div>
              </div>
              <div className="step-body">
                <div className="step-label">
                  <span>{s.icon}</span> {s.label}
                </div>
                <div className="step-desc">{s.desc}</div>
                <div>
                  {state === 'active' && <span className="step-status-chip chip-active">● Active</span>}
                  {state === 'done'   && <span className="step-status-chip chip-done">✓ Done</span>}
                  {state === 'idle'   && <span className="step-status-chip chip-idle">○ Pending</span>}
                </div>
              </div>
            </button>
          )
        })}
      </div>

      {/* Footer */}
      <div className="sidebar-footer">
        <div className="system-status-row">
          <div className="status-pill" />
          <span className="status-text">System Online</span>
        </div>
        <div className="sidebar-meta">
          MLSecOps Platform v1.0<br />
          Capstone · {new Date().getFullYear()}
        </div>
      </div>
    </aside>
  )
}

/* ── TOPBAR ──────────────────────────────── */
function Topbar({ activeStep, isTraining, canRun, onRun, onReset }) {
  const step = STEPS[activeStep]
  return (
    <header className="topbar">
      <div className="topbar-left">
        <div className="topbar-title">
          {step.icon} {step.label}
        </div>
        <div className="topbar-breadcrumb">
          MLSecOps / <span>Pipeline</span> / {step.label}
        </div>
      </div>
      <div className="topbar-right">
        <div className="topbar-badge">
          <span style={{ width: 7, height: 7, borderRadius: '50%', background: 'var(--success)', boxShadow: '0 0 6px var(--success)' }} />
          Pipeline Ready
        </div>
        <button
          id="topbar-reset"
          className="btn btn-ghost btn-sm"
          onClick={onReset}
        >
          ↺ Reset
        </button>
        <button
          id="topbar-run"
          className={`btn-run-top${isTraining ? ' running' : ''}`}
          disabled={!canRun || isTraining}
          onClick={onRun}
        >
          {isTraining ? '⏳ Training…' : '▶ Run Pipeline'}
        </button>
      </div>
    </header>
  )
}

/* ── STEP 1 — Dataset ────────────────────── */
function DatasetPanel({ datasets, onAdd, onRemove }) {
  const fileRef = useRef()
  const [drag, setDrag] = useState(false)
  const [targetCol, setTargetCol] = useState('label')
  const [split, setSplit] = useState('0.2')
  const [scaler, setScaler] = useState('standard')

  const handleDrop = useCallback(e => {
    e.preventDefault(); setDrag(false)
    Array.from(e.dataTransfer.files).forEach(onAdd)
  }, [onAdd])

  const fmtBytes = b => b < 1048576 ? `${(b / 1024).toFixed(1)} KB` : `${(b / 1048576).toFixed(1)} MB`

  return (
    <>
      {/* Upload */}
      <div className="panel anim">
        <div className="panel-header">
          <div className="panel-title-row">
            <div className="panel-icon">📂</div>
            <div>
              <div className="panel-title">Data Source</div>
              <div className="panel-subtitle">Upload dataset files for training</div>
            </div>
          </div>
          {datasets.length > 0 && (
            <span className="panel-badge badge-green">{datasets.length} file{datasets.length !== 1 ? 's' : ''} loaded</span>
          )}
        </div>
        <div className="panel-body">
          <div
            id="upload-zone"
            className={`upload-zone${drag ? ' drag-over' : ''}`}
            onClick={() => fileRef.current.click()}
            onDragOver={e => { e.preventDefault(); setDrag(true) }}
            onDragLeave={() => setDrag(false)}
            onDrop={handleDrop}
          >
            <div className="upload-icon-box">📁</div>
            <div className="upload-primary">Drop files here or click to browse</div>
            <div className="upload-secondary">Supports structured data files up to 500 MB</div>
            <div className="format-chips">
              {['.csv', '.json', '.parquet', '.xlsx', '.tsv'].map(f => (
                <span key={f} className="format-chip">{f}</span>
              ))}
            </div>
            <input ref={fileRef} type="file" multiple accept=".csv,.json,.parquet,.xlsx,.tsv"
              style={{ display: 'none' }}
              onChange={e => { Array.from(e.target.files).forEach(onAdd); e.target.value = '' }} />
          </div>

          {/* File list */}
          {datasets.length > 0 && (
            <div className="dataset-list">
              {datasets.map((ds, i) => (
                <div key={i} className="dataset-item">
                  <div className="ds-icon">{ds.name.endsWith('.csv') ? '📊' : ds.name.endsWith('.json') ? '📋' : '🗄️'}</div>
                  <div className="ds-info">
                    <div className="ds-name">{ds.name}</div>
                    <div className="ds-meta">{fmtBytes(ds.size)} · {new Date().toLocaleTimeString()}</div>
                  </div>
                  <span className="badge badge-ready">Ready</span>
                  <button id={`remove-ds-${i}`} className="btn btn-ghost btn-xs" onClick={() => onRemove(i)}>✕</button>
                </div>
              ))}
            </div>
          )}

          {/* Demo files */}
          {datasets.length === 0 && (
            <div className="dataset-list">
              {[
                { name: 'network_traffic.csv',    meta: '14.2 MB · 128,450 rows',  icon: '📊' },
                { name: 'malware_features.parquet', meta: '8.7 MB · 84,320 rows',  icon: '🗄️' },
              ].map((d, i) => (
                <div key={i} className="dataset-item" style={{ opacity: .45 }}>
                  <div className="ds-icon">{d.icon}</div>
                  <div className="ds-info">
                    <div className="ds-name">{d.name}</div>
                    <div className="ds-meta">{d.meta} · Demo</div>
                  </div>
                  <span className="badge badge-demo">Demo</span>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* Preprocessing */}
      <div className="panel anim d1">
        <div className="panel-header">
          <div className="panel-title-row">
            <div className="panel-icon">⚙️</div>
            <div>
              <div className="panel-title">Preprocessing Config</div>
              <div className="panel-subtitle">Configure feature engineering & splits</div>
            </div>
          </div>
        </div>
        <div className="panel-body">
          <div className="form-row form-row-3">
            <div className="input-group">
              <label className="input-label" htmlFor="target-col">Target Column</label>
              <input id="target-col" className="input-field" type="text" value={targetCol}
                onChange={e => setTargetCol(e.target.value)} placeholder="label" />
            </div>
            <div className="input-group">
              <label className="input-label" htmlFor="test-split">Test Split</label>
              <select id="test-split" className="input-field" value={split} onChange={e => setSplit(e.target.value)}>
                <option value="0.1">10% Test / 90% Train</option>
                <option value="0.2">20% Test / 80% Train</option>
                <option value="0.3">30% Test / 70% Train</option>
                <option value="0.4">40% Test / 60% Train</option>
              </select>
            </div>
            <div className="input-group">
              <label className="input-label" htmlFor="scaler">Feature Scaling</label>
              <select id="scaler" className="input-field" value={scaler} onChange={e => setScaler(e.target.value)}>
                <option value="standard">Standard Scaler (Z-score)</option>
                <option value="minmax">Min-Max Normalization</option>
                <option value="robust">Robust Scaler</option>
                <option value="none">No Scaling</option>
              </select>
            </div>
          </div>
        </div>
      </div>
    </>
  )
}

/* ── STEP 2 — Model ──────────────────────── */
function ModelPanel({ selected, onSelect }) {
  const model = MODELS.find(m => m.id === selected)
  const [params, setParams] = useState(model?.params || {})

  useEffect(() => {
    const m = MODELS.find(m => m.id === selected)
    if (m) setParams({ ...m.params })
  }, [selected])

  return (
    <>
      <div className="panel anim">
        <div className="panel-header">
          <div className="panel-title-row">
            <div className="panel-icon">🤖</div>
            <div>
              <div className="panel-title">Algorithm Selection</div>
              <div className="panel-subtitle">Choose an ML algorithm for your task</div>
            </div>
          </div>
          {selected && (
            <span className="panel-badge badge-blue">{model?.name}</span>
          )}
        </div>
        <div className="panel-body">
          <div className="model-grid">
            {MODELS.map(m => (
              <div
                key={m.id}
                id={`model-${m.id}`}
                className={`model-card${selected === m.id ? ' selected' : ''}`}
                onClick={() => onSelect(m.id)}
                role="radio"
                aria-checked={selected === m.id}
                tabIndex={0}
                onKeyDown={e => e.key === 'Enter' && onSelect(m.id)}
              >
                <div className="model-card-top">
                  <div className="model-emoji" style={{ background: MODEL_ICON_BG[m.id] }}>{m.icon}</div>
                  <div className="radio-circle">
                    <div className="radio-dot" />
                  </div>
                </div>
                <div className="model-name">{m.name}</div>
                <div className="model-type">{m.type}</div>
                <div className="model-desc">{m.desc}</div>
                <div className="model-stats">
                  <div className="model-stat">
                    <div className="model-stat-val">{m.stats.accuracy}</div>
                    <div className="model-stat-lbl">Accuracy</div>
                  </div>
                  <div className="model-stat">
                    <div className="model-stat-val" style={{ fontSize: 10 }}>{m.stats.speed}</div>
                    <div className="model-stat-lbl">Speed</div>
                  </div>
                  <div className="model-stat">
                    <div className="model-stat-val" style={{ fontSize: 10 }}>{m.stats.memory}</div>
                    <div className="model-stat-lbl">Memory</div>
                  </div>
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>

      {/* Hyperparameters */}
      {selected && (
        <div className="panel anim d1">
          <div className="panel-header">
            <div className="panel-title-row">
              <div className="panel-icon">🔧</div>
              <div>
                <div className="panel-title">Hyperparameters</div>
                <div className="panel-subtitle">{model?.name} — tune training parameters</div>
              </div>
            </div>
          </div>
          <div className="panel-body">
            <div className="hyperparam-grid">
              {Object.entries(params).map(([k, v]) => (
                <div key={k} className="input-group">
                  <label className="input-label" htmlFor={`hp-${k}`}>{k.replace(/_/g, ' ')}</label>
                  <input id={`hp-${k}`} className="input-field" type="text" value={v}
                    onChange={e => setParams(p => ({ ...p, [k]: e.target.value }))} />
                </div>
              ))}
            </div>
          </div>
        </div>
      )}
    </>
  )
}

/* ── STEP 3 — Output ─────────────────────── */
function OutputPanel({ isTraining, progress, logs, hasResults }) {
  return (
    <>
      {/* Console + progress */}
      <div className="panel anim">
        <div className="panel-header">
          <div className="panel-title-row">
            <div className="panel-icon">⌨️</div>
            <div>
              <div className="panel-title">Training Console</div>
              <div className="panel-subtitle">Real-time pipeline execution logs</div>
            </div>
          </div>
          {hasResults && <span className="panel-badge badge-green">✓ Complete</span>}
          {isTraining && <span className="panel-badge badge-blue">⏳ Running</span>}
        </div>
        <div className="panel-body">
          {(isTraining || hasResults) && (
            <div className="prog-wrap">
              <div className="prog-header">
                <span>Epoch Progress</span>
                <span style={{ fontFamily: 'JetBrains Mono', color: 'var(--blue-400)' }}>{progress}%</span>
              </div>
              <div className="prog-track">
                <div className="prog-fill" style={{ width: `${progress}%` }} />
              </div>
            </div>
          )}
          <div className="console" id="console-out">
            {logs.length === 0 && (
              <div className="log-line">
                <span className="log-ts">--:--:--</span>
                <span className="log-info">INFO</span>
                <span className="log-txt">Pipeline ready. Configure dataset & model, then click Run Pipeline…</span>
              </div>
            )}
            {logs.map((l, i) => (
              <div key={i} className="log-line">
                <span className="log-ts">{l.time}</span>
                <span className={`log-${l.level}`}>{l.level.toUpperCase().padEnd(7)}</span>
                <span className="log-txt">{l.msg}</span>
              </div>
            ))}
          </div>
        </div>
      </div>

      {/* Empty state */}
      {!hasResults && !isTraining && (
        <div className="panel anim d1">
          <div className="panel-body">
            <div className="empty-state">
              <div className="empty-state-icon">📊</div>
              <div className="empty-state-title">No results yet</div>
              <div className="empty-state-desc">Select a model and run the pipeline to see<br />metrics, charts, and visual analytics here.</div>
            </div>
          </div>
        </div>
      )}

      {/* Metrics */}
      {hasResults && (
        <>
          <div className="panel anim d1">
            <div className="panel-header">
              <div className="panel-title-row">
                <div className="panel-icon">🎯</div>
                <div>
                  <div className="panel-title">Performance Metrics</div>
                  <div className="panel-subtitle">Key evaluation scores on test set</div>
                </div>
              </div>
            </div>
            <div className="panel-body">
              <div className="metrics-grid">
                {[
                  { lbl: 'Accuracy',    val: '97.5%', delta: '+2.3%', dir: 'up', c: 'mt-blue'   },
                  { lbl: 'F1-Score',    val: '0.944', delta: '+0.012', dir: 'up', c: 'mt-green'  },
                  { lbl: 'AUC-ROC',    val: '0.984', delta: '+0.008', dir: 'up', c: 'mt-purple'  },
                  { lbl: 'Loss',        val: '0.102', delta: '−0.089', dir: 'up', c: 'mt-amber'  },
                  { lbl: 'Precision',   val: '95.2%', delta: '+1.8%', dir: 'up', c: 'mt-blue'   },
                  { lbl: 'Recall',      val: '92.1%', delta: '+3.1%', dir: 'up', c: 'mt-green'  },
                  { lbl: 'Specificity', val: '96.3%', delta: '+1.2%', dir: 'up', c: 'mt-purple'  },
                  { lbl: 'Train Time',  val: '14.2s', delta: '−3.1s', dir: 'up', c: 'mt-amber'  },
                ].map((m, i) => (
                  <div key={i} className={`metric-tile ${m.c}`}>
                    <div className="metric-tile-lbl">{m.lbl}</div>
                    <div className="metric-tile-val">{m.val}</div>
                    <div className={`metric-tile-delta delta-${m.dir}`}>
                      {m.dir === 'up' ? '↑' : '↓'} {m.delta} vs baseline
                    </div>
                  </div>
                ))}
              </div>
            </div>
          </div>

          {/* Charts row 1 */}
          <div className="charts-grid-2 anim d2" style={{ marginBottom: 20 }}>
            {/* Loss chart */}
            <div className="chart-panel">
              <div className="chart-header">
                <div>
                  <div className="chart-title">Training Loss</div>
                  <div className="chart-sub">Train vs validation over epochs</div>
                </div>
                <div className="chart-legend">
                  <span className="legend-item"><span className="legend-dot" style={{ background: '#3b82f6' }} />Train</span>
                  <span className="legend-item"><span className="legend-dot" style={{ background: '#22d3ee' }} />Val</span>
                </div>
              </div>
              <ResponsiveContainer width="100%" height={200}>
                <AreaChart data={HISTORY} margin={{ top: 4, right: 4, left: -22, bottom: 0 }}>
                  <defs>
                    <linearGradient id="gTrain" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="5%"  stopColor="#3b82f6" stopOpacity={.3} />
                      <stop offset="95%" stopColor="#3b82f6" stopOpacity={0}  />
                    </linearGradient>
                    <linearGradient id="gVal" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="5%"  stopColor="#22d3ee" stopOpacity={.2} />
                      <stop offset="95%" stopColor="#22d3ee" stopOpacity={0}  />
                    </linearGradient>
                  </defs>
                  <CartesianGrid strokeDasharray="3 3" stroke="rgba(59,130,246,.08)" />
                  <XAxis dataKey="epoch" tick={{ fill: '#3d5070', fontSize: 10 }} stroke="transparent" />
                  <YAxis tick={{ fill: '#3d5070', fontSize: 10 }} stroke="transparent" />
                  <Tooltip content={<ChartTip />} />
                  <Area type="monotone" dataKey="trainLoss" name="Train Loss" stroke="#3b82f6" strokeWidth={2} fill="url(#gTrain)" dot={false} />
                  <Area type="monotone" dataKey="valLoss"   name="Val Loss"   stroke="#22d3ee" strokeWidth={2} fill="url(#gVal)"   dot={false} />
                </AreaChart>
              </ResponsiveContainer>
            </div>

            {/* Accuracy chart */}
            <div className="chart-panel">
              <div className="chart-header">
                <div>
                  <div className="chart-title">Accuracy Curves</div>
                  <div className="chart-sub">Train vs validation accuracy</div>
                </div>
                <div className="chart-legend">
                  <span className="legend-item"><span className="legend-dot" style={{ background: '#10b981' }} />Train</span>
                  <span className="legend-item"><span className="legend-dot" style={{ background: '#f59e0b' }} />Val</span>
                </div>
              </div>
              <ResponsiveContainer width="100%" height={200}>
                <LineChart data={HISTORY} margin={{ top: 4, right: 4, left: -22, bottom: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="rgba(59,130,246,.08)" />
                  <XAxis dataKey="epoch" tick={{ fill: '#3d5070', fontSize: 10 }} stroke="transparent" />
                  <YAxis domain={[40, 100]} tick={{ fill: '#3d5070', fontSize: 10 }} stroke="transparent" />
                  <Tooltip content={<ChartTip />} />
                  <Line type="monotone" dataKey="trainAcc" name="Train Acc" stroke="#10b981" strokeWidth={2} dot={false} />
                  <Line type="monotone" dataKey="valAcc"   name="Val Acc"   stroke="#f59e0b" strokeWidth={2} dot={false} strokeDasharray="4 2" />
                </LineChart>
              </ResponsiveContainer>
            </div>
          </div>

          {/* Charts row 2 */}
          <div className="charts-grid-2 anim d3" style={{ marginBottom: 20 }}>
            {/* ROC */}
            <div className="chart-panel">
              <div className="chart-header">
                <div>
                  <div className="chart-title">ROC Curve</div>
                  <div className="chart-sub">AUC = 0.984 · Receiver Operating Characteristic</div>
                </div>
              </div>
              <ResponsiveContainer width="100%" height={200}>
                <AreaChart data={ROC_DATA} margin={{ top: 4, right: 4, left: -22, bottom: 0 }}>
                  <defs>
                    <linearGradient id="gRoc" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="5%"  stopColor="#8b5cf6" stopOpacity={.35} />
                      <stop offset="95%" stopColor="#8b5cf6" stopOpacity={0}  />
                    </linearGradient>
                  </defs>
                  <CartesianGrid strokeDasharray="3 3" stroke="rgba(59,130,246,.08)" />
                  <XAxis dataKey="fpr" tick={{ fill: '#3d5070', fontSize: 10 }} stroke="transparent" />
                  <YAxis tick={{ fill: '#3d5070', fontSize: 10 }} stroke="transparent" />
                  <Tooltip content={<ChartTip />} />
                  <Area type="monotone" dataKey="tpr" name="TPR" stroke="#8b5cf6" strokeWidth={2} fill="url(#gRoc)" dot={false} />
                </AreaChart>
              </ResponsiveContainer>
            </div>

            {/* Radar */}
            <div className="chart-panel">
              <div className="chart-header">
                <div>
                  <div className="chart-title">Performance Radar</div>
                  <div className="chart-sub">Multi-dimensional model evaluation</div>
                </div>
              </div>
              <ResponsiveContainer width="100%" height={200}>
                <RadarChart data={RADAR_DATA} margin={{ top: 10, right: 20, left: 20, bottom: 10 }}>
                  <PolarGrid stroke="rgba(59,130,246,.12)" />
                  <PolarAngleAxis dataKey="metric" tick={{ fill: '#6b82a8', fontSize: 10 }} />
                  <Radar name="Score" dataKey="value" stroke="#3b82f6" fill="#3b82f6" fillOpacity={.22} strokeWidth={2} />
                  <Tooltip content={<ChartTip />} />
                </RadarChart>
              </ResponsiveContainer>
            </div>
          </div>

          {/* Confusion matrix + feature importance */}
          <div className="charts-grid-2 anim d3">
            {/* Confusion matrix */}
            <div className="chart-panel">
              <div className="chart-header">
                <div>
                  <div className="chart-title">Confusion Matrix</div>
                  <div className="chart-sub">Predicted vs actual class distribution</div>
                </div>
              </div>
              <div className="cm-grid">
                <div />
                <div className="cm-h">Pred: 0</div>
                <div className="cm-h">Pred: 1</div>
                <div className="cm-rh">Act: 0</div>
                <div className="cm-cell cm-tn">8,234</div>
                <div className="cm-cell cm-fp">312</div>
                <div className="cm-rh">Act: 1</div>
                <div className="cm-cell cm-fn">198</div>
                <div className="cm-cell cm-tp">7,891</div>
              </div>
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10, marginTop: 14 }}>
                {[
                  { lbl: 'True Positives',  val: 7891, c: 'var(--success)' },
                  { lbl: 'True Negatives',  val: 8234, c: 'var(--success)' },
                  { lbl: 'False Positives', val: 312,  c: 'var(--danger)'  },
                  { lbl: 'False Negatives', val: 198,  c: 'var(--danger)'  },
                ].map((r, i) => (
                  <div key={i} style={{ display: 'flex', justifyContent: 'space-between', fontSize: 11, color: 'var(--text-400)' }}>
                    <span>{r.lbl}</span>
                    <span style={{ color: r.c, fontWeight: 700, fontFamily: 'JetBrains Mono' }}>{r.val.toLocaleString()}</span>
                  </div>
                ))}
              </div>
            </div>

            {/* Feature importance */}
            <div className="chart-panel">
              <div className="chart-header">
                <div>
                  <div className="chart-title">Feature Importance</div>
                  <div className="chart-sub">Top contributing features (normalized)</div>
                </div>
              </div>
              <div className="fi-list">
                {FEATURES.map((f, i) => (
                  <div key={i} className="fi-row">
                    <div className="fi-top">
                      <span className="fi-name">{f.name}</span>
                      <span className="fi-val">{(f.value * 100).toFixed(0)}%</span>
                    </div>
                    <div className="fi-track">
                      <div className="fi-fill" style={{ width: `${f.value * 100}%` }} />
                    </div>
                  </div>
                ))}
              </div>
            </div>
          </div>
        </>
      )}
    </>
  )
}

/* ── KPI Bar ─────────────────────────────── */
function KpiBar({ hasResults, isTraining, selectedModel, datasetsCount }) {
  const model = MODELS.find(m => m.id === selectedModel)
  return (
    <div className="kpi-bar">
      {[
        { icon: '🎯', label: 'Model Accuracy', value: hasResults ? '97.5%' : model ? model.stats.accuracy : '—', c: 'kpi-blue' },
        { icon: '📁', label: 'Datasets Loaded', value: datasetsCount || '2 Demo', c: 'kpi-green' },
        { icon: '🤖', label: 'Selected Model', value: model ? model.name.split(' ')[0] : 'None', c: 'kpi-purple' },
        { icon: '⚡', label: 'Pipeline Status', value: isTraining ? 'Training' : hasResults ? 'Complete' : 'Idle', c: 'kpi-amber' },
      ].map((k, i) => (
        <div key={i} className={`kpi-card ${k.c}`}>
          <div className="kpi-icon">{k.icon}</div>
          <div className="kpi-body">
            <div className="kpi-value">{k.value}</div>
            <div className="kpi-label">{k.label}</div>
          </div>
        </div>
      ))}
    </div>
  )
}

/* ── APP ROOT ────────────────────────────── */
export default function App() {
  const [activeStep, setActiveStep]   = useState(0)
  const [datasets, setDatasets]       = useState([])
  const [selectedModel, setModel]     = useState('')
  const [isTraining, setTraining]     = useState(false)
  const [progress, setProgress]       = useState(0)
  const [logs, setLogs]               = useState([])
  const [hasResults, setHasResults]   = useState(false)
  const timerRef = useRef(null)

  const addDataset    = useCallback(f => setDatasets(p => [...p, f]), [])
  const removeDataset = useCallback(i => setDatasets(p => p.filter((_, j) => j !== i)), [])

  const selectModel = useCallback(id => {
    setModel(id)
    setActiveStep(1)
  }, [])

  const ts = () => new Date().toLocaleTimeString('en', { hour12: false })

  const runPipeline = useCallback(() => {
    if (isTraining) return
    setTraining(true)
    setProgress(0)
    setLogs([])
    setHasResults(false)
    setActiveStep(2)
    let prog = 0, logIdx = 0
    timerRef.current = setInterval(() => {
      prog = Math.min(prog + Math.random() * 8 + 2, 100)
      setProgress(Math.round(prog))
      if (logIdx < LOGS.length) {
        const entry = LOGS[logIdx++]
        setLogs(p => [...p, { ...entry, time: ts() }])
      }
      if (prog >= 100) {
        clearInterval(timerRef.current)
        setTraining(false)
        setHasResults(true)
        setProgress(100)
      }
    }, 600)
  }, [isTraining])

  const reset = useCallback(() => {
    clearInterval(timerRef.current)
    setTraining(false)
    setProgress(0)
    setLogs([])
    setHasResults(false)
    setActiveStep(0)
  }, [])

  useEffect(() => () => clearInterval(timerRef.current), [])

  /* Step states */
  const stepStates = {
    0: activeStep === 0 ? 'active' : activeStep > 0 ? 'done' : 'idle',
    1: activeStep === 1 ? 'active' : activeStep > 1 ? 'done' : 'idle',
    2: activeStep === 2 ? 'active' : hasResults ? 'done' : 'idle',
  }

  const canRun = !!selectedModel

  return (
    <div className="dashboard-shell">
      <Sidebar active={activeStep} setActive={setActiveStep} stepStates={stepStates} />

      <Topbar
        activeStep={activeStep}
        isTraining={isTraining}
        canRun={canRun}
        onRun={runPipeline}
        onReset={reset}
      />

      <main className="main-area">
        <KpiBar
          hasResults={hasResults}
          isTraining={isTraining}
          selectedModel={selectedModel}
          datasetsCount={datasets.length || null}
        />

        {activeStep === 0 && (
          <DatasetPanel datasets={datasets} onAdd={addDataset} onRemove={removeDataset} />
        )}
        {activeStep === 1 && (
          <ModelPanel selected={selectedModel} onSelect={selectModel} />
        )}
        {activeStep === 2 && (
          <OutputPanel isTraining={isTraining} progress={progress} logs={logs} hasResults={hasResults} />
        )}
      </main>
    </div>
  )
}
