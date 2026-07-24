/**
 * @file Live creativity control (realtime drawing,
 * docs/roadmap/realtime-drawing.md): streams the preview panel's Creativity
 * slider (0.0..1.0) to the server as `live_creativity` messages
 * (PROTOCOL.md §3), debounced so a slider drag sends at most one message per
 * idle window. The ComfyUI-side `PhotoshopLiveCreativity` node maps the value
 * onto a denoise band (falling back to its own widget when the slider was
 * never touched), and the frontend live loop re-renders on each change
 * (`cpsb.livecreativity`) — so the user tunes how much the AI reinterprets
 * their drawing from inside Photoshop, without opening ComfyUI.
 *
 * Same fire-and-forget, keep-latest posture as `livePrompt.js`: only the
 * final value matters, the socket-not-open case drops silently, and the next
 * drag re-sends.
 */

const { connection } = require('./connection.js')

/** Idle window before a slider drag is flushed as one message. */
const LIVE_CREATIVITY_DEBOUNCE_MS = 120

let timer = /** @type {ReturnType<typeof setTimeout> | null} */ (null)
let pendingValue = 0.5

/**
 * Queues the current creativity value (0.0..1.0) for sending. Coalesces a
 * rapid drag into one `live_creativity` per idle window; the trailing send
 * always carries the LATEST value. Non-finite input is ignored.
 * @param {number} value
 * @returns {void}
 */
function setLiveCreativity(value) {
  const v = Number(value)
  if (!Number.isFinite(v)) return
  pendingValue = Math.max(0, Math.min(1, v))
  if (timer) clearTimeout(timer)
  timer = setTimeout(() => {
    timer = null
    connection.send({ type: 'live_creativity', value: pendingValue })
  }, LIVE_CREATIVITY_DEBOUNCE_MS)
}

module.exports = { setLiveCreativity, LIVE_CREATIVITY_DEBOUNCE_MS }
