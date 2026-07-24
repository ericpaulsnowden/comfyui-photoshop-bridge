/**
 * @file The realtime-drawing live loop (docs/roadmap/realtime-drawing.md M2):
 * every `cpsb.live` event (a new frame in the server's keep-latest slot), every
 * `cpsb.liveprompt` event (the prompt field changed) and every
 * `cpsb.livecreativity` event (the creativity slider moved) can queue ONE
 * coalesced re-run of the current workflow, so re-renders track the user's
 * strokes, prompt tweaks AND creativity changes with no busy-looping and no
 * queue pileup.
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
 *
 * REFINE (refine pass R2, `cpsb.refine`): a Refine click runs the graph's
 * muted refine branch exactly once — un-mute every muted
 * `PhotoshopRefineSource` (remembering each node's ORIGINAL mode), queue one
 * run, and when that queue drains restore the modes and resume the live
 * loop. While refining, live triggers set `trailing` instead of queueing
 * (Eric's auto-pause decision: the refine gets the GPU to itself; the newest
 * strokes render on resume). NOT gated on arming — a refine is an explicit
 * click and works with `auto_queue` Off or no Live Canvas at all.
 */

import { app } from '../../../scripts/app.js'
import { api as comfyApi } from '../../../scripts/api.js'
import * as api from './api.js'

/** The Live Canvas node's class id (must match `__init__.py`'s mapping). */
const LIVE_CANVAS_NODE_TYPE = 'PhotoshopLiveCanvas'

/** The Refine Source node's class id (refine pass R2). */
const REFINE_SOURCE_NODE_TYPE = 'PhotoshopRefineSource'

/** litegraph node modes: 0 = ALWAYS (active), 2 = NEVER (muted). */
const NODE_MODE_ALWAYS = 0

let queueInFlight = false
let trailing = false
let queueRemaining = 0

/** True from a `cpsb.refine` queue until that queue drains — pauses the live
 * auto-queue (Eric's decision: refine gets the GPU to itself; strokes keep
 * streaming and the trailing state renders on resume). */
let refining = false

/** The Refine Source nodes this refine un-muted, with their original modes,
 * so the re-mute restores EXACTLY what the user had (muted vs bypassed).
 * @type {{node: any, mode: number}[]} */
let unmutedRefineNodes = []

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
  if (refining) {
    // A refine holds the GPU (Eric's auto-pause decision). Strokes keep
    // streaming server-side; render the newest state when the refine drains.
    trailing = true
    return
  }
  if (queueInFlight || queueRemaining > 0) {
    // Busy — remember that newer state exists, render it when the queue
    // drains. One flag, not a counter: only the NEWEST state matters.
    trailing = true
    return
  }
  fireQueue()
}

/**
 * Collects every `PhotoshopRefineSource` node in the graph, searched
 * recursively through subgraphs (the `isArmed` walk).
 * @param {any} [graph]
 * @param {number} [depth]
 * @returns {any[]}
 */
function collectRefineSources(graph = app.graph, depth = 0) {
  if (!graph || depth > 8) return []
  const nodes = graph._nodes
  if (!Array.isArray(nodes)) return []
  const found = []
  for (const node of nodes) {
    if ((node.comfyClass || node.type) === REFINE_SOURCE_NODE_TYPE) found.push(node)
    if (node.subgraph) found.push(...collectRefineSources(node.subgraph, depth + 1))
  }
  return found
}

/**
 * A Refine click's upload completed (`cpsb.refine`): run the refine branch
 * exactly once — un-mute every muted `PhotoshopRefineSource`, queue ONE run,
 * and let {@link onComfyStatus} re-mute + resume the live loop when the
 * queue drains. Deliberately NOT gated on {@link isArmed}: a refine is an
 * explicit user action and must work even with `auto_queue` Off or no Live
 * Canvas in the graph. Verified against a real ComfyUI (0.28.0, test rig):
 * a muted node's dependent subtree is pruned from queues, and un-mute +
 * queue executes it.
 * @returns {void}
 */
function onRefineRequest() {
  const sources = collectRefineSources()
  if (!sources.length) {
    api.warn(
      'refine requested, but the current workflow has no "Photoshop Refine Source" node — ' +
        'add a (muted) refine branch to use Refine'
    )
    return
  }
  for (const node of sources) {
    const mode = node.mode ?? NODE_MODE_ALWAYS
    if (mode !== NODE_MODE_ALWAYS) {
      unmutedRefineNodes.push({ node, mode })
      node.mode = NODE_MODE_ALWAYS
    }
  }
  refining = true
  fireQueue()
}

/**
 * Restores the modes {@link onRefineRequest} overrode (muted stays muted,
 * bypassed stays bypassed) and ends the refine pause.
 * @returns {void}
 */
function endRefine() {
  for (const entry of unmutedRefineNodes) {
    entry.node.mode = entry.mode
  }
  unmutedRefineNodes = []
  refining = false
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
  if (remaining !== 0) return
  if (refining && !queueInFlight) {
    // The refine queue drained (success OR error — either way the run is
    // over): re-mute the refine branch and resume the live loop.
    endRefine()
  }
  if (trailing && !queueInFlight && !refining) {
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
  api.onLiveCreativity(() => {
    try {
      requestQueue()
    } catch (error) {
      api.warn('live loop: failed to handle cpsb.livecreativity', error)
    }
  })
  api.onRefine(() => {
    try {
      onRefineRequest()
    } catch (error) {
      api.warn('live loop: failed to handle cpsb.refine', error)
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
