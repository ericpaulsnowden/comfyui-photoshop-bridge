/**
 * @file POSTs exported PNG bytes to `/cpsb/upload` (docs/PROTOCOL.md §2:
 * `multipart/form-data` with `handoff_id`, `image`, `source: "plugin"`),
 * with retry-and-backoff on network failure. Never throws — failures are
 * logged and surfaced to the caller as a boolean so handoffs.js can update
 * a handoff's panel-visible status without wrapping every call in its own
 * try/catch.
 */

const { connection } = require('./connection.js')
const { logWarn, logError, describeError } = require('./log.js')

/** Retry budget for a single upload (quality bar: "retry x3 with backoff"). */
const MAX_ATTEMPTS = 3

/** Base backoff delay between attempts, scaled by attempt number. */
const RETRY_DELAY_MS = 1000

/**
 * @param {number} ms
 * @returns {Promise<void>}
 */
function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

/**
 * Uploads one edit's PNG bytes for `handoffId`. Retries up to
 * {@link MAX_ATTEMPTS} times with backoff on network failure (`fetch`
 * throwing, or a 5xx/other unexpected status). A 404/409 — unknown or
 * inactive handoff (docs/PROTOCOL.md §2) — is not retried, since trying the
 * identical request again cannot change the handoff's state on the server.
 * @param {string} handoffId
 * @param {Uint8Array} pngBytes
 * @returns {Promise<boolean>} True if the server accepted the upload.
 */
async function uploadEdit(handoffId, pngBytes) {
  for (let attempt = 1; attempt <= MAX_ATTEMPTS; attempt++) {
    try {
      const body = new FormData()
      body.append('handoff_id', handoffId)
      body.append('image', new Blob([pngBytes], { type: 'image/png' }), 'edit.png')
      body.append('source', 'plugin')
      const response = await fetch(`${connection.getHttpOrigin()}/cpsb/upload`, { method: 'POST', body })
      if (response.ok) {
        return true
      }
      if (response.status === 404 || response.status === 409) {
        logError(
          `upload for handoff ${handoffId} rejected (HTTP ${response.status}) — not ` +
            `retrying, the handoff is unknown or no longer active`
        )
        return false
      }
      logWarn(
        `upload for handoff ${handoffId} failed (HTTP ${response.status}), ` +
          `attempt ${attempt}/${MAX_ATTEMPTS}`
      )
    } catch (error) {
      logWarn(
        `upload for handoff ${handoffId} threw on attempt ${attempt}/${MAX_ATTEMPTS}: ` +
          describeError(error)
      )
    }
    if (attempt < MAX_ATTEMPTS) {
      await delay(RETRY_DELAY_MS * attempt)
    }
  }
  logError(`upload for handoff ${handoffId} failed after ${MAX_ATTEMPTS} attempts`)
  return false
}

module.exports = { uploadEdit }
