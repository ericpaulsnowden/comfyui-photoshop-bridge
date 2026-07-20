/**
 * @file "Re-open in Photoshop" button widget for the `PhotoshopAnnotate` node
 * (PROTOCOL.md §6d). The Annotate node has no `node.imgs` and is not on
 * `menu.js`'s node-menu allowlist (`getNodeMenuItems`'s `node.imgs?.length`
 * gate, extended only for Load PSD and a written-file Compose node — see
 * that file's own doc comment), so today the ONLY way to reopen an
 * annotation's Instructions PSD — with the Instructions layer and any
 * painted strokes intact — is the sidebar gallery's "Re-open" card action
 * (`gallery.js`'s `reopenInPhotoshop`, `mode:"original"`). This module puts
 * the same affordance directly on the node, since a user actively painting
 * an annotation shouldn't have to leave the canvas and hunt through the
 * "Photoshop Edits" sidebar tab to keep going.
 *
 * Button-widget idiom copied from `compose.js`'s "Browse..." button
 * (`findOrCreateBrowseButton`): a `button`-type widget, `serialize: false` +
 * `canvasOnly: true` so it never lands in a saved workflow's
 * `widgets_values` and can never collide positionally with a real
 * backend-declared widget, created idempotently (found-by-name first, an
 * `__cpsb*Attached` flag on the node guards the whole attach call) exactly
 * like every other `attach*` in this package. Unlike the Browse button, this
 * one has no single fixed anchor widget to render directly after (no
 * `insertWidgetAfter` call) — it simply appends at the end via
 * `node.addWidget`'s own default placement, the same way compose.js's own
 * "Written: ..."/"Copy Path" buttons do.
 *
 * Click handler reuses the SAME open path the gallery's Re-open button
 * already goes through end to end: {@link state.getActiveHandoffForNode}
 * finds this node's active handoff, then {@link open.openInteractive} POSTs
 * `/cpsb/open` with `mode:"original"` — the one mode that reopens an
 * EXISTING handoff's `psd_path` with no rewrite (`cpsb/routes.py` ~628-632),
 * so the Instructions layer and any painted strokes survive. Routing through
 * `open.openInteractive` (rather than calling `api.openHandoff` directly)
 * means this button gets the PROTOCOL.md §2/§7 428 remote-open confirm, the
 * 409 chooser, and server-message error toasts for free, identically to
 * every other open call site in this extension (`open.js`'s own file
 * header) — wherever Photoshop actually is, local or remote.
 *
 * `gallery.js`'s `openBodyFromMeta(meta, mode)` is the exact shape this
 * module needs but is a private, unexported helper there, and `gallery.js`
 * is out of scope for this change — so {@link reopenInPhotoshop} below
 * replicates its five-field body construction minimally rather than
 * importing it.
 */

import * as api from './api.js'
import * as open from './open.js'
import * as state from './state.js'
import * as ui from './ui.js'

/**
 * Class id for the Annotate node (PROTOCOL.md §6d). Must match
 * `NODE_CLASS_MAPPINGS`'s key in the repo's top-level `__init__.py`
 * (out of scope for this file) verbatim — confirmed there as
 * `"PhotoshopAnnotate": _cpsb_annotate.PhotoshopAnnotate`.
 */
export const ANNOTATE_NODE_TYPE = 'PhotoshopAnnotate'

/**
 * Name for the "Re-open in Photoshop" button widget this file creates.
 * NOT one of `cpsb/annotate.py`'s `INPUT_TYPES` entries — a purely
 * client-side affordance, `serialize: false` (see
 * {@link findOrCreateReopenButton}) so it never appears in a saved
 * workflow's `widgets_values`.
 */
const REOPEN_BUTTON_WIDGET_NAME = 'cpsb_annotate_reopen'

/**
 * @param {import('../../../scripts/app.js').LGraphNode} node
 * @returns {boolean}
 */
export function isAnnotateNode(node) {
  return (node.comfyClass || node.type) === ANNOTATE_NODE_TYPE
}

/**
 * @param {import('../../../scripts/app.js').LGraphNode} node
 * @param {string} name
 * @returns {import('../../../scripts/app.js').IBaseWidget | undefined}
 */
function findWidgetByName(node, name) {
  return node.widgets?.find((w) => w.name === name)
}

/**
 * Builds the `/cpsb/open` body for reopening *meta*'s handoff exactly as it
 * is — `mode:"original"` (this file's header). Mirrors `gallery.js`'s
 * private `openBodyFromMeta(meta, 'original')` field-for-field; kept as a
 * separate minimal copy here since that helper isn't exported and
 * `gallery.js` is out of scope for this change.
 * @param {import('./api.js').CpsbHandoffMeta} meta
 * @returns {import('./api.js').CpsbOpenRequest}
 */
function reopenBodyFromMeta(meta) {
  return {
    filename: meta.source.filename,
    subfolder: meta.source.subfolder,
    type: meta.source.type,
    origin_node_id: meta.origin_node_id,
    origin_kind: meta.origin_kind,
    workflow_name: meta.workflow_name,
    mode: 'original'
  }
}

/**
 * The button's click handler. Looks up this node's active handoff
 * (workflow-scoped, per {@link state.getActiveHandoffForNode}'s own doc
 * comment) and reopens it via the shared interactive open flow; if there is
 * none yet — a fresh node that has never run in PS mode, or one whose
 * annotation has already reached a terminal status — shows an informational
 * toast instead of a dead/error-producing request, mirroring how
 * `gallery.js`'s `addAsNode`/`revealInWorkflow` handle their own "nothing to
 * act on yet" cases.
 * @param {import('../../../scripts/app.js').LGraphNode} node
 */
async function reopenInPhotoshop(node) {
  const handoff = state.getActiveHandoffForNode(String(node.id))
  if (!handoff) {
    ui.showToast({
      severity: 'info',
      summary: 'No annotation to reopen yet',
      detail:
        'Run this Annotate node once to create an annotation, then Re-open ' +
        'to keep painting.'
    })
    return
  }
  try {
    await open.openInteractive(reopenBodyFromMeta(handoff), {
      successSummary: 'Re-opening in Photoshop…'
    })
  } catch (error) {
    // Belt only -- open.openInteractive already catches and toasts every
    // failure mode itself (428 decline is silent by design, 409/503/other
    // all show their own toast); this mirrors gallery.js's own defensive
    // wrapping around its card actions in case a future change to
    // openInteractive ever lets something escape.
    api.warn('annotate Re-open failed', error)
  }
}

/**
 * Creates (once) the "Re-open in Photoshop" button widget on *node* and
 * wires its click to {@link reopenInPhotoshop}. `serialize: false` +
 * `canvasOnly: true` mirrors every other purely-client-side button this
 * package adds (`compose.js`'s Browse/Written/Copy-Path buttons). Idempotent
 * by name: a pre-existing button (found by name) is returned as-is rather
 * than duplicated.
 * @param {import('../../../scripts/app.js').LGraphNode} node
 * @returns {import('../../../scripts/app.js').IBaseWidget}
 */
function findOrCreateReopenButton(node) {
  const existing = findWidgetByName(node, REOPEN_BUTTON_WIDGET_NAME)
  if (existing) return existing
  const button = node.addWidget(
    'button',
    REOPEN_BUTTON_WIDGET_NAME,
    REOPEN_BUTTON_WIDGET_NAME,
    () => reopenInPhotoshop(node),
    { serialize: false, canvasOnly: true }
  )
  button.label = 'Re-open in Photoshop'
  button.tooltip =
    'Reopen this annotation’s Instructions PSD in Photoshop, with your ' +
    'painted strokes intact — same as the "Photoshop Edits" sidebar ' +
    'gallery’s Re-open action.'
  return button
}

/**
 * Installs the "Re-open in Photoshop" button on one `PhotoshopAnnotate`
 * node instance. Call from `nodeCreated`. Idempotent per node instance (the
 * same `__cpsb*Attached`-flag convention every other `attach*` in this
 * package already uses — see `compose.js`/`loadpsd.js`/`badges.js`) and a
 * no-op for any other node type.
 * @param {import('../../../scripts/app.js').LGraphNode} node
 */
export function attachReopenButton(node) {
  if (!isAnnotateNode(node)) return
  if (node.__cpsbReopenAttached) return
  node.__cpsbReopenAttached = true

  findOrCreateReopenButton(node)
}
