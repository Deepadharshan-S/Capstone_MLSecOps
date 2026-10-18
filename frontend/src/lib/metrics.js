// Client-side classification metrics for the Testing page.
//
// POST /models/{name}/predict (and /deployments/{id}/predict) return only
// `predictions` — the expected labels stay with the caller — so accuracy,
// precision, recall, F1 and the confusion matrix are computed here.
//
// Averaging mirrors the backend so the numbers are directly comparable with
// the metrics on a job record: ray_wrapper.py:125-127 uses sklearn with
// `average="weighted", zero_division=0`, and confusion_matrix() is ordered by
// sorted unique labels (2x2 → {tn, fp, fn, tp}).
//
// Labels are compared as strings so a CSV cell parsed as the number 1 still
// matches a model that predicts the string "1".

const canon = (v) => (v === null || v === undefined ? '' : String(v))

/** Numeric-aware sort, matching how sklearn orders numeric label arrays. */
function labelSort(a, b) {
  const na = a === '' ? NaN : Number(a)
  const nb = b === '' ? NaN : Number(b)
  const aNum = Number.isFinite(na)
  const bNum = Number.isFinite(nb)
  if (aNum && bNum) return na - nb
  if (aNum) return -1
  if (bNum) return 1
  return a < b ? -1 : a > b ? 1 : 0
}

const safeDiv = (num, den) => (den > 0 ? num / den : 0)

/**
 * @param {Array} expected  ground-truth labels, one per input row
 * @param {Array} predicted model predictions, same length
 * @returns {object|null} metrics, or null when the inputs aren't comparable
 */
export function computeClassificationMetrics(expected, predicted) {
  if (!Array.isArray(expected) || !Array.isArray(predicted)) return null
  if (!expected.length || expected.length !== predicted.length) return null

  const labels = [...new Set([...expected, ...predicted].map(canon))].sort(labelSort)
  const indexOf = new Map(labels.map((l, i) => [l, i]))

  const n = labels.length
  const confusion = Array.from({ length: n }, () => new Array(n).fill(0))
  const mismatches = []
  let correct = 0

  for (let i = 0; i < expected.length; i++) {
    const e = indexOf.get(canon(expected[i]))
    const p = indexOf.get(canon(predicted[i]))
    if (e === undefined || p === undefined) continue // unreachable by construction
    confusion[e][p] += 1
    if (e === p) correct += 1
    else mismatches.push({ index: i, expected: expected[i], predicted: predicted[i] })
  }

  const total = expected.length
  const support = confusion.map((row) => row.reduce((a, b) => a + b, 0))

  const perClass = labels.map((label, i) => {
    const tp = confusion[i][i]
    const fp = labels.reduce((sum, _, c) => (c === i ? sum : sum + confusion[c][i]), 0)
    const fn = support[i] - tp
    const precision = safeDiv(tp, tp + fp)
    const recall = safeDiv(tp, tp + fn)
    const f1 = precision + recall > 0 ? (2 * precision * recall) / (precision + recall) : 0
    return { label, support: support[i], precision, recall, f1 }
  })

  const weighted = (key) =>
    perClass.reduce((sum, c) => sum + c[key] * c.support, 0) / total

  return {
    total,
    correct,
    accuracy: correct / total,
    precision: weighted('precision'),
    recall: weighted('recall'),
    f1: weighted('f1'),
    labels,
    support,
    confusion,
    perClass,
    // Same shape JobResults' ConfusionMatrix already renders for binary runs.
    binary: n === 2
      ? { tn: confusion[0][0], fp: confusion[0][1], fn: confusion[1][0], tp: confusion[1][1] }
      : null,
    mismatches,
  }
}
