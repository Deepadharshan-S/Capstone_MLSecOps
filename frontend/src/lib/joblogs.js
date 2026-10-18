/* ══════════════════════════════════════════════════════
   Raw training-log text → console entries.

   GET /models/jobs/{id}/logs returns the head-pod tail (or the MinIO
   archive) as ONE opaque string, while TrainingConsole renders
   { time, level, msg } entries. This is the adapter between them.

   Ray stamps most lines with an ISO timestamp, but not every emitter does
   — so a leading timestamp is used when present and left blank otherwise.
   Stamping every line with the moment we happened to fetch would make a
   200-line replay read as if it all happened at once.
   ══════════════════════════════════════════════════════ */

const ISO_CLOCK = /^\s*\d{4}-\d{2}-\d{2}[T ](\d{2}:\d{2}:\d{2})/
const CLOCK = /^\s*(\d{2}:\d{2}:\d{2})/

// Substring matching on purpose: the backend's own classifier does the same,
// and over-colouring a log is worse than leaving a line neutral.
const ERRORISH = /error|exception|traceback|fatal/i
const WARNISH = /warn/i

function levelOf(line) {
  if (ERRORISH.test(line)) return 'error'
  if (WARNISH.test(line)) return 'warn'
  return 'info'
}

function stampOf(line) {
  const iso = ISO_CLOCK.exec(line)
  if (iso) return iso[1]
  const clock = CLOCK.exec(line)
  return clock ? clock[1] : ''
}

/**
 * @param {string} text   raw log body from GET /models/jobs/{id}/logs
 * @param {{limit?: number}} [opts]
 * @returns {Array<{time: string, level: string, msg: string}>}
 *          most recent `limit` non-empty lines, oldest first
 */
export function parseJobLogText(text, { limit = 400 } = {}) {
  if (typeof text !== 'string' || !text.trim()) return []

  const out = []
  for (const raw of text.replace(/\r\n/g, '\n').split('\n')) {
    const line = raw.trim()
    if (!line) continue
    out.push({ time: stampOf(line), level: levelOf(line), msg: line })
  }

  // The endpoint already tails, but a 5000-line tail would render a drawer
  // nobody can scroll past — keep the newest slice.
  return out.length > limit ? out.slice(-limit) : out
}
