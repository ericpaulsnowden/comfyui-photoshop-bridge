/**
 * @file Node overlay chip while editing ("Editing in Photoshop…" spinner →
 * checkmark for ~2s), driven by `cpsb.status` / `cpsb.updated`.
 *
 * Implemented via a chained `node.onDrawForeground` installed from
 * `nodeCreated`, per the project's chosen approach — `onDrawForeground` has
 * been part of `LGraphNode` for the entire history of the canvas renderer
 * (confirmed current signature in `Comfy-Org/ComfyUI_frontend`
 * `src/lib/litegraph/src/LGraphNode.ts`:
 * `onDrawForeground?(ctx, canvas, canvasElement)`, invoked from
 * `LGraphCanvas.ts` as `node.onDrawForeground?.(ctx, this, this.canvas)`),
 * so it degrades uniformly across old and new frontends rather than
 * depending on the newer `node.badges`/`LGraphBadge` mechanism this same
 * codebase also has. The chip is drawn just below the node's title bar
 * (`LiteGraph.NODE_TITLE_HEIGHT`), deliberately inside the body's positive-y
 * space rather than the title bar itself, so it can never collide with
 * `node.title_buttons` or native `node.badges`, both of which render in
 * negative-y title-bar space.
 *
 * The spinner needs to animate while the canvas would otherwise sit
 * perfectly idle for however long the user takes in Photoshop, so a single
 * shared timer forces a repaint — but only while at least one node is
 * actually in the `editing` state, so there is zero background work when
 * nothing is active.
 */

import { app } from '../../../scripts/app.js'
import * as api from './api.js'

/** @typedef {{kind: "editing" | "edited", since: number}} BadgeState */

/** @type {Map<string, BadgeState>} */
const badgesByNode = new Map()

const EDITED_BADGE_MS = 2000
const SPIN_PERIOD_MS = 900
const REPAINT_INTERVAL_MS = 120

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
 * @param {CanvasRenderingContext2D} ctx
 * @param {import('../../../scripts/app.js').LGraphNode} node
 */
function drawBadge(ctx, node) {
  const badge = badgesByNode.get(String(node.id))
  if (!badge) return // cheap when inactive: one Map lookup, nothing else.
  if (node.flags?.collapsed) return

  const label = badge.kind === 'editing' ? 'Editing in Photoshop…' : 'Edited'
  const iconSize = 12
  const paddingX = 8
  const gap = 5
  const pillHeight = 20

  ctx.save()
  ctx.font = '11px sans-serif'
  const textWidth = ctx.measureText(label).width
  const pillWidth = paddingX * 2 + iconSize + gap + textWidth
  const x = Math.max(4, node.size[0] - pillWidth - 6)
  const y = 6

  ctx.fillStyle = 'rgba(20, 20, 24, 0.85)'
  ctx.strokeStyle = badge.kind === 'editing' ? '#5b9bd5' : '#4caf50'
  ctx.lineWidth = 1
  roundRectPath(ctx, x, y, pillWidth, pillHeight, pillHeight / 2)
  ctx.fill()
  ctx.stroke()

  const iconCx = x + paddingX + iconSize / 2
  const iconCy = y + pillHeight / 2
  if (badge.kind === 'editing') {
    drawSpinner(ctx, iconCx, iconCy, iconSize / 2)
  } else {
    drawCheckmark(ctx, iconCx, iconCy, iconSize / 2)
  }

  ctx.fillStyle = '#e6e6e6'
  ctx.textBaseline = 'middle'
  ctx.fillText(label, x + paddingX + iconSize + gap, iconCy + 0.5)
  ctx.restore()
}

/**
 * Installs the chained `onDrawForeground` hook on one node instance. Call
 * from `nodeCreated`. Idempotent per node instance.
 * @param {import('../../../scripts/app.js').LGraphNode} node
 */
export function installBadgeHook(node) {
  if (node.__cpsbBadgeHookInstalled) return
  node.__cpsbBadgeHookInstalled = true

  const original = node.onDrawForeground
  node.onDrawForeground = function (ctx, canvas, canvasElement) {
    const result = original?.call(this, ctx, canvas, canvasElement)
    drawBadge(ctx, this)
    return result
  }
}

/**
 * @param {string} nodeId
 * @param {import('./api.js').CpsbStatus} status
 */
function notifyStatus(nodeId, status) {
  if (status === 'editing') {
    badgesByNode.set(nodeId, { kind: 'editing', since: Date.now() })
    ensureAnimationTimer()
  } else {
    // pending (not yet actually open) and every terminal status
    // (edited handled separately via cpsb.updated; cancelled / discarded /
    // superseded / error all mean "nothing left to show here").
    badgesByNode.delete(nodeId)
  }
  app.graph?.setDirtyCanvas(true, false)
}

/**
 * @param {string} nodeId
 */
function notifyEdited(nodeId) {
  badgesByNode.set(nodeId, { kind: 'edited', since: Date.now() })
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
      notifyStatus(detail.origin_node_id, detail.status)
    } catch (error) {
      api.warn('failed to update node badge from cpsb.status', error)
    }
  })
  api.onUpdated((detail) => {
    try {
      notifyEdited(detail.origin_node_id)
    } catch (error) {
      api.warn('failed to update node badge from cpsb.updated', error)
    }
  })
}
