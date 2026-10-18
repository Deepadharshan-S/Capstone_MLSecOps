// Minimal CSV helpers shared by the dataset preview (DatasetDetail) and the
// Testing page's test-run loader.
//
// Deliberately dependency-free: this only has to be correct for well-formed
// CSV (quoted fields, doubled quotes), which is all the preview and the
// test-run loader ever read.

/**
 * Split one CSV line into trimmed cells. Handles quoted commas and doubled
 * quotes; does not handle embedded newlines inside a quoted field.
 */
export function splitCsvLine(line) {
  const out = []
  let cur = ''
  let inQuotes = false
  for (let i = 0; i < line.length; i++) {
    const ch = line[i]
    if (inQuotes) {
      if (ch === '"') {
        if (line[i + 1] === '"') { cur += '"'; i++ } else { inQuotes = false }
      } else { cur += ch }
    } else if (ch === '"') {
      inQuotes = true
    } else if (ch === ',') {
      out.push(cur); cur = ''
    } else { cur += ch }
  }
  out.push(cur)
  return out.map((c) => c.trim())
}

/**
 * Parse CSV text into `{ columns, rows }`.
 *
 * Returns `{ empty: true }` when there is no content, or `{ isText: true,
 * text }` when the first line has no delimiter (the file isn't tabular).
 * `rowTruncated` reports that more data rows exist past `maxRows`.
 */
export function parseCsvTable(text, { maxRows = Infinity } = {}) {
  const lines = text.split(/\r?\n/).filter((l) => l.trim() !== '')
  if (lines.length === 0) return { empty: true }
  if (!lines[0].includes(',')) return { isText: true, text }

  const columns = splitCsvLine(lines[0])
  const dataLines = lines.slice(1)
  const limit = Number.isFinite(maxRows) ? Math.max(0, maxRows) : dataLines.length
  const rows = dataLines.slice(0, limit).map(splitCsvLine)
  return {
    columns,
    rows,
    rowTruncated: dataLines.length > rows.length,
  }
}
