/**
 * @file The realtime-drawing live loop (docs/roadmap/realtime-drawing.md M2):
 * every `cpsb.live` event (a new frame in the server's keep-latest slot) —
 * and every `cpsb.liveprompt` event (the user edited the panel's Live prompt
 * field) — can queue ONE coalesced re-run of the current workflow, so
 * re-renders track the user's strokes AND prompt tweaks with no busy-looping
 * and no queue pileup.
 *
 * WHY event-driven, not Auto-Queue "Instant": every serious realtime
 * integration researched (Krita AI Diffusion's own ComfyClient, the shipped
 * comfyui-photoshop plugin, ComfyStream/RealtimeNodes) drives execution
 * per-frame rather than spinning ComfyUI's browser Auto-Queue — and this
 * pack already queues per-arriving-edit for the save-triggered round trips
 * (`pasteback.js`'s `maybeAutoQueue`). This file is that same seam, with
 * Krita-style single-slot backpressure on top. (Auto-Queue Instant still
 * WORKS with the Live Canvas node — IS_CHANGED gating keeps it cheap — it's
 * just never required.)
 *
 * BACKPRESSURE — at most one of our runs queued at a time in the common
 * case ("single slot"; a narrowly-raced extra queue is possible between
 * queuePrompt resolving and the next status event, and is absorbed by the
 * Live Canvas node's IS_CHANGED caching — near-free by design):
 * `cpsb.live` while ComfyUI is busy sets a `trailing` flag instead of
 * stacking another queue; when the queue drains (ComfyUI's own `status`
 * event, `exec_info.queue_remaining === 0`), one trailing run fires and
 * picks up whatever frame is NEWEST BY THEN — intermediate frames are
 * deliberately never rendered (keep-latest, exactly Krita's QueuedJob
 * semantics). The graph side is already safe regardless: the Live Canvas
 * node's IS_CHANGED means even a redundant queue is served from cache.
 *
 * ARMING — the loop only fires when the CURRENT graph contains a
 * `PhotoshopLiveCanvas` node whose `auto_queue` widget is "On" (read
 * client-side per event — the same widget-read gating `pasteback.js` uses
 * for the bridge node's mode). No Live Canvas node, or all "Off": frames
 * still stream server-side and the next MANUAL queue picks up the newest;
 * this file just stays out of the way.
 */

import { app } from '../../../scripts/app.js'
import { api as comfyApi } from '../../../scripts/api.js'
import * as api from './api.js'

/** The Live Canvas node's class id (must match `__init__.py`'s mapping). */
const LIVE_CANVAS_NODE_TYPE = 'PhotoshopLiveCanvas'

let queueInFlight = false
let trailing = false
let queueRemaining = 0

/**
 * Whether *node* is an ACTIVE armed Live Canvas: right type, `auto_queue`
 * "On", and not muted/bypassed (`node.mode` 0 = ALWAYS — a muted node is
 * excluded from execution by ComfyUI itself, so it must not drive queueing
 * either; review-caught, 2026-07-24).
 * @param {any} node
 * @returns {boolean}
 */
function isArmedNode(node) {
  return (
    (node.comfyClass || node.type) === LIVE_CANVAS_NODE_TYPE &&
    (node.mode ?? 0) === 0 &&
    node.widgets?.find((w) => w.name === 'auto_queue')?.value === 'On'
  )
}

/**
 * Whether the current graph arms the live loop: at least one active Live
 * Canvas node with `auto_queue` = "On", searched RECURSIVELY through
 * subgraph nodes (review-caught, 2026-07-24: a Live Canvas tucked inside a
 * subgraph is executed by ComfyUI like any other node, so it must arm the
 * loop too — a root-only scan silently never armed). Read fresh per event,
 * never cached — the user flipping the widget (or deleting the node) takes
 * effect on the very next frame.
 * @param {any} [graph] - Defaults to the root graph.
 * @param {number} [depth] - Recursion guard; subgraphs a dozen deep are not
 * a real workflow shape.
 * @returns {boolean}
 */
function isArmed(graph = app.graph, depth = 0) {
  if (!graph || depth > 8) return false
  const nodes = graph._nodes
  if (!Array.isArray(nodes)) return false
  for (const node of nodes) {
    if (isArmedNode(node)) return true
    if (node.subgraph && isArmed(node.subgraph, depth + 1)) return true
  }
  return false
}

/**
 * Queues one run. `queueInFlight` covers the enqueue round-trip;
 * `queueRemaining` (ComfyUI's own status feed, below) covers execution —
 * together they enforce the single-slot rule the file doc describes.
 * @returns {void}
 */
function fireQueue() {
  queueInFlight = true
  app
    .queuePrompt(0)
    .catch((error) => {
      api.warn('live loop: queuePrompt failed', error)
    })
    .finally(() => {
      queueInFlight = false
    })
}

/**
 * A live change happened (a new frame, OR the panel prompt was edited) —
 * request one coalesced re-run. Both triggers share this seam: a prompt
 * tweak should re-render exactly like a stroke, and the same single-slot
 * backpressure applies to both (only the NEWEST state matters, whichever
 * input changed).
 * @returns {void}
 */
function requestQueue() {
  if (!isArmed()) return
  if (queueInFlight || queueRemaining > 0) {
    // Busy — remember that newer state exists, render it when the queue
    // drains. One flag, not a counter: only the NEWEST state matters.
    trailing = true
    return
  }
  fireQueue()
}

/**
 * ComfyUI's own execution-status feed. `detail.exec_info.queue_remaining`
 * shape confirmed against `Comfy-Org/ComfyUI_frontend`'s api.ts `status`
 * event payload (the same feed the stock queue counter renders from).
 * @param {CustomEvent} event
 * @returns {void}
 */
function onComfyStatus(event) {
  const remaining = event?.detail?.exec_info?.queue_remaining
  if (typeof remaining !== 'number') return
  queueRemaining = remaining
  if (remaining === 0 && trailing && !queueInFlight) {
    trailing = false
    if (isArmed()) fireQueue()
  }
}

/**
 * Subscribes to `cpsb.live` + ComfyUI's `status`. Call once from `cpsb.js`'s
 * `setup()`.
 * @returns {void}
 */
export function init() {
  api.onLive(() => {
    try {
      requestQueue()
    } catch (error) {
      api.warn('live loop: failed to handle cpsb.live', error)
    }
  })
  api.onLivePrompt(() => {
    try {
      requestQueue()
    } catch (error) {
      api.warn('live loop: failed to handle cpsb.liveprompt', error)
    }
  })
  comfyApi.addEventListener('status', (event) => {
    try {
      onComfyStatus(/** @type {CustomEvent} */ (event))
    } catch (error) {
      api.warn('live loop: failed to handle status event', error)
    }
  })
}
