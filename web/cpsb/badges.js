/**
 * @file Node overlay badge for an active Photoshop round trip: a small pill
 * reading "Editing in Photoshop…" (animated spinner) while `editing`,
 * briefly "Edited" (checkmark, ~2s) right after an edit lands, and — while
 * `editing` — a hover-revealed ✕ that cancels the handoff directly from the
 * node (PROTOCOL.md §2 `/cpsb/cancel`: "The frontend surfaces it directly on
 * the node's 'Editing in Photoshop…' badge (hover → cancel)").  Driven by
 * `cpsb.status` / `cpsb.updated`.
 *
 * Implemented via chained `node.onDrawForeground` / `onMouseDown` /
 * `onMouseMove` / `onMouseLeave` hooks installed from `nodeCreated`, per the
 * project's chosen approach — these have been part of `LGraphNode` for the
 * entire history of the canvas renderer (confirmed current signatures in
 * `Comfy-Org/ComfyUI_frontend` `src/lib/litegraph/src/LGraphNode.ts`:
 * `onDrawForeground?(ctx, canvas, canvasElement)` (~line 686),
 * `onMouseDown?(e, pos, canvas): boolean` (~line 741 — doc comment: "Blocks
 * drag if return value is truthy"; `pos` is already offset from
 * `LGraphNode.pos`, i.e. node-local), `onMouseMove?(e, pos, canvas): void`
 * (~line 787), `onMouseLeave?(e): void` (~line 692) — so this degrades
 * uniformly across old and new frontends rather than depending on the newer
 * `node.badges`/`LGraphBadge` mechanism this same codebase also has.
 *
 * A truthy `onMouseDown` return both blocks the node drag AND replaces the
 * pointer's default select-click with a no-op — confirmed at
 * `LGraphCanvas.ts` `_processNodeClick`, ~line 2930:
 * `if (node.onMouseDown?.(e, pos, this)) { pointer.onClick = () => {}; return }`
 * — the exact call site litegraph's own native title-button hit-test uses
 * one block above (~line 2909-2920) to consume a click directly on
 * mousedown, without waiting for a matching mouseup. This file's ✕ follows
 * that same native precedent rather than tracking down/up state itself.
 *
 * `pos` in every one of those hooks is offset from `node.pos` — the SAME
 * node-local coordinate space `onDrawForeground`'s `ctx` already draws in:
 * `LGraphCanvas.ts` translates `ctx` by `node.pos` (~line 5148,
 * `ctx.translate(node.pos[0], node.pos[1])`) before calling `drawNode`, and
 * `drawNode`'s own body-vs-title split puts the title bar in negative-y
 * space, the body in positive-y (confirmed by its title-button layout,
 * ~line 5705: `button_y = -title_height + ...`). So a rect computed once
 * while drawing can be reused verbatim for hit-testing — see `pillRect` /
 * `closeRect` below, which are recomputed every draw and consulted as-is by
 * the mouse hooks, guaranteeing the two can never drift apart.
 *
 * Placement: on a node WITH a displayed image the badge is centered over
 * the IMAGE preview region rather than pinned below the title bar, so it
 * reads as "THIS PICTURE is being edited" — see `getPreciseImageRect` /
 * `getFallbackImageRect` for how that region is determined. On a node with
 * NO image preview at all (e.g. a Photoshop Bridge node mid-wait), there is
 * no image region to center over and the node body is fully occupied by
 * slot rows + widgets — so the pill instead anchors strictly BELOW the last
 * widget row (`getBelowWidgetsRect`), never over slots or widgets.
 *
 * `drawBadge` then CLAMPS the anchored position to stay fully inside the
 * node's own body (`node.size`, both x and y) before drawing — this used to
 * be x-only, letting the y position grow past the node's bottom edge on a
 * short node (litegraph only clips node DRAWING when `node.clip_area` is
 * set — `LGraphCanvas.ts` `drawNode`, ~line 5673 — which ComfyUI nodes don't
 * set, so the overflow rendered fine). The clamp is required for
 * correctness, not just cosmetics: litegraph resolves which node a
 * click/hover belongs to via `LGraph.getNodeOnPos` -> `LGraphNode
 * .isPointInside`, which tests the pointer against `node.boundingRect` —
 * bounded by `pos[1] + size[1]`, the node's bottom edge, with no margin
 * below it (confirmed in a fresh `Comfy-Org/ComfyUI_frontend` checkout,
 * `src/lib/litegraph/src/LGraph.ts` ~line 1239 and `LGraphNode.ts` ~line
 * 2091/2162). A badge drawn past that edge is never routed to this node's
 * `onMouseDown`/`onMouseMove` at all (not a wrong-coordinate-space bug — the
 * event is attributed to a different node, or none). That was the exact
 * regression on short, imageless nodes (Edit in Photoshop / Annotate for
 * Edit / Compose Layers to PSD with only a few widgets): the ✕ drew fine but
 * could never be clicked or even hover-revealed. See `drawBadge`'s `maxY`
 * for the fix — clickable now wins over letting the pill grow past the
 * bottom.
 *
 * The spinner needs to animate while the canvas would otherwise sit
 * perfectly idle for however long the user takes in Photoshop, so a single
 * shared timer forces a repaint — but only while at least one node is
 * actually in the `editing` state, so there is zero background work when
 * nothing is active. Hover tracking is likewise free when inactive: every
 * handler below bails out after a single `Map` lookup unless a cancelable
 * badge exists for that specific node.
 */

import { app } from '../../../scripts/app.js'
import * as api from './api.js'
import * as ui from './ui.js'

/**
 * @typedef {Object} BadgeRect
 * @property {number} x
 * @property {number} y
 * @property {number} w
 * @property {number} h
 */

/**
 * @typedef {Object} BadgeState
 * @property {"editing" | "edited"} kind
 * @property {number} since
 * @property {string} [handoffId] - From the triggering `cpsb.status` /
 * `cpsb.updated` payload (PROTOCOL.md §5 — both always carry `handoff_id`).
 * Only an `"editing"` badge is cancelable; this is what {@link handleMouseDown}
 * hands to `api.cancelHandoff`.
 * @property {boolean} [hovered] - Whether the pointer is currently over the
 * pill, which reveals the ✕ (only meaningful while `kind === "editing"`).
 * @property {BadgeRect} [pillRect] - Node-local bounds of the whole pill,
 * recomputed every draw. Hovering anywhere inside it sets `hovered`.
 * @property {BadgeRect} [closeRect] - Node-local hit-rect for the ✕ itself,
 * recomputed every draw alongside `pillRect` so drawing and hit-testing can
 * never drift apart.
 */

/** @type {Map<string, BadgeState>} */
const badgesByNode = new Map()

const EDITED_BADGE_MS = 2000
const SPIN_PERIOD_MS = 900
const REPAINT_INTERVAL_MS = 120

/** Pill layout constants. */
const ICON_SIZE = 12
const PADDING_X = 8
const LABEL_GAP = 5
const PILL_HEIGHT = 20
const CLOSE_SIZE = 13
const CLOSE_GAP = 6

/**
 * Litegraph's default single-widget row height
 * (`src/lib/litegraph/src/LiteGraphGlobal.ts` line 51:
 * `NODE_WIDGET_HEIGHT = 20`). Used only when `window.LiteGraph` is somehow
 * unavailable at draw time (it is installed before any extension's `setup()`
 * — see pasteback.js's header) or a widget exposes no `computedHeight`.
 */
const DEFAULT_WIDGET_HEIGHT = 20

/**
 * Litegraph's slot row height (`src/lib/litegraph/src/LiteGraphGlobal.ts`
 * line 50: `NODE_SLOT_HEIGHT = 20`). Same fallback role as
 * {@link DEFAULT_WIDGET_HEIGHT}, for {@link getBelowWidgetsRect}'s
 * never-drawn-node estimate.
 */
const DEFAULT_SLOT_HEIGHT = 20

/**
 * Gap between the bottom of the last widget row and the top of the pill in
 * {@link getBelowWidgetsRect} ("the loader should be after the two UI
 * fields" — it must clear the widgets, not touch them).
 */
const BELOW_WIDGETS_MARGIN = 6

/**
 * Minimum height of the fallback "image region" ({@link getFallbackImageRect}),
 * so centering the pill in it stays sane on a very short node.
 */
const MIN_FALLBACK_HEIGHT = 40

let animationTimer = /** @type {ReturnType<typeof setInterval> | null} */ (null)

function hasActiveSpinner() {
  for (const badge of badgesByNode.values()) {
    if (badge.kind === 'editing') return true
  }
  return false
}

function ensureAnimationTimer() {
  if (animationTimer || !hasActiveSpinner()) return
  animationTimer = setInterval(() => {
    if (!hasActiveSpinner()) {
      clearInterval(animationTimer)
      animationTimer = null
      return
    }
    app.graph?.setDirtyCanvas(true, false)
  }, REPAINT_INTERVAL_MS)
}

/**
 * Draws a rounded-rect path, falling back to manual `arcTo` construction on
 * canvases without `CanvasRenderingContext2D.roundRect` (widely supported,
 * but not universal on very old Chromium/Electron builds).
 * @param {CanvasRenderingContext2D} ctx
 * @param {number} x
 * @param {number} y
 * @param {number} w
 * @param {number} h
 * @param {number} r
 */
function roundRectPath(ctx, x, y, w, h, r) {
  if (typeof ctx.roundRect === 'function') {
    ctx.beginPath()
    ctx.roundRect(x, y, w, h, r)
    return
  }
  // Fallback for canvases without the (relatively recent) roundRect method.
  ctx.beginPath()
  ctx.moveTo(x + r, y)
  ctx.arcTo(x + w, y, x + w, y + h, r)
  ctx.arcTo(x + w, y + h, x, y + h, r)
  ctx.arcTo(x, y + h, x, y, r)
  ctx.arcTo(x, y, x + w, y, r)
  ctx.closePath()
}

/**
 * A rotating open arc, phase derived from the wall clock so every node's
 * spinner stays in sync without per-node timer state.
 * @param {CanvasRenderingContext2D} ctx
 * @param {number} cx
 * @param {number} cy
 * @param {number} r
 */
function drawSpinner(ctx, cx, cy, r) {
  const phase = (Date.now() % SPIN_PERIOD_MS) / SPIN_PERIOD_MS
  const start = phase * Math.PI * 2
  ctx.save()
  ctx.strokeStyle = '#5b9bd5'
  ctx.lineWidth = 2
  ctx.beginPath()
  ctx.arc(cx, cy, r, start, start + Math.PI * 1.3)
  ctx.stroke()
  ctx.restore()
}

/**
 * @param {CanvasRenderingContext2D} ctx
 * @param {number} cx
 * @param {number} cy
 * @param {number} r
 */
function drawCheckmark(ctx, cx, cy, r) {
  ctx.save()
  ctx.strokeStyle = '#4caf50'
  ctx.lineWidth = 2
  ctx.lineCap = 'round'
  ctx.lineJoin = 'round'
  ctx.beginPath()
  ctx.moveTo(cx - r * 0.6, cy)
  ctx.lineTo(cx - r * 0.15, cy + r * 0.5)
  ctx.lineTo(cx + r * 0.6, cy - r * 0.5)
  ctx.stroke()
  ctx.restore()
}

/**
 * The cancel ✕ glyph, drawn only while the pointer is hovering the pill
 * (see `drawBadge`) — hidden otherwise so an untouched badge reads as a
 * plain status chip.
 * @param {CanvasRenderingContext2D} ctx
 * @param {number} cx
 * @param {number} cy
 * @param {number} r - Half-length of each diagonal stroke.
 */
function drawCloseGlyph(ctx, cx, cy, r) {
  ctx.save()
  ctx.strokeStyle = '#e6e6e6'
  ctx.lineWidth = 1.5
  ctx.lineCap = 'round'
  ctx.beginPath()
  ctx.moveTo(cx - r, cy - r)
  ctx.lineTo(cx + r, cy + r)
  ctx.moveTo(cx + r, cy - r)
  ctx.lineTo(cx - r, cy + r)
  ctx.stroke()
  ctx.restore()
}

/**
 * @param {[number, number]} pos - Node-local point, as passed to
 * `onMouseDown`/`onMouseMove`.
 * @param {BadgeRect} rect
 * @returns {boolean}
 */
function isInRect([x, y], rect) {
  return x >= rect.x && x <= rect.x + rect.w && y >= rect.y && y <= rect.y + rect.h
}

/**
 * Node-local rect of the currently-relevant displayed image, when it can be
 * determined precisely.
 *
 * ComfyUI_frontend's built-in image-preview widget populates
 * `node.imageRects` (`Rect[]`, `src/types/litegraph-augmentation.d.ts` line
 * 182) with one `[x, y, w, h]` per thumbnail — in the SAME node-local
 * coordinate space `onDrawForeground`/`onMouseDown` use — but only while it
 * is rendering the "all thumbnails" grid, i.e. `node.imageIndex == null`
 * (`src/renderer/extensions/vueNodes/widgets/composables/useImagePreviewWidget.ts`
 * lines 120 and 159-188: the array is rebuilt from scratch inside that
 * branch only, keyed by index, every draw). Once a single image is selected
 * (`imageIndex` a number — the common case for a one-image output), that
 * same file's lines 246-270 compute the displayed rect as a plain local
 * variable with no field to read it back from afterward, so `imageRects`
 * would otherwise still hold a stale grid-mode rect left over from before
 * the image was selected. This deliberately never trusts the array in that
 * case, rather than risk drawing over the wrong spot —
 * {@link getFallbackImageRect} covers it instead.
 * @param {import('../../../scripts/app.js').LGraphNode} node
 * @returns {BadgeRect | null}
 */
function getPreciseImageRect(node) {
  if (node.imageIndex != null || !Array.isArray(node.imageRects)) return null
  const idx = node.overIndex ?? 0
  const rect = node.imageRects[idx]
  if (!rect || rect[2] <= 0 || rect[3] <= 0) return null
  return { x: rect[0], y: rect[1], w: rect[2], h: rect[3] }
}

/**
 * Fallback anchor for a node WITH a displayed image (`node.imgs` non-empty)
 * when {@link getPreciseImageRect} can't determine an exact rect (by far the
 * common case: a single displayed image). Approximates "the image preview
 * area" as the node body below its input widgets, down to the bottom of the
 * node. Nodes with NO image at all never reach this —
 * {@link getBelowWidgetsRect} handles them instead, since for those the
 * body below the widgets is empty canvas, not a picture to overlay.
 *
 * ComfyUI_frontend always appends the image preview as the LAST widget
 * after any input widgets (`src/services/litegraphService.ts`
 * `unsafeUpdatePreviews`/`addDrawBackgroundHandler`, which calls
 * `showCanvasImagePreview` → `src/composables/node/useNodeCanvasImagePreview.ts`
 * → `node.addCustomWidget`), and that widget stretches to fill the node's
 * height (`computeLayoutSize` only returns a `minHeight`), so its own
 * top-to-bottom span is, in virtually every real layout, indistinguishable
 * from "the rest of the node body". Excluding it from the "widgets" tally —
 * rather than summing every widget's height, which would land just below
 * the image instead of over it — is what keeps this region centered on the
 * image.
 * @param {import('../../../scripts/app.js').LGraphNode} node
 * @returns {BadgeRect}
 */
function getFallbackImageRect(node) {
  const widgets = (node.widgets ?? []).filter((w) => !w.hidden)
  const layoutWidgets = node.imgs?.length && widgets.length ? widgets.slice(0, -1) : widgets
  const widgetsBottom = layoutWidgets.length
    ? Math.max(
        ...layoutWidgets.map((w) => (w.y ?? 0) + (w.computedHeight ?? DEFAULT_WIDGET_HEIGHT))
      )
    : 0
  const top = Math.max(widgetsBottom, 4)
  const bottom = Math.max(top + MIN_FALLBACK_HEIGHT, node.size[1])
  return { x: 0, y: top, w: node.size[0], h: bottom - top }
}

/**
 * Anchor for a node with NO displayed image (e.g. the Photoshop Bridge node
 * itself while it waits): a pill-height strip strictly BELOW the last
 * widget row, so the pill can never cover the node's slot rows or widgets
 * ("the loader should be after the two UI fields"). Returning a strip of
 * exactly `PILL_HEIGHT` makes `drawBadge`'s shared centering math place the
 * pill at precisely this strip's `y` when it fits — on a node too short for
 * that (the common case for a bare 2-4-widget bridge/annotate/compose node),
 * `drawBadge` now clamps the pill's y back up so it stays inside the node's
 * clickable body (never upward past the top, where it would cover the input
 * slots) rather than letting it extend past the bottom edge: overflowing the
 * body used to render harmlessly but made the cancel ✕ (and its hover
 * reveal) permanently unreachable, since litegraph's own click/hover
 * routing never attributes a point past `node.size[1]` to this node at all
 * — see `drawBadge`'s `maxY` comment and this file's header for the fuller
 * explanation and the regression this fixes.
 *
 * Widget bottom-edge geometry, verified in `Comfy-Org/ComfyUI_frontend`
 * `src/lib/litegraph/src/LGraphNode.ts`:
 * - `_arrangeWidgets` assigns each visible widget's node-local top:
 *   `w.y = y; y += w.computedHeight ?? 0` (lines 4206-4210), starting from
 *   `startY = widgets_start_y ?? (widgets_up ? 0 : widgetStartY) + 2`
 *   (line 4154), where `arrange()` derives `widgetStartY` from the measured
 *   slot rows' bounds: `slotsBounds[1] + slotsBounds[3] - this.pos[1]`
 *   (lines 4280-4285) — i.e. widgets sit below the slot rows, and a
 *   standard (non-custom) widget's `computedHeight` is
 *   `LiteGraph.NODE_WIDGET_HEIGHT + 4` (line 4181), spacing included.
 * - `drawWidgets` records `widget.last_y = y` at draw time (line 3985) — on
 *   this frontend a copy of `widget.y`, on classic litegraph the only
 *   populated field — and the node's own hit-testing consumes `last_y` the
 *   same way (`getWidgetOnPos`, lines 2291-2296). So `last_y` is preferred
 *   here (draw-truth on every frontend generation), then `y` when it is a
 *   positive number (its pre-`arrange()` default is 0, which is
 *   indistinguishable from "unset" and was the root cause of this pill
 *   previously landing over the slot rows).
 * - Both can be legitimately absent before the node's first frame:
 *   `onDrawForeground` fires BEFORE `arrange()`/`drawNodeWidgets` within
 *   `drawNode` (`LGraphCanvas.ts` lines 5722 vs 5728-5743). For that one
 *   frame the estimate below reconstructs the same layout arithmetic from
 *   the constants: `max(#inputs, #outputs)` slot rows of `NODE_SLOT_HEIGHT`
 *   (`LiteGraphGlobal.ts` line 50) for `widgetStartY`, `+ 2` (line 4154),
 *   then one `NODE_WIDGET_HEIGHT + 4` row (line 4181, `LiteGraphGlobal.ts`
 *   line 51) per visible widget.
 * @param {import('../../../scripts/app.js').LGraphNode} node
 * @returns {BadgeRect}
 */
function getBelowWidgetsRect(node) {
  const LiteGraph = window.LiteGraph
  const widgetHeight = LiteGraph?.NODE_WIDGET_HEIGHT ?? DEFAULT_WIDGET_HEIGHT
  const slotHeight = LiteGraph?.NODE_SLOT_HEIGHT ?? DEFAULT_SLOT_HEIGHT
  const widgets = (node.widgets ?? []).filter((w) => !w.hidden)

  let widgetsBottom = null
  for (const w of widgets) {
    const top =
      typeof w.last_y === 'number'
        ? w.last_y
        : typeof w.y === 'number' && w.y > 0
          ? w.y
          : null
    if (top === null) continue
    const bottom = top + (typeof w.computedHeight === 'number' ? w.computedHeight : widgetHeight)
    if (widgetsBottom === null || bottom > widgetsBottom) widgetsBottom = bottom
  }

  if (widgetsBottom === null) {
    // Never drawn yet — reconstruct the layout arithmetic (see JSDoc):
    // slot rows, the +2 widget-start pad, then stacked widget rows.
    const slotRows = Math.max(node.inputs?.length ?? 0, node.outputs?.length ?? 0)
    widgetsBottom = slotRows * slotHeight + 2 + widgets.length * (widgetHeight + 4)
  }

  return {
    x: 0,
    y: widgetsBottom + BELOW_WIDGETS_MARGIN,
    w: node.size[0],
    h: PILL_HEIGHT
  }
}

/**
 * @param {import('../../../scripts/app.js').LGraphNode} node
 * @returns {BadgeRect}
 */
function getBadgeAnchorRect(node) {
  const precise = getPreciseImageRect(node)
  if (precise) return precise
  return node.imgs?.length ? getFallbackImageRect(node) : getBelowWidgetsRect(node)
}

/**
 * @param {CanvasRenderingContext2D} ctx
 * @param {import('../../../scripts/app.js').LGraphNode} node
 */
function drawBadge(ctx, node) {
  const nodeId = String(node.id)
  const badge = badgesByNode.get(nodeId)
  if (!badge) return // cheap when inactive: one Map lookup, nothing else.
  if (node.flags?.collapsed) return

  const cancelable = badge.kind === 'editing' && !!badge.handoffId
  const label = badge.kind === 'editing' ? 'Editing in Photoshop…' : 'Edited'

  ctx.save()
  ctx.font = '11px sans-serif'
  const textWidth = ctx.measureText(label).width
  let pillWidth = PADDING_X * 2 + ICON_SIZE + LABEL_GAP + textWidth
  if (cancelable) pillWidth += CLOSE_GAP + CLOSE_SIZE

  const anchor = getBadgeAnchorRect(node)
  const maxX = Math.max(4, node.size[0] - pillWidth - 4)
  const x = Math.min(Math.max(anchor.x + (anchor.w - pillWidth) / 2, 4), maxX)
  // Clamped to the node's own body (never past `node.size[1]`), NOT just
  // floored at 4 like `x` used to be alone — see this file's header
  // ("Universal cancel") for why: litegraph's own click/hover routing
  // (`LGraph.getNodeOnPos` -> `LGraphNode.isPointInside`, confirmed in a
  // fresh `Comfy-Org/ComfyUI_frontend` checkout,
  // `src/lib/litegraph/src/LGraph.ts` ~line 1239 and
  // `src/lib/litegraph/src/LGraphNode.ts` ~line 2162) tests the pointer
  // against `node.boundingRect`, which `measure()` populates as exactly
  // `[pos[0], pos[1] - titleHeight, size[0], size[1] + titleHeight]`
  // (`LGraphNode.ts` ~line 2091) — i.e. it stops dead at the node's bottom
  // edge, `pos[1] + size[1]`, with no margin below it. `LGraphCanvas`
  // resolves which node a mousedown/mousemove even belongs to via that same
  // `getNodeOnPos` call BEFORE ever reaching `_processNodeClick`/
  // `node.onMouseDown` (`LGraphCanvas.ts` ~line 2291) or `node.onMouseMove`
  // (~line 3328/3401) — so a badge drawn past the bottom edge isn't hit-
  // tested in the wrong coordinate space, it is never routed to this node's
  // mouse hooks AT ALL, for both click and hover. That is exactly what
  // {@link getBelowWidgetsRect} used to produce on a short, imageless node
  // (Edit in Photoshop / Annotate for Edit / Compose Layers to PSD with only
  // a few widgets): its own JSDoc previously called the overflow "harmless"
  // for drawing, but it silently made the cancel ✕ (and its hover reveal)
  // completely unreachable — the exact regression report ("can't cancel...
  // this needs to be universal any time it shows up"). Clickable now wins
  // over that old "grow past the bottom" allowance.
  const maxY = Math.max(4, node.size[1] - PILL_HEIGHT - 4)
  const y = Math.min(Math.max(anchor.y + (anchor.h - PILL_HEIGHT) / 2, 4), maxY)

  ctx.fillStyle = 'rgba(20, 20, 24, 0.85)'
  ctx.strokeStyle = badge.kind === 'editing' ? '#5b9bd5' : '#4caf50'
  ctx.lineWidth = 1
  roundRectPath(ctx, x, y, pillWidth, PILL_HEIGHT, PILL_HEIGHT / 2)
  ctx.fill()
  ctx.stroke()

  const iconCx = x + PADDING_X + ICON_SIZE / 2
  const iconCy = y + PILL_HEIGHT / 2
  if (badge.kind === 'editing') {
    drawSpinner(ctx, iconCx, iconCy, ICON_SIZE / 2)
  } else {
    drawCheckmark(ctx, iconCx, iconCy, ICON_SIZE / 2)
  }

  ctx.fillStyle = '#e6e6e6'
  ctx.textAlign = 'left'
  ctx.textBaseline = 'middle'
  ctx.fillText(label, x + PADDING_X + ICON_SIZE + LABEL_GAP, iconCy + 0.5)

  badge.pillRect = { x, y, w: pillWidth, h: PILL_HEIGHT }

  if (cancelable) {
    const closeCx = x + pillWidth - PADDING_X - CLOSE_SIZE / 2
    const closeCy = y + PILL_HEIGHT / 2
    badge.closeRect = {
      x: closeCx - CLOSE_SIZE / 2,
      y: closeCy - CLOSE_SIZE / 2,
      w: CLOSE_SIZE,
      h: CLOSE_SIZE
    }
    // Hidden until hover (PROTOCOL.md §2: "hover → cancel") so an untouched
    // badge reads as a plain status chip rather than inviting an accidental
    // click.
    if (badge.hovered) {
      ctx.beginPath()
      ctx.arc(closeCx, closeCy, CLOSE_SIZE / 2, 0, Math.PI * 2)
      ctx.fillStyle = 'rgba(255, 255, 255, 0.16)'
      ctx.fill()
      drawCloseGlyph(ctx, closeCx, closeCy, CLOSE_SIZE * 0.32)
    }
  } else {
    badge.closeRect = undefined
  }

  ctx.restore()
}

/**
 * Handles a click on the ✕: optimistically clears the badge for a
 * responsive feel, then calls the authoritative `/cpsb/cancel` route
 * (PROTOCOL.md §2). The real confirmation is the `cpsb.status` "cancelled"
 * event (see `notifyStatus`) — this is purely a perceived-latency win. On
 * failure the handoff is presumably still `editing` server-side, so the
 * badge is restored (unless something else already legitimately replaced it
 * while the request was in flight) and the user is told why, mirroring
 * `menu.js`'s own error-toast handling.
 * @param {string} nodeId
 * @param {string} handoffId
 */
function cancelFromBadge(nodeId, handoffId) {
  const previous = badgesByNode.get(nodeId)
  badgesByNode.delete(nodeId)
  app.graph?.setDirtyCanvas(true, false)
  api.cancelHandoff(handoffId).catch((error) => {
    if (previous && !badgesByNode.has(nodeId)) {
      badgesByNode.set(nodeId, previous)
      // The shared spinner timer may have stopped itself in the meantime if
      // this was the only active "editing" badge in the graph (its own
      // hasActiveSpinner check, on the very next tick) — restart it so the
      // restored badge's spinner doesn't freeze.
      ensureAnimationTimer()
      app.graph?.setDirtyCanvas(true, false)
    }
    ui.showToast({
      severity: 'error',
      summary: 'Failed to cancel',
      detail: error instanceof Error ? error.message : String(error)
    })
  })
}

/**
 * @param {import('../../../scripts/app.js').LGraphNode} node
 * @param {[number, number]} pos - Node-local point (offset from `node.pos`).
 * @returns {boolean} Whether the click was consumed. A truthy return blocks
 * the node's own drag/selection handling — see this file's header for the
 * exact `LGraphCanvas.ts` call site and its native title-button precedent.
 */
function handleMouseDown(node, pos) {
  const badge = badgesByNode.get(String(node.id))
  if (!badge?.closeRect || badge.kind !== 'editing' || !badge.handoffId) return false
  if (!isInRect(pos, badge.closeRect)) return false
  cancelFromBadge(String(node.id), badge.handoffId)
  return true
}

/**
 * @param {import('../../../scripts/app.js').LGraphNode} node
 * @param {[number, number]} pos - Node-local point (offset from `node.pos`).
 */
function updateHover(node, pos) {
  const badge = badgesByNode.get(String(node.id))
  if (!badge?.pillRect || badge.kind !== 'editing') return // cheap bail: no cancelable badge here.
  const hovered = isInRect(pos, badge.pillRect)
  if (hovered === !!badge.hovered) return // no visible change, skip the repaint.
  badge.hovered = hovered
  app.graph?.setDirtyCanvas(true, false)
}

/**
 * @param {import('../../../scripts/app.js').LGraphNode} node
 */
function clearHover(node) {
  const badge = badgesByNode.get(String(node.id))
  if (badge?.hovered) {
    badge.hovered = false
    app.graph?.setDirtyCanvas(true, false)
  }
}

/**
 * Installs the chained `onDrawForeground`/`onMouseDown`/`onMouseMove`/
 * `onMouseLeave` hooks on one node instance. Call from `nodeCreated`.
 * Idempotent per node instance.
 * @param {import('../../../scripts/app.js').LGraphNode} node
 */
export function installBadgeHook(node) {
  if (node.__cpsbBadgeHookInstalled) return
  node.__cpsbBadgeHookInstalled = true

  const originalDraw = node.onDrawForeground
  node.onDrawForeground = function (ctx, canvas, canvasElement) {
    const result = originalDraw?.call(this, ctx, canvas, canvasElement)
    drawBadge(ctx, this)
    return result
  }

  // Hit-testing for the ✕, chained the same way as onDrawForeground above.
  // Every handler bails after one Map lookup when this node has no
  // cancelable badge, so installing these on every node (matching
  // onDrawForeground's own installation policy) costs nothing while
  // inactive.
  const originalMouseDown = node.onMouseDown
  node.onMouseDown = function (e, pos, canvas) {
    const result = originalMouseDown?.call(this, e, pos, canvas)
    if (result) return result
    return handleMouseDown(this, pos)
  }

  const originalMouseMove = node.onMouseMove
  node.onMouseMove = function (e, pos, canvas) {
    originalMouseMove?.call(this, e, pos, canvas)
    updateHover(this, pos)
  }

  const originalMouseLeave = node.onMouseLeave
  node.onMouseLeave = function (e) {
    originalMouseLeave?.call(this, e)
    clearHover(this)
  }
}

/**
 * @param {import('./api.js').CpsbStatusEvent} detail
 */
function notifyStatus({ origin_node_id: nodeId, handoff_id: handoffId, status }) {
  if (status === 'editing') {
    badgesByNode.set(nodeId, { kind: 'editing', since: Date.now(), handoffId })
    ensureAnimationTimer()
  } else {
    // Every other status (pending / cancelled / discarded / superseded /
    // error — "edited" is conveyed solely by cpsb.updated, see
    // notifyEdited: handoff.py's ingest path calls _emit_updated, never
    // _emit_status, so a cpsb.status "edited" event never actually arrives)
    // means "this handoff has nothing left to show here". Only clear the
    // badge if it still belongs to THIS handoff, or predates handoff-id
    // tracking — a late cancel/error confirmation for a superseded handoff
    // must not blow away a newer "editing" badge for the same node (e.g.
    // the user hits Start Fresh immediately after Cancel, racing the old
    // handoff's server-side confirmation).
    const current = badgesByNode.get(nodeId)
    if (!current || !current.handoffId || current.handoffId === handoffId) {
      badgesByNode.delete(nodeId)
    }
  }
  app.graph?.setDirtyCanvas(true, false)
}

/**
 * @param {import('./api.js').CpsbUpdatedEvent} detail
 */
function notifyEdited({ origin_node_id: nodeId, handoff_id: handoffId }) {
  badgesByNode.set(nodeId, { kind: 'edited', since: Date.now(), handoffId })
  app.graph?.setDirtyCanvas(true, false)
  setTimeout(() => {
    const current = badgesByNode.get(nodeId)
    if (current?.kind === 'edited') badgesByNode.delete(nodeId)
    app.graph?.setDirtyCanvas(true, false)
  }, EDITED_BADGE_MS)
}

/**
 * Subscribes to `cpsb.status` / `cpsb.updated`. Call once from `cpsb.js`'s
 * `setup()`.
 */
export function init() {
  api.onStatusChanged((detail) => {
    try {
      notifyStatus(detail)
    } catch (error) {
      api.warn('failed to update node badge from cpsb.status', error)
    }
  })
  api.onUpdated((detail) => {
    try {
      notifyEdited(detail)
    } catch (error) {
      api.warn('failed to update node badge from cpsb.updated', error)
    }
  })
}
