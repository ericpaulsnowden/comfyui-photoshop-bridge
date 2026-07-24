/**
 * @file Live prompt control (realtime drawing prompt control,
 * docs/roadmap/realtime-drawing.md): streams the text the user types in the
 * panel's Live Mode prompt field to the server as `live_prompt` messages
 * (PROTOCOL.md §3), debounced so a burst of keystrokes sends at most one
 * message per idle window. The ComfyUI-side `PhotoshopLivePrompt` node serves
 * the newest text (falling back to its own node widget when the field is
 * empty), and the frontend live loop re-renders on each change
 * (`cpsb.liveprompt`) — so the user can steer the render from inside
 * Photoshop instead of tabbing to the ComfyUI graph.
 *
 * Fire-and-forget like `liveMode.js`'s frames: `connection.send` drops
 * silently if the socket isn't open, and the next keystroke (or a reconnect's
 * next edit) re-sends. There is nothing to retry — the server slot is
 * keep-latest, so only the final text ever matters.
 */

const { connection } = require('./connection.js')

/** Idle window before a burst of keystrokes is flushed as one message. */
const LIVE_PROMPT_DEBOUNCE_MS = 250

let timer = /** @type {ReturnType<typeof setTimeout> | null} */ (null)
let pendingText = ''

/**
 * Queues the current prompt text for sending. Coalesces rapid typing into one
 * `live_prompt` per idle window (keep-latest — only the final text matters);
 * the trailing send always carries the LATEST value, never a stale mid-burst
 * one.
 * @param {string} text
 * @returns {void}
 */
function setLivePrompt(text) {
  pendingText = typeof text === 'string' ? text : ''
  if (timer) clearTimeout(timer)
  timer = setTimeout(() => {
    timer = null
    connection.send({ type: 'live_prompt', text: pendingText })
  }, LIVE_PROMPT_DEBOUNCE_MS)
}

module.exports = { setLivePrompt, LIVE_PROMPT_DEBOUNCE_MS }
