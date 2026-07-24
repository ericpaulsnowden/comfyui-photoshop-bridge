/**
 * @file Live Mode — save-free canvas capture for realtime drawing
 * (docs/roadmap/realtime-drawing.md M1). While active for one document, this
 * streams a keep-latest JPEG of that document's composite to the server as
 * `live_frame` messages (PROTOCOL.md §3) after every detected change — no
 * save, no handoff, no disk. The ComfyUI-side `PhotoshopLiveCanvas` node
 * serves the newest frame; ComfyUI's own caching (IS_CHANGED keyed on the
 * server's frame counter) makes redundant re-queues near-free.
 *
 * CHANGE DETECTION — a cheap poll of `document.activeHistoryState.id`, NOT a
 * notification listener, and NOT pixel diffing:
 * - `historyStateChanged` notifications were researched and rejected: a
 *   Creative Cloud Developer Forums report ("Photoshop event delay and UXP
 *   batchPlay") says such events fire only after the user "clicks away",
 *   not promptly at stroke end, and BOTH shipped realtime precedents poll
 *   instead (Krita AI Diffusion diffs its canvas at 10Hz; the decompiled
 *   comfyui-photoshop UXP plugin polls the history-state id every 300ms —
 *   the exact mechanism used here). The poll is a plain DOM property read:
 *   no pixels move, no executeAsModal, effectively free at 4Hz.
 * - Whether the id updates PROMPTLY at stroke mouse-up (vs. also lagging
 *   until "click away") is this feature's keystone unknown — spike S-A in
 *   the roadmap, verified live by the owner's checklist item, not assumed
 *   here. If it lags, the M4 fallback is Krita-style capture-and-diff.
 * - Undo/redo also changes the history id — deliberately IN scope: undoing
 *   a stroke should re-render the reverted canvas too.
 *
 * CAPTURE — `imaging.getPixels({ targetSize })` + `encodeImageData`:
 * downscales AT CAPTURE using Photoshop's own resolution-pyramid cache
 * (documented "dramatic performance improvements"; the docs' own advice is
 * "use the smallest possible target size"), then encodes JPEG — the
 * documented, JPEG-only UXP encoder, and what the one shipped realtime
 * Photoshop↔SD plugin actually uses on the wire. `imageData.dispose()` in a
 * `finally` is the documented-mandatory memory hygiene for polling capture.
 * Wrapped in `core.executeAsModal` per this plugin's universal convention
 * for imaging calls (exporter.js) — mid-brushstroke behavior is the
 * roadmap's spike S-B, owner-verified.
 *
 * KEEP-LATEST — one capture in flight, ever (`capturing` guard): a tick that
 * lands while the previous capture/encode is still running just skips; the
 * next tick re-checks the (by then newer) history id. Frames are
 * fire-and-forget over the existing control websocket — no acks, no
 * retries; a dropped frame is replaced by the next stroke's.
 */

const { app, core, imaging } = require('photoshop')

const { connection } = require('./connection.js')
const { logInfo, logWarn, describeError } = require('./log.js')

/** How often the history-state id is polled while Live Mode is active. */
const LIVE_POLL_MS = 300

/** Long-side pixel cap for captured frames (roadmap: never move full-res). */
const LIVE_TARGET_SIZE = 768

/**
 * @typedef {Object} CpsbLiveState
 * @property {boolean} active
 * @property {number | null} documentId
 * @property {string} docTitle
 * @property {number} framesSent
 * @property {number | null} lastFrameAt - `Date.now()` of the last sent frame.
 * @property {number | null} lastCaptureMs - Duration of the last capture+encode.
 * @property {string | null} lastError
 */

/** @type {CpsbLiveState} */
const state = {
  active: false,
  documentId: null,
  docTitle: '',
  framesSent: 0,
  lastFrameAt: null,
  lastCaptureMs: null,
  lastError: null
}

/** Panel re-render hook — mirrors handoffs.js's registryEvents convention. */
const liveEvents = new EventTarget()

let timer = /** @type {ReturnType<typeof setInterval> | null} */ (null)
let lastHistoryId = /** @type {number | null} */ (null)
let capturing = false
let seq = 0

function notifyChanged() {
  liveEvents.dispatchEvent(new CustomEvent('change', { detail: { ...state } }))
}

/**
 * @param {number} documentId
 * @returns {import('photoshop').Document | null}
 */
function findDocumentById(documentId) {
  for (let i = 0; i < app.documents.length; i++) {
    if (app.documents[i].id === documentId) return app.documents[i]
  }
  return null
}

/**
 * The watched document's current history-state id, or `null` when it can't
 * be read (no such state yet, or a Photoshop version quirk) — a null simply
 * skips this tick rather than stopping the session.
 * @param {import('photoshop').Document} doc
 * @returns {number | null}
 */
function readHistoryId(doc) {
  try {
    const historyState = doc.activeHistoryState
    return historyState ? historyState.id : null
  } catch (_error) {
    return null
  }
}

/**
 * Captures the watched document once (downscaled JPEG) and sends it as a
 * `live_frame`. Never throws — a failed capture logs, records the error for
 * the panel, and waits for the next tick.
 * @param {import('photoshop').Document} doc
 * @returns {Promise<void>}
 */
async function captureAndSend(doc) {
  const startedAt = Date.now()
  let jpegBase64
  try {
    jpegBase64 = await core.executeAsModal(
      async () => {
        const { imageData } = await imaging.getPixels({
          documentID: doc.id,
          targetSize: { width: LIVE_TARGET_SIZE },
          componentSize: 8
        })
        try {
          return await imaging.encodeImageData({ imageData, base64: true })
        } finally {
          // Documented-mandatory for polled capture: release native memory
          // now, not whenever GC runs (Imaging API "Performance
          // considerations" — plugin memory warnings otherwise).
          imageData.dispose()
        }
      },
      { commandName: 'ComfyUI: live capture' }
    )
  } catch (error) {
    state.lastError = describeError(error)
    logWarn(`live capture failed (will retry on next change): ${state.lastError}`)
    notifyChanged()
    return
  }

  state.lastCaptureMs = Date.now() - startedAt
  seq += 1
  connection.send({
    type: 'live_frame',
    seq,
    data_b64: jpegBase64,
    doc_title: doc.title
  })
  state.framesSent += 1
  state.lastFrameAt = Date.now()
  state.lastError = null
  notifyChanged()
}

/**
 * One poll tick: cheap history-id read; capture only on change. See the
 * file doc comment for why polling, and for the keep-latest `capturing`
 * guard.
 * @returns {void}
 */
function tick() {
  if (!state.active || state.documentId == null) return
  const doc = findDocumentById(state.documentId)
  if (!doc) {
    logInfo(`live mode: document "${state.docTitle}" closed — stopping`)
    stopLive()
    return
  }
  if (connection.getState().status !== 'connected') return // frames would drop anyway
  if (capturing) return // keep-latest: never stack captures
  const historyId = readHistoryId(doc)
  if (historyId == null) return
  if (lastHistoryId !== null && historyId === lastHistoryId) return
  lastHistoryId = historyId
  capturing = true
  captureAndSend(doc)
    .catch((error) => {
      // captureAndSend never rejects by contract; belt only.
      logWarn(`unexpected live-capture rejection: ${describeError(error)}`)
    })
    .finally(() => {
      capturing = false
    })
}

/**
 * Starts Live Mode for the CURRENT active document. One session at a time
 * by design (roadmap M1): starting while active for another document
 * switches to the new one.
 * @returns {boolean} Whether a session is now active.
 */
function startLive() {
  /** @type {import('photoshop').Document | null} */
  let doc
  try {
    doc = app.activeDocument
  } catch (_error) {
    doc = null
  }
  if (!doc) {
    state.lastError = 'No active document'
    logWarn('live mode: no active document to watch')
    notifyChanged()
    return false
  }
  state.active = true
  state.documentId = doc.id
  state.docTitle = doc.title
  state.framesSent = 0
  state.lastFrameAt = null
  state.lastCaptureMs = null
  state.lastError = null
  lastHistoryId = null // first tick always captures a baseline frame
  if (!timer) {
    timer = setInterval(tick, LIVE_POLL_MS)
  }
  logInfo(`live mode ON for "${doc.title}" (${LIVE_POLL_MS}ms poll, ${LIVE_TARGET_SIZE}px)`)
  notifyChanged()
  return true
}

/** Stops the session (idempotent). @returns {void} */
function stopLive() {
  if (timer) {
    clearInterval(timer)
    timer = null
  }
  if (state.active) {
    logInfo(`live mode OFF for "${state.docTitle}" (${state.framesSent} frames sent)`)
  }
  state.active = false
  state.documentId = null
  lastHistoryId = null
  notifyChanged()
}

/** @returns {boolean} Whether a session is now active. */
function toggleLive() {
  if (state.active) {
    stopLive()
    return false
  }
  return startLive()
}

/** @returns {CpsbLiveState} A snapshot for the panel. */
function getLiveState() {
  return { ...state }
}

module.exports = { toggleLive, startLive, stopLive, getLiveState, liveEvents }
