/**
 * @file Receives `add_layer_chunk` transfers (refine pass R1, PROTOCOL.md
 * §3): the server-side `PhotoshopAddLayer` node pushes a full-quality PNG in
 * self-contained chunks; this module reassembles them per `transfer_id` and
 * places the image into the CURRENT document via `addAsLayer.js`, honoring
 * the main panel's Stack-vs-Replace preference (prefs.js).
 *
 * Loaded for its side effect from index.js (like previewPanel.js's own
 * `result_frame` listener): the listener registers at plugin load, so pushes
 * land even while no panel is open. Buffers are per-transfer and dropped on
 * disconnect (a dead connection can never complete a transfer — the server
 * would have logged the failed send on its side).
 */

const { connection, base64Decode } = require('./connection.js')
const { logInfo, logWarn, describeError } = require('./log.js')
const { addImageAsLayer } = require('./addAsLayer.js')
const { getRefinedLayerMode } = require('./prefs.js')

/** @type {Map<string, {chunks: string[], layerName: string}>} */
const pending = new Map()

connection.addEventListener('message', (event) => {
  const msg = /** @type {CustomEvent} */ (event).detail
  if (!msg || msg.type !== 'add_layer_chunk') return
  const transferId = msg.transfer_id
  const total = msg.total
  if (typeof transferId !== 'string' || typeof total !== 'number' || total <= 0) return
  if (typeof msg.data_b64 !== 'string') return

  let entry = pending.get(transferId)
  if (!entry) {
    entry = { chunks: [], layerName: typeof msg.layer_name === 'string' ? msg.layer_name : '' }
    pending.set(transferId, entry)
  }
  entry.chunks.push(msg.data_b64)
  if (entry.chunks.length < total) return

  pending.delete(transferId)
  const layerName = entry.layerName || 'ComfyUI refined'
  const mode = getRefinedLayerMode()
  let bytes
  try {
    bytes = base64Decode(entry.chunks.join(''))
  } catch (error) {
    logWarn(`add_layer transfer ${transferId} had undecodable payload: ${describeError(error)}`)
    return
  }
  logInfo(
    `placing refined image as layer "${layerName}" (${mode}, ${Math.round(bytes.length / 1024)} KB)`
  )
  addImageAsLayer(bytes, 'png', { layerName, mode }).catch((error) => {
    logWarn(`refined layer placement failed: ${describeError(error)}`)
  })
})

connection.addEventListener('statechange', (event) => {
  const state = /** @type {CustomEvent} */ (event).detail
  if (state && state.status !== 'connected' && pending.size) {
    pending.clear() // a dropped connection can never complete these
  }
})

module.exports = {}
