// Client-side row sorting shared by the datasets catalog and the dataset
// files table.
//
// `sort` is the `{ key, dir }` shape DataTable's header click handler emits,
// so a caller can hand the state straight back as `sort`/`onSortChange` and
// sort the rows it renders. `formatters` lets a column sort on something
// other than the raw field (e.g. parsing an ISO timestamp), keyed by
// DataTable's `sortKey`.

/** Empty means "no value" — not `0`, which is a legitimate size/count. */
function isEmpty(v) {
  return v === null || v === undefined || v === ''
}

/**
 * Return a new array sorted by `sort`. Does not mutate `rows`.
 *
 * - Empty values sink to the bottom in *both* directions, so an ascending
 *   list isn't dominated by "—" rows.
 * - Numeric strings compare numerically ("row2" < "row10"), everything else
 *   uses a case/diacritic-insensitive locale comparison.
 */
export function sortRows(rows, sort, formatters = {}) {
  if (!sort || !sort.key) return rows
  const { key, dir } = sort
  const get = formatters[key]
  const factor = dir === 'desc' ? -1 : 1

  return [...rows].sort((a, b) => {
    const rawA = get ? get(a) : a?.[key]
    const rawB = get ? get(b) : b?.[key]

    if (isEmpty(rawA) && isEmpty(rawB)) return 0
    if (isEmpty(rawA)) return 1
    if (isEmpty(rawB)) return -1

    const numA = Number(rawA)
    const numB = Number(rawB)
    const bothNumeric = Number.isFinite(numA) && Number.isFinite(numB)
    const cmp = bothNumeric
      ? numA - numB
      : String(rawA).localeCompare(String(rawB), undefined, { numeric: true, sensitivity: 'base' })

    // Ties keep insertion order: Array#sort is stable in every engine the
    // app targets, so a column with many equal values stays in source order.
    return cmp * factor
  })
}
