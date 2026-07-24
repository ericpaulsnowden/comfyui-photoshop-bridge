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
const { setLivePrompt } = require('./livePrompt.js')
const { setLiveCreativity } = require('./liveCreativity.js')

/** Slider's initial position (percent). Purely cosmetic until the user drags
 * it — until then the `PhotoshopLiveCreativity` node uses its own widget. */
const DEFAULT_CREATIVITY_PCT = 60

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
/** @type {HTMLElement | null} */
let promptField = null
/** @type {HTMLElement | null} */
let creativitySlider = null

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

  // A flex-fill wrapper OWNS the available space; the <img> inside is bounded
  // by BOTH dimensions and sized `auto`, so it always keeps its natural aspect
  // ratio. The previous layout put `flex: 1 1 auto` directly on the <img>,
  // which in a column flexbox forces the image to grow to fill the column's
  // height while `width: 100%` fixed its width independently -- i.e. it
  // squashed/stretched the render whenever the panel was resized to a
  // different aspect than the image (the skew Eric reported). Constraining the
  // image with max-width/max-height + width/height:auto preserves aspect on
  // its own, even on UXP builds that ignore object-fit; object-fit:contain is
  // belt-and-suspenders where it IS honored, and the wrapper centers whatever
  // letterboxing results.
  const imageWrap = document.createElement('div')
  imageWrap.id = 'cpsb-preview-image-wrap'
  imageWrap.style.flex = '1 1 auto'
  imageWrap.style.minHeight = '0'
  imageWrap.style.display = 'flex'
  imageWrap.style.alignItems = 'center'
  imageWrap.style.justifyContent = 'center'
  imageWrap.style.overflow = 'hidden'

  imageEl = document.createElement('img')
  imageEl.id = 'cpsb-preview-image'
  imageEl.style.maxWidth = '100%'
  imageEl.style.maxHeight = '100%'
  imageEl.style.width = 'auto'
  imageEl.style.height = 'auto'
  imageEl.style.objectFit = 'contain'
  imageWrap.appendChild(imageEl)

  // --- Controls UNDER the image: prompt + creativity slider. They live in
  // THIS (preview/output) panel by design — they sit with the result they
  // affect, and they survive collapsing the main "ComfyUI" panel. Built with
  // Spectrum widgets via createElement, since this panel has no HTML document
  // of its own (see the file doc). Both stream to the server (debounced) and
  // drive the matching nodes; a workflow without those nodes just ignores the
  // stream.
  const controls = document.createElement('div')
  controls.id = 'cpsb-preview-controls'
  controls.style.flex = '0 0 auto'
  controls.style.padding = '8px 0 0'

  const promptLabel = document.createElement('div')
  promptLabel.textContent = 'PROMPT'
  promptLabel.style.fontSize = '10px'
  promptLabel.style.opacity = '0.6'
  promptLabel.style.marginBottom = '4px'

  promptField = document.createElement('sp-textfield')
  promptField.id = 'cpsb-preview-prompt'
  promptField.setAttribute('multiline', '')
  promptField.setAttribute(
    'placeholder',
    'Describe what you want — drives the Photoshop Live Prompt node'
  )
  promptField.style.width = '100%'
  promptField.addEventListener('input', () => {
    try {
      setLivePrompt(/** @type {any} */ (promptField).value || '')
    } catch (error) {
      logWarn(`live prompt send failed: ${describeError(error)}`)
    }
  })

  const creativityHeader = document.createElement('div')
  creativityHeader.style.display = 'flex'
  creativityHeader.style.flexDirection = 'row'
  creativityHeader.style.justifyContent = 'space-between'
  creativityHeader.style.fontSize = '10px'
  creativityHeader.style.opacity = '0.6'
  creativityHeader.style.margin = '10px 0 4px'
  const creativityLabel = document.createElement('span')
  creativityLabel.textContent = 'CREATIVITY'
  const creativityValue = document.createElement('span')
  creativityValue.id = 'cpsb-preview-creativity-value'
  creativityHeader.appendChild(creativityLabel)
  creativityHeader.appendChild(creativityValue)

  /** @param {number} pct */
  const renderCreativityValue = (pct) => {
    creativityValue.textContent = `${Math.round(pct)}% · ${pct <= 50 ? 'faithful' : 'imaginative'}`
  }

  creativitySlider = document.createElement('sp-slider')
  creativitySlider.id = 'cpsb-preview-creativity'
  creativitySlider.setAttribute('min', '0')
  creativitySlider.setAttribute('max', '100')
  creativitySlider.setAttribute('value', String(DEFAULT_CREATIVITY_PCT))
  creativitySlider.style.width = '100%'
  renderCreativityValue(DEFAULT_CREATIVITY_PCT)
  creativitySlider.addEventListener('input', () => {
    const pct = Number(/** @type {any} */ (creativitySlider).value)
    if (!Number.isFinite(pct)) return
    renderCreativityValue(pct)
    try {
      setLiveCreativity(pct / 100)
    } catch (error) {
      logWarn(`live creativity send failed: ${describeError(error)}`)
    }
  })

  const creativityHint = document.createElement('div')
  creativityHint.textContent =
    'Low = hug your drawing · High = reinterpret it (drives Photoshop Live Creativity → denoise).'
  creativityHint.style.fontSize = '10px'
  creativityHint.style.opacity = '0.5'
  creativityHint.style.marginTop = '4px'

  controls.appendChild(promptLabel)
  controls.appendChild(promptField)
  controls.appendChild(creativityHeader)
  controls.appendChild(creativitySlider)
  controls.appendChild(creativityHint)

  statusEl = document.createElement('div')
  statusEl.id = 'cpsb-preview-status'
  statusEl.style.flex = '0 0 auto'
  statusEl.style.fontSize = '11px'
  statusEl.style.opacity = '0.7'
  statusEl.style.padding = '8px 0 0'
  statusEl.textContent =
    'Waiting for a render — add a "Photoshop Live Preview" node to the workflow.'

  rootDiv.appendChild(imageWrap)
  rootDiv.appendChild(controls)
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
