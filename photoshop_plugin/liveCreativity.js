/**
 * @file Live creativity control (realtime drawing,
 * docs/roadmap/realtime-drawing.md): streams the preview panel's Low/Medium/
 * High Creativity buttons (as 0.0..1.0 levels) to the server as
 * `live_creativity` messages (PROTOCOL.md §3), debounced so a rapid run of
 * clicks sends at most one message per idle window. The ComfyUI-side
 * `PhotoshopLiveCreativity` node maps the value onto a denoise band (falling
 * back to its own widget when no level was ever chosen), and the frontend
 * live loop re-renders on each change (`cpsb.livecreativity`) — so the user
 * tunes how much the AI reinterprets their drawing from inside Photoshop,
 * without opening ComfyUI.
 *
 * Same fire-and-forget, keep-latest posture as `livePrompt.js`: only the
 * final value matters, the socket-not-open case drops silently, and the next
 * click re-sends.
 */

const { connection } = require('./connection.js')
const { getCreativityRange } = require('./prefs.js')
const { getCaptureSize } = require('./liveMode.js')

/** Idle window before a run of level clicks is flushed as one message. */
const LIVE_CREATIVITY_DEBOUNCE_MS = 120

let timer = /** @type {ReturnType<typeof setTimeout> | null} */ (null)
let pendingValue = 0.5
/** Whether the user has actually chosen a level this session. Until they
 * have, nothing is sent — the graph's own widget stays in charge, so a band
 * change alone must never silently start overriding it. */
let hasValue = false

/**
 * Queues the current creativity value (0.0..1.0) for sending. Coalesces
 * rapid clicks into one `live_creativity` per idle window; the trailing send
 * always carries the LATEST value. Non-finite input is ignored.
 * @param {number} value
 * @returns {void}
 */
function setLiveCreativity(value) {
  const v = Number(value)
  if (!Number.isFinite(v)) return
  pendingValue = Math.max(0, Math.min(1, v))
  hasValue = true
  if (timer) clearTimeout(timer)
  timer = setTimeout(() => {
    timer = null
    // Carry the band chosen for the CURRENT capture size (prefs.js) so the
    // node maps this level exactly as the panel says — the fix for "the
    // creativity levels behave differently at different resolutions": the
    // band follows the capture size instead of being one fixed graph-side
    // range. A workflow whose node has its own widgets still works: the
    // node only prefers this band when the panel sent one.
    const range = getCreativityRange(getCaptureSize())
    connection.send({
      type: 'live_creativity',
      value: pendingValue,
      min_denoise: range.min,
      max_denoise: range.max
    })
  }, LIVE_CREATIVITY_DEBOUNCE_MS)
}

/**
 * Re-sends the last chosen creativity level with the band that applies NOW —
 * called when the capture size or the range preset changes, so the graph
 * picks the new band up immediately instead of waiting for the user to
 * re-click a level. A no-op until a level has actually been chosen.
 * @returns {void}
 */
function resendLiveCreativity() {
  if (!hasValue) return
  setLiveCreativity(pendingValue)
}

module.exports = { setLiveCreativity, resendLiveCreativity, LIVE_CREATIVITY_DEBOUNCE_MS }
