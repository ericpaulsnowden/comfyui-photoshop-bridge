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

const { app } = require('photoshop')
const { connection } = require('./connection.js')
const { logInfo, logWarn, describeError } = require('./log.js')
const { setLivePrompt } = require('./livePrompt.js')
const { setLiveCreativity } = require('./liveCreativity.js')
const { liveEvents, getLiveState } = require('./liveMode.js')
const { makeDocKey, getDocSettings, saveDocSettings } = require('./livePrefs.js')
const { addImageAsLayer, addRenderAsLayer } = require('./addAsLayer.js')
const { requestRefineSafe } = require('./refineCapture.js')

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
/** The same frame's raw base64 (what "Add as a layer" writes to disk). */
let latestJpegB64 = /** @type {string | null} */ (null)
/** The hover overlay hosting the "Add as a layer" button. @type {HTMLElement | null} */
let addLayerOverlay = null
/** @type {any} */
let addLayerButton = null
/** Re-entrancy guard: one add at a time. */
let addingLayer = false

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
/** The Refine button (refine pass R2). @type {any} */
let refineButton = null
/** True from a Refine click until its result lands (or times out). */
let refining = false
/** @type {ReturnType<typeof setTimeout> | null} */
let refineResetTimer = null
/** Bound on waiting for a refined result before the button resets — refine
 * chains can be slow (SUPIR-class), so generous. */
const REFINE_BUTTON_TIMEOUT_MS = 180000
/** The Live-Mode documentId whose settings are currently loaded, so the
 * per-file restore fires once per session-doc, not on every frame's
 * liveEvents change. @type {number | null} */
let loadedLiveDocId = null

/**
 * The document the panel's controls currently "belong to": the Live-Mode
 * session's watched document while one is active (edits mid-session must
 * never land on a different file), else the active document (so a prompt
 * typed BEFORE starting Live Mode is saved under the doc that's about to be
 * watched). `null` when neither exists — persistence is skipped, never
 * guessed (owner ask: tie to the file, no strange cross-file behavior).
 * @returns {string | null}
 */
function resolveDocKey() {
  const live = getLiveState()
  if (live.active && live.documentId != null) {
    for (let i = 0; i < app.documents.length; i++) {
      if (app.documents[i].id === live.documentId) return makeDocKey(app.documents[i])
    }
    return null // watched doc vanished mid-tick — skip rather than misfile
  }
  try {
    return makeDocKey(app.activeDocument)
  } catch (_error) {
    return null
  }
}

/** Persists the panel's current control values under the owning document. */
function persistControls() {
  if (!promptField) return
  saveDocSettings(resolveDocKey(), {
    prompt: /** @type {any} */ (promptField).value || '',
    creativityKey: selectedCreativityKey
  })
}

/**
 * Loads *docKey*'s stored settings into the panel and streams them to the
 * server, so the graph immediately uses what the panel now shows. No stored
 * entry → adopt the panel's CURRENT content for this document instead
 * (covers "typed the prompt, then started Live Mode": the pre-typed text is
 * saved under the doc rather than clobbered by an empty restore).
 * @param {string | null} docKey
 * @returns {void}
 */
function applyDocSettings(docKey) {
  if (!promptField) return
  const stored = getDocSettings(docKey)
  if (!stored) {
    persistControls()
    return
  }
  /** @type {any} */ (promptField).value = stored.prompt
  selectedCreativityKey = stored.creativityKey
  refreshCreativityButtons()
  try {
    setLivePrompt(stored.prompt)
    if (stored.creativityKey) {
      const level = CREATIVITY_LEVELS.find((l) => l.key === stored.creativityKey)
      if (level) setLiveCreativity(level.value)
    }
  } catch (error) {
    logWarn(`restoring live-control settings failed: ${describeError(error)}`)
  }
}

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
  // Theme-aware text color, same variable panel.html uses. Without it this
  // panel's text inherited a near-black default — unreadable on Photoshop's
  // dark panel background (Eric's report). The labels' opacity accents now
  // dim a READABLE base instead of dimming black.
  rootDiv.style.color = 'var(--uxp-host-text-color)'

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

  // Hover overlay: a centered "Add as a layer" button over the render (owner
  // ask 2026-07-24 — roll over the image, click, get a new layer). Shown via
  // pointerenter/leave in JS (UXP :hover on arbitrary divs is not reliable),
  // and only once a render exists. pointerenter/leave don't fire when moving
  // onto the overlay's own button (they ignore child transitions), so the
  // button stays put while the cursor is over it.
  imageWrap.style.position = 'relative'
  addLayerOverlay = document.createElement('div')
  addLayerOverlay.id = 'cpsb-preview-addlayer-overlay'
  addLayerOverlay.style.position = 'absolute'
  addLayerOverlay.style.left = '0'
  addLayerOverlay.style.top = '0'
  addLayerOverlay.style.right = '0'
  addLayerOverlay.style.bottom = '0'
  addLayerOverlay.style.display = 'none'
  addLayerOverlay.style.alignItems = 'center'
  addLayerOverlay.style.justifyContent = 'center'

  addLayerButton = document.createElement('sp-button')
  addLayerButton.setAttribute('variant', 'cta')
  addLayerButton.textContent = 'Add as a layer'
  addLayerButton.addEventListener('click', () => {
    onAddAsLayer()
  })
  addLayerOverlay.appendChild(addLayerButton)
  imageWrap.appendChild(addLayerOverlay)

  imageWrap.addEventListener('pointerenter', () => {
    if (latestJpegB64 && addLayerOverlay) addLayerOverlay.style.display = 'flex'
  })
  imageWrap.addEventListener('pointerleave', () => {
    if (addLayerOverlay && !addingLayer) addLayerOverlay.style.display = 'none'
  })

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
    persistControls() // per-document restore across reloads (livePrefs.js)
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

  // Refine (refine pass R2): captures the document at full quality, uploads
  // it, and the ComfyUI live loop runs the workflow's muted refine branch
  // exactly once (pausing live re-renders while it works). The button stays
  // busy until the refined result lands as the next result_frame.
  const refineRow = document.createElement('div')
  refineRow.style.display = 'flex'
  refineRow.style.flexDirection = 'row'
  refineRow.style.marginTop = '10px'
  refineButton = document.createElement('sp-button')
  refineButton.setAttribute('variant', 'secondary')
  refineButton.textContent = 'Refine'
  refineButton.style.flex = '1 1 0'
  refineButton.addEventListener('click', () => {
    onRefineClick()
  })
  refineRow.appendChild(refineButton)

  const refineHint = document.createElement('div')
  refineHint.textContent =
    'Runs the workflow’s refine branch once at high quality (needs a muted "Photoshop Refine Source" chain). Live re-renders pause while it works.'
  refineHint.style.fontSize = '10px'
  refineHint.style.opacity = '0.5'
  refineHint.style.marginTop = '4px'

  controls.appendChild(promptLabel)
  controls.appendChild(promptField)
  controls.appendChild(creativityLabel)
  controls.appendChild(creativityRow)
  controls.appendChild(creativityHint)
  controls.appendChild(refineRow)
  controls.appendChild(refineHint)

  // Onboarding hint ONLY: shown until the first render arrives, then hidden
  // for good. The old always-on line (doc title + render count) was removed —
  // Eric: redundant with the main ComfyUI panel.
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

  // First build (plugin just loaded): restore the ACTIVE document's stored
  // prompt/creativity, so a Photoshop restart doesn't come up blank before
  // any Live session starts. Read-only when nothing is stored.
  const startupSettings = getDocSettings(resolveDocKey())
  if (startupSettings) {
    /** @type {any} */ (promptField).value = startupSettings.prompt
    selectedCreativityKey = startupSettings.creativityKey
    refreshCreativityButtons()
  }

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
    // First render: retire the onboarding hint. No doc-title/render-count
    // line — redundant with the main panel (Eric, 2026-07-24).
    statusEl.style.display = 'none'
  }
}

/**
 * The "Add as a layer" click: pushes the current render into the active
 * document (addAsLayer.js), with in-button progress/error feedback. One add
 * at a time; the frame is snapshotted up front so a new render landing
 * mid-add can't swap the bytes under it.
 *
 * QUALITY (refine pass R1): first fetches the server's FULL-QUALITY copy of
 * the render (`connection.requestLastRender()` — the un-downscaled,
 * losslessly-kept original of the JPEG this panel displays) and places THAT;
 * the panel's own 1024px display JPEG is only the fallback when the fetch
 * fails (disconnected, or the server restarted since the render).
 * @returns {void}
 */
function onAddAsLayer() {
  if (addingLayer || !latestJpegB64 || !addLayerButton) return
  const frameB64 = latestJpegB64
  addingLayer = true
  addLayerButton.textContent = 'Adding…'
  addLayerButton.setAttribute('disabled', '')
  connection
    .requestLastRender()
    .then((pngBytes) => addImageAsLayer(pngBytes, 'png', { layerName: 'ComfyUI render' }))
    .catch((fetchError) => {
      logWarn(
        `full-quality render fetch failed (${describeError(fetchError)}) — placing the ` +
          'panel display copy instead'
      )
      return addRenderAsLayer(frameB64)
    })
    .then(() => {
      addLayerButton.textContent = 'Added ✓'
    })
    .catch((error) => {
      logWarn(`"Add as a layer" failed: ${describeError(error)}`)
      addLayerButton.textContent = 'Failed — see log'
    })
    .finally(() => {
      addingLayer = false
      setTimeout(() => {
        if (addLayerButton) addLayerButton.textContent = 'Add as a layer'
        addLayerButton?.removeAttribute('disabled')
        if (addLayerOverlay) addLayerOverlay.style.display = 'none'
      }, 1200)
    })
}

/** Resets the Refine button to its idle state. @returns {void} */
function resetRefineButton() {
  refining = false
  if (refineResetTimer) {
    clearTimeout(refineResetTimer)
    refineResetTimer = null
  }
  if (refineButton) {
    refineButton.textContent = 'Refine'
    refineButton.removeAttribute('disabled')
  }
}

/**
 * The Refine click: capture + upload (refineCapture.js), then wait for the
 * refined result to land as the next `result_frame` (which resets the
 * button), bounded by {@link REFINE_BUTTON_TIMEOUT_MS}.
 * @returns {void}
 */
function onRefineClick() {
  if (refining || !refineButton) return
  refining = true
  refineButton.textContent = 'Capturing…'
  refineButton.setAttribute('disabled', '')
  requestRefineSafe().then((requestId) => {
    if (!refining) return // already reset (e.g. reconnect)
    if (requestId == null) {
      // Failed — refineCapture already logged the why.
      if (refineButton) refineButton.textContent = 'Refine failed — see log'
      refineResetTimer = setTimeout(resetRefineButton, 2500)
      return
    }
    if (refineButton) refineButton.textContent = 'Refining…'
    // A refine chain can be legitimately slow; if nothing ever lands, reset
    // rather than wedge the button forever.
    refineResetTimer = setTimeout(resetRefineButton, REFINE_BUTTON_TIMEOUT_MS)
  })
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
  persistControls() // per-document restore across reloads (livePrefs.js)
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
  latestJpegB64 = msg.data_b64
  latestDataUri = `data:image/jpeg;base64,${msg.data_b64}`
  showLatest()
  // A result landing while a Refine is in flight IS the refined result
  // (live re-renders are paused during a refine) — the button's done signal.
  if (refining) resetRefineButton()
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
  // A mid-refine disconnect can never deliver the refined result on this
  // connection — unwedge the button rather than waiting out the long timeout.
  if (status !== 'connected' && refining) {
    resetRefineButton()
  }
  lastConnStatus = status
})

// Per-file restore on Live-Mode start: when a session begins watching a NEW
// document, load that document's stored prompt/creativity into the panel and
// stream them — the controls follow the file being drawn on, never a stale
// one (owner ask 2026-07-24). liveEvents fires per frame too, so this gates
// on the documentId transition, not on every event.
liveEvents.addEventListener('change', (event) => {
  const state = /** @type {CustomEvent} */ (event).detail
  if (!state) return
  if (!state.active) {
    loadedLiveDocId = null // a restart on the same doc re-loads (harmless)
    return
  }
  if (state.documentId === loadedLiveDocId) return
  loadedLiveDocId = state.documentId
  try {
    applyDocSettings(resolveDocKey())
  } catch (error) {
    logWarn(`per-file control restore failed: ${describeError(error)}`)
  }
})

module.exports = { mountPreviewPanel }
