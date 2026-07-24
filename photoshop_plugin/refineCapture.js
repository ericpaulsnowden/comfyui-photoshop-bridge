/**
 * @file The Refine click (refine pass R2, docs/roadmap/realtime-drawing.md):
 * captures the ACTIVE document at full quality and uploads it as a chunked
 * `refine_push` (PROTOCOL.md §3), which makes the server bump its refine
 * request id and emit `cpsb.refine` — the frontend live loop then runs the
 * workflow's (muted) refine branch exactly once.
 *
 * CAPTURE: `doc.duplicate()` → proportional downscale to ≤4096px long side
 * when needed (`resizeImage`, the ceiling Eric picked — "PSD size ideally,
 * proportionally capped below 4096") → `exportFlattenedDocument` (the
 * spike-6-proven PNG export path `exporter.js` already uses everywhere) →
 * close the duplicate. All inside ONE `executeAsModal` scope. A whole-doc
 * PNG export takes ~1-3s on big documents — fine for a deliberate Refine
 * click (this is NOT the per-stroke live path).
 *
 * The uploaded canvas is the refine branch's `canvas` source
 * (`PhotoshopRefineSource`, §6f); the branch's `render` source is the
 * server's own full-quality last-render slot, no upload needed.
 */

const { app, core } = require('photoshop')
const { connection } = require('./connection.js')
const { logInfo, logError, describeError } = require('./log.js')
const { exportFlattenedDocument } = require('./exporter.js')

/** Long-side pixel ceiling for the uploaded canvas capture (see file doc). */
const REFINE_MAX_SIDE = 4096

let refineCounter = 0

/**
 * Captures the active document (full quality, ≤{@link REFINE_MAX_SIDE}px
 * long side) and uploads it as a `refine_push`.
 * @returns {Promise<number>} The server's new refine `request_id`.
 * @throws {Error} When no document is open, not connected, the export fails,
 * or the server rejects the upload.
 */
async function requestRefine() {
  /** @type {import('photoshop').Document | null} */
  let doc
  try {
    doc = app.activeDocument
  } catch (_error) {
    doc = null
  }
  if (!doc) {
    throw new Error('No open document to refine')
  }
  if (connection.getState().status !== 'connected') {
    throw new Error('Not connected to ComfyUI')
  }

  const bytes = await core.executeAsModal(
    async () => {
      const dup = await doc.duplicate()
      try {
        const width = Number(dup.width)
        const height = Number(dup.height)
        const longSide = Math.max(width, height)
        if (longSide > REFINE_MAX_SIDE) {
          const factor = REFINE_MAX_SIDE / longSide
          await dup.resizeImage(Math.round(width * factor), Math.round(height * factor))
        }
        return await exportFlattenedDocument(dup, { flatten: true })
      } finally {
        await dup.closeWithoutSaving()
      }
    },
    { commandName: 'ComfyUI: refine capture' }
  )

  refineCounter += 1
  const refineId = `refine-${Date.now()}-${refineCounter}`
  logInfo(`refine: uploading ${Math.round(bytes.length / 1024)} KB canvas capture`)
  const requestId = await connection.pushRefineOverWs(refineId, bytes)
  logInfo(`refine: request ${requestId} accepted — the refine branch runs once`)
  return requestId
}

/**
 * Never-throwing wrapper for UI call sites: logs and returns null on any
 * failure.
 * @returns {Promise<number | null>}
 */
async function requestRefineSafe() {
  try {
    return await requestRefine()
  } catch (error) {
    logError(`Refine failed: ${describeError(error)}`)
    return null
  }
}

module.exports = { requestRefine, requestRefineSafe, REFINE_MAX_SIDE }
