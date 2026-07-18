/**
 * @file Three independent per-node display concerns for the "Compose Layers
 * to PSD" node (`PhotoshopComposePSD`, PROTOCOL.md §6c), all owned here
 * because this is where the rest of this package already does
 * compose-node-specific widget/socket manipulation:
 *
 * 1. Auto-growing `image_N` inputs: "connecting one reveals the next empty
 *    socket." The backend (`cpsb/compose_psd.py`, out of scope for this
 *    file) declares a static, generous range of OPTIONAL `image_1..image_20`
 *    sockets (`MAX_IMAGE_INPUTS` there) so the node accepts any number >= 1;
 *    this module is purely a display concern layered on top — hiding every
 *    disconnected socket except one, and revealing the next as the user
 *    connects — so a freshly-added node doesn't show 20 empty sockets at once.
 * 2. Append-target widgets (product owner, verbatim: "When append to
 *    existing is false can the two existing fields below that be
 *    disabled"): `existing_psd`/`existing_psd_path` (the `append_to_existing`
 *    BOOLEAN's target-selection widgets, `cpsb/compose_psd.py`'s
 *    `INPUT_TYPES`, added v0.5.20) are greyed out and click-blocked whenever
 *    `append_to_existing` is `false`, and restored live the instant it's
 *    toggled back to `true` — cosmetic only, the backend always reads
 *    whatever values they hold regardless. See this file's "Append-target
 *    widgets" section below for the `widget.disabled` mechanism and why it
 *    was chosen over `widget.hidden`.
 * 3. "Written: &lt;filename&gt;" display (product owner gap, verbatim: "And
 *    for 'don't open' how do I later find and open the file?"): the backend
 *    now emits a `cpsb.compose_written` event (`cpsb/compose_psd.py`
 *    `_emit_compose_written`) immediately after every real PSD write, for
 *    all three `mode` values alike — including `MODE_DONT_OPEN`, which never
 *    opens Photoshop and never creates a handoff, so none of this pack's
 *    handoff-driven discoverability surface (gallery, badges, node-menu
 *    active-handoff submenu) ever learns the file exists. This module's
 *    {@link init} subscribes to that event and shows the written filename as
 *    a plain, non-clickable text row plus a separate "Copy Path" button
 *    (product owner, verbatim: "Can the written message be just text and
 *    there be a separate 'Copy Path' button. And can that button copy the
 *    full path not just the file name") on the originating node;
 *    {@link getWrittenFileRef}/{@link hasWrittenFile} let `menu.js` also
 *    offer "Open in Photoshop" for a Compose node with no `node.imgs` at all
 *    (build brief item 4).
 *
 * See this file's "Written-file display" section below for the full design
 * (including what does and does not survive a browser reload).
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
import * as api from './api.js'
import * as pasteback from './pasteback.js'
import * as state from './state.js'
import * as ui from './ui.js'

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

// -----------------------------------------------------------------------
// Append-target widgets (this file's header, section 2; product owner,
// verbatim: "When append to existing is false can the two existing fields
// below that be disabled"): `existing_psd`/`existing_psd_path`
// (`cpsb/compose_psd.py`'s `INPUT_TYPES`, added v0.5.20, the last three
// required inputs — `append_to_existing` BOOLEAN, `existing_psd` COMBO,
// `existing_psd_path` STRING) only matter when `append_to_existing` is
// `true`; when it's `false` this section greys the other two out and blocks
// clicks on them, purely cosmetic — the backend's own `execute()` still
// receives whatever values they currently hold either way (it simply
// ignores them when `append_to_existing` is falsy, same as before this
// section existed).
//
// Mechanism decision — `widget.disabled`, NOT `widget.hidden` (+ a manual
// node-size recompute, this file's OWN fallback pattern for `image_N`
// SOCKETS above): verified against the CURRENT `Comfy-Org/ComfyUI_frontend`
// source, the same established methodology this file's header already uses
// for every other litegraph API claim here (cloned into a scratch checkout
// rather than coded from memory):
// - `BaseWidget` (`src/lib/litegraph/src/widgets/BaseWidget.ts` ~line
//   101-106) has a real, current, first-class `disabled` getter/setter
//   backed by `_state.disabled`. Every widget `cpsb/compose_psd.py`'s
//   `INPUT_TYPES` declares (BOOLEAN/COMBO/STRING) is constructed as a real
//   `BaseWidget` subclass instance by ComfyUI's own node-construction path
//   (`BooleanWidget`/`ComboWidget`/`TextWidget`, `widgetMap.ts`), so setting
//   `.disabled` on one of THIS node's own widgets needs no monkey-patching —
//   it's the same property the class already has.
// - `LGraphNode.drawWidgets` (`LGraphNode.ts` ~line 3956-3961: `widget.
//   computedDisabled = widget.disabled || this.getSlotFromWidget(widget)?.
//   link != null`) recomputes `computedDisabled` from `.disabled` on EVERY
//   draw pass, and (~line 3990) halves `ctx.globalAlpha` for any
//   `computedDisabled` widget — visually greyed out automatically, with no
//   manual per-frame bookkeeping from this module: set `.disabled` once,
//   request one redraw (`setDirtyCanvas`), done.
// - `LGraphCanvas`'s own pointer-down handler (`LGraphCanvas.ts` ~line
//   2877: `node.getWidgetOnPos(x, y)`, the exact call that decides which
//   widget receives `processWidgetClick`) uses `getWidgetOnPos`'s default
//   `includeDisabled = false` (`LGraphNode.ts` ~line 2259-2277: skips any
//   `(widget.computedDisabled && !includeDisabled)` widget entirely) — so a
//   disabled COMBO/STRING widget's dropdown/text-edit box never opens; the
//   click never reaches the widget at all. This is real click-blocking, not
//   just a visual dimming.
// `hidden` was rejected for THIS case (unlike the `image_N` sockets above,
// where litegraph SLOTS genuinely have no visibility flag at all, forcing
// remove/re-add): litegraph WIDGETS already have this first-class
// enable/disable flag that does exactly what was asked ("disabled", not
// "gone" — the product owner's own word), so reaching for `hidden` (a
// different visual effect — the row vanishes and the node resizes) plus a
// manual size recompute would be solving a problem `disabled` doesn't have.
//
// Toggle-timing: `append_to_existing`'s widget `callback` only fires for an
// INTERACTIVE toggle (a real pointer click, via `BaseWidget.setValue`) —
// {@link updateAppendTargetWidgetsEnabled} is chained onto it (never
// clobbering whatever callback, if any, ComfyUI's own construction path
// already assigned) for the "reacts immediately" requirement. A WORKFLOW
// LOAD instead assigns the saved value directly (`LGraphNode.configure`:
// `widget.value = info.widgets_values[i++]`, no `callback` invoked) — so
// {@link scheduleAppendTargetSync} exists for exactly that path, the same
// defer-until-`app.configuringGraph`-settles idiom
// {@link scheduleStabilize}/{@link scheduleRestoreWrittenDisplay} already
// establish elsewhere in this file (see this file's header for the timing
// argument itself, which applies identically here: at the `nodeCreated`
// moment this node's OWN `configure()` hasn't run yet, so `append_to_
// existing`'s widget still holds its just-constructed DEFAULT value, not
// whatever a saved workflow restores moments later).
// -----------------------------------------------------------------------

const APPEND_TO_EXISTING_WIDGET_NAME = 'append_to_existing'
const EXISTING_PSD_WIDGET_NAME = 'existing_psd'
const EXISTING_PSD_PATH_WIDGET_NAME = 'existing_psd_path'

/**
 * @param {import('../../../scripts/app.js').LGraphNode} node
 * @param {string} name
 * @returns {import('../../../scripts/app.js').IBaseWidget | undefined}
 */
function findWidgetByName(node, name) {
  return node.widgets?.find((w) => w.name === name)
}

/**
 * Applies the node's current `append_to_existing` value to the `.disabled`
 * flag of `existing_psd`/`existing_psd_path` (this section's header): both
 * disabled when `append_to_existing` is falsy, both re-enabled the instant
 * it's truthy. A no-op if `append_to_existing` itself isn't present (an
 * impossible case for a node built from the current `INPUT_TYPES`, but
 * cheap to guard) — leaves whichever of the other two widgets DOES exist
 * untouched rather than assuming both always do.
 * @param {import('../../../scripts/app.js').LGraphNode} node
 */
function updateAppendTargetWidgetsEnabled(node) {
  const toggle = findWidgetByName(node, APPEND_TO_EXISTING_WIDGET_NAME)
  if (!toggle) return
  const enabled = toggle.value === true
  const existingPsd = findWidgetByName(node, EXISTING_PSD_WIDGET_NAME)
  const existingPsdPath = findWidgetByName(node, EXISTING_PSD_PATH_WIDGET_NAME)
  if (existingPsd) existingPsd.disabled = !enabled
  if (existingPsdPath) existingPsdPath.disabled = !enabled
  node.graph?.setDirtyCanvas(true, false)
}

/**
 * Deferred, `configuringGraph`-safe initial sync of the append-target
 * widgets' `.disabled` flag — see this section's header ("Toggle-timing")
 * for why a deferred pass, separate from the live `callback` chain, is the
 * only place a just-loaded workflow's `append_to_existing` value ever gets
 * applied to `existing_psd`/`existing_psd_path`.
 * @param {import('../../../scripts/app.js').LGraphNode} node
 */
function scheduleAppendTargetSync(node) {
  if (node.__cpsbAppendTargetSyncTimer) return
  node.__cpsbAppendTargetSyncTimer = setTimeout(() => {
    node.__cpsbAppendTargetSyncTimer = null
    if (!node.graph) return // removed from the graph since being scheduled
    if (app.configuringGraph) {
      scheduleAppendTargetSync(node) // workflow still loading -- retry once it settles
      return
    }
    updateAppendTargetWidgetsEnabled(node)
  }, STABILIZE_DEBOUNCE_MS)
}

/**
 * Installs the append-target enable/disable behavior on one
 * `PhotoshopComposePSD` node instance (this section's header). Exported —
 * matching every other `attach*` in this file being its own clean,
 * independently-callable entry point — but ALSO invoked directly from
 * {@link attachAutoGrowInputs} below, since THIS change's scope is
 * `compose.js` only and `web/cpsb.js`'s own `nodeCreated` wiring (where
 * every other `attach*` here is actually invoked) is out of bounds for it;
 * idempotent per node instance (the guard immediately below), so a future
 * explicit `nodeCreated` wire-up of this function directly is a safe,
 * ordinary no-op layered on top of this. A no-op for any other node type.
 * @param {import('../../../scripts/app.js').LGraphNode} node
 */
export function attachAppendTargetWidgets(node) {
  if (!isComposePsdNode(node)) return
  if (node.__cpsbAppendTargetAttached) return
  node.__cpsbAppendTargetAttached = true

  const toggle = findWidgetByName(node, APPEND_TO_EXISTING_WIDGET_NAME)
  if (toggle) {
    const originalCallback = toggle.callback
    toggle.callback = function (value, ...rest) {
      const result = originalCallback?.call(this, value, ...rest)
      updateAppendTargetWidgetsEnabled(node)
      return result
    }
  }

  const originalOnRemoved = node.onRemoved
  node.onRemoved = function () {
    if (node.__cpsbAppendTargetSyncTimer) {
      clearTimeout(node.__cpsbAppendTargetSyncTimer)
      node.__cpsbAppendTargetSyncTimer = null
    }
    return originalOnRemoved?.call(this)
  }

  scheduleAppendTargetSync(node)
}

// -----------------------------------------------------------------------
// "Written: <filename>" display (this file's header, section 3; product
// owner gap: "for 'don't open' how do I later find and open the file?").
//
// Persistence honesty, up front: the backend deliberately creates NO
// handoff and NO meta.json for this event (`cpsb/compose_psd.py`
// `_emit_compose_written`'s own docstring — that is the whole point of
// keeping `MODE_DONT_OPEN` free of Photoshop entanglement), so there is no
// server-side record to re-fetch after a reload the way badges.js/state.js
// re-sync handoff status from `GET /cpsb/status`. What this module actually
// does:
//   - The event payload is held ONLY in memory on the node instance
//     (`node.__cpsbLastWritten`) for as long as this browser tab's JS
//     context lives — reliable while the tab stays open, gone the instant
//     it doesn't (a real page unload, not just losing focus).
//   - As a best-effort survival path across an ACTUAL browser reload of the
//     SAME tab (`REASONABLY PRACTICAL`, not guaranteed): every write is also
//     mirrored into `window.localStorage`, keyed by this browser + the
//     current workflow name + this node's id (see {@link writtenStorageKey}
//     — the same per-node-id scoping concern `state.js`'s own
//     `workflowMatches` already documents for handoff lookups, since a bare
//     numeric node id is only unique WITHIN one workflow). `nodeCreated`
//     restores from this on every node (re)construction — which covers a
//     plain browser refresh of the same workflow, since ComfyUI's own
//     workflow autosave/restore recreates the same nodes with the same ids.
//     It does NOT survive: a different browser/profile, a cleared
//     localStorage, private/incognito storage partitioning, or opening the
//     saved `.json` workflow file fresh in a different session — all of
//     which are expected, not bugs, given the deliberate no-server-record
//     constraint above.
//   - `localStorage` access is feature-detected exactly like `open.js`'s own
//     `REMOTE_OPEN_ALLOWED_KEY` convention (try/catch around every access,
//     one-time `[cpsb]` warning via {@link warnLocalStorageUnavailable} if
//     it throws) — a broken/disabled localStorage degrades silently to
//     "in-memory only for this tab," never a crash.
//
// Two widgets, not one (product owner, verbatim: "Can the written message be
// just text and there be a separate 'Copy Path' button. And can that button
// copy the full path not just the file name"):
//   - A plain, non-clickable TEXT ROW ({@link WRITTEN_TEXT_WIDGET_NAME}) —
//     "Written: <filename> (on ComfyUI machine)". Non-interactive widget
//     type chosen: a `button`-type widget PERMANENTLY `.disabled = true` —
//     the exact same `widget.disabled` mechanism this file's "Append-target
//     widgets" section verifies against the current `Comfy-Org/
//     ComfyUI_frontend` source in detail (see that section's header for the
//     full citation trail: `BaseWidget`'s real `disabled` property,
//     `LGraphNode.drawWidgets`' automatic 50%-alpha dimming, `getWidgetOnPos`
//     excluding `computedDisabled` widgets from ever receiving a click).
//     Reused here rather than researching a second mechanism, because it is
//     litegraph's own answer to exactly this: "greyed out and inert, but
//     still occupying its row" — no genuine dedicated read-only/label
//     widget type exists in this frontend (mirrors `settings.js`'s own
//     documented finding for the ComfyUI-settings-panel case, a different
//     surface with the same absence).
//   - A separate "Copy Path" BUTTON ({@link WRITTEN_COPY_PATH_WIDGET_NAME})
//     that copies the FULL, absolute, server-side path
//     ({@link api.CpsbComposeWrittenEvent}'s `path` field, added alongside
//     this change) — never the bare filename the text row shows.
//     Pre-upgrade `localStorage` records (written by an older frontend
//     build, before `path` existed) have no path to copy: rather than
//     silently falling back to copying the bare filename under a "Copy
//     Path" label (which would lie about what got copied), this button is
//     `.disabled` whenever {@link CpsbWrittenFileRef.path} is absent — see
//     {@link setWrittenDisplay}'s own handling of that case.
// -----------------------------------------------------------------------

/**
 * @typedef {import('./api.js').CpsbImageRef & {path: string | null}} CpsbWrittenFileRef
 * The shape held in `node.__cpsbLastWritten`, mirrored into `localStorage`,
 * and returned by {@link getWrittenFileRef}: {@link api.CpsbImageRef}'s three
 * fields (all `menu.js`'s re-open request needs) plus `path` — the full,
 * absolute, server-side location from {@link api.CpsbComposeWrittenEvent}.
 * `path` is `null` only for a record a PRE-upgrade frontend build persisted
 * before this field existed (see this section's header, "Two widgets, not
 * one" — the "Copy Path" button disables itself rather than copying the
 * bare filename under a path-labeled button in that case).
 */

/** `localStorage` key prefix for {@link writtenStorageKey}. */
const WRITTEN_STORAGE_PREFIX = 'cpsb.composeWritten:'

/** Widget name for the plain, non-clickable "Written: ..." text row (this section). */
const WRITTEN_TEXT_WIDGET_NAME = 'cpsb_written'

/** Widget name for the separate "Copy Path" button (this section). */
const WRITTEN_COPY_PATH_WIDGET_NAME = 'cpsb_written_copy_path'

/** Guards {@link warnLocalStorageUnavailable} so it fires at most once per session. */
let writtenLocalStorageWarned = false

/**
 * @param {unknown} error
 */
function warnLocalStorageUnavailable(error) {
  if (writtenLocalStorageWarned) return
  writtenLocalStorageWarned = true
  api.warn(
    'localStorage is unavailable; a Compose node’s "Written: ..." display ' +
      'will not survive a browser reload this session',
    error
  )
}

/**
 * @param {import('../../../scripts/app.js').LGraphNode} node
 * @returns {string} Scoped by workflow name (best-effort — see
 * {@link state.getWorkflowName}) so the same numeric node id in a DIFFERENT
 * workflow never shows this node's stale written-filename (mirrors
 * `state.js`'s own `workflowMatches` scoping rationale for handoff lookups).
 */
function writtenStorageKey(node) {
  return `${WRITTEN_STORAGE_PREFIX}${state.getWorkflowName() || ''}::${node.id}`
}

/**
 * @param {import('../../../scripts/app.js').LGraphNode} node
 * @returns {CpsbWrittenFileRef | null} `path` is `null` (not just absent)
 * for a record a pre-upgrade frontend build persisted before that field
 * existed (this section's header) — never silently coerced to `""` or
 * dropped, so {@link setWrittenDisplay}'s "Copy Path" disable check can
 * distinguish "no path was ever recorded" from "recorded as empty."
 */
function loadPersistedWritten(node) {
  try {
    const raw = window.localStorage.getItem(writtenStorageKey(node))
    if (!raw) return null
    const parsed = JSON.parse(raw)
    if (parsed && typeof parsed.filename === 'string' && parsed.filename) {
      return {
        filename: parsed.filename,
        subfolder: typeof parsed.subfolder === 'string' ? parsed.subfolder : '',
        type: parsed.type || 'input',
        path: typeof parsed.path === 'string' && parsed.path ? parsed.path : null
      }
    }
    return null
  } catch (error) {
    warnLocalStorageUnavailable(error)
    return null
  }
}

/**
 * @param {import('../../../scripts/app.js').LGraphNode} node
 * @param {CpsbWrittenFileRef} ref
 */
function persistWritten(node, ref) {
  try {
    window.localStorage.setItem(writtenStorageKey(node), JSON.stringify(ref))
  } catch (error) {
    warnLocalStorageUnavailable(error)
  }
}

/**
 * @param {{filename: string}} ref
 * @returns {string} The button widget's visible label. Deliberately names
 * the file, not a path: PROTOCOL.md's two-machine setup (ComfyUI and
 * Photoshop/browser can be on different machines) makes a full server-side
 * absolute path confusing to a remote user, and a "Reveal in Finder"-style
 * affordance would be meaningless there — so this shows the same
 * input/-relative filename the node's own STRING output already returns,
 * with an explicit "(on ComfyUI machine)" marker so a remote user is never
 * left assuming it is a local, browser-side path. No reveal-in-OS affordance
 * is offered anywhere near this widget, deliberately.
 */
function writtenLabel({ filename }) {
  return `Written: ${filename} (on ComfyUI machine)`
}

/**
 * Copies *text* to the clipboard, preferring the modern async
 * `navigator.clipboard` API and falling back to the long-standing
 * `document.execCommand('copy')` trick (via a detached, invisible
 * `<textarea>`) for older/insecure-context frontends where
 * `navigator.clipboard` is unavailable — the same kind of graceful-degrade
 * every other browser-API touchpoint in this package already does (compare
 * `ui.js`'s toast/dialog fallbacks).
 * @param {string} text
 * @returns {Promise<boolean>} Whether the copy is believed to have succeeded.
 */
async function copyToClipboard(text) {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text)
      return true
    } catch (error) {
      api.warn('navigator.clipboard.writeText failed, trying the execCommand fallback', error)
    }
  }
  try {
    const textarea = document.createElement('textarea')
    textarea.value = text
    textarea.setAttribute('readonly', '')
    textarea.style.position = 'fixed'
    textarea.style.opacity = '0'
    document.body.appendChild(textarea)
    textarea.select()
    const ok = document.execCommand('copy')
    textarea.remove()
    return ok
  } catch (error) {
    api.warn('clipboard fallback copy failed', error)
    return false
  }
}

/**
 * @param {import('../../../scripts/app.js').LGraphNode} node
 * @returns {import('../../../scripts/app.js').IBaseWidget | undefined}
 */
function getWrittenTextWidget(node) {
  return node.widgets?.find((w) => w.name === WRITTEN_TEXT_WIDGET_NAME)
}

/**
 * @param {import('../../../scripts/app.js').LGraphNode} node
 * @returns {import('../../../scripts/app.js').IBaseWidget | undefined}
 */
function getCopyPathWidget(node) {
  return node.widgets?.find((w) => w.name === WRITTEN_COPY_PATH_WIDGET_NAME)
}

/**
 * Creates (once) or updates the "Written: <filename>" text row plus the
 * separate "Copy Path" button on *node*, and records *ref* on the node
 * instance for {@link getWrittenFileRef}/menu.js's re-open gate (this
 * section's header, "Two widgets, not one"). Both are `button`-type widgets
 * with `serialize: false` — the same convention `loadpsd.js`'s own upload
 * button already uses — so neither is ever written into `widgets_values`:
 * they carry no INPUT_TYPES-declared meaning the backend would need to read
 * back, and keeping them out of the saved graph JSON avoids any risk of
 * ever colliding, positionally, with a future backend-declared widget
 * (`cpsb/compose_psd.py`'s own "append new widgets at the very END of
 * required" rule exists for exactly this class of concern). Session
 * persistence instead goes through `localStorage` (see this section's
 * header) — a deliberately separate mechanism from graph serialization.
 * @param {import('../../../scripts/app.js').LGraphNode} node
 * @param {CpsbWrittenFileRef} ref
 */
function setWrittenDisplay(node, ref) {
  node.__cpsbLastWritten = ref

  let textWidget = getWrittenTextWidget(node)
  if (!textWidget) {
    textWidget = node.addWidget(
      'button',
      WRITTEN_TEXT_WIDGET_NAME,
      WRITTEN_TEXT_WIDGET_NAME,
      () => {}, // never invoked -- permanently `.disabled` below blocks every click
      { serialize: false, canvasOnly: true }
    )
    // Permanently non-interactive (this section's header, "Two widgets, not
    // one"): set once, at creation, and never toggled back -- unlike the
    // Copy Path button below, this row's disabled-ness isn't conditional on
    // anything.
    textWidget.disabled = true
  }
  textWidget.label = writtenLabel(ref)
  // Opportunistic — not every widget-rendering path shows a tooltip, but
  // when it does, this reiterates the same "(on ComfyUI machine)" clarity
  // the label already carries (build brief item 3: "tooltip OR label"). No
  // "click to copy" wording here anymore -- this row isn't clickable.
  textWidget.tooltip = `${ref.filename} — on the ComfyUI machine's input folder.`

  let copyPathWidget = getCopyPathWidget(node)
  if (!copyPathWidget) {
    copyPathWidget = node.addWidget(
      'button',
      WRITTEN_COPY_PATH_WIDGET_NAME,
      WRITTEN_COPY_PATH_WIDGET_NAME,
      async () => {
        const current = node.__cpsbLastWritten
        if (!current?.path) return // disabled below whenever this is true -- defensive only
        const ok = await copyToClipboard(current.path)
        ui.showToast({
          severity: ok ? 'success' : 'error',
          summary: ok ? 'Path copied' : 'Could not copy path',
          detail: current.path
        })
      },
      { serialize: false, canvasOnly: true }
    )
  }
  copyPathWidget.label = 'Copy Path'
  // Pre-upgrade-localStorage-record handling (this section's header): a
  // `ref.path` of `null` means no full path was ever recorded for this
  // write (an older frontend build persisted it before `path` existed) --
  // disable rather than fall back to copying the bare filename under a
  // "Copy Path" label, which would silently copy something other than what
  // the button claims to.
  const hasPath = typeof ref.path === 'string' && ref.path.length > 0
  copyPathWidget.disabled = !hasPath
  copyPathWidget.tooltip = hasPath
    ? `${ref.path} — click to copy the full path.`
    : 'No full path was recorded for this write (from a build before ' +
      '"Copy Path" existed) -- run this node again to record one.'

  node.graph?.setDirtyCanvas(true, false)
}

/**
 * @param {import('../../../scripts/app.js').LGraphNode} node
 * @returns {CpsbWrittenFileRef | null} This node's most recently written
 * file, from THIS session (in-memory) or restored from `localStorage` at
 * `nodeCreated` time — whichever is freshest, since {@link setWrittenDisplay}
 * always updates both. `null` before any write has ever been observed for
 * this node instance (a fresh node, or one that has only ever run in a way
 * that hasn't reached a write yet). Used by `menu.js`'s
 * {@link hasWrittenFile}-gated re-open item (build brief item 4) — that
 * caller only ever reads the pre-existing `filename`/`subfolder`/`type`
 * fields, so adding `path` here is additive and doesn't change what it
 * already relies on.
 */
export function getWrittenFileRef(node) {
  const ref = node.__cpsbLastWritten
  if (!ref || typeof ref.filename !== 'string' || !ref.filename) return null
  return {
    filename: ref.filename,
    subfolder: ref.subfolder || '',
    type: ref.type || 'input',
    path: typeof ref.path === 'string' && ref.path ? ref.path : null
  }
}

/**
 * @param {import('../../../scripts/app.js').LGraphNode} node
 * @returns {boolean} Whether {@link getWrittenFileRef} would return non-null.
 */
export function hasWrittenFile(node) {
  return getWrittenFileRef(node) !== null
}

/**
 * Deferred, `configuringGraph`-safe restore of a persisted "Written: ..."
 * display for *node* — the exact same defer-until-configured idiom
 * {@link scheduleStabilize}/{@link stabilizeImageInputs} already establish in
 * this file (see this file's header for why: a workflow restore's node id is
 * not reliably final until `app.configuringGraph` drops back to `false`, so
 * reading `localStorage` any earlier risks looking up the wrong key).
 * @param {import('../../../scripts/app.js').LGraphNode} node
 */
function scheduleRestoreWrittenDisplay(node) {
  if (node.__cpsbWrittenRestoreTimer) return
  node.__cpsbWrittenRestoreTimer = setTimeout(() => {
    node.__cpsbWrittenRestoreTimer = null
    if (!node.graph) return // removed from the graph since being scheduled
    if (app.configuringGraph) {
      scheduleRestoreWrittenDisplay(node)
      return
    }
    const persisted = loadPersistedWritten(node)
    if (persisted) setWrittenDisplay(node, persisted)
  }, STABILIZE_DEBOUNCE_MS)
}

/**
 * Installs the "Written: <filename>" display on one `PhotoshopComposePSD`
 * node instance. Call from `nodeCreated`. Idempotent per node instance (the
 * same convention every other `attach*`/`installBadgeHook` in this package
 * already uses) and a no-op for any other node type.
 * @param {import('../../../scripts/app.js').LGraphNode} node
 */
export function attachWrittenDisplay(node) {
  if (!isComposePsdNode(node)) return
  if (node.__cpsbWrittenDisplayAttached) return
  node.__cpsbWrittenDisplayAttached = true

  const originalOnRemoved = node.onRemoved
  node.onRemoved = function () {
    if (node.__cpsbWrittenRestoreTimer) {
      clearTimeout(node.__cpsbWrittenRestoreTimer)
      node.__cpsbWrittenRestoreTimer = null
    }
    return originalOnRemoved?.call(this)
  }

  scheduleRestoreWrittenDisplay(node)
}

/**
 * Handles one `cpsb.compose_written` event (see {@link api.CpsbComposeWrittenEvent}):
 * finds the originating node and updates its "Written: ..." display, if it
 * still exists and is still a Compose node (both are simple no-ops
 * otherwise — unlike an edit arriving from Photoshop, PLAN.md/PROTOCOL.md
 * define no "node deleted while this was in flight" toast for this purely
 * informational event; there is nothing the user was waiting on).
 * @param {import('./api.js').CpsbComposeWrittenEvent} payload
 */
function handleComposeWritten(payload) {
  const node = pasteback.getNodeByIdFlexible(payload.node_id)
  if (!node || !isComposePsdNode(node)) return
  const ref = {
    filename: payload.filename,
    subfolder: payload.subfolder || '',
    type: payload.type || 'input',
    path: typeof payload.path === 'string' && payload.path ? payload.path : null
  }
  persistWritten(node, ref)
  setWrittenDisplay(node, ref)
}

/**
 * Subscribes to `cpsb.compose_written`. Call once from `cpsb.js`'s `setup()`
 * (mirrors `pasteback.init()`'s single-global-listener shape — one
 * subscription for the whole session, looking up the target node fresh on
 * every event, rather than a per-node listener).
 */
export function init() {
  api.onComposeWritten(handleComposeWritten)
}

/**
 * Installs the auto-growing `image_N` input behavior on one
 * `PhotoshopComposePSD` node instance (PROTOCOL.md §6c). Call from
 * `nodeCreated`. Idempotent per node instance (the same convention
 * `badges.installBadgeHook`/`loadpsd.attachUploadWidget` already use) and a
 * no-op for any other node type. ALSO installs
 * {@link attachAppendTargetWidgets} — see that function's own doc comment
 * for why it's invoked from here rather than from `web/cpsb.js`'s
 * `nodeCreated` directly.
 * @param {import('../../../scripts/app.js').LGraphNode} node
 */
export function attachAutoGrowInputs(node) {
  if (!isComposePsdNode(node)) return
  attachAppendTargetWidgets(node)
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
