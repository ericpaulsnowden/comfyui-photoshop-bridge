/**
 * @file Wires up the "ComfyUI" panel's DOM (panel.html) to live plugin
 * state: the connection pill with failure diagnostics (target URL, last
 * error, retry countdown), the active-handoffs list with per-document
 * "Send" buttons, and the Advanced section's log ring buffer.
 * Pure UI glue — holds no state of its own beyond the DOM it renders into,
 * and every element it needs already exists in panel.html's static markup
 * (this plugin has exactly one panel, which is also the plugin's one and
 * only HTML document).
 *
 * The Advanced disclosure is a plain div toggled from JS because UXP does
 * not support `<details>`/`<summary>` (unsupported elements render as bare
 * divs and never collapse) nor the `hidden` attribute — both per Adobe's
 * uxp-api HTML reference ("Unsupported Elements" / "Unsupported
 * Attributes").
 */

const { connection } = require('./connection.js')
const { getActiveHandoffs, registryEvents, deliverEdit, clearAllHandoffs } = require('./handoffs.js')
const { getLogLines, onLogLine, logError, describeError } = require('./log.js')
const { isAutoFixEnabled, setAutoFixEnabled } = require('./prefs.js')
const { toggleLive, getLiveState, liveEvents } = require('./liveMode.js')

// Same version source the `hello` handshake message uses (connection.js):
// require('uxp').versions.plugin is documented to match manifest.json's
// "version" field, so the panel's version line and the handshake can never
// disagree.
const uxp = require('uxp')

/** @type {Record<import('./connection.js').CpsbConnectionState['status'], string>} */
const STATUS_LABELS = {
  disconnected: 'Disconnected',
  connecting: 'Connecting…',
  connected: 'Connected'
}

let initialized = false

/**
 * Wires up the panel. Safe to call more than once — only the first call
 * attaches listeners; in practice `index.js` calls this exactly once, from
 * the panel entrypoint's `create()`.
 * @returns {void}
 */
function initPanel() {
  if (initialized) return
  initialized = true

  const statusDot = /** @type {HTMLElement} */ (document.getElementById('cpsb-status-dot'))
  const statusText = /** @type {HTMLElement} */ (document.getElementById('cpsb-status-text'))
  const serverUrlEl = /** @type {HTMLElement} */ (document.getElementById('cpsb-server-url'))
  // Editable server-address field + its Apply button and inline error line
  // (Advanced section). This is where the user points the plugin at another
  // machine; the read-only serverUrlEl above shows the currently-active URL.
  const serverInput = /** @type {HTMLInputElement} */ (
    document.getElementById('cpsb-server-input')
  )
  const serverApply = /** @type {HTMLElement} */ (document.getElementById('cpsb-server-apply'))
  const serverErrorEl = /** @type {HTMLElement} */ (
    document.getElementById('cpsb-server-error')
  )
  const lastErrorEl = /** @type {HTMLElement} */ (document.getElementById('cpsb-last-error'))
  // Muted, Advanced-section home for a transient connection error's detail —
  // the raw close code/reason lives here so the top line can stay calm.
  const lastErrorAdvancedEl = /** @type {HTMLElement} */ (
    document.getElementById('cpsb-last-error-advanced')
  )
  const retryEl = /** @type {HTMLElement} */ (document.getElementById('cpsb-retry-line'))
  // Connect/Disconnect control (the user "cancel") + the calm standby message.
  const connectToggle = /** @type {HTMLElement} */ (
    document.getElementById('cpsb-connect-toggle')
  )
  const standbyLine = /** @type {HTMLElement} */ (
    document.getElementById('cpsb-standby-line')
  )
  // The always-visible boot banner index.js paints at load; panel.js takes
  // it over once connection state is known (adds the server version).
  const versionEl = /** @type {HTMLElement} */ (document.getElementById('cpsb-version'))
  const handoffList = /** @type {HTMLElement} */ (document.getElementById('cpsb-handoff-list'))
  const handoffClear = /** @type {HTMLElement} */ (document.getElementById('cpsb-handoff-clear'))
  const handoffActions = /** @type {HTMLElement} */ (
    document.getElementById('cpsb-handoff-actions')
  )
  // Advanced-section opt-out for the Maximize PSD Compatibility auto-fix
  // (prefs.js) — default ON, persisted like the server-address field.
  const maxCompatToggle = /** @type {HTMLInputElement} */ (
    document.getElementById('cpsb-maxcompat-toggle')
  )
  const logEl = /** @type {HTMLElement} */ (document.getElementById('cpsb-log'))
  // The whole Advanced body (version, URL, log) collapses together.
  const advancedBody = /** @type {HTMLElement} */ (
    document.getElementById('cpsb-advanced-body')
  )
  const advancedToggle = /** @type {HTMLElement} */ (
    document.getElementById('cpsb-advanced-toggle')
  )
  const advancedCaret = /** @type {HTMLElement} */ (
    document.getElementById('cpsb-advanced-caret')
  )

  /** @type {ReturnType<typeof setInterval> | null} */
  let retryTicker = null

  /**
   * Builds the retry/connecting line for a non-connected state.
   * @param {import('./connection.js').CpsbConnectionState} state
   * @returns {string} Empty string when there is nothing to show.
   */
  function retryText(state) {
    if (state.status === 'connecting') {
      return 'Connecting to ComfyUI…'
    }
    if (state.nextRetryAt != null) {
      const seconds = Math.max(0, Math.ceil((state.nextRetryAt - Date.now()) / 1000))
      // Phrased as waiting, not failing: a not-yet-reachable ComfyUI (still
      // starting up) is the common case and is nothing the user must act on.
      return `Waiting for ComfyUI — retrying in ${seconds}s`
    }
    return ''
  }

  /**
   * Renders the version self-identification line: plugin version alone
   * while not connected, plugin + server versions once connected, with an
   * amber "update available" heads-up when the two differ (docs/PROTOCOL.md
   * §9 — a mismatch is informational, the connection is still fine; never
   * red, never "refuse").
   * @param {import('./connection.js').CpsbConnectionState} state
   * @returns {void}
   */
  function renderVersionLine(state) {
    const pluginVersion = uxp.versions.plugin
    if (state.status === 'connected' && state.serverVersion) {
      const mismatch = state.serverVersion !== pluginVersion
      versionEl.textContent = mismatch
        ? `Plugin v${pluginVersion} • Server v${state.serverVersion} · update available`
        : `Plugin v${pluginVersion} • Server v${state.serverVersion}`
      // On mismatch, add the color-only accent class alongside the banner
      // class (which keeps the banner's weight/size); the accent is
      // self-sufficient so styling never depends on CSS cascade order.
      versionEl.className = mismatch ? 'cpsb-version cpsb-version-mismatch' : 'cpsb-version'
      return
    }
    versionEl.textContent = `Plugin v${pluginVersion}`
    versionEl.className = 'cpsb-version'
  }

  /** @returns {void} */
  function renderConnection() {
    const state = connection.getState()
    const standby = state.standby // 'superseded' | 'manual' | null
    serverUrlEl.textContent = state.url
    renderVersionLine(state)

    // The connection control toggles by intent: Connect while standing by
    // (idle, awaiting the user), Disconnect otherwise (so a stuck retry or a
    // tug-of-war can be stopped). Loud (cta) only when Connect is the
    // action -- this button holds the panel's single reserved CTA slot, so
    // Disconnect (a "cancel", not the primary ask) stays secondary.
    if (standby) {
      connectToggle.textContent = 'Connect'
      connectToggle.setAttribute('variant', 'cta')
    } else {
      connectToggle.textContent = 'Disconnect'
      connectToggle.setAttribute('variant', 'secondary')
    }

    // Status pill. Standby is idle, NOT a fault, so it shows a neutral (grey)
    // dot rather than the red disconnected dot.
    if (standby) {
      statusDot.className = 'cpsb-dot'
      statusText.textContent = standby === 'superseded' ? 'Standing by' : 'Disconnected'
    } else {
      statusDot.className = `cpsb-dot cpsb-dot-${state.status}`
      statusText.textContent = STATUS_LABELS[state.status] || state.status
    }

    // Standby explanation — a state, not an error.
    if (standby === 'superseded') {
      standbyLine.textContent =
        'Another Photoshop is connected to this ComfyUI. This one is standing by — ' +
        'press Connect to take over.'
      standbyLine.style.display = 'block'
    } else if (standby === 'manual') {
      standbyLine.textContent = 'Disconnected. Press Connect to reconnect.'
      standbyLine.style.display = 'block'
    } else {
      standbyLine.style.display = 'none'
    }

    // When connected, or when standing by (idle — no retrying), there is no
    // error/retry chatter to show; clear it all and stop the countdown ticker.
    if (state.status === 'connected' || standby) {
      lastErrorEl.style.display = 'none'
      lastErrorAdvancedEl.style.display = 'none'
      retryEl.style.display = 'none'
      if (retryTicker) {
        clearInterval(retryTicker)
        retryTicker = null
      }
      return
    }
    // A BLOCKING error (permission denial — the user must act) is the only one
    // that breaks out to the top, in red. A TRANSIENT error (ComfyUI not up
    // yet / retrying) stays out of the top entirely; its detail is mirrored
    // into the muted Advanced line so it's available without shouting. The
    // calm "Waiting for ComfyUI…" retry line below is the top-level signal
    // that it's just something to wait for.
    if (state.lastError && state.lastErrorBlocking) {
      lastErrorEl.textContent = `Action needed: ${state.lastError}`
      lastErrorEl.style.display = 'block'
    } else {
      lastErrorEl.style.display = 'none'
    }
    if (state.lastError) {
      lastErrorAdvancedEl.textContent = `Last connection error: ${state.lastError}`
      lastErrorAdvancedEl.style.display = 'block'
    } else {
      lastErrorAdvancedEl.style.display = 'none'
    }
    const retry = retryText(state)
    retryEl.textContent = retry
    retryEl.style.display = retry ? 'block' : 'none'
    // Tick once a second while not connected so the "Retrying in Ns"
    // countdown counts down instead of freezing at its first value.
    if (!retryTicker) {
      retryTicker = setInterval(renderConnection, 1000)
    }
  }

  /**
   * @param {import('./handoffs.js').CpsbHandoffRecord} record
   * @returns {HTMLElement}
   */
  function buildHandoffItem(record) {
    // Plain divs, not <ul>/<li>: UXP treats list elements as bare divs
    // anyway (uxp-api HTML reference, "Unsupported Elements").
    const item = document.createElement('div')
    item.className = 'cpsb-handoff-item'

    const name = document.createElement('div')
    name.className = 'cpsb-handoff-name'
    name.textContent = record.docTitle
    name.title = record.docTitle

    const status = document.createElement('div')
    status.className = 'cpsb-handoff-status'
    status.textContent = record.status

    const button = document.createElement('sp-button')
    button.setAttribute('variant', 'secondary')
    button.setAttribute('quiet', '')
    button.textContent = 'Send'
    button.addEventListener('click', () => {
      deliverEdit(record.handoffId).catch((error) => {
        logError(`"Send" (panel) failed: ${describeError(error)}`)
      })
    })

    item.appendChild(name)
    item.appendChild(status)
    item.appendChild(button)
    return item
  }

  /** @returns {void} */
  function renderHandoffs() {
    const records = getActiveHandoffs()
    handoffList.innerHTML = ''
    // Only offer "Clear list" when there's something to clear.
    handoffActions.className = records.length
      ? 'cpsb-conn-actions'
      : 'cpsb-conn-actions cpsb-collapsed'
    if (records.length === 0) {
      const empty = document.createElement('div')
      empty.className = 'cpsb-empty'
      empty.textContent = 'No documents from ComfyUI are currently open.'
      handoffList.appendChild(empty)
      return
    }
    for (const record of records) {
      handoffList.appendChild(buildHandoffItem(record))
    }
  }

  /**
   * @param {import('./log.js').CpsbLogLine} line
   * @returns {void}
   */
  function appendLogLine(line) {
    const time = new Date(line.ts).toLocaleTimeString()
    logEl.textContent += `[${time}] ${line.level.toUpperCase()} ${line.message}\n`
    logEl.scrollTop = logEl.scrollHeight
  }

  advancedToggle.addEventListener('click', () => {
    const collapsed = advancedBody.className.indexOf('cpsb-collapsed') !== -1
    advancedBody.className = collapsed ? '' : 'cpsb-collapsed'
    advancedCaret.textContent = collapsed ? '▾' : '▸'
  })

  /**
   * Applies the server-address field: normalizes + reconnects via
   * `connection.setServerBase`. Validation errors (empty/malformed input) are
   * shown inline using the existing error styling; on success the field is
   * rewritten with the normalized value and the normal statechange rendering
   * (the pill, the active-URL line) shows the reconnect result.
   * @returns {void}
   */
  function applyServerBase() {
    try {
      connection.setServerBase(serverInput.value)
      serverErrorEl.textContent = ''
      serverErrorEl.style.display = 'none'
      // Reflect the normalized form back to the user (e.g. scheme/path stripped).
      serverInput.value = connection.getServerBase()
    } catch (error) {
      serverErrorEl.textContent = describeError(error)
      serverErrorEl.style.display = 'block'
    }
  }

  serverApply.addEventListener('click', applyServerBase)
  // Prefill with the active base and start with the error line hidden.
  serverInput.value = connection.getServerBase()
  serverErrorEl.style.display = 'none'

  // Maximize PSD Compatibility auto-fix toggle: prefill from the persisted
  // setting (default ON — see prefs.js's isAutoFixEnabled), persist on every
  // change. The new value is read fresh by prefs.js on the NEXT connect —
  // toggling it does not retroactively affect an attempt already made this
  // session (see prefs.js's `_attempted` doc comment).
  maxCompatToggle.checked = isAutoFixEnabled()
  maxCompatToggle.addEventListener('change', () => {
    setAutoFixEnabled(Boolean(maxCompatToggle.checked))
  })

  // Connect/Disconnect: Connect when standing by (reclaim the slot), otherwise
  // Disconnect (bow out / stop retrying). The label is kept in sync by
  // renderConnection; this reads the live state so a mid-render click is safe.
  connectToggle.addEventListener('click', () => {
    if (connection.getState().standby) {
      connection.connect()
    } else {
      connection.disconnect()
    }
  })

  connection.addEventListener('statechange', renderConnection)
  handoffClear.addEventListener('click', () => {
    clearAllHandoffs()
  })

  registryEvents.addEventListener('change', renderHandoffs)
  onLogLine(appendLogLine)

  // Live Mode (realtime drawing M1): toggle + status line, re-rendered on
  // every liveEvents change (a sent frame, an error, start/stop).
  const liveToggle = /** @type {HTMLElement} */ (document.getElementById('cpsb-live-toggle'))
  const liveStatus = /** @type {HTMLElement} */ (document.getElementById('cpsb-live-status'))

  function renderLive() {
    const live = getLiveState()
    liveToggle.textContent = live.active ? 'Stop Live' : 'Start Live'
    if (!live.active) {
      liveStatus.textContent =
        live.lastError ||
        'Streams the active document to the Photoshop Live Canvas node as you draw.'
      return
    }
    const parts = [`Watching "${live.docTitle}"`, `${live.framesSent} frames`]
    if (live.lastCaptureMs != null) parts.push(`${live.lastCaptureMs}ms capture`)
    if (live.lastFrameAt != null) {
      // Refreshed per liveEvents change (each frame/error), not on a clock —
      // a live fps ticker is the roadmap's M4 polish, not M2's.
      parts.push(`last frame ${Math.max(0, Math.round((Date.now() - live.lastFrameAt) / 1000))}s ago`)
    }
    if (live.lastError) parts.push(`last error: ${live.lastError}`)
    liveStatus.textContent = parts.join(' · ')
  }

  liveToggle.addEventListener('click', () => {
    try {
      toggleLive()
    } catch (error) {
      logError(`Live toggle failed: ${describeError(error)}`)
    }
  })
  liveEvents.addEventListener('change', renderLive)

  renderConnection()
  renderHandoffs()
  renderLive()
  for (const line of getLogLines()) appendLogLine(line)
}

module.exports = { initPanel }
