/**
 * @file Handles the `cpsb.updated` event (PLAN.md §3, PROTOCOL.md §5): an
 * edit arrived from Photoshop and needs to land back in the graph.
 *
 * Widget value format verified against `Comfy-Org/ComfyUI_frontend`
 * `src/scripts/app.ts` (`ComfyApp.pasteFromClipspace`) and every other
 * builder of this string across the codebase (e.g.
 * `src/composables/painter/usePainter.ts`,
 * `src/extensions/core/load3d.ts`):
 * `(subfolder ? subfolder + "/" : "") + filename + (type ? " [" + type + "]" : "")` —
 * exactly the clipspace precedent PLAN.md §3 calls out.
 *
 * Node-preview refresh uses `app.nodeOutputs[node.id]` + `node.imgs` +
 * `graph.setDirtyCanvas`, the same extension-reachable surface
 * `pasteFromClipspace` itself updates for canvas-mode rendering — the
 * *internal* Vue node-output Pinia store it also updates
 * (`useNodeOutputStore().updateNodeImages`) is not importable from
 * third-party code, so this mirrors clipspace at the level actually exposed
 * to extensions, matching PLAN.md §3's own description ("mirrors clipspace's
 * onClipspaceEditorSave").
 *
 * "Add as node" uses `LiteGraph.createNode` + `graph.add`, reached via
 * `window.LiteGraph` — confirmed as the intentional backward-compatibility
 * surface for third-party extensions in
 * `src/composables/useGlobalLitegraph.ts` ("Assign all properties of
 * LiteGraph to window to make it backward compatible"), invoked from
 * `GraphCanvas.vue` well before any extension's `setup()` runs. `app.ts`'s
 * own public export surface does not re-export `LiteGraph`, so the global is
 * the correct, documented-by-intent way to reach it from here.
 */

import { app } from '../../../scripts/app.js'
import * as api from './api.js'
import * as settings from './settings.js'
import * as state from './state.js'
import * as ui from './ui.js'

/**
 * @param {import('./api.js').CpsbImageRef} ref
 * @returns {string} e.g. `"photoshop/a1b2c3d4/edit_002.png [input]"` (the
 * managed-folder segment is whatever `ref.subfolder` actually says, per
 * `docs/PROTOCOL.md` §1/§2 — server-configurable, default `"photoshop"`).
 */
function buildWidgetValue({ filename, subfolder, type }) {
  return `${subfolder ? subfolder + '/' : ''}${filename}${type ? ` [${type}]` : ''}`
}

/**
 * Looks a node up by id, tolerating the string/number mismatch between
 * PROTOCOL.md ids (`origin_node_id` is a string) and litegraph node ids
 * (numbers for ordinary graphs). On current `Comfy-Org/ComfyUI_frontend`
 * main this is technically unnecessary: `LGraph.ts` declares
 * `_nodes_by_id: Record<NodeId, LGraphNode> = {}` — a **plain object, not a
 * Map** — and `getNodeById` indexes it directly, so a string id coerces to
 * the same property key as its numeric twin. But that is an implementation
 * detail, so when the raw lookup misses, a numeric retry covers any future
 * frontend that switches to strict-keyed storage.
 * @param {string | number} id
 * @returns {import('../../../scripts/app.js').LGraphNode | null}
 */
export function getNodeByIdFlexible(id) {
  const graph = app.graph
  if (!graph || typeof graph.getNodeById !== 'function') return null
  const direct = graph.getNodeById(id)
  if (direct) return direct
  if (typeof id === 'string' && id.trim() !== '') {
    const numeric = Number(id)
    if (Number.isFinite(numeric)) return graph.getNodeById(numeric) ?? null
  }
  return null
}

/**
 * @param {import('./api.js').CpsbImageRef | null | undefined} a
 * @param {import('./api.js').CpsbImageRef | null | undefined} b
 * @returns {boolean}
 */
function sameImageRef(a, b) {
  return (
    !!a &&
    !!b &&
    a.filename === b.filename &&
    (a.subfolder || '') === (b.subfolder || '') &&
    (a.type || '') === (b.type || '')
  )
}

/**
 * Locates which of the node's currently displayed images belongs to this
 * handoff: the slot either still shows the handoff's original source image,
 * or already shows a *previous* edit of the same handoff from an earlier
 * round trip — without the second clause, a multi-save session would stop
 * matching after the first edit replaced the source in that slot.
 *
 * Every edit of one handoff lives in the same on-disk folder for the
 * handoff's whole lifetime (PROTOCOL.md §1), so the just-arrived edit's own
 * `subfolder` — read verbatim from the triggering `cpsb.updated` payload,
 * never reconstructed from a hardcoded literal — is also every earlier
 * edit's subfolder. The managed folder name is server-configurable (default
 * `"photoshop"`, PROTOCOL.md §1/§2), so this must never assume a literal
 * like `"cpsb"`.
 * @param {import('../../../scripts/app.js').LGraphNode} node
 * @param {string} editSubfolder - The handoff's actual edit subfolder, taken
 * verbatim from the triggering event (see {@link refreshNodePreview}).
 * @param {import('./api.js').CpsbImageRef | null} sourceRef
 * @returns {number} Index into `node.imgs`, or -1 if no slot matches.
 */
function findEditedSlot(node, editSubfolder, sourceRef) {
  if (!Array.isArray(node.imgs)) return -1
  for (let i = 0; i < node.imgs.length; i++) {
    const parsed = api.parseImageRef(node.imgs[i]?.src)
    if (!parsed) continue
    if (sameImageRef(parsed, sourceRef) || (editSubfolder && parsed.subfolder === editSubfolder))
      return i
  }
  return -1
}

/**
 * @param {import('../../../scripts/app.js').LGraphNode} node
 * @returns {import('../../../scripts/app.js').IBaseWidget | undefined}
 */
function findImageWidget(node) {
  return node.widgets?.find((w) => w.name === 'image' && w.type !== 'button')
}

/**
 * Refreshes what the node actually displays: a fresh `<img>` sourced from
 * ComfyUI's own `/view`, `node.imgs`/`imageIndex`, `app.nodeOutputs`, and a
 * canvas repaint. Safe to call even when the widget's own callback already
 * did some of this — cheap and idempotent.
 *
 * Batch-aware: when the handoff context identifies which of the node's N
 * displayed images the edit belongs to ({@link findEditedSlot}), only that
 * slot is replaced — in `node.imgs` (with `imageIndex` moved to the replaced
 * slot) and in the matching `app.nodeOutputs` entry (preserving array length
 * and order) — so an edit returning to a batch SaveImage/Preview node never
 * clobbers the other N-1 previews. Only when no slot can be identified
 * (e.g. the node re-rendered with different images since the handoff was
 * opened) does it fall back to replacing the whole preview with the edit.
 * @param {import('../../../scripts/app.js').LGraphNode} node
 * @param {import('./api.js').CpsbImageRef} ref
 * @param {{handoffId?: string, sourceRef?: import('./api.js').CpsbImageRef | null}} [context]
 */
function refreshNodePreview(node, ref, { handoffId, sourceRef } = {}) {
  const img = new Image()
  img.src = api.viewUrl(ref)
  const entry = { filename: ref.filename, subfolder: ref.subfolder, type: ref.type }
  // ref.subfolder is this handoff's actual edit subfolder, taken verbatim
  // from the triggering event/response payload (see findEditedSlot) — never
  // a hardcoded literal.
  const slot = handoffId ? findEditedSlot(node, ref.subfolder, sourceRef ?? null) : -1

  if (slot >= 0) {
    node.imgs[slot] = img
    node.imageIndex = slot
    const outputs = app.nodeOutputs?.[String(node.id)]
    if (outputs && Array.isArray(outputs.images)) {
      // Prefer matching the entry by its own {filename, subfolder, type}
      // fields (same predicate as findEditedSlot); fall back to the parallel
      // index when the arrays are congruent.
      const outIdx = outputs.images.findIndex(
        (im) =>
          im && (sameImageRef(im, sourceRef) || (im.subfolder || '') === ref.subfolder)
      )
      if (outIdx >= 0) {
        outputs.images[outIdx] = entry
      } else if (outputs.images.length === node.imgs.length) {
        outputs.images[slot] = entry
      }
    }
  } else {
    node.imgs = [img]
    node.imageIndex = 0
    if (app.nodeOutputs) {
      app.nodeOutputs[String(node.id)] = { images: [entry] }
    }
  }
  node.graph?.setDirtyCanvas(true, false)
}

/**
 * Sets the node's image widget to the new edit, fires its callback, and
 * refreshes the preview. Returns whether a widget was found at all — a
 * Photoshop Bridge node may not expose one (PROTOCOL.md §5: "same on its
 * widget if present"), in which case the status badge is the only signal
 * (badges.js, driven by `cpsb.status`).
 * @param {import('../../../scripts/app.js').LGraphNode} node
 * @param {import('./api.js').CpsbImageRef} ref
 * @param {{handoffId?: string, sourceRef?: import('./api.js').CpsbImageRef | null}} [context]
 * @returns {boolean}
 */
function pasteToWidget(node, ref, context) {
  const widget = findImageWidget(node)
  if (!widget) return false
  widget.value = buildWidgetValue(ref)
  try {
    widget.callback?.(widget.value)
  } catch (error) {
    api.warn('image widget callback threw while pasting back a Photoshop edit', error)
  }
  refreshNodePreview(node, ref, context)
  return true
}

/**
 * @returns {[number, number]} Last known graph-space cursor position, or the
 * origin if unavailable — used only when the origin node no longer exists.
 */
function getFallbackPosition() {
  const mouse = app.canvas?.graph_mouse
  if (Array.isArray(mouse) && mouse.length === 2) return [mouse[0], mouse[1]]
  return [0, 0]
}

/**
 * Creates an unconnected `LoadImage` node near `originNode` (or at a
 * fallback position if the origin no longer exists), pre-populated with the
 * edit. Opt-in only — called from the "[Add as node]" toast action here and
 * reused by `gallery.js`'s "Add as node" card action.
 * @param {import('../../../scripts/app.js').LGraphNode | null} originNode
 * @param {import('./api.js').CpsbImageRef} ref
 */
export function addLoadImageNodeNear(originNode, ref) {
  const graph = app.graph
  const LiteGraph = window.LiteGraph
  if (!graph) {
    ui.showToast({ severity: 'error', summary: 'Could not add node', detail: 'No active graph.' })
    return
  }
  if (!LiteGraph || typeof LiteGraph.createNode !== 'function') {
    api.warn('window.LiteGraph.createNode is unavailable on this frontend — cannot add a node')
    ui.showToast({
      severity: 'error',
      summary: 'Could not add node',
      detail: 'This frontend version does not expose LiteGraph.createNode.'
    })
    return
  }
  const newNode = LiteGraph.createNode('LoadImage')
  if (!newNode) {
    ui.showToast({
      severity: 'error',
      summary: 'Could not add node',
      detail: '"LoadImage" node type is not registered.'
    })
    return
  }
  const [baseX, baseY] = originNode?.pos ?? getFallbackPosition()
  const offsetX = originNode?.size?.[0] ?? 200
  newNode.pos = [baseX + offsetX + 40, baseY]
  graph.add(newNode)

  const widget = findImageWidget(newNode)
  if (widget) {
    widget.value = buildWidgetValue(ref)
    try {
      widget.callback?.(widget.value)
    } catch (error) {
      api.warn('LoadImage widget callback threw for the newly added node', error)
    }
  }
  graph.setDirtyCanvas(true, true)
  app.canvas?.selectNode?.(newNode)
}

/**
 * Queues the workflow when the setting allows it for this origin kind
 * (PLAN.md §3 / PROTOCOL.md §5). Uses the documented `app.queuePrompt`
 * overload (`number, batchCount = 1`); `0` means "append normally", matching
 * the long-standing extension convention.
 * @param {import('./api.js').CpsbOriginKind} originKind
 */
function maybeAutoQueue(originKind) {
  if (originKind !== 'load_image' && originKind !== 'bridge_node') return
  if (!settings.getAutoQueue()) return
  app.queuePrompt(0).catch((error) => {
    api.warn('auto-queue after Photoshop edit failed', error)
  })
}

/**
 * Resolves the handoff context an incoming edit needs for batch-slot
 * matching: the handoff id plus the original source image ref from the
 * client-side cache (seeded well before any edit can arrive, since the
 * handoff was created by an earlier `/cpsb/open`).
 * @param {import('./api.js').CpsbUpdatedEvent} payload
 * @returns {{handoffId: string, sourceRef: import('./api.js').CpsbImageRef | null}}
 */
function handoffContext(payload) {
  const meta = state.getHandoffById(payload.handoff_id)
  return { handoffId: payload.handoff_id, sourceRef: meta?.source ?? null }
}

/**
 * @param {import('./api.js').CpsbUpdatedEvent} payload
 */
function handleLoadImageOrBridge(payload) {
  const node = getNodeByIdFlexible(payload.origin_node_id)
  const ref = { filename: payload.filename, subfolder: payload.subfolder, type: payload.type }

  if (!node) {
    // PLAN.md §6: node deleted while editing — degrade to the terminal-output
    // toast so the edit is never silently lost (it also stays in the gallery
    // regardless).
    handleMissingNode(ref)
    return
  }

  const pasted = pasteToWidget(node, ref, handoffContext(payload))
  if (!pasted) {
    api.debugLog(
      `no image widget found on node ${payload.origin_node_id} for origin_kind ` +
        `"${payload.origin_kind}" — relying on cpsb.status for the badge`
    )
  }
  maybeAutoQueue(payload.origin_kind)
}

/**
 * @param {import('./api.js').CpsbImageRef} ref
 */
function handleMissingNode(ref) {
  ui.showActionToast({
    summary: 'Edit received — original node no longer exists',
    detail: 'The node this edit belongs to was removed from the workflow.',
    actionLabel: 'Add as node',
    onAction: () => addLoadImageNodeNear(null, ref)
  })
}

/**
 * @param {import('./api.js').CpsbUpdatedEvent} payload
 */
function handleTerminalOutput(payload) {
  const node = getNodeByIdFlexible(payload.origin_node_id)
  const ref = { filename: payload.filename, subfolder: payload.subfolder, type: payload.type }

  if (!node) {
    handleMissingNode(ref)
    return
  }

  refreshNodePreview(node, ref, handoffContext(payload))

  const detail = payload.sibling_output
    ? `Also saved as ${payload.sibling_output.filename} in the output folder.`
    : undefined

  ui.showActionToast({
    summary: 'Edit received from Photoshop',
    detail,
    actionLabel: 'Add as node',
    onAction: () => addLoadImageNodeNear(node, ref)
  })
  // No auto-queue for terminal_output: nothing re-executable changed, and
  // the opt-in added node is unconnected by construction (PLAN.md §3).
}

/**
 * @param {import('./api.js').CpsbUpdatedEvent} payload
 */
function handleUpdated(payload) {
  if (payload.origin_kind === 'terminal_output') {
    handleTerminalOutput(payload)
    return
  }
  handleLoadImageOrBridge(payload)
}

/**
 * Subscribes to `cpsb.updated`. Call once from `cpsb.js`'s `setup()`.
 */
export function init() {
  api.onUpdated(handleUpdated)
}
