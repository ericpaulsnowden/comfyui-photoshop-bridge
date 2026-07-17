/**
 * @file Sidebar gallery tab ("Photoshop Edits", PLAN.md §2) via
 * `app.extensionManager.registerSidebarTab`. Reverse-chronological handoff
 * list with before/after thumbnails, status chips, per-card actions, a
 * Tier-2 connection pill, a dismissible upgrade banner, and drag-and-drop
 * manual import. Refreshes only from `state.js`'s change notifications
 * (themselves driven by the `cpsb.*` websocket events) — no polling.
 *
 * Card actions are STATUS-SCOPED via one explicit table
 * ({@link cardCapabilities}) rather than ad-hoc conditions — the first
 * field round exposed exactly the failure class that invites: Re-open
 * offered on a cancelled card 404s ("No active handoff for this node",
 * `/cpsb/open mode:"original"` requires an ACTIVE handoff, PROTOCOL.md §2),
 * Discard offered on an already-discarded card is a pointless no-op, a drop
 * onto a terminal card 409s ("not accepting uploads"). Every open request
 * goes through `open.js` (shared 428 remote-confirm / 409 chooser / server-
 * message toasts — bypassing that shared flow here is precisely what broke
 * gallery Re-open for remote browsers), and every remaining action is
 * try/catch-wrapped with the server's own error message surfaced
 * (`api.errorMessage`), never a generic failure line.
 *
 * `registerSidebarTab` shape verified against `Comfy-Org/ComfyUI_frontend`
 * `src/types/extensionTypes.ts` (`CustomSidebarTabExtension`:
 * `render(container): void` + a separate `destroy(): void`, not a cleanup
 * function returned from `render`) and
 * `src/components/common/ExtensionSlot.vue` (confirms `destroy()` fires
 * `onBeforeUnmount` — i.e. `render`/`destroy` run on every tab
 * mount/unmount, not just once per page load, since the panel only mounts
 * whichever sidebar tab is currently active). Tab icon: `SidebarIcon.vue`
 * renders a string `icon` as a bare `<i :class="icon">` in the same
 * document (lines 21-24), so the custom `cpsb-ps-icon` class + this
 * extension's injected stylesheet (a `::before { content: 'Ps' }`
 * lettermark — a generic text mnemonic drawn here, deliberately NOT Adobe's
 * trademarked Photoshop logo) renders in the tab strip exactly like an
 * icon-font glyph; `registerGalleryTab` injects the styles at registration
 * time since the strip renders long before this panel first mounts.
 */

import { app } from '../../../scripts/app.js'
import * as api from './api.js'
import * as open from './open.js'
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
 * @typedef {Object} CpsbCardCapabilities
 * @property {boolean} reopen - "Re-open" (`mode:"original"` — same PSD,
 * layers intact).
 * @property {boolean} openFresh - "Open fresh copy" (`mode:"new"` from the
 * card's recorded source triple).
 * @property {boolean} cancel
 * @property {boolean} discard
 * @property {boolean} dropImport - Whether drag-and-drop manual import is
 * accepted.
 */

/**
 * THE per-status action table — one place, exhaustively enumerated, and
 * deliberately dependency-free so it can be exercised outside a browser
 * (review-time verification extracts this exact function text and asserts
 * every status row plus the invariants: reopen/openFresh are mutually
 * exclusive, and no terminal status offers Cancel, Discard, or drop
 * import). Grounding, per PROTOCOL.md §2:
 *
 *  - `mode:"original"` requires an ACTIVE handoff (pending/editing/edited;
 *    "stale" is display-only sugar over `editing`) — offering Re-open on a
 *    terminal card guarantees a 404, the exact field bug this fixes. A
 *    terminal card gets "Open fresh copy" instead (`mode:"new"` from the
 *    recorded source triple; if that file has since been purged the server's
 *    "Source image not found" message is surfaced verbatim).
 *  - `/cpsb/cancel` is the escape hatch for a round trip that hasn't
 *    delivered yet (pending/editing/stale). It's technically idempotent on
 *    terminal handoffs, but a button that can only ever no-op is noise — and
 *    on `edited` cards "cancel" is misleading (the edit already landed), so
 *    those offer Discard.
 *  - `/cpsb/discard` is the gallery's "remove this card / stop watching"
 *    (§2): offered for stale (its PROTOCOL-described purpose) and for
 *    `edited` (dismiss a finished round trip). NEVER on terminal cards — a
 *    second terminal transition changes nothing the user can see (G2).
 *  - `/cpsb/upload` (drag-drop import) 409s unless the handoff is active —
 *    terminal cards don't register drop handlers at all.
 *
 * Reveal and Add-as-node are NOT status-gated: the origin node and any
 * already-ingested edit files exist (or don't) independently of handoff
 * status, and both actions carry their own guards.
 *
 * Kept dependency-free (plain switch on the display status string) so the
 * matrix test can run it outside a browser.
 * @param {import('./api.js').CpsbStatus | "stale"} displayStatus
 * @returns {CpsbCardCapabilities}
 */
function cardCapabilities(displayStatus) {
  switch (displayStatus) {
    case 'pending':
    case 'editing':
      return { reopen: true, openFresh: false, cancel: true, discard: false, dropImport: true }
    case 'stale':
      return { reopen: true, openFresh: false, cancel: true, discard: true, dropImport: true }
    case 'edited':
      return { reopen: true, openFresh: false, cancel: false, discard: true, dropImport: true }
    case 'cancelled':
    case 'discarded':
    case 'superseded':
    case 'error':
      return { reopen: false, openFresh: true, cancel: false, discard: false, dropImport: false }
    default:
      // Unknown/future status: safest is terminal-like (no state-changing
      // buttons that could 404/409), but still allow starting over.
      return { reopen: false, openFresh: true, cancel: false, discard: false, dropImport: false }
  }
}

/**
 * @param {import('./api.js').CpsbHandoffMeta} meta
 */
function revealInWorkflow(meta) {
  try {
    const node = pasteback.getNodeByIdFlexible(meta.origin_node_id)
    if (!node) {
      ui.showToast({
        severity: 'warn',
        summary: 'Node no longer exists',
        detail:
          'The node this handoff belongs to was removed from the workflow, ' +
          'or belongs to a different workflow than the one that is open.'
      })
      return
    }
    const canvas = app.canvas
    if (!canvas || typeof canvas.centerOnNode !== 'function') {
      ui.showToast({
        severity: 'warn',
        summary: 'Cannot reveal node',
        detail: 'This ComfyUI frontend does not expose canvas navigation.'
      })
      return
    }
    canvas.centerOnNode(node)
    canvas.selectNode?.(node)
  } catch (error) {
    api.warn('gallery Reveal failed', error)
    ui.showToast({
      severity: 'error',
      summary: 'Failed to reveal node',
      detail: api.errorMessage(error)
    })
  }
}

/**
 * Builds the `/cpsb/open` body shared by {@link reopenInPhotoshop} and
 * {@link openFreshCopy} — always the handoff's own recorded source triple
 * and origin, exactly what the handoff was created from.
 * @param {import('./api.js').CpsbHandoffMeta} meta
 * @param {"original" | "new"} mode
 * @returns {import('./api.js').CpsbOpenRequest}
 */
function openBodyFromMeta(meta, mode) {
  return {
    filename: meta.source.filename,
    subfolder: meta.source.subfolder,
    type: meta.source.type,
    origin_node_id: meta.origin_node_id,
    origin_kind: meta.origin_kind,
    workflow_name: meta.workflow_name,
    mode
  }
}

/**
 * "Re-open" for ACTIVE cards only ({@link cardCapabilities}): same handoff,
 * same PSD, layers intact (`mode:"original"`). Routed through the shared
 * `open.js` flow — the 428 remote-open confirm, the 409 chooser, and
 * server-message error toasts all apply here identically to the node
 * context menu (the previous direct `api.openHandoff` call bypassed all
 * three, which is what broke gallery Re-open on remote browsers).
 * @param {import('./api.js').CpsbHandoffMeta} meta
 */
async function reopenInPhotoshop(meta) {
  await open.openInteractive(openBodyFromMeta(meta, 'original'), {
    successSummary: 'Re-opening in Photoshop…'
  })
}

/**
 * "Open fresh copy" for TERMINAL cards ({@link cardCapabilities}): a brand
 * new handoff from this card's recorded source image (`mode:"new"`). If the
 * node meanwhile has a NEWER active handoff, the shared flow's 409 chooser
 * handles it; if the source file has been purged, the server's "Source
 * image not found" message is shown verbatim.
 * @param {import('./api.js').CpsbHandoffMeta} meta
 */
async function openFreshCopy(meta) {
  await open.openInteractive(openBodyFromMeta(meta, 'new'), {
    successSummary: 'Opening a fresh copy in Photoshop…'
  })
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
      detail: api.errorMessage(error)
    })
  }
}

/**
 * @param {import('./api.js').CpsbHandoffMeta} meta
 */
async function discardHandoffCard(meta) {
  const confirmed = await ui.confirmDialog({
    title: 'Remove from the Photoshop Edits list?',
    message:
      `This removes "${meta.workflow_name || 'this entry'}" (node ` +
      `${meta.origin_node_id}) from the list and stops watching it for ` +
      'further edits. Your images and workflow are untouched.'
  })
  if (!confirmed) return
  try {
    await api.discardHandoff(meta.handoff_id)
  } catch (error) {
    ui.showToast({
      severity: 'error',
      summary: 'Failed to discard',
      detail: api.errorMessage(error)
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
  try {
    const node = pasteback.getNodeByIdFlexible(meta.origin_node_id)
    pasteback.addLoadImageNodeNear(node, {
      filename: latestEdit.filename,
      subfolder: api.editSubfolder(meta),
      type: 'input'
    })
  } catch (error) {
    // addLoadImageNodeNear has its own feature-detect toasts for the known
    // degradations (no graph, no LiteGraph.createNode, LoadImage type
    // missing); this catch is the belt for anything it didn't foresee.
    api.warn('gallery Add-as-node failed', error)
    ui.showToast({
      severity: 'error',
      summary: 'Could not add node',
      detail: api.errorMessage(error)
    })
  }
}

/**
 * Drag-and-drop manual import — attached only for cards whose status still
 * accepts uploads ({@link cardCapabilities}.dropImport; PROTOCOL.md §2:
 * `/cpsb/upload` 409s on anything non-active). A race is still possible
 * (status flips between render and drop), so the server's own 409 message
 * — "Handoff is cancelled, not accepting uploads" — is surfaced verbatim
 * when it happens.
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
    try {
      const file = Array.from(event.dataTransfer?.files ?? []).find((f) =>
        f.type.startsWith('image/')
      )
      if (!file) {
        ui.showToast({ severity: 'warn', summary: 'No image file found in that drop' })
        return
      }
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
        detail: api.errorMessage(error)
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
 * An `<img>` thumbnail that degrades to a neutral placeholder box instead
 * of the browser's broken-image glyph when its file no longer exists — a
 * routine occurrence here, not an edge case: handoff folders are purged
 * wholesale after `cleanup_days` while the card can still be on screen, and
 * a terminal card's files can be gone while its meta survives the session.
 * `error` doesn't bubble, but `ui.el` attaches directly to the element, so
 * the swap is reliable.
 * @param {string} src
 * @param {string} alt
 * @returns {HTMLElement}
 */
function buildThumb(src, alt) {
  const img = ui.el('img', {
    className: 'cpsb-card-thumb',
    attrs: { src, alt, loading: 'lazy' }
  })
  img.addEventListener(
    'error',
    () => {
      img.replaceWith(
        ui.el('div', {
          className: 'cpsb-card-thumb cpsb-card-thumb-missing',
          text: 'No preview',
          attrs: { title: 'The image file is no longer on the server.' }
        })
      )
    },
    { once: true }
  )
  return img
}

/**
 * @param {import('./api.js').CpsbHandoffMeta} meta
 * @returns {HTMLElement}
 */
function buildCard(meta) {
  const latestEdit = meta.edits?.[meta.edits.length - 1]
  const capabilities = cardCapabilities(state.getDisplayStatus(meta))
  // Reveal only makes sense when the origin node is actually in the graph
  // that's open right now — otherwise `centerOnNode` has nothing to center
  // on and the action just fails (the user's "Reveal fails most of the
  // time"). `getNodeByIdFlexible` reads live `app.graph`, so this is the
  // current-workflow check, and buildCard re-runs on every rebuild() (each
  // cpsb.* event / tab mount), so a workflow switch while the gallery is
  // open re-evaluates it — visibility is never cached across renders.
  const originNodePresent = !!pasteback.getNodeByIdFlexible(meta.origin_node_id)

  const thumbs = ui.el('div', { className: 'cpsb-card-thumbs' })
  thumbs.appendChild(buildThumb(api.thumbUrl(meta.handoff_id), 'Original'))
  if (latestEdit) {
    thumbs.appendChild(ui.el('span', { className: 'cpsb-card-thumb-arrow', text: '→' }))
    thumbs.appendChild(
      buildThumb(
        api.viewUrl({
          filename: latestEdit.filename,
          subfolder: api.editSubfolder(meta),
          type: 'input'
        }),
        'Latest edit'
      )
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

  // Assembled strictly from the cardCapabilities table — see its doc for
  // the per-status rationale. Reveal shows only when its origin node is in
  // the current graph (above); Add-as-node only needs an edit to exist.
  const actions = ui.el('div', { className: 'cpsb-card-actions' })
  if (originNodePresent) {
    actions.appendChild(
      ui.el('button', {
        className: 'cpsb-card-action',
        text: 'Reveal',
        on: { click: () => revealInWorkflow(meta) }
      })
    )
  }
  if (capabilities.reopen) {
    actions.appendChild(
      ui.el('button', {
        className: 'cpsb-card-action',
        text: 'Re-open',
        on: { click: () => reopenInPhotoshop(meta) }
      })
    )
  }
  if (capabilities.openFresh) {
    actions.appendChild(
      ui.el('button', {
        className: 'cpsb-card-action',
        text: 'Open fresh copy',
        on: { click: () => openFreshCopy(meta) }
      })
    )
  }
  if (latestEdit) {
    actions.appendChild(
      ui.el('button', {
        className: 'cpsb-card-action',
        text: 'Add as node',
        on: { click: () => addAsNode(meta) }
      })
    )
  }
  if (capabilities.cancel) {
    actions.appendChild(
      ui.el('button', {
        className: 'cpsb-card-action',
        text: 'Cancel',
        on: { click: () => cancelHandoffCard(meta) }
      })
    )
  }
  if (capabilities.discard) {
    // Labeled "Remove from list", not "Discard" — the user couldn't tell
    // what Discard did. It only drops this entry from the gallery and stops
    // watching the handoff; it touches no image or workflow the user cares
    // about (the tooltip says so, and discardHandoffCard's confirm repeats
    // it). Still routes to /cpsb/discard unchanged.
    const removeButton = ui.el('button', {
      className: 'cpsb-card-action cpsb-card-action-danger',
      text: 'Remove from list',
      on: { click: () => discardHandoffCard(meta) }
    })
    removeButton.title =
      'Remove this entry from the Photoshop Edits list. Your images and ' +
      'workflow are untouched.'
    actions.appendChild(removeButton)
  }

  const card = ui.el('div', { className: 'cpsb-card', children: [thumbs, header, metaLine, actions] })
  if (capabilities.dropImport) attachDropHandlers(card, meta)
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

/**
 * The generic "Ps" text lettermark (panel-header identity, mirroring the
 * tab-strip icon). Drawn as styled text in a rounded outline box — a
 * mnemonic of our own, NOT Adobe's trademarked Photoshop logo/brand asset.
 * @returns {HTMLElement}
 */
function buildBrandMark() {
  return ui.el('span', {
    className: 'cpsb-brand-mark',
    text: 'Ps',
    attrs: { title: 'Photoshop Bridge', 'aria-hidden': 'true' }
  })
}

function rebuild() {
  if (!rootEl) return
  try {
    rootEl.replaceChildren()

    rootEl.appendChild(
      ui.el('div', {
        className: 'cpsb-gallery-header',
        children: [
          buildBrandMark(),
          ui.el('div', {
            className: 'cpsb-gallery-header-right',
            children: [buildConnectionPill(), buildVersionLabel()]
          })
        ]
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
      // Per-card guard: one malformed meta (e.g. from a hand-edited or
      // truncated meta.json the backend recovered) must degrade to one
      // stub card, not blank the whole panel.
      try {
        list.appendChild(buildCard(meta))
      } catch (error) {
        api.warn('failed to render gallery card', meta?.handoff_id, error)
        list.appendChild(
          ui.el('div', {
            className: 'cpsb-card',
            children: [
              ui.el('div', {
                className: 'cpsb-card-meta',
                text: `Handoff ${meta?.handoff_id ?? '(unknown)'} could not be displayed.`
              })
            ]
          })
        )
      }
    }
    rootEl.appendChild(list)
  } catch (error) {
    // Last-ditch guard so a rendering bug reads as a message in the panel
    // instead of a silently empty (or half-built) sidebar tab.
    api.warn('gallery rebuild failed', error)
    try {
      rootEl.replaceChildren(
        ui.el('div', {
          className: 'cpsb-gallery-empty',
          text: 'The Photoshop Edits panel hit an error — see the browser console.'
        })
      )
    } catch {
      /* container itself is gone; nothing left to do */
    }
  }
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
  // The tab-strip button renders as soon as the tab is registered — long
  // before renderGallery ever runs — so the stylesheet carrying the
  // `.cpsb-ps-icon::before` lettermark must be in <head> NOW, not at first
  // panel mount (see this file's header for the SidebarIcon.vue citation).
  ui.injectStyles()
  extensionManager.registerSidebarTab({
    id: 'cpsb.gallery',
    icon: 'cpsb-ps-icon',
    title: 'Photoshop Edits',
    tooltip: 'Photoshop Edits — round trips for this workflow',
    type: 'custom',
    render: renderGallery,
    destroy: destroyGallery
  })
}
