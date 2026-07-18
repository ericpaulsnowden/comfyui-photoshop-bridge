/**
 * @file Delivers one edit's exported PNG bytes to the server, with
 * retry-and-backoff on failure. Never throws — failures are logged and
 * surfaced to the caller as a boolean so handoffs.js can update a handoff's
 * panel-visible status without wrapping every call in its own try/catch.
 *
 * Two transports, chosen per call by the plugin's CURRENT connection mode
 * (docs/PROTOCOL.md §2/§3):
 * - LOCAL mode: the original `multipart/form-data` HTTP POST to
 *   `/cpsb/upload` (`handoff_id`, `image`, `source: "plugin"`). Its
 *   `http://localhost` request is exempt from UXP's cleartext-to-remote-
 *   host block, so this keeps working unchanged.
 * - REMOTE mode: the identical bytes as chunked, base64 `upload_edit`
 *   websocket messages (`connection.uploadEditOverWs`). A `POST` to a
 *   non-localhost `http://` origin is exactly the failure this whole
 *   cross-machine fix exists for — UXP blocks it outright — while the
 *   plugin's control `ws://` connection is already proven to work remotely.
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
 * Uploads one edit's PNG bytes for `handoffId`, choosing the transport by
 * `connection.getState().localMode` at call time: `false` (REMOTE) goes
 * over the websocket; `true` or `null` (LOCAL, or mode not yet known) keeps
 * the original HTTP POST — see the file doc comment for why each exists.
 * @param {string} handoffId
 * @param {Uint8Array} pngBytes
 * @returns {Promise<boolean>} True if the server accepted the upload.
 */
async function uploadEdit(handoffId, pngBytes) {
  if (connection.getState().localMode === false) {
    return uploadEditOverWebsocket(handoffId, pngBytes)
  }
  return uploadEditOverHttp(handoffId, pngBytes)
}

/**
 * LOCAL-mode transport — unchanged HTTP POST behavior. Retries up to
 * {@link MAX_ATTEMPTS} times with backoff on network failure (`fetch`
 * throwing, or a 5xx/other unexpected status). A 404/409 — unknown or
 * inactive handoff (docs/PROTOCOL.md §2) — is not retried, since trying the
 * identical request again cannot change the handoff's state on the server.
 * @param {string} handoffId
 * @param {Uint8Array} pngBytes
 * @returns {Promise<boolean>}
 */
async function uploadEditOverHttp(handoffId, pngBytes) {
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

/**
 * REMOTE-mode transport — the same retry-with-backoff quality bar as
 * {@link uploadEditOverHttp}, but over `connection.uploadEditOverWs`
 * (chunked `upload_edit` websocket messages + an `upload_ok`/`upload_error`
 * ack) instead of `fetch`. An `upload_error` whose `.reason` is
 * `"unknown_handoff"` or `"inactive"` — the websocket equivalent of the
 * HTTP path's non-retried 404/409 — is not retried either, since resending
 * the identical bytes cannot change the handoff's server-side state. Every
 * other failure (timeout, dropped connection, any other `upload_error`)
 * retries like a network failure would over HTTP.
 * @param {string} handoffId
 * @param {Uint8Array} pngBytes
 * @returns {Promise<boolean>}
 */
async function uploadEditOverWebsocket(handoffId, pngBytes) {
  for (let attempt = 1; attempt <= MAX_ATTEMPTS; attempt++) {
    try {
      await connection.uploadEditOverWs(handoffId, pngBytes)
      return true
    } catch (error) {
      const reason = /** @type {any} */ (error) && /** @type {any} */ (error).reason
      if (reason === 'unknown_handoff' || reason === 'inactive') {
        logError(
          `upload for handoff ${handoffId} rejected (${describeError(error)}) — not ` +
            `retrying, the handoff is unknown or no longer active`
        )
        return false
      }
      logWarn(
        `upload for handoff ${handoffId} failed over websocket, attempt ` +
          `${attempt}/${MAX_ATTEMPTS}: ${describeError(error)}`
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
