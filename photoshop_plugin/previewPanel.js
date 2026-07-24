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

/**
 * Creativity is a THREE-STEP choice, not a continuous slider (Eric's
 * feedback: the 0–100 slider was too granular — nothing changed perceptibly
 * between neighbouring percents, only across low/medium/high). Each level
 * sends a 0..1 value that `PhotoshopLiveCreativity` maps onto its denoise
 * band, so Low/Medium/High land on the band's min / midpoint / max.
 * @type {{ key: string, label: string, value: number }[]}
 */
const CREATIVITY_LEVELS = [
  { key: 'low', label: 'Low', value: 0.0 },
  { key: 'medium', label: 'Medium', value: 0.5 },
  { key: 'high', label: 'High', value: 1.0 }
]

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
/** key -> the Low/Medium/High <sp-button> elements. @type {Record<string, any>} */
let creativityButtons = {}
/** The level the user has picked, or null while the graph's own widget drives
 * denoise (nothing sent until the user chooses). @type {string | null} */
let selectedCreativityKey = null
/** Last connection status seen, so we re-flush controls only on the
 * transition INTO 'connected' (the reconnect desync fix). */
let lastConnStatus = /** @type {string | null} */ (null)

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

  const creativityLabel = document.createElement('div')
  creativityLabel.textContent = 'CREATIVITY'
  creativityLabel.style.fontSize = '10px'
  creativityLabel.style.opacity = '0.6'
  creativityLabel.style.margin = '10px 0 4px'

  // Low / Medium / High as a segmented row of buttons (not a slider) — the
  // selected level gets the loud CTA variant, the rest stay quiet.
  const creativityRow = document.createElement('div')
  creativityRow.style.display = 'flex'
  creativityRow.style.flexDirection = 'row'
  creativityButtons = {}
  for (const level of CREATIVITY_LEVELS) {
    const btn = document.createElement('sp-button')
    btn.setAttribute('variant', 'secondary')
    btn.setAttribute('quiet', '')
    btn.textContent = level.label
    btn.style.flex = '1 1 0'
    btn.style.marginRight = level.key === 'high' ? '0' : '6px'
    btn.addEventListener('click', () => selectCreativity(level.key))
    creativityButtons[level.key] = btn
    creativityRow.appendChild(btn)
  }

  const creativityHint = document.createElement('div')
  creativityHint.textContent =
    'Low = hug your drawing · High = reinterpret it. Until you pick, the graph’s own setting is used.'
  creativityHint.style.fontSize = '10px'
  creativityHint.style.opacity = '0.5'
  creativityHint.style.marginTop = '4px'

  controls.appendChild(promptLabel)
  controls.appendChild(promptField)
  controls.appendChild(creativityLabel)
  controls.appendChild(creativityRow)
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

/** Highlights whichever creativity button is currently selected. @returns {void} */
function refreshCreativityButtons() {
  for (const level of CREATIVITY_LEVELS) {
    const btn = creativityButtons[level.key]
    if (!btn) continue
    const active = level.key === selectedCreativityKey
    btn.setAttribute('variant', active ? 'cta' : 'secondary')
    if (active) btn.removeAttribute('quiet')
    else btn.setAttribute('quiet', '')
  }
}

/**
 * Picks a creativity level, highlights it, and streams its 0..1 value.
 * @param {string} key
 * @returns {void}
 */
function selectCreativity(key) {
  const level = CREATIVITY_LEVELS.find((l) => l.key === key)
  if (!level) return
  selectedCreativityKey = key
  refreshCreativityButtons()
  try {
    setLiveCreativity(level.value)
  } catch (error) {
    logWarn(`live creativity send failed: ${describeError(error)}`)
  }
}

/**
 * Re-sends the panel's current control values to the server. Called on every
 * (re)connect: a new plugin connection resets the server's keep-latest slots
 * to empty, so without this the panel would still SHOW a prompt/level the
 * graph no longer receives (Eric hit exactly this after toggling Live Mode /
 * reconnecting — he had to retype to re-sync). The prompt is always re-sent
 * (empty just clears, matching an empty field); creativity is re-sent only if
 * the user actually picked a level (otherwise the graph's own widget stays in
 * charge).
 * @returns {void}
 */
function flushControls() {
  if (!promptField) return // panel never built — nothing to sync
  try {
    setLivePrompt(/** @type {any} */ (promptField).value || '')
    if (selectedCreativityKey) {
      const level = CREATIVITY_LEVELS.find((l) => l.key === selectedCreativityKey)
      if (level) setLiveCreativity(level.value)
    }
  } catch (error) {
    logWarn(`control re-sync failed: ${describeError(error)}`)
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

// Re-sync the panel's controls whenever the connection (re)establishes: a new
// server-side connection starts with EMPTY prompt/creativity slots, so the
// panel's shown values must be re-sent or the graph silently loses them.
// Registered at module load (like the message listener) so it survives
// reconnects even while the panel is unmounted.
connection.addEventListener('statechange', (event) => {
  const state = /** @type {CustomEvent} */ (event).detail
  const status = state && state.status
  if (status === 'connected' && lastConnStatus !== 'connected') {
    flushControls()
  }
  lastConnStatus = status
})

module.exports = { mountPreviewPanel }
