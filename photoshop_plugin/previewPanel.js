/**
 * @file The "ComfyUI Preview" panel (realtime drawing M3,
 * docs/roadmap/realtime-drawing.md): a SECOND panel entrypoint the user
 * docks beside the canvas, showing the newest rendered result the ComfyUI
 * graph pushed back (`result_frame`, PROTOCOL.md §3 — sent by the
 * `PhotoshopLivePreview` node after each render). Draw on the canvas, watch
 * the AI re-render land here — without leaving Photoshop.
 *
 * MULTI-PANEL: one plugin, several `{"type": "panel"}` manifest entries and
 * one shared JS context, per Adobe's own EntryPoints reference (its
 * canonical example is a multi-key `panels` map) and a working
 * community-verified example (Creative Cloud Developer Forums, "Multiple
 * panels with different components/functionality" — davidebarranca's
 * three-panel manifest). Known caveats from that thread, honored here:
 * `show()` fires ONCE at creation (don't rely on it re-running per-open),
 * `hide()` may not fire reliably, and element lookups should use
 * `getElementById`-style references, not `querySelector`. The panel's DOM is
 * built HERE and attached into the root node the entrypoint hands us —
 * panel.html stays the MAIN panel's document, untouched.
 *
 * DISPLAY: an `<img>` whose `src` is swapped to a fresh JPEG data URI per
 * `result_frame`. Research found nothing documenting a UXP throttle on img
 * refresh, but nothing confirming multi-Hz smoothness either (roadmap spike
 * S-C, owner-verified via the checklist) — if it stutters in practice, the
 * planned fallback is a `<canvas>` + drawImage swap, which this module's
 * single `showFrame` seam keeps to a one-function change.
 *
 * Frames are keep-latest: each replaces the last, nothing is stored. The
 * module is required by index.js so its `connection` listener registers at
 * plugin load — a `result_frame` arriving while the panel has never been
 * opened is simply remembered as the latest, shown whenever the panel first
 * mounts.
 */

const { connection } = require('./connection.js')
const { logInfo, logWarn, describeError } = require('./log.js')

/** The latest frame, kept even while the panel is unmounted. */
let latestDataUri = /** @type {string | null} */ (null)
let latestDocTitle = ''
let framesReceived = 0

/** Built once, reattached on every mount. @type {HTMLElement | null} */
let rootDiv = null
/** @type {HTMLImageElement | null} */
let imageEl = null
/** @type {HTMLElement | null} */
let statusEl = null

/**
 * Builds the panel DOM once. Plain DOM + inline styles: this document's CSS
 * lives in panel.html (the MAIN panel's document); a second panel gets its
 * own root and should not depend on the other panel's stylesheet being in
 * scope.
 * @returns {HTMLElement}
 */
function buildDom() {
  if (rootDiv) return rootDiv
  rootDiv = document.createElement('div')
  rootDiv.id = 'cpsb-preview-root'
  rootDiv.style.display = 'flex'
  rootDiv.style.flexDirection = 'column'
  rootDiv.style.height = '100%'
  rootDiv.style.padding = '8px'

  imageEl = document.createElement('img')
  imageEl.id = 'cpsb-preview-image'
  imageEl.style.width = '100%'
  imageEl.style.flex = '1 1 auto'
  // UXP img has no object-fit guarantee; width-bound scaling is the safe
  // baseline and keeps aspect via natural sizing.
  imageEl.style.height = 'auto'

  statusEl = document.createElement('div')
  statusEl.id = 'cpsb-preview-status'
  statusEl.style.fontSize = '11px'
  statusEl.style.opacity = '0.7'
  statusEl.style.padding = '6px 0 0'
  statusEl.textContent =
    'Waiting for a render — add a "Photoshop Live Preview" node to the workflow.'

  rootDiv.appendChild(imageEl)
  rootDiv.appendChild(statusEl)
  return rootDiv
}

/**
 * The one display seam (see file doc: canvas fallback would replace only
 * this).
 * @returns {void}
 */
function showLatest() {
  if (!imageEl || !statusEl) return
  if (latestDataUri) {
    imageEl.src = latestDataUri
    statusEl.textContent = latestDocTitle
      ? `${latestDocTitle} · ${framesReceived} renders`
      : `${framesReceived} renders`
  }
}

/**
 * Mounts the panel into the entrypoint-provided root node. Tolerates the
 * shape differences between UXP versions (some hand the node directly, some
 * an event carrying `.node`) — and a missing node entirely, which logs
 * rather than throws so the main panel is never collateral damage.
 * @param {any} eventOrNode
 * @returns {void}
 */
function mountPreviewPanel(eventOrNode) {
  try {
    const node =
      eventOrNode && eventOrNode.node
        ? eventOrNode.node
        : eventOrNode && typeof eventOrNode.appendChild === 'function'
          ? eventOrNode
          : null
    if (!node) {
      logWarn('preview panel: no root node provided by the entrypoint — cannot mount')
      return
    }
    const dom = buildDom()
    if (dom.parentNode !== node) {
      node.appendChild(dom)
    }
    showLatest()
    logInfo('preview panel mounted')
  } catch (error) {
    logWarn(`preview panel mount failed: ${describeError(error)}`)
  }
}

connection.addEventListener('message', (event) => {
  const msg = /** @type {CustomEvent} */ (event).detail
  if (!msg || msg.type !== 'result_frame') return
  if (typeof msg.data_b64 !== 'string' || !msg.data_b64) return
  latestDataUri = `data:image/jpeg;base64,${msg.data_b64}`
  latestDocTitle = typeof msg.doc_title === 'string' ? msg.doc_title : ''
  framesReceived += 1
  showLatest()
})

module.exports = { mountPreviewPanel }
