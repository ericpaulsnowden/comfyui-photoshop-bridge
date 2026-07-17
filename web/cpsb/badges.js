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
 * The badge is centered over the node's IMAGE preview region rather than
 * pinned below the title bar, so it reads as "THIS PICTURE is being edited"
 * rather than a generic corner status chip — see `getPreciseImageRect` /
 * `getFallbackImageRect` for how that region is determined (and where it
 * falls back when it can't be determined precisely).
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
 * Litegraph's own default single-widget height
 * (`src/lib/litegraph/src/LiteGraphGlobal.ts`: `NODE_WIDGET_HEIGHT = 20`),
 * used only as a last-resort per-widget height estimate in
 * {@link getFallbackImageRect} for a widget that doesn't expose its own
 * `computedHeight`. Inlined rather than read from `window.LiteGraph` so this
 * cheap fallback heuristic has no extra runtime dependency.
 */
const DEFAULT_WIDGET_HEIGHT = 20

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
 * Fallback anchor when {@link getPreciseImageRect} can't determine an exact
 * rect (by far the common case: a single displayed image). Approximates
 * "the image preview area" as the node body below its input widgets, down
 * to the bottom of the node.
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
 * @param {import('../../../scripts/app.js').LGraphNode} node
 * @returns {BadgeRect}
 */
function getBadgeAnchorRect(node) {
  return getPreciseImageRect(node) ?? getFallbackImageRect(node)
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
  const y = Math.max(4, anchor.y + (anchor.h - PILL_HEIGHT) / 2)

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
