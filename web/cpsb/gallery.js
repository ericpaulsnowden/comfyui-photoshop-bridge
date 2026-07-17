/**
 * @file Sidebar gallery tab ("Photoshop Edits", PLAN.md §2) via
 * `app.extensionManager.registerSidebarTab`. Reverse-chronological handoff
 * list with before/after thumbnails, status chips, per-card actions, a
 * Tier-2 connection pill, a dismissible upgrade banner, and drag-and-drop
 * manual import. Refreshes only from `state.js`'s change notifications
 * (themselves driven by the `cpsb.*` websocket events) — no polling.
 *
 * `registerSidebarTab` shape verified against `Comfy-Org/ComfyUI_frontend`
 * `src/types/extensionTypes.ts` (`CustomSidebarTabExtension`:
 * `render(container): void` + a separate `destroy(): void`, not a cleanup
 * function returned from `render`) and
 * `src/components/common/ExtensionSlot.vue` (confirms `destroy()` fires
 * `onBeforeUnmount` — i.e. `render`/`destroy` run on every tab
 * mount/unmount, not just once per page load, since the panel only mounts
 * whichever sidebar tab is currently active).
 */

import { app } from '../../../scripts/app.js'
import * as api from './api.js'
import * as state from './state.js'
import * as settings from './settings.js'
import * as pasteback from './pasteback.js'
import * as ui from './ui.js'

const STATUS_LABELS = {
  pending: 'Pending',
  editing: 'Editing',
  edited: 'Edited',
  error: 'Error',
  stale: 'Stale',
  cancelled: 'Cancelled',
  discarded: 'Discarded',
  superseded: 'Superseded'
}

let registered = false
let rootEl = /** @type {HTMLElement | null} */ (null)
let unsubscribeState = /** @type {(() => void) | null} */ (null)

/**
 * @param {import('./api.js').CpsbHandoffMeta} meta
 */
function revealInWorkflow(meta) {
  const node = pasteback.getNodeByIdFlexible(meta.origin_node_id)
  if (!node) {
    ui.showToast({
      severity: 'warn',
      summary: 'Node no longer exists',
      detail: 'The node this handoff belongs to was removed from the workflow.'
    })
    return
  }
  app.canvas?.centerOnNode?.(node)
  app.canvas?.selectNode?.(node)
}

/**
 * @param {import('./api.js').CpsbHandoffMeta} meta
 */
async function reopenInPhotoshop(meta) {
  try {
    await api.openHandoff({
      filename: meta.source.filename,
      subfolder: meta.source.subfolder,
      type: meta.source.type,
      origin_node_id: meta.origin_node_id,
      origin_kind: meta.origin_kind,
      workflow_name: meta.workflow_name,
      mode: 'original'
    })
    ui.showToast({ severity: 'info', summary: 'Re-opening in Photoshop…' })
  } catch (error) {
    ui.showToast({
      severity: 'error',
      summary: 'Failed to re-open in Photoshop',
      detail: error instanceof Error ? error.message : String(error)
    })
  }
}

/**
 * Cancels a `pending`/`editing` handoff directly from its gallery card
 * (PROTOCOL.md §2 `/cpsb/cancel`: "cancelling is always available
 * immediately, not gated on the stale timeout"). No confirmation dialog, by
 * design — this mirrors the one-click cancel affordance on the node's
 * "Editing in Photoshop…" badge (`badges.js`) rather than `discardHandoffCard`'s
 * confirm-then-destroy pattern, so the two entry points to the same action
 * behave consistently and stay low-friction (the whole point of surfacing
 * cancel at all is to give a stuck handoff an easy way out).
 * @param {import('./api.js').CpsbHandoffMeta} meta
 */
async function cancelHandoffCard(meta) {
  try {
    await api.cancelHandoff(meta.handoff_id)
  } catch (error) {
    ui.showToast({
      severity: 'error',
      summary: 'Failed to cancel',
      detail: error instanceof Error ? error.message : String(error)
    })
  }
}

/**
 * @param {import('./api.js').CpsbHandoffMeta} meta
 */
async function discardHandoffCard(meta) {
  const confirmed = await ui.confirmDialog({
    title: 'Discard this handoff?',
    message:
      `This removes "${meta.workflow_name || 'this handoff'}" (node ` +
      `${meta.origin_node_id}) from the gallery and stops watching for ` +
      'further edits. This cannot be undone.'
  })
  if (!confirmed) return
  try {
    await api.discardHandoff(meta.handoff_id)
  } catch (error) {
    ui.showToast({
      severity: 'error',
      summary: 'Failed to discard',
      detail: error instanceof Error ? error.message : String(error)
    })
  }
}

/**
 * @param {import('./api.js').CpsbHandoffMeta} meta
 */
function addAsNode(meta) {
  const latestEdit = meta.edits?.[meta.edits.length - 1]
  if (!latestEdit) {
    ui.showToast({
      severity: 'warn',
      summary: 'No edit to add yet',
      detail: 'This handoff has not received an edit from Photoshop yet.'
    })
    return
  }
  const node = pasteback.getNodeByIdFlexible(meta.origin_node_id)
  pasteback.addLoadImageNodeNear(node, {
    filename: latestEdit.filename,
    subfolder: api.editSubfolder(meta),
    type: 'input'
  })
}

/**
 * @param {HTMLElement} card
 * @param {import('./api.js').CpsbHandoffMeta} meta
 */
function attachDropHandlers(card, meta) {
  card.addEventListener('dragover', (event) => {
    event.preventDefault()
    card.classList.add('cpsb-card-dragover')
  })
  card.addEventListener('dragleave', () => {
    card.classList.remove('cpsb-card-dragover')
  })
  card.addEventListener('drop', async (event) => {
    event.preventDefault()
    card.classList.remove('cpsb-card-dragover')
    const file = Array.from(event.dataTransfer?.files ?? []).find((f) =>
      f.type.startsWith('image/')
    )
    if (!file) {
      ui.showToast({ severity: 'warn', summary: 'No image file found in that drop' })
      return
    }
    try {
      await api.uploadEdit(meta.handoff_id, file, 'manual')
      ui.showToast({
        severity: 'success',
        summary: 'Image imported',
        detail: 'Added as a manual edit for this handoff.'
      })
    } catch (error) {
      ui.showToast({
        severity: 'error',
        summary: 'Import failed',
        detail: error instanceof Error ? error.message : String(error)
      })
    }
  })
}

/**
 * @param {import('./api.js').CpsbHandoffMeta} meta
 * @returns {HTMLElement}
 */
function buildStatusChip(meta) {
  const status = state.getDisplayStatus(meta)
  const label = STATUS_LABELS[status] ?? status
  const chip = ui.el('span', { className: `cpsb-chip cpsb-chip-${status}`, text: label })
  if (meta.status === 'error' && meta.error) chip.title = meta.error
  return chip
}

/**
 * A small "Mask" chip for a card whose latest edit carries an extracted
 * mask (PROTOCOL.md §1/§4, `CpsbEdit.mask`) — presence-only signal, no
 * other behavior (mask *consumption* is the Photoshop Bridge node's MASK
 * output, entirely backend-side per PROTOCOL.md §6).
 * @param {import('./api.js').CpsbEdit | undefined} latestEdit
 * @returns {HTMLElement | null}
 */
function buildMaskChip(latestEdit) {
  if (!latestEdit?.mask) return null
  const chip = ui.el('span', { className: 'cpsb-chip cpsb-chip-mask', text: 'Mask' })
  chip.title = latestEdit.mask.filename
  return chip
}

/**
 * @param {import('./api.js').CpsbHandoffMeta} meta
 * @returns {HTMLElement}
 */
function buildCard(meta) {
  const latestEdit = meta.edits?.[meta.edits.length - 1]

  const thumbs = ui.el('div', { className: 'cpsb-card-thumbs' })
  thumbs.appendChild(
    ui.el('img', {
      className: 'cpsb-card-thumb',
      attrs: { src: api.thumbUrl(meta.handoff_id), alt: 'Original', loading: 'lazy' }
    })
  )
  if (latestEdit) {
    thumbs.appendChild(ui.el('span', { className: 'cpsb-card-thumb-arrow', text: '→' }))
    thumbs.appendChild(
      ui.el('img', {
        className: 'cpsb-card-thumb',
        attrs: {
          src: api.viewUrl({
            filename: latestEdit.filename,
            subfolder: api.editSubfolder(meta),
            type: 'input'
          }),
          alt: 'Latest edit',
          loading: 'lazy'
        }
      })
    )
  }

  // Grouped in their own flex row (rather than as two more direct children
  // of `.cpsb-card-header`) so `justify-content: space-between` keeps
  // pairing exactly two items — title vs. this group — instead of spacing
  // three chips apart with the mask chip stranded in the middle.
  const maskChip = buildMaskChip(latestEdit)
  const headerBadges = ui.el('div', {
    className: 'cpsb-card-header-badges',
    children: [...(maskChip ? [maskChip] : []), buildStatusChip(meta)]
  })
  const header = ui.el('div', {
    className: 'cpsb-card-header',
    children: [
      ui.el('span', {
        className: 'cpsb-card-title',
        text: meta.workflow_name || 'Untitled workflow'
      }),
      headerBadges
    ]
  })

  const metaLine = ui.el('div', {
    className: 'cpsb-card-meta',
    text: `Node ${meta.origin_node_id} · ${ui.formatRelativeTime(meta.updated_ts)}`
  })

  const actions = ui.el('div', { className: 'cpsb-card-actions' })
  actions.append(
    ui.el('button', {
      className: 'cpsb-card-action',
      text: 'Reveal',
      on: { click: () => revealInWorkflow(meta) }
    }),
    ui.el('button', {
      className: 'cpsb-card-action',
      text: 'Re-open',
      on: { click: () => reopenInPhotoshop(meta) }
    })
  )
  if (latestEdit) {
    actions.appendChild(
      ui.el('button', {
        className: 'cpsb-card-action',
        text: 'Add as node',
        on: { click: () => addAsNode(meta) }
      })
    )
  }
  // Cancel vs Discard is status-scoped rather than always offering both:
  // pending/editing (including stale, PROTOCOL.md §2) can still be actively
  // waited on server-side, so Cancel is the meaningful action; a stale one
  // additionally gets Discard, the gallery-specific "give up on this and
  // remove it" cleanup PROTOCOL.md §2 describes Discard as being for. Every
  // other (terminal) status only gets Discard — cancelling something already
  // finished is a no-op the backend accepts idempotently, but not worth
  // exposing as a button.
  const isActive = meta.status === 'pending' || meta.status === 'editing'
  const isStale = state.getDisplayStatus(meta) === 'stale'
  if (isActive) {
    actions.appendChild(
      ui.el('button', {
        className: 'cpsb-card-action',
        text: 'Cancel',
        on: { click: () => cancelHandoffCard(meta) }
      })
    )
  }
  if (!isActive || isStale) {
    actions.appendChild(
      ui.el('button', {
        className: 'cpsb-card-action cpsb-card-action-danger',
        text: 'Discard',
        on: { click: () => discardHandoffCard(meta) }
      })
    )
  }

  const card = ui.el('div', { className: 'cpsb-card', children: [thumbs, header, metaLine, actions] })
  attachDropHandlers(card, meta)
  return card
}

/**
 * @returns {HTMLElement}
 */
function buildConnectionPill() {
  const tierInfo = state.getTierInfo()
  const summary = tierInfo.tier2Connected
    ? `Photoshop: Connected${tierInfo.psVersion ? ` (${tierInfo.psVersion})` : ''}`
    : 'Photoshop: Not connected'
  return ui.el('div', {
    className: `cpsb-pill ${tierInfo.tier2Connected ? 'cpsb-pill-connected' : 'cpsb-pill-disconnected'}`,
    text: summary
  })
}

/**
 * Subtle "vX.Y.Z" label for the gallery header, showing this frontend
 * build's own version — the one value known with certainty as soon as this
 * module runs (the backend's version arrives asynchronously via
 * `/cpsb/status` and may still be `null` on a very first paint). Full detail
 * — both versions plus the Tier-2 connection state — is in the tooltip
 * rather than inline, per the "show subtly" instruction.
 * @returns {HTMLElement}
 */
function buildVersionLabel() {
  const serverVersion = state.getServerVersion()
  const tierInfo = state.getTierInfo()
  const connSummary = tierInfo.tier2Connected
    ? `Photoshop panel connected${tierInfo.psVersion ? ` (Photoshop ${tierInfo.psVersion})` : ''}`
    : 'Photoshop panel not connected'
  const label = ui.el('span', {
    className: 'cpsb-gallery-version',
    text: `v${api.FRONTEND_VERSION}`
  })
  label.title =
    `Backend v${serverVersion || 'unknown'} · Frontend v${api.FRONTEND_VERSION}\n` +
    connSummary
  return label
}

/**
 * @returns {boolean} Whether the backend's reported version differs from
 * this frontend build's own — `false` while the server version is still
 * unknown (pre-first-fetch), never a false positive.
 */
function isVersionMismatched() {
  const serverVersion = state.getServerVersion()
  return !!serverVersion && serverVersion !== api.FRONTEND_VERSION
}

/**
 * Persistent (not auto-dismissing, unlike the one-time toast in cpsb.js)
 * mismatch line for the gallery header — stays visible for as long as the
 * mismatch does, since re-opening the sidebar tab re-runs `rebuild()`.
 * @returns {HTMLElement}
 */
function buildVersionMismatchNotice() {
  const serverVersion = state.getServerVersion()
  return ui.el('div', {
    className: 'cpsb-gallery-version-mismatch',
    text:
      `Version mismatch — backend v${serverVersion}, frontend ` +
      `v${api.FRONTEND_VERSION}. Restart the ComfyUI server or hard-refresh ` +
      'the browser.'
  })
}

/**
 * @param {HTMLElement} container
 */
function renderUpgradeBanner(container) {
  if (!settings.getShowUpgradeBanner()) return
  if (state.getTierInfo().tier2Connected) return

  const banner = ui.el('div', { className: 'cpsb-upgrade-banner' })
  banner.append(
    ui.el('div', {
      className: 'cpsb-upgrade-banner-text',
      text:
        'Make round trips instant — install the ComfyUI panel for ' +
        'Photoshop (also enables remote ComfyUI).'
    }),
    ui.el('button', {
      className: 'cpsb-dialog-button',
      text: 'Dismiss',
      on: {
        click: () => {
          settings.setShowUpgradeBanner(false)
          banner.remove()
        }
      }
    })
  )
  container.appendChild(banner)
}

function rebuild() {
  if (!rootEl) return
  rootEl.replaceChildren()

  rootEl.appendChild(
    ui.el('div', {
      className: 'cpsb-gallery-header',
      children: [buildConnectionPill(), buildVersionLabel()]
    })
  )
  if (isVersionMismatched()) {
    rootEl.appendChild(buildVersionMismatchNotice())
  }
  renderUpgradeBanner(rootEl)

  const handoffs = state.getAllHandoffs()
  if (handoffs.length === 0) {
    rootEl.appendChild(
      ui.el('div', {
        className: 'cpsb-gallery-empty',
        text: 'No Photoshop round trips yet. Right-click an image on any node to get started.'
      })
    )
    return
  }

  const list = ui.el('div', { className: 'cpsb-gallery-list' })
  for (const meta of handoffs) {
    list.appendChild(buildCard(meta))
  }
  rootEl.appendChild(list)
}

/**
 * `render` for the custom sidebar tab. Runs on every mount (each time the
 * user switches to this tab), not just once.
 * @param {HTMLElement} container
 */
function renderGallery(container) {
  ui.injectStyles()
  container.classList.add('cpsb-gallery-root')
  rootEl = container
  rebuild()
  unsubscribeState = state.subscribe(rebuild)
}

/**
 * `destroy` for the custom sidebar tab. Runs on every unmount.
 */
function destroyGallery() {
  unsubscribeState?.()
  unsubscribeState = null
  rootEl?.replaceChildren()
  rootEl = null
}

/**
 * Registers the "Photoshop Edits" sidebar tab. Call once from `cpsb.js`'s
 * `setup()`. Degrades to a single console.warn (no sidebar tab at all) on
 * frontends without `registerSidebarTab`.
 */
export function registerGalleryTab() {
  if (registered) return
  // Called as a method on extensionManager (not detached into a bare
  // function reference) so `this` resolves correctly inside it regardless
  // of whether a given frontend version's implementation relies on it.
  const extensionManager = app.extensionManager
  if (!extensionManager || typeof extensionManager.registerSidebarTab !== 'function') {
    api.warn(
      'app.extensionManager.registerSidebarTab is unavailable on this ' +
        'frontend — the Photoshop Edits sidebar tab will not be shown'
    )
    return
  }
  registered = true
  extensionManager.registerSidebarTab({
    id: 'cpsb.gallery',
    icon: 'pi pi-image',
    title: 'Photoshop Edits',
    tooltip: 'Photoshop round trips for this workflow',
    type: 'custom',
    render: renderGallery,
    destroy: destroyGallery
  })
}
