/**
 * @file The realtime-drawing live loop (docs/roadmap/realtime-drawing.md M2):
 * every `cpsb.live` event (a new frame in the server's keep-latest slot), every
 * `cpsb.liveprompt` event (the prompt field changed) and every
 * `cpsb.livecreativity` event (the creativity level changed) can queue ONE
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
 *
 * REFINE RECOVERY — the restore-and-resume above normally rides on ComfyUI's
 * queue-drain `status` event, but some paths submit NO job (so no drain ever
 * comes), and one path delivers the drain BEFORE we hear back:
 * - `app.queuePrompt` REJECTS, or THROWS SYNCHRONOUSLY: nothing was
 *   submitted. The enqueue's own failure handler restores the modes and ends
 *   the refine pause (and the in-flight bookkeeping is released either way —
 *   a synchronous throw used to leave it set forever).
 * - EARLY DRAIN: a fast job can queue AND finish (`status` 1 then 0) before
 *   `app.queuePrompt`'s promise settles (it does more async work after the
 *   POST). A drain is deliberately ignored while an enqueue is in flight (a
 *   stale pre-enqueue "0" must not end a refine that has not been
 *   serialised yet), so the settle handler re-checks: if the queue went busy
 *   since this fire and is idle now, the drain already happened — finish it
 *   then. Every restore goes through the idempotent {@link endRefine}, so a
 *   drain and a failure can never double-restore, and a late event can never
 *   re-apply an old mode over something the user changed since.
 * Known limit: ComfyUI's own `app.queuePrompt` also swallows submission
 * errors (a graph that fails validation shows its error dialog and RESOLVES
 * `false`) — indistinguishable here from "queued behind a running enqueue",
 * which also resolves `false` — so that case still waits for the next queue
 * activity to restore the modes.
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

/**
 * The `app.queuePrompt` calls we made that have not settled yet (resolved,
 * rejected or thrown). A SET, not a boolean: overlapping enqueues (a live fire
 * still in flight when Refine is clicked, or a double click) must each be
 * accounted for, or the first to settle would clear the flag under the others.
 * Each entry says whether it carries a refine.
 * @type {Set<{refine: boolean}>}
 */
const pendingEnqueues = new Set()
let trailing = false
let queueRemaining = 0

/** True once ComfyUI reported a NON-EMPTY queue (`queue_remaining` > 0) since
 * the newest {@link fireQueue} — i.e. our job was accepted. It is what lets the
 * settle handler recognise an early drain (queued and finished before
 * `app.queuePrompt` resolved). */
let queueSeenBusy = false

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
 * Whether any of our `app.queuePrompt` calls is still unsettled — the
 * "enqueue round-trip" half of the single-slot rule (the other half is
 * `queueRemaining`, ComfyUI's own status feed).
 * @returns {boolean}
 */
function isEnqueueing() {
  return pendingEnqueues.size > 0
}

/**
 * Queues one run. The pending-enqueue set covers the enqueue round-trip;
 * `queueRemaining` (ComfyUI's own status feed, below) covers execution —
 * together they enforce the single-slot rule the file doc describes. Every
 * outcome of `app.queuePrompt` — resolved, rejected OR a synchronous throw —
 * ends in {@link settleEnqueue}, so the in-flight state can never be left set.
 * @param {boolean} [forRefine] - This enqueue carries a Refine (its failure
 * must end the refine pause; a plain live fire's must not).
 * @returns {void}
 */
function fireQueue(forRefine = false) {
  const entry = { refine: forRefine }
  pendingEnqueues.add(entry)
  queueSeenBusy = false
  let result
  try {
    result = Promise.resolve(app.queuePrompt(0))
  } catch (error) {
    settleEnqueue(entry, true, error)
    return
  }
  result.then(
    () => settleEnqueue(entry, false),
    (error) => settleEnqueue(entry, true, error)
  )
}

/**
 * One `app.queuePrompt` call finished, one way or another: release its
 * in-flight slot, then recover. A FAILED refine enqueue submitted no job, so
 * no queue-drain event will ever restore the source nodes — do it here (unless
 * another refine enqueue is still pending: that one owns the recovery, and
 * restoring now could re-mute the branch before it is serialised). A
 * successful settle re-checks for an early drain. A failed plain live fire
 * does not retry on its own (a dead server would loop); the next live event
 * does.
 * @param {{refine: boolean}} entry
 * @param {boolean} failed
 * @param {unknown} [error]
 * @returns {void}
 */
function settleEnqueue(entry, failed, error) {
  pendingEnqueues.delete(entry)
  try {
    if (failed) {
      api.warn('live loop: queuePrompt failed', error)
      const refineStillPending = [...pendingEnqueues].some((e) => e.refine)
      if (entry.refine && refining && !refineStillPending) {
        endRefine()
        resumeLive()
      }
      return
    }
    if (queueSeenBusy) onQueueQuiet() // early drain: it already came and went
  } catch (settleError) {
    api.warn('live loop: failed to settle queuePrompt', settleError)
  }
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
  if (isEnqueueing() || queueRemaining > 0) {
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
  try {
    for (const node of sources) {
      const mode = node.mode ?? NODE_MODE_ALWAYS
      if (mode !== NODE_MODE_ALWAYS) {
        unmutedRefineNodes.push({ node, mode })
        node.mode = NODE_MODE_ALWAYS
      }
    }
    refining = true
    fireQueue(true)
  } catch (error) {
    // Never leave the branch un-muted behind a refine that did not start.
    endRefine()
    throw error
  }
}

/**
 * Restores the modes {@link onRefineRequest} overrode (muted stays muted,
 * bypassed stays bypassed) and ends the refine pause. Idempotent: the entry
 * list is emptied, so a second call (a drain AND a failure both arriving) is a
 * no-op and can never re-apply a stale mode over a change the user made since.
 * @returns {void}
 */
function endRefine() {
  const entries = unmutedRefineNodes
  unmutedRefineNodes = []
  refining = false
  for (const entry of entries) {
    entry.node.mode = entry.mode
  }
}

/**
 * Fires the coalesced trailing run, if one is owed and nothing blocks it (no
 * refine pause, no enqueue in flight, and an idle queue). The queue check
 * matters on the failure path: a rejected refine enqueue can settle while an
 * earlier live job is still executing, and firing then would put two runs in
 * the queue — the single-slot rule. `trailing` stays set instead, and the
 * drain fires it.
 * @returns {void}
 */
function resumeLive() {
  if (!trailing || refining || isEnqueueing() || queueRemaining > 0) return
  trailing = false
  if (isArmed()) fireQueue()
}

/**
 * The queue is idle and no enqueue is in flight: a refine (if one is running)
 * is over — success OR error, either way the run has ended — so re-mute its
 * branch, then resume the live loop.
 * @returns {void}
 */
function onQueueQuiet() {
  if (queueRemaining !== 0 || isEnqueueing()) return
  if (refining) endRefine()
  resumeLive()
}

/**
 * ComfyUI's own execution-status feed. `detail.exec_info.queue_remaining`
 * shape confirmed against `Comfy-Org/ComfyUI_frontend`'s api.ts `status`
 * event payload (the same feed the stock queue counter renders from).
 * A drain seen while an enqueue is still in flight is NOT acted on here (see
 * the REFINE RECOVERY note in the file doc): {@link settleEnqueue} re-checks
 * once the enqueue settles.
 * @param {CustomEvent} event
 * @returns {void}
 */
function onComfyStatus(event) {
  const remaining = event?.detail?.exec_info?.queue_remaining
  if (typeof remaining !== 'number') return
  queueRemaining = remaining
  if (remaining !== 0) {
    if (remaining > 0) queueSeenBusy = true
    return
  }
  onQueueQuiet()
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
