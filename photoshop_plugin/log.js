/**
 * @file Tiny ring-buffer logger shared by every module in this plugin. Two
 * jobs only: (1) keep the last 50 lines around in memory for the panel's
 * collapsed "Advanced" section, and (2) keep the real Photoshop console
 * quiet — `console.warn` fires only for `logError`, prefixed `[cpsb]`, never
 * for routine status chatter (`logInfo`/`logWarn`). No dependencies, no
 * build step, safe to import from any other file in this plugin.
 */

const MAX_LINES = 50

/** @typedef {'info' | 'warn' | 'error'} CpsbLogLevel */

/**
 * @typedef {Object} CpsbLogLine
 * @property {number} ts - `Date.now()` when the line was recorded.
 * @property {CpsbLogLevel} level
 * @property {string} message
 */

/** @type {CpsbLogLine[]} */
const buffer = []

/** @type {Set<(line: CpsbLogLine) => void>} */
const listeners = new Set()

/**
 * @param {CpsbLogLevel} level
 * @param {string} message
 * @returns {void}
 */
function record(level, message) {
  const line = { ts: Date.now(), level, message }
  buffer.push(line)
  if (buffer.length > MAX_LINES) buffer.shift()
  if (level === 'error') {
    // The only console output this plugin ever produces — a real fault,
    // not routine status chatter (quality bar: no console spam).
    console.warn(`[cpsb] ${message}`)
  }
  for (const listener of listeners) {
    try {
      listener(line)
    } catch (_error) {
      // A broken listener (e.g. the panel mid-teardown) must never take
      // logging itself down with it.
    }
  }
}

/**
 * Records an informational line (connection state changes, handoff
 * lifecycle events, etc). Never reaches the console — visible only in the
 * panel's Advanced log view.
 * @param {string} message
 * @returns {void}
 */
function logInfo(message) {
  record('info', message)
}

/**
 * Records a recoverable-but-notable line (a reconnect attempt, taking the
 * export fallback path). Never reaches the console.
 * @param {string} message
 * @returns {void}
 */
function logWarn(message) {
  record('warn', message)
}

/**
 * Records a real fault and mirrors it to `console.warn`, prefixed `[cpsb]`.
 * @param {string} message
 * @returns {void}
 */
function logError(message) {
  record('error', message)
}

/**
 * @returns {CpsbLogLine[]} A snapshot of up to the last 50 log lines, oldest
 * first. Safe to hold onto — mutating the returned array does not affect
 * the internal buffer.
 */
function getLogLines() {
  return buffer.slice()
}

/**
 * Subscribes to new log lines as they're recorded, used by the panel's
 * Advanced section to append live rather than re-rendering the whole log on
 * every line.
 * @param {(line: CpsbLogLine) => void} callback
 * @returns {() => void} Unsubscribe function.
 */
function onLogLine(callback) {
  listeners.add(callback)
  return () => listeners.delete(callback)
}

/**
 * Best-effort stringification of anything that might be thrown or returned
 * as an "error" — a real `Error`, a plain string, or (notably) a Photoshop
 * `batchPlay` failure result, which is a plain object such as
 * `{_obj: "error", message: "...", result: -25922}` rather than an `Error`
 * instance (see the "Result value" section of Adobe's batchPlay reference).
 * @param {unknown} error
 * @returns {string}
 */
function describeError(error) {
  if (error instanceof Error) return error.message
  if (typeof error === 'string') return error
  if (error && typeof error === 'object' && typeof (/** @type {any} */ (error).message) === 'string') {
    return /** @type {any} */ (error).message
  }
  try {
    return JSON.stringify(error)
  } catch (_e) {
    return String(error)
  }
}

module.exports = { logInfo, logWarn, logError, getLogLines, onLogLine, describeError }
