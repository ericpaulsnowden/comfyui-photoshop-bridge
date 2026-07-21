/**
 * @file Four independent per-node display/interaction concerns for the
 * "Compose Layers to PSD" node (`PhotoshopComposePSD`, PROTOCOL.md §6c), all
 * owned here because this is where the rest of this package already does
 * compose-node-specific widget/socket manipulation:
 *
 * 1. Auto-growing `image_N` inputs: "connecting one reveals the next empty
 *    socket." The backend (`cpsb/compose_psd.py`, out of scope for this
 *    file) declares a static, generous range of OPTIONAL `image_1..image_20`
 *    sockets (`MAX_IMAGE_INPUTS` there) so the node accepts any number >= 1;
 *    this module is purely a display concern layered on top — hiding every
 *    disconnected socket except one, and revealing the next as the user
 *    connects — so a freshly-added node doesn't show 20 empty sockets at once.
 * 2. The "Browse..." button (a user report, verbatim: "For existing psd
 *    path we need a path picker... so a user can choose a path by
 *    navigating through it vs typing or pasting a path"): created directly
 *    after `existing_psd_path` (`cpsb/compose_psd.py`'s `INPUT_TYPES`) --
 *    its click handler opens `browse.js`'s server-backed directory-browser
 *    dialog (`GET /cpsb/fs/list`, `cpsb/routes.py` -- STANDARD-fs-browse.md,
 *    migrated 2026-07-19 off the old `GET /cpsb/browse`) rather than
 *    requiring a typed/pasted server-side path. v0.5.28 removed the
 *    `append_to_existing` BOOLEAN and `existing_psd` COMBO this button used
 *    to sit alongside
 *    (product owner, verbatim: "Remove the append_to_existing checkbox and
 *    always make that on. Also remove the existing_psd selector. Just have
 *    the browse capability.") — `existing_psd_path` alone now drives the
 *    backend's "append to existing document" feature (empty = fresh file,
 *    non-empty = append into that target), so the button is ALWAYS enabled;
 *    there is no longer a toggle to track or grey out against. See this
 *    file's "Existing-PSD-path Browse button" section below.
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
 * 4. Renamable `image_N` INPUT SLOTS (product owner, verbatim: "you should
 *    be able to change the names of the input nodes by double clicking on
 *    them. The name of the node should become the layer names. Remove the
 *    separate layer name textbox."): double-clicking an `image_N` slot opens
 *    a rename prompt exactly like `comfyui-epsnodes`' `EPSSwitcher`
 *    (`eps_image/switcher.js`, FORMAT.md §6.4 "Renamable rows") -- the same
 *    `onDblClick`/`onInputDblClick` dual-hook shape, the same
 *    `LGraphCanvas.prompt`-with-`window.prompt`-fallback dialog, and the same
 *    "set `input.label`, never `input.name`" display-only contract (clean-
 *    room reimplementation here, not a copy — see this file's "Renamable
 *    image_N inputs" section for the citations). Every slot's current label
 *    is kept serialized into the backend's hidden `layer_names` STRING
 *    widget (`cpsb/compose_psd.py`'s `INPUT_TYPES`) as a JSON object, which
 *    is what turns a renamed slot into that layer's actual name in the
 *    written PSD (`cpsb/compose_psd.py`'s `_resolve_layer_names`) -- the
 *    former separate `layer_name` STRING widget this replaces is gone
 *    entirely, per the product owner's own ask above.
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
import * as browse from './browse.js'
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
 * Name of the backend-declared hidden STRING widget (`cpsb/compose_psd.py`'s
 * `INPUT_TYPES`) this file publishes every `image_N` slot's custom label
 * into, as a JSON object (`_parse_layer_names`'s own contract:
 * `{"image_N": "label"}`, absent key == no custom name -- see the "Renamable
 * image_N inputs" section below). A REAL required widget (unlike the
 * `serialize: false` buttons elsewhere in this file), so it round-trips with
 * the saved workflow like any other backend-declared value; this file only
 * ever hides its on-canvas row (`.hidden = true`), never its serialization.
 */
const LAYER_NAMES_WIDGET_NAME = 'layer_names'

/**
 * Half-height of the Y band (around a row's `getConnectionPos`) that counts
 * as "this row" for double-click rename hit-testing (the "Renamable image_N
 * inputs" section below) -- mirrors comfyui-epsnodes' `eps_image/
 * switcher.js` `ROW_HIT_HALF_HEIGHT`: rows are ~20px apart (litegraph's
 * default slot height), and 9 leaves a small deadzone between adjacent rows
 * rather than an ambiguous overlap.
 */
const ROW_HIT_HALF_HEIGHT = 9

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
 * Like {@link getImageInputs}, but paired with each input's own index into
 * `node.inputs` -- needed by the rename hit-testing below
 * ({@link imageInputAtLocalY}), which must call `node.getConnectionPos(true,
 * idx)` with the REAL slot index, not its position among just the image_N
 * inputs.
 * @param {import('../../../scripts/app.js').LGraphNode} node
 * @returns {{idx: number, input: import('../../../scripts/app.js').IBaseWidget}[]}
 */
function imageInputEntries(node) {
  const entries = []
  const inputs = node.inputs ?? []
  for (let idx = 0; idx < inputs.length; idx++) {
    if (imageInputIndex(inputs[idx].name) !== null) entries.push({ idx, input: inputs[idx] })
  }
  return entries
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
 *
 * Also re-publishes {@link LAYER_NAMES_WIDGET_NAME} ({@link publishLayerNames})
 * once the above settles -- this is what satisfies "on configure/load" for
 * the rename feature (the "Renamable image_N inputs" section below): this
 * function's own `app.configuringGraph` guard already defers its real work
 * until a workflow restore's sockets/labels are fully in place, and it also
 * runs once on every ordinary connect/disconnect, so publishing here (rather
 * than adding a SEPARATE `configure()` hook) reuses that already-proven
 * timing instead of re-deriving it.
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

  publishLayerNames(node)
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
// Renamable image_N inputs (this file's header, section 4; product owner
// verbatim: "you should be able to change the names of the input nodes by
// double clicking on them. The name of the node should become the layer
// names. Remove the separate layer name textbox.").
//
// This is a clean-room reimplementation of the PROVEN pattern already
// shipped in `comfyui-epsnodes`' `eps_image/switcher.js` (FORMAT.md §6.4
// "Renamable rows") -- not a copy (different repo, this file's own style),
// but the same load-bearing techniques, cited here rather than re-derived:
//   - `LGraphCanvas.prompt(title, value, callback, event)` when this fork
//     has it (confirmed present there against a local ComfyUI_frontend
//     checkout), else a plain `window.prompt` fallback -- see
//     {@link promptForImageInputLabel}.
//   - Setting `input.label` ONLY -- `input.name` (the backend kwargs
//     contract `cpsb/compose_psd.py`'s `execute(self, ..., **kwargs)` keys
//     on) and every existing link are left completely untouched, so a saved
//     workflow's `image_7` link is always restored onto a real `image_7`
//     socket exactly as before this feature existed (this file's header,
//     "FORK NOTICE" paragraph, already establishes that invariant for the
//     auto-grow machinery; renaming does not change it). An empty/whitespace
//     label DELETES the `.label` property (not `= ""`), so litegraph's own
//     `label || name` draw-time fallback shows the plain socket name again
//     immediately -- see {@link setInputLabel}.
//   - Two double-click hooks, not one, because litegraph dispatches a
//     double-click differently depending on where it lands:
//     `onInputDblClick(index, e)` fires for a click within litegraph's OWN
//     input hit-region (the socket dot and its name/label text --
//     `LGraphCanvas.ts`'s inputs loop registers this and `return`s before
//     reaching anything else); `onDblClick(e, pos, canvas)` fires for a
//     click anywhere ELSE in the node body (this node draws no per-row
//     toggle box the way `EPSSwitcher` does, so that is simply blank row
//     space here). `pos[1] < 0` excludes the title bar (litegraph's own
//     signal for "this was the title"). See {@link wireImageInputRename}.
//   - Persistence: `switcher.js`'s own file header documents a live,
//     verified round trip (rename a row, reload the page, re-read
//     `node.inputs[i].label`) coming back unchanged, backed by
//     `node/slotUtils.ts`'s `inputAsSerialisable` explicitly destructuring
//     `label` into the serialized POJO -- the same litegraph fork this
//     package targets (this file's own header cites the identical
//     `Comfy-Org/ComfyUI_frontend` source convention). That is why this file
//     trusts plain `input.label` too, with no node-property fallback map:
//     the round-trip guarantee comes from the SAME underlying mechanism,
//     already proven elsewhere in this monorepo, not re-derived here.
//
// **The `layer_names` bridge to the backend** (mirrors `switcher.js`'s own
// `toggles` widget -- see that file's module docstring "toggles is the
// enabled-set bridge to the backend"): `cpsb/compose_psd.py`'s `INPUT_TYPES`
// declares a REQUIRED `layer_names` STRING widget (default `""`) at the
// exact position the removed `layer_name` STRING widget used to occupy.
// {@link hideLayerNamesWidget} hides its on-canvas row once, at attach time
// (`.hidden = true` only -- it stays a real, serialized widget, so it still
// round-trips with the saved workflow); {@link publishLayerNames} keeps its
// value in lockstep with every `image_N` slot's current `.label` as a JSON
// object (`{"image_1": "Sky", ...}`, absent key == no custom name --
// `cpsb/compose_psd.py`'s `_parse_layer_names`), called from
// {@link setInputLabel} on every rename commit AND from
// {@link stabilizeImageInputs}'s own already-`configuringGraph`-safe tail
// (covering the "on configure/load" case without a second, separately-timed
// hook).
// -----------------------------------------------------------------------

/**
 * @param {import('../../../scripts/app.js').LGraphNode} node
 * @returns {import('../../../scripts/app.js').IBaseWidget | undefined}
 */
function getLayerNamesWidget(node) {
  return findWidgetByName(node, LAYER_NAMES_WIDGET_NAME)
}

/**
 * Hides the backend-declared `layer_names` widget's on-canvas row (this
 * section's header) -- called once, at attach time. Mirrors
 * `eps_image/switcher.js`'s own `hideTogglesWidget`: `.hidden = true` is
 * enough to stop it drawing (`badges.js`'s own widget-layout pass in this
 * same package already filters on `!w.hidden`) without stopping it from
 * serializing, since `serialize` itself is untouched. A missing widget
 * (defensive only -- `cpsb/compose_psd.py`'s `INPUT_TYPES` always declares
 * it) just warns; renaming would then have nothing to write into, but
 * nothing else on the node breaks.
 * @param {import('../../../scripts/app.js').LGraphNode} node
 */
function hideLayerNamesWidget(node) {
  const widget = getLayerNamesWidget(node)
  if (!widget) {
    api.warn(
      'PhotoshopComposePSD node is missing its `layer_names` widget; ' +
        'renaming image_N inputs will not reach the backend'
    )
    return
  }
  widget.hidden = true
}

/**
 * @param {import('../../../scripts/app.js').LGraphNode} node
 * @returns {Record<string, string>} `{"image_N": "label"}` for every
 * `image_N` input that currently carries a non-blank `.label` -- an input
 * with no label (never renamed, or reset back to blank) is simply absent,
 * matching `cpsb/compose_psd.py`'s `_parse_layer_names` "absence means no
 * custom name" contract.
 */
function collectImageInputLabels(node) {
  const labels = {}
  for (const input of getImageInputs(node)) {
    const label = typeof input.label === 'string' ? input.label.trim() : ''
    if (label) labels[input.name] = label
  }
  return labels
}

/**
 * Writes {@link collectImageInputLabels}'s current snapshot into the hidden
 * `layer_names` widget (this section's header). A no-op if that widget is
 * missing (already warned once by {@link hideLayerNamesWidget}). Safe and
 * cheap to call often -- every call recomputes the full snapshot from the
 * inputs themselves (the authoritative source), so it can never drift from
 * what is actually drawn on the node.
 * @param {import('../../../scripts/app.js').LGraphNode} node
 */
function publishLayerNames(node) {
  const widget = getLayerNamesWidget(node)
  if (!widget) return
  widget.value = JSON.stringify(collectImageInputLabels(node))
}

/**
 * The text litegraph actually draws for *input* -- `label || name`, matching
 * `measureSlots.ts`'s own precedence (`eps_image/switcher.js`'s identical
 * `displayText` helper, cited here for the same reasoning).
 * @param {object} input
 * @returns {string}
 */
function displayText(input) {
  return (input && (input.label || input.name)) || ''
}

/**
 * Sets or clears *input*'s display label -- `input.name` and every existing
 * link are NEVER touched (this section's header). An empty/whitespace
 * *label* deletes the `.label` property entirely (rather than setting
 * `""`), so {@link displayText}/litegraph's own `label || name` fallback
 * shows the plain socket name again immediately. Re-publishes
 * `layer_names` right away so the very next queue already sees the rename
 * (`cpsb/compose_psd.py`'s `IS_CHANGED` hashes that widget's value, so this
 * also correctly invalidates any cached/consumable result for this node).
 * @param {import('../../../scripts/app.js').LGraphNode} node
 * @param {object} input
 * @param {string} label
 */
function setInputLabel(node, input, label) {
  const trimmed = (label ?? '').trim()
  if (trimmed) input.label = trimmed
  else delete input.label
  publishLayerNames(node)
  node.graph?.setDirtyCanvas(true, true)
}

/**
 * Best-effort active `LGraphCanvas` for callbacks that don't receive one
 * directly ({@link wireImageInputRename}'s `onInputDblClick` branch) -- the
 * same `app.canvas` access pattern this package already uses elsewhere
 * (e.g. `pasteback.js`).
 * @returns {object|null}
 */
function activeCanvas() {
  return app?.canvas ?? null
}

/**
 * Opens the rename editor for *input* (this section's header): `LGraphCanvas
 * .prompt` when this fork has it, else a plain `window.prompt` fallback.
 * `window.prompt` returns `null` on Cancel but `""` on an intentional
 * OK-with-empty-field -- `commit` only skips the `null` case, so clearing
 * the field still resets the label via {@link setInputLabel}.
 * @param {import('../../../scripts/app.js').LGraphNode} node
 * @param {object} input
 * @param {object|null} canvas
 * @param {Event} event
 */
function promptForImageInputLabel(node, input, canvas, event) {
  const commit = (value) => {
    if (value == null) return
    setInputLabel(node, input, value)
  }
  if (canvas && typeof canvas.prompt === 'function') {
    canvas.prompt('Layer name', displayText(input), commit, event)
  } else {
    commit(window.prompt('Layer name', displayText(input)))
  }
}

/**
 * @param {import('../../../scripts/app.js').LGraphNode} node
 * @param {number} localY node-local Y (graph Y minus `node.pos[1]`)
 * @returns {object|null} the `image_N` input at *localY* -- connected or the
 * trailing spare -- or `null` if no row is close enough. Mirrors
 * `eps_image/switcher.js`'s own `rowAtLocalY`.
 */
function imageInputAtLocalY(node, localY) {
  if (typeof node.getConnectionPos !== 'function') return null
  for (const { idx, input } of imageInputEntries(node)) {
    let pos
    try {
      pos = node.getConnectionPos(true, idx)
    } catch (error) {
      continue
    }
    if (Math.abs(localY - (pos[1] - node.pos[1])) <= ROW_HIT_HALF_HEIGHT) return input
  }
  return null
}

/**
 * Wraps `onInputDblClick` and `onDblClick` so double-clicking an `image_N`
 * input -- connected or the trailing spare -- opens the rename prompt (this
 * section's header explains why both hooks are needed). Idempotent-safe to
 * call once per node (guarded by its caller, {@link attachLayerRenaming}).
 * @param {import('../../../scripts/app.js').LGraphNode} node
 */
function wireImageInputRename(node) {
  const originalOnInputDblClick = node.onInputDblClick
  node.onInputDblClick = function (index, e) {
    let result
    if (typeof originalOnInputDblClick === 'function') {
      result = originalOnInputDblClick.apply(this, arguments)
    }
    try {
      const input = this.inputs?.[index]
      if (input && imageInputIndex(input.name) !== null) {
        promptForImageInputLabel(this, input, activeCanvas(), e)
      }
    } catch (error) {
      api.warn('onInputDblClick rename failed', error)
    }
    return result
  }

  const originalOnDblClick = node.onDblClick
  node.onDblClick = function (e, pos, canvas) {
    let result
    if (typeof originalOnDblClick === 'function') {
      result = originalOnDblClick.apply(this, arguments)
    }
    try {
      if (Array.isArray(pos) && pos[1] >= 0) {
        const input = imageInputAtLocalY(this, pos[1])
        if (input) promptForImageInputLabel(this, input, canvas || activeCanvas(), e)
      }
    } catch (error) {
      api.warn('onDblClick rename failed', error)
    }
    return result
  }
}

/**
 * Installs the renamable-`image_N`-inputs feature on one
 * `PhotoshopComposePSD` node instance (this section's header). Call from
 * `nodeCreated` -- invoked directly from {@link attachAutoGrowInputs} below,
 * the same reason {@link attachAppendTargetWidgets} is (this change's scope
 * is `compose.js` only; `web/cpsb.js`'s own `nodeCreated` wiring is out of
 * bounds for it). Idempotent per node instance and a no-op for any other
 * node type.
 * @param {import('../../../scripts/app.js').LGraphNode} node
 */
export function attachLayerRenaming(node) {
  if (!isComposePsdNode(node)) return
  if (node.__cpsbLayerRenamingAttached) return
  node.__cpsbLayerRenamingAttached = true

  hideLayerNamesWidget(node)
  wireImageInputRename(node)
}

// -----------------------------------------------------------------------
// Existing-PSD-path Browse button (this file's header, section 2; a user
// report, verbatim: "For existing psd path we need a path picker... so a
// user can choose a path by navigating through it vs typing or pasting a
// path"): creates a "Browse..." button widget directly after
// `existing_psd_path` (`cpsb/compose_psd.py`'s `INPUT_TYPES`) whose click
// handler opens `browse.js`'s server-backed directory-browser dialog
// (`GET /cpsb/fs/list`, `cpsb/routes.py` -- STANDARD-fs-browse.md) and
// writes the chosen path back
// onto `existing_psd_path`.
//
// v0.5.28 simplification (product owner, verbatim: "Remove the
// append_to_existing checkbox and always make that on. Also remove the
// existing_psd selector. Just have the browse capability. Users can use
// that to either select any existing file, or make a new one."): this used
// to be gated on an `append_to_existing` BOOLEAN widget -- greyed out and
// click-blocked while it was `false`, re-enabled the instant it was toggled
// `true` (`widget.disabled`, per the now-removed "Append-target widgets"
// design this section replaces). That toggle -- and the `existing_psd`
// COMBO beside it -- are GONE from the backend's `INPUT_TYPES` entirely;
// `existing_psd_path` alone now drives everything (empty = fresh file,
// non-empty = append into that target). With no toggle left to gate on,
// this button is simply ALWAYS enabled: there is nothing to grey out, and
// no `.disabled`/enable-sync bookkeeping is needed any more.
// -----------------------------------------------------------------------

const EXISTING_PSD_PATH_WIDGET_NAME = 'existing_psd_path'

/**
 * Name for the "Browse..." button widget this file creates directly after
 * `existing_psd_path` (this file's header, section 2). NOT one of
 * `cpsb/compose_psd.py`'s `INPUT_TYPES` entries -- a purely client-side
 * affordance, `serialize: false` (see {@link findOrCreateBrowseButton}) so it
 * never appears in a saved workflow's `widgets_values` and can never collide,
 * positionally, with a real backend-declared widget.
 */
const BROWSE_BUTTON_WIDGET_NAME = 'cpsb_existing_psd_browse'

/**
 * @param {import('../../../scripts/app.js').LGraphNode} node
 * @param {string} name
 * @returns {import('../../../scripts/app.js').IBaseWidget | undefined}
 */
function findWidgetByName(node, name) {
  return node.widgets?.find((w) => w.name === name)
}

/**
 * Moves *widget* (already appended to the END of `node.widgets` by
 * `node.addWidget`) so it renders directly after *afterWidget* instead.
 * litegraph widgets carry no explicit "position" field to set — `node.widgets`
 * is a plain array, and the layout/draw pass positions each widget by
 * walking that array top-to-bottom in order (the same fact this file's
 * header already cites for socket order), so reordering the array is the
 * only way to reorder the rendered rows. *afterWidget*'s index is looked up
 * AGAIN after splicing *widget* out (rather than reused from before), so this
 * is correct regardless of which of the two originally came first in the
 * array. A no-op if either widget is missing from `node.widgets` (defensive
 * only — both are always present when this is actually called).
 * @param {import('../../../scripts/app.js').LGraphNode} node
 * @param {import('../../../scripts/app.js').IBaseWidget} widget
 * @param {import('../../../scripts/app.js').IBaseWidget} afterWidget
 */
function insertWidgetAfter(node, widget, afterWidget) {
  const widgets = node.widgets
  if (!widgets) return
  const widgetIndex = widgets.indexOf(widget)
  if (widgetIndex === -1 || !widgets.includes(afterWidget)) return
  widgets.splice(widgetIndex, 1)
  widgets.splice(widgets.indexOf(afterWidget) + 1, 0, widget)
}

/**
 * Creates (once) the "Browse..." button widget directly after
 * *existingPsdPathWidget* (this file's header, section 2) and wires its click
 * to {@link browse.openBrowseDialog}. `serialize: false` + `canvasOnly: true`
 * mirrors every other purely-client-side button this file already adds
 * (`cpsb_written`/`cpsb_written_copy_path` below) — never written into
 * `widgets_values`, so it can never collide positionally with a real
 * backend-declared widget. Idempotent: a pre-existing button (found by name)
 * is returned as-is rather than duplicated. ALWAYS enabled (this section's
 * header) -- no `.disabled` is ever set on it.
 * @param {import('../../../scripts/app.js').LGraphNode} node
 * @param {import('../../../scripts/app.js').IBaseWidget} existingPsdPathWidget
 * @returns {import('../../../scripts/app.js').IBaseWidget}
 */
function findOrCreateBrowseButton(node, existingPsdPathWidget) {
  const existing = findWidgetByName(node, BROWSE_BUTTON_WIDGET_NAME)
  if (existing) return existing
  const browseButton = node.addWidget(
    'button',
    BROWSE_BUTTON_WIDGET_NAME,
    BROWSE_BUTTON_WIDGET_NAME,
    () => browse.openBrowseDialog(node, existingPsdPathWidget),
    { serialize: false, canvasOnly: true }
  )
  browseButton.label = 'Browse...'
  browseButton.tooltip =
    'Navigate the ComfyUI machine’s filesystem to pick (or name a new) .psd/.psb target. ' +
    'Leave existing_psd_path empty to write a fresh, auto-numbered file instead.'
  insertWidgetAfter(node, browseButton, existingPsdPathWidget)
  return browseButton
}

/**
 * Installs the "Browse..." button on one `PhotoshopComposePSD` node
 * instance (this section's header). NAME KEPT as `attachAppendTargetWidgets`
 * (not renamed to something Browse-button-specific) even though it now only
 * does ONE thing: `web/cpsb.js`'s `nodeCreated` hook (out of scope for this
 * change) calls `compose.attachAppendTargetWidgets(node)` by this exact
 * name, so renaming the export here would silently break that call site.
 * Exported — matching every other `attach*` in this file being its own
 * clean, independently-callable entry point — but ALSO invoked directly
 * from {@link attachAutoGrowInputs} below, since THIS change's scope is
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

  const existingPsdPathWidget = findWidgetByName(node, EXISTING_PSD_PATH_WIDGET_NAME)
  if (existingPsdPathWidget) {
    findOrCreateBrowseButton(node, existingPsdPathWidget)
  }
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
//     type chosen: a `button`-type widget PERMANENTLY `.disabled = true`.
//     Verified against the current `Comfy-Org/ComfyUI_frontend` source
//     (this project's established methodology, cloned into a scratch
//     checkout rather than coded from memory): `BaseWidget`
//     (`src/lib/litegraph/src/widgets/BaseWidget.ts` ~line 101-106) has a
//     real, current, first-class `disabled` getter/setter; `LGraphNode.
//     drawWidgets` (`LGraphNode.ts` ~line 3956-3961) recomputes
//     `computedDisabled` from `.disabled` on every draw pass and halves
//     `ctx.globalAlpha` for it (visually greyed out, no manual per-frame
//     bookkeeping needed); `LGraphCanvas`'s own pointer-down handler
//     (`getWidgetOnPos`'s default `includeDisabled = false`, `LGraphNode.ts`
//     ~line 2259-2277) skips any `computedDisabled` widget entirely, so a
//     disabled widget's click never reaches it -- real click-blocking, not
//     just a visual dimming. This is litegraph's own answer to "greyed out
//     and inert, but still occupying its row" — no genuine dedicated
//     read-only/label widget type exists in this frontend (mirrors
//     `settings.js`'s own documented finding for the ComfyUI-settings-panel
//     case, a different surface with the same absence).
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
 * {@link attachAppendTargetWidgets} (the "Browse..." button) and
 * {@link attachLayerRenaming} (double-click-to-rename an `image_N` slot) —
 * see those functions' own doc comments for why they're invoked from here
 * rather than from `web/cpsb.js`'s `nodeCreated` directly.
 * @param {import('../../../scripts/app.js').LGraphNode} node
 */
export function attachAutoGrowInputs(node) {
  if (!isComposePsdNode(node)) return
  attachAppendTargetWidgets(node)
  attachLayerRenaming(node)
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
  // §6c) -- and, via stabilizeImageInputs's own tail, the first
  // publishLayerNames() call for this node. Always deferred, never run
  // synchronously here: this correctly covers both the interactive "drag a
  // fresh node onto the canvas" case (configuringGraph is already false, so
  // the deferred call runs on the very next tick) and a workflow-load case
  // (deferred until configuration settles) with the exact same code path --
  // see stabilizeImageInputs's own docstring and this file's header.
  scheduleStabilize(node)
}
