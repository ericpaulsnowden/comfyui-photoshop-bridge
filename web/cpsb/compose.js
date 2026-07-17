/**
 * @file Auto-growing `image_N` inputs for the "Compose Layers to PSD" node
 * (`PhotoshopComposePSD`, PROTOCOL.md §6c): "connecting one reveals the next
 * empty socket." The backend (`cpsb/compose_psd.py`, out of scope for this
 * file) declares a static, generous range of OPTIONAL `image_1..image_20`
 * sockets (`MAX_IMAGE_INPUTS` there) so the node accepts any number >= 1;
 * this module is purely a display concern layered on top — hiding every
 * disconnected socket except one, and revealing the next as the user
 * connects — so a freshly-added node doesn't show 20 empty sockets at once.
 *
 * FORK NOTICE (PROTOCOL.md §6c: "FORK the rgthree MIT pattern, with
 * attribution"): the "keep exactly one trailing empty slot, prune the
 * rest" shape below is adapted from
 * [rgthree/rgthree-comfy](https://github.com/rgthree/rgthree-comfy)'s
 * `Any Switch` (`src_web/comfyui/any_switch.ts`, MIT license — see
 * https://github.com/rgthree/rgthree-comfy/blob/main/LICENSE), specifically
 * its `stabilize()` method's "`removeUnusedInputsFromEnd` then
 * `addAnyInput()`" idea and its debounced `onConnectionsChange`/
 * `onConnectionsChainChange` trigger. This is NOT a copy of rgthree's code
 * (that class is woven into rgthree's own `RgthreeBaseServerNode`/
 * `followConnectionUntilType` framework, none of which is a published
 * stable API for external consumption — a fork of the PATTERN, not the
 * source, per research/research-multilayer-compose.md §2.2/§2.4's own
 * recommendation) and it differs in one load-bearing way: rgthree's sockets
 * are anonymous and get RENAMED on every stabilize
 * (`any_${this.inputs.length + 1}`) because their type is generic and
 * discovered from whatever connects; ours are STABLE, BACKEND-DECLARED,
 * fixed-name/fixed-type (`IMAGE`) sockets (`image_1`, `image_2`, ...) that
 * this module only ever shows or hides — it never invents a new socket name
 * ComfyUI's own `execute(self, ..., **kwargs)` (`cpsb/compose_psd.py`)
 * wouldn't recognize, and a saved workflow's `image_7` link is always
 * restored onto a real `image_7` socket, never a renumbered one.
 *
 * litegraph API verified against the CURRENT `Comfy-Org/ComfyUI_frontend`
 * source (this project's established standard — see e.g. `loadpsd.js`'s own
 * header — cloned into a scratch checkout rather than coded from memory):
 * - `LGraphNode.addInput(name, type, extraInfo)` / `.removeInput(slotIndex)`
 *   are the real, current, public slot-mutation API
 *   (`src/lib/litegraph/src/LGraphNode.ts`, ~line 1700/1726) — `addInput`
 *   pushes a new `INodeInputSlot` and returns it; `removeInput` disconnects
 *   (if linked) and splices it out of `node.inputs`. Both are what THIS
 *   package's own backend-declared sockets are shown/hidden through: no
 *   "hidden" boolean exists on `INodeSlot` in this frontend version
 *   (`src/lib/litegraph/src/interfaces.ts` ~line 302-320 — confirmed by
 *   reading the full interface) — the ONLY way to make a slot disappear
 *   from the rendered node is to actually remove it, so "hide"/"reveal" in
 *   this module's own vocabulary (and PROTOCOL.md §6c's) means
 *   remove/re-add, not a visibility flag.
 * - Every `image_N` socket already exists on the node from the moment it is
 *   constructed, BEFORE the `nodeCreated` extension hook this module's
 *   {@link attachAutoGrowInputs} is called from ever runs: ComfyUI's own
 *   node-construction path calls `addInputs(node, nodeData.inputs)` (which
 *   creates one socket per declared required/optional input via
 *   `addInputSocket`, `src/services/litegraphService.ts` ~line 216-230,
 *   ~line 332-339) synchronously inside the node constructor, strictly
 *   BEFORE `void extensionService.invokeExtensionsAsync('nodeCreated',
 *   this)` fires (confirmed at both the plain-node and subgraph-node
 *   construction sites, e.g. ~line 405-409). So this module's job at
 *   `nodeCreated` time is exactly "prune the 19 extra ones down to 1", never
 *   "create the first one".
 * - `onConnectionsChange(type, index, isConnected, linkInfo, slot)` is the
 *   real, current per-node hook (`LGraphNode.ts` ~line 623-630;
 *   `type === NodeSlotType.INPUT` for an input-side change,
 *   `src/lib/litegraph/src/types/globalEnums.ts` ~line 2-5:
 *   `NodeSlotType.INPUT = 1`); `LiteGraph.INPUT` (the runtime global this
 *   file actually reads, per this project's established `window.LiteGraph`
 *   convention — see badges.js/pasteback.js) mirrors the same value
 *   (`LiteGraphGlobal.ts` ~line 110: `INPUT = NodeSlotType.INPUT`).
 * - `RenderShape.HollowCircle = 7` (`globalEnums.ts` ~line 8-17) is the
 *   exact shape ComfyUI's own `addInputSocket` already assigned every
 *   `image_N` socket at creation (`isOptional ? RenderShape.HollowCircle :
 *   undefined` — every socket this node declares is optional), reused here
 *   so a socket this module re-adds after connecting still renders
 *   identically to one ComfyUI created directly.
 * - `app.configuringGraph` (`src/scripts/app.ts` ~line 285-287,
 *   `configuringGraphLevel > 0`) is `true` for the ENTIRE duration of
 *   `LGraph.prototype.configure` (~line 843-852: wrapped
 *   `configuringGraphLevel++`/`--`), which is what runs when a saved
 *   workflow is loaded — node creation (and this module's `nodeCreated`
 *   call) happens INSIDE that window, strictly BEFORE the same call
 *   restores this node's saved LINKS. Pruning sockets eagerly at
 *   `nodeCreated` time during a workflow load would delete the very socket
 *   a saved connection is about to be restored onto (e.g. a saved `image_7`
 *   link, with `image_2..image_6` never connected, arriving after this
 *   module had already pruned them down to just `image_1`) — silently
 *   dropping that connection. {@link scheduleStabilize} guards against this
 *   by deferring via `setTimeout(fn, 0)`: since `configure()` and its link
 *   restoration are fully synchronous, ANY macrotask-deferred callback
 *   naturally runs only after `configuringGraphLevel` has already dropped
 *   back to 0 (or, in the pathological worst case, is re-checked and
 *   deferred again) — so the actual prune/reveal logic never needs to run
 *   DURING a restore, only after.
 */

import { app } from '../../../scripts/app.js'

/**
 * Class id for the Compose Layers to PSD node (PROTOCOL.md §6c). Must match
 * `NODE_CLASS_MAPPINGS`'s key in the repo's top-level `__init__.py`
 * (out of scope for this file) verbatim.
 */
export const COMPOSE_PSD_NODE_TYPE = 'PhotoshopComposePSD'

/**
 * Upper bound on the `image_N` sockets the backend declares
 * (`cpsb/compose_psd.py`'s `MAX_IMAGE_INPUTS`, out of scope for this file --
 * kept in sync by hand, the same convention `cpsb/load_psd.py` and
 * `cpsb/routes.py` already use for their own small, stable, hand-mirrored
 * constants). This module never tries to reveal a socket beyond this index.
 */
const MAX_IMAGE_INPUTS = 20

/** `RenderShape.HollowCircle` (this file's header: `globalEnums.ts`). */
const RENDER_SHAPE_HOLLOW_CIRCLE = 7

/** Debounce window before a connection-change actually prunes/reveals sockets. */
const STABILIZE_DEBOUNCE_MS = 64

const IMAGE_INPUT_NAME_RE = /^image_(\d+)$/

/**
 * @param {import('../../../scripts/app.js').LGraphNode} node
 * @returns {boolean}
 */
export function isComposePsdNode(node) {
  return (node.comfyClass || node.type) === COMPOSE_PSD_NODE_TYPE
}

/**
 * @param {string} name
 * @returns {number|null} The `N` in `image_N`, or `null` if *name* doesn't match.
 */
function imageInputIndex(name) {
  const match = IMAGE_INPUT_NAME_RE.exec(name)
  return match ? Number(match[1]) : null
}

/**
 * @param {import('../../../scripts/app.js').LGraphNode} node
 * @returns {import('../../../scripts/app.js').IBaseWidget[]}
 */
function getImageInputs(node) {
  return (node.inputs ?? []).filter((input) => imageInputIndex(input.name) !== null)
}

/**
 * Removes the `image_N`-named input socket, by name, if currently present.
 * A no-op if it isn't (already removed, or never existed).
 * @param {import('../../../scripts/app.js').LGraphNode} node
 * @param {string} name
 */
function removeImageInputByName(node, name) {
  const index = (node.inputs ?? []).findIndex((input) => input.name === name)
  if (index !== -1) node.removeInput(index)
}

/**
 * Adds the `image_N`-named socket back, styled identically to how ComfyUI's
 * own node-construction path created it originally (this file's header).
 * @param {import('../../../scripts/app.js').LGraphNode} node
 * @param {number} index
 */
function addImageInput(node, index) {
  node.addInput(`image_${index}`, 'IMAGE', { shape: RENDER_SHAPE_HOLLOW_CIRCLE })
}

/**
 * The actual "hide all disconnected except one, reveal the next as the
 * visible one connects" pass (PROTOCOL.md §6c). Safe to call at any time —
 * defers itself via {@link scheduleStabilize} instead of acting while
 * `app.configuringGraph` (a workflow load in progress; see this file's
 * header for why running here would drop saved connections).
 *
 * Two responsibilities, run in order:
 * 1. Among the node's current `image_N` sockets (in ascending `N` order,
 *    which is how they're both declared and rendered), every CONNECTED one
 *    is always kept; among the DISCONNECTED ones, only the FIRST is kept —
 *    every later disconnected one is removed. This also naturally collapses
 *    a socket the user just disconnected back down to "one trailing empty
 *    slot" (PROTOCOL.md §6c's own framing), matching rgthree's
 *    prune-then-grow `stabilize()` shape (this file's header).
 * 2. If step 1 left NO disconnected socket at all (every declared one, up
 *    to what currently exists, is connected), reveal the next missing index
 *    (bounded by {@link MAX_IMAGE_INPUTS}) so there is always exactly one
 *    empty socket available to connect into next.
 * @param {import('../../../scripts/app.js').LGraphNode} node
 */
function stabilizeImageInputs(node) {
  if (!node.graph) return // removed from the graph since being scheduled
  if (app.configuringGraph) {
    scheduleStabilize(node) // workflow still loading -- retry once it settles
    return
  }

  let keptADisconnectedSlot = false
  for (const input of getImageInputs(node)) {
    if (input.link != null) continue // always keep connected sockets
    if (!keptADisconnectedSlot) {
      keptADisconnectedSlot = true // this is THE one trailing empty slot
      continue
    }
    removeImageInputByName(node, input.name)
  }

  if (!keptADisconnectedSlot) {
    const present = new Set(getImageInputs(node).map((input) => imageInputIndex(input.name)))
    for (let index = 1; index <= MAX_IMAGE_INPUTS; index++) {
      if (!present.has(index)) {
        addImageInput(node, index)
        break
      }
    }
  }

  node.graph?.setDirtyCanvas?.(true, true)
}

/**
 * Debounced, defer-safe trigger for {@link stabilizeImageInputs} — the
 * `rgthree`-pattern-derived "keep exactly one trailing empty slot" refinement
 * (this file's header), plus the `app.configuringGraph` guard
 * {@link stabilizeImageInputs} itself applies once actually run.
 * @param {import('../../../scripts/app.js').LGraphNode} node
 */
function scheduleStabilize(node) {
  if (node.__cpsbComposeStabilizeTimer) return
  node.__cpsbComposeStabilizeTimer = setTimeout(() => {
    node.__cpsbComposeStabilizeTimer = null
    stabilizeImageInputs(node)
  }, STABILIZE_DEBOUNCE_MS)
}

/**
 * Installs the auto-growing `image_N` input behavior on one
 * `PhotoshopComposePSD` node instance (PROTOCOL.md §6c). Call from
 * `nodeCreated`. Idempotent per node instance (the same convention
 * `badges.installBadgeHook`/`loadpsd.attachUploadWidget` already use) and a
 * no-op for any other node type.
 * @param {import('../../../scripts/app.js').LGraphNode} node
 */
export function attachAutoGrowInputs(node) {
  if (!isComposePsdNode(node)) return
  if (node.__cpsbComposeInputsAttached) return
  node.__cpsbComposeInputsAttached = true

  const originalOnConnectionsChange = node.onConnectionsChange
  node.onConnectionsChange = function (type, index, isConnected, linkInfo, slot) {
    const result = originalOnConnectionsChange?.call(this, type, index, isConnected, linkInfo, slot)
    if (type === (globalThis.LiteGraph?.INPUT ?? 1)) scheduleStabilize(this)
    return result
  }

  const originalOnRemoved = node.onRemoved
  node.onRemoved = function () {
    if (node.__cpsbComposeStabilizeTimer) {
      clearTimeout(node.__cpsbComposeStabilizeTimer)
      node.__cpsbComposeStabilizeTimer = null
    }
    return originalOnRemoved?.call(this)
  }

  // The initial "hide all but the first empty socket" pass (PROTOCOL.md
  // §6c). Always deferred, never run synchronously here: this correctly
  // covers both the interactive "drag a fresh node onto the canvas" case
  // (configuringGraph is already false, so the deferred call runs on the
  // very next tick) and a workflow-load case (deferred until configuration
  // settles) with the exact same code path -- see stabilizeImageInputs's
  // own docstring and this file's header.
  scheduleStabilize(node)
}
