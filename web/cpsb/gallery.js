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
    subfolder: `cpsb/${meta.handoff_id}`,
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
            subfolder: `cpsb/${meta.handoff_id}`,
            type: 'input'
          }),
          alt: 'Latest edit',
          loading: 'lazy'
        }
      })
    )
  }

  const header = ui.el('div', {
    className: 'cpsb-card-header',
    children: [
      ui.el('span', {
        className: 'cpsb-card-title',
        text: meta.workflow_name || 'Untitled workflow'
      }),
      buildStatusChip(meta)
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
  actions.appendChild(
    ui.el('button', {
      className: 'cpsb-card-action cpsb-card-action-danger',
      text: 'Discard',
      on: { click: () => discardHandoffCard(meta) }
    })
  )

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

  rootEl.appendChild(ui.el('div', { className: 'cpsb-gallery-header', children: [buildConnectionPill()] }))
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
