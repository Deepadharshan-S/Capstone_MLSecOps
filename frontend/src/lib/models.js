// Shared model catalogue + helpers (no React components — fast-refresh clean).
//
// Canonical picker — mirrors backend/app/services/ml_ops/supported_models.py
// and the estimators dict in ray_wrapper.py::execute_pipeline_task.
// id === backendType (canonical name); legacy aliases resolve via backendType.

export const AVAILABLE_ALGORITHMS = [
  { id: 'random_forest',       name: 'Random Forest',           type: 'Ensemble',          icon: '🌲', color: '#22c55e', desc: 'Robust ensemble of decision trees for classification.', stats: { speed: 'Fast', memory: '256MB' }, params: { n_estimators: 100, max_depth: 10, min_samples_split: 2 } },
  { id: 'logistic_regression', name: 'Logistic Regression',     type: 'Linear Model',      icon: '📊', color: '#06b6d4', desc: 'Simple, interpretable baseline for classification.', stats: { speed: 'Very Fast', memory: '64MB' }, params: { C: 1.0, max_iter: 1000, solver: 'lbfgs' } },
  { id: 'decision_tree',       name: 'Decision Tree',           type: 'Tree Model',        icon: '🌳', color: '#10b981', desc: 'Interpretable tree-based classifier.', stats: { speed: 'Fast', memory: '64MB' }, params: { max_depth: 10, min_samples_split: 2 } },
  { id: 'gradient_boosting',   name: 'Gradient Boosting',       type: 'Ensemble',          icon: '📈', color: '#f97316', desc: 'Stage-wise additive boosting (sklearn).', stats: { speed: 'Medium', memory: '256MB' }, params: { n_estimators: 100, learning_rate: 0.1, max_depth: 3 } },
  { id: 'svm',                 name: 'Support Vector Machine',  type: 'Kernel Method',     icon: '📐', color: '#f59e0b', desc: 'Effective in high-dimensional spaces with a clear margin.', stats: { speed: 'Fast', memory: '128MB' }, params: { kernel: 'rbf', C: 1.0, gamma: 'scale' } },
  { id: 'knn',                 name: 'K-Nearest Neighbors',     type: 'Instance-Based',    icon: '🧭', color: '#14b8a6', desc: 'Non-parametric classifier based on nearby samples.', stats: { speed: 'Fast', memory: '64MB' }, params: { n_neighbors: 5, weights: 'uniform' } },
  { id: 'adaboost',            name: 'AdaBoost',                type: 'Ensemble',          icon: '🎯', color: '#ef4444', desc: 'Adaptive boosting of weak learners.', stats: { speed: 'Fast', memory: '128MB' }, params: { n_estimators: 50, learning_rate: 1.0 } },
  { id: 'extra_trees',         name: 'Extra Trees',             type: 'Ensemble',          icon: '🌴', color: '#84cc16', desc: 'Extremely randomized trees, fast variance reduction.', stats: { speed: 'Fast', memory: '256MB' }, params: { n_estimators: 100, max_depth: 10 } },
  { id: 'naive_bayes',         name: 'Naive Bayes',             type: 'Probabilistic',     icon: '🎲', color: '#a855f7', desc: 'Gaussian NB baseline, no hyperparameters.', stats: { speed: 'Very Fast', memory: '32MB' }, params: {} },
  { id: 'sgd',                 name: 'SGD Classifier',          type: 'Linear Model',      icon: '📉', color: '#64748b', desc: 'Linear model trained with stochastic gradient descent.', stats: { speed: 'Very Fast', memory: '64MB' }, params: { loss: 'hinge', alpha: 0.0001, max_iter: 1000 } },
  { id: 'ridge',               name: 'Ridge Classifier',        type: 'Linear Model',      icon: '🧱', color: '#0ea5e9', desc: 'Ridge-regularized linear classifier.', stats: { speed: 'Very Fast', memory: '64MB' }, params: { alpha: 1.0 } },
  { id: 'mlp',                 name: 'Neural Network (MLP)',    type: 'Deep Learning',     icon: '🧠', color: '#8b5cf6', desc: 'Multi-layer perceptron. hidden_layer_sizes as "128,64".', stats: { speed: 'Slow', memory: '1 GB' }, params: { hidden_layer_sizes: '128,64', activation: 'relu', alpha: 0.0001, max_iter: 200 } },
  { id: 'xgboost',             name: 'XGBoost',                 type: 'Gradient Boosting', icon: '⚡', color: '#3b82f6', desc: 'Optimized gradient boosting. Needs xgboost installed.', stats: { speed: 'Medium', memory: '512MB' }, optional: true, params: { n_estimators: 200, learning_rate: 0.1, max_depth: 6 } },
  { id: 'lightgbm',            name: 'LightGBM',                type: 'Gradient Boosting', icon: '💡', color: '#eab308', desc: 'Fast gradient boosting. Needs lightgbm installed.', stats: { speed: 'Medium', memory: '512MB' }, optional: true, params: { n_estimators: 100, learning_rate: 0.1, num_leaves: 31 } },
]

export const MODEL_ALIASES = {
  svc: 'svm', linear_svc: 'svm', kneighbors: 'knn',
  gaussian_nb: 'naive_bayes', xgb: 'xgboost', lgb: 'lightgbm',
}

export function canonicalId(name) {
  const key = (name || '').toLowerCase().replace(/[\s-]/g, '_')
  if (MODEL_ALIASES[key]) return MODEL_ALIASES[key]
  // Match canonical id or backendType, longest match wins (e.g.
  // 'logistic_regression' before partial matches).
  const hit = AVAILABLE_ALGORITHMS.filter(a => key.includes(a.id))
    .sort((x, y) => y.id.length - x.id.length)[0]
  return hit ? hit.id : key
}

export function resolveModelPreset(name) {
  return AVAILABLE_ALGORITHMS.find(a => a.id === canonicalId(name)) || null
}

// Mirrors backend get_repo_name() — dataset_id sent to the API must be
// the lakeFS repo name (no extension), not the display filename.
export function toRepoName(name) {
  let s = (name || '').toLowerCase().replace(/\.[^.]+$/, '')
  s = s.replace(/[^a-z0-9-]/g, '-').replace(/-+/g, '-').replace(/^-|-$/g, '')
  if (s.length < 3) s = (s + 'repo').slice(0, 3)
  if (s.length > 63) s = s.slice(0, 63).replace(/-+$/, '')
  return s
}

export function formatAccuracy(val) {
  if (!val || val === 0) return '—'
  return val <= 1 ? `${(val * 100).toFixed(1)}%` : `${val.toFixed(1)}%`
}

/**
 * lakeFS reports CommitResponse.creation_date as a unix timestamp in SECONDS.
 * Accept seconds, milliseconds or an ISO string so a shape change can't
 * silently render 1970 dates.
 */
export function fmtUnixDate(value) {
  if (value == null || value === '') return '—'
  if (typeof value === 'string' && !/^\d+$/.test(value)) {
    const d = new Date(value)
    return Number.isNaN(d.getTime()) ? value : d.toLocaleString()
  }
  let n = Number(value)
  if (Number.isNaN(n)) return String(value)
  if (n < 1e12) n *= 1000 // seconds → ms
  const d = new Date(n)
  return Number.isNaN(d.getTime()) ? String(value) : d.toLocaleString()
}

export const MODEL_ICON_BG = {
  random_forest: 'rgba(34,197,94,.14)',
  logistic_regression: 'rgba(6,182,212,.14)',
  decision_tree: 'rgba(16,185,129,.14)',
  gradient_boosting: 'rgba(249,115,22,.14)',
  svm:           'rgba(245,158,11,.14)',
  knn:           'rgba(20,184,166,.14)',
  adaboost:      'rgba(239,68,68,.14)',
  extra_trees:   'rgba(132,204,22,.14)',
  naive_bayes:   'rgba(168,85,247,.14)',
  sgd:           'rgba(100,116,139,.14)',
  ridge:         'rgba(14,165,233,.14)',
  mlp:           'rgba(139,92,246,.14)',
  xgboost:       'rgba(59,130,246,.14)',
  lightgbm:      'rgba(234,179,8,.14)',
}
