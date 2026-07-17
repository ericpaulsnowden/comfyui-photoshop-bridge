/**
 * @file Context-menu integration (PROTOCOL.md §8). Registers "Open in
 * Photoshop" / "Edit Original in Photoshop" + "Start Fresh in Photoshop" /
 * "Open all N in Photoshop" on any node whose `node.imgs` is non-empty.
 *
 * Menu registration: the modern frontend invokes **both**
 * `node.getExtraMenuOptions` (via litegraph's own `getNodeMenuOptions`) and
 * every registered extension's `getNodeMenuItems` for the same right-click —
 * confirmed by reading `ComfyUI_frontend`
 * `src/composables/useContextMenuTranslation.ts`, which wraps
 * `LGraphCanvas.prototype.getNodeMenuOptions` to call the original (which
 * itself calls `node.getExtraMenuOptions?.()`) and
 * `app.collectNodeMenuItems(node)` and concatenates the results. Registering
 * both hooks would therefore duplicate every item. `app.collectNodeMenuItems`
 * only exists on frontends that support the modern hook, so its presence is
 * used as the feature-detection gate: the legacy `getExtraMenuOptions`
 * monkeypatch is installed only when that method is absent (older
 * frontends, where it is the sole mechanism).
 *
 * Image-upload-widget detection mirrors the core `Comfy.UploadImage`
 * extension (`src/extensions/core/uploadImage.ts`): scan the raw node
 * definition's `input.required`/`input.optional` for an entry whose options
 * carry `image_upload`/`video_upload`/`animated_image_upload`, captured once
 * per node type in `beforeRegisterNodeDef`. A live-widget-name check is kept
 * as a fallback for node types that somehow bypass that hook.
 */

import { app } from '../../../scripts/app.js'
import * as api from './api.js'
import * as state from './state.js'
import * as ui from './ui.js'

/** Node type names (`nodeData.name`, mirrored by `node.comfyClass`/`node.type`) known to carry an image-upload-style widget. */
const imageUploadNodeTypes = new Set()

/** Marks a patched prototype so a node-def reload never chains the monkeypatch through itself twice. */
const LEGACY_PATCH_FLAG = '__cpsbGetExtraMenuOptionsPatched'

const MAX_BATCH_OPEN = 8

/**
 * @returns {boolean} Whether this frontend supports the declarative
 * `getNodeMenuItems` extension hook.
 */
function supportsGetNodeMenuItems() {
  return typeof app.collectNodeMenuItems === 'function'
}

/**
 * Called from `beforeRegisterNodeDef` for every node type at startup.
 * Records whether this type has an image-upload-style input so
 * {@link deriveOriginKind} can classify it as `load_image` without needing a
 * live node instance.
 * @param {import('../../../scripts/app.js').ComfyNodeDef} nodeData
 */
export function captureImageUploadType(nodeData) {
  const name = nodeData?.name
  if (!name) return
  const groups = [nodeData?.input?.required, nodeData?.input?.optional]
  for (const group of groups) {
    if (!group) continue
    for (const inputSpec of Object.values(group)) {
      const options = Array.isArray(inputSpec) ? inputSpec[1] : undefined
      if (
        options &&
        (options.image_upload || options.video_upload || options.animated_image_upload)
      ) {
        imageUploadNodeTypes.add(name)
        return
      }
    }
  }
}

/**
 * Fallback for node types not captured by {@link captureImageUploadType}
 * (e.g. a type registered through a path that skips `beforeRegisterNodeDef`).
 * Mirrors the widget-name convention `pasteFromClipspace` itself relies on
 * (every load-image-style node names its file combo `"image"`).
 * @param {import('../../../scripts/app.js').LGraphNode} node
 * @returns {boolean}
 */
function nodeHasImageUploadWidget(node) {
  return !!node.widgets?.some((w) => w.name === 'image' && w.type !== 'button')
}

/**
 * @param {import('../../../scripts/app.js').LGraphNode} node
 * @returns {import('./api.js').CpsbOriginKind} Always `load_image` or
 * `terminal_output` — `bridge_node` is only ever assigned server-side, when
 * the handoff is created by a Photoshop Bridge node's own execution, never
 * from a menu click (see PROTOCOL.md §6).
 */
export function deriveOriginKind(node) {
  const typeKey = node.comfyClass || node.type
  if (typeKey && imageUploadNodeTypes.has(typeKey)) return 'load_image'
  if (nodeHasImageUploadWidget(node)) return 'load_image'
  return 'terminal_output'
}

/**
 * @param {import('./api.js').CpsbApiError["body"]} body - 503 response body.
 * @returns {string}
 */
function describeUnavailable(body) {
  if (body && typeof body === 'object' && body.error) return String(body.error)
  return 'Neither Photoshop (Tier 1) nor the Photoshop panel plugin (Tier 2) is available.'
}

let remoteBrowsingNoted = false

/**
 * One-time-per-session heads-up when the page is browsed via a non-local
 * hostname (e.g. `--listen` + LAN address): Photoshop opens on the SERVER's
 * machine. Informational only — never gates anything (PROTOCOL.md §7); on
 * the common same-machine-via-LAN-address setup the note is simply harmless.
 */
function maybeNoteRemoteBrowsing() {
  if (remoteBrowsingNoted || !state.isRemoteBrowsingLikely()) return
  remoteBrowsingNoted = true
  ui.showToast({
    severity: 'info',
    summary: 'Photoshop opens on the ComfyUI server’s machine',
    detail:
      'You’re browsing ComfyUI via a network address. If Photoshop runs on a ' +
      'different machine than the ComfyUI server, install the Photoshop panel plugin.'
  })
}

/**
 * POSTs `/cpsb/open` for a single image on a node, handling the 409
 * existing-handoff response with the Edit Original / Start Fresh chooser.
 * @param {import('../../../scripts/app.js').LGraphNode} node
 * @param {"new" | "original" | "fresh"} mode
 * @param {number} [imageIndex]
 */
async function openInPhotoshop(node, mode, imageIndex = node.imageIndex ?? 0) {
  const ref = api.parseImageRef(node.imgs?.[imageIndex]?.src)
  if (!ref) {
    ui.showToast({
      severity: 'error',
      summary: 'Could not open in Photoshop',
      detail: 'This image has no resolvable file reference.'
    })
    return
  }
  const body = {
    filename: ref.filename,
    subfolder: ref.subfolder,
    type: ref.type,
    origin_node_id: String(node.id),
    origin_kind: deriveOriginKind(node),
    workflow_name: state.getWorkflowName(),
    mode
  }
  try {
    await api.openHandoff(body)
    // Toast only after the POST succeeds — the 409 path below shows its own
    // chooser instead (and the recursive re-call toasts exactly once), and a
    // 503/other failure must not be preceded by a false "Opening…".
    ui.showToast({
      severity: 'info',
      summary: 'Opening in Photoshop…',
      detail: 'ComfyUI will watch this file and bring back your edits automatically.'
    })
    maybeNoteRemoteBrowsing()
    // No further UI here by design — the cpsb.status/cpsb.updated events
    // drive the node badge (badges.js) and the gallery (gallery.js).
  } catch (error) {
    if (error instanceof api.CpsbApiError && error.status === 409) {
      const choice = await ui.chooseDialog({
        title: 'Already editing this image',
        message:
          'An edit is already in progress for this node. Continue editing ' +
          'the same Photoshop document, or start over from the current image?',
        choices: [
          { label: 'Edit Original', value: 'original', primary: true },
          { label: 'Start Fresh', value: 'fresh' }
        ]
      })
      if (choice === 'original' || choice === 'fresh') {
        await openInPhotoshop(node, choice, imageIndex)
      }
      return
    }
    if (error instanceof api.CpsbApiError && error.status === 503) {
      ui.showToast({
        severity: 'error',
        summary: 'Photoshop not available',
        detail: describeUnavailable(error.body)
      })
      return
    }
    ui.showToast({
      severity: 'error',
      summary: 'Failed to open in Photoshop',
      detail: error instanceof Error ? error.message : String(error)
    })
  }
}

/**
 * "Open all N in Photoshop": opens every image on the node without
 * interrupting the batch with a per-image dialog — an image that already
 * has an active handoff is silently re-opened (`mode: "original"`) instead
 * of popping the Edit Original / Start Fresh chooser N times.
 * @param {import('../../../scripts/app.js').LGraphNode} node
 */
async function openAllInPhotoshop(node) {
  const count = node.imgs.length
  const originKind = deriveOriginKind(node)
  const workflowName = state.getWorkflowName()
  let opened = 0
  for (let i = 0; i < count; i++) {
    const ref = api.parseImageRef(node.imgs[i]?.src)
    if (!ref) continue
    const body = {
      filename: ref.filename,
      subfolder: ref.subfolder,
      type: ref.type,
      origin_node_id: String(node.id),
      origin_kind: originKind,
      workflow_name: workflowName,
      mode: 'new'
    }
    try {
      await api.openHandoff(body)
      opened++
    } catch (error) {
      if (error instanceof api.CpsbApiError && error.status === 409) {
        try {
          await api.openHandoff({ ...body, mode: 'original' })
          opened++
          continue
        } catch (retryError) {
          api.debugLog('batch re-open (original) failed', retryError)
        }
      } else {
        api.debugLog('batch open failed for image', i, error)
      }
    }
  }
  const failed = count - opened
  ui.showToast({
    severity: failed > 0 ? 'warn' : 'info',
    summary: `Opening ${opened} of ${count} images in Photoshop…`,
    detail: failed > 0 ? `${failed} could not be opened — see console for details.` : undefined
  })
}

/**
 * Builds the context-menu items for one node. Shared by both registration
 * paths (the modern `getNodeMenuItems` hook and the legacy
 * `getExtraMenuOptions` monkeypatch).
 *
 * When `state.js` already knows neither tier is reachable (Tier 1 gated by
 * the server or the PROTOCOL.md §7 client-side non-localhost check, and no
 * Tier 2 plugin connected), the items are shown disabled with an inline
 * reason rather than left clickable to fail every time — `tier2Connected`
 * in particular is updated instantly by the dedicated `cpsb.tier2` event, so
 * this is not meaningfully stale. This is deliberately a courtesy, not the
 * source of truth: `/cpsb/open` still authoritatively decides per request
 * (see the 503 handling in {@link openInPhotoshop}), since a menu built from
 * slightly-stale client state could otherwise under- or over-disable.
 * @param {import('../../../scripts/app.js').LGraphNode} node
 * @returns {import('../../../scripts/app.js').IContextMenuValue[]}
 */
export function getNodeMenuItems(node) {
  if (!node.imgs?.length) return []

  const items = []
  const activeHandoff = state.getActiveHandoffForNode(String(node.id))
  const tierInfo = state.getTierInfo()
  const unavailable = !tierInfo.tier1Effective && !tierInfo.tier2Connected
  const suffix = unavailable ? ' (unavailable)' : ''

  if (activeHandoff) {
    items.push({
      content: `Edit Original in Photoshop${suffix}`,
      disabled: unavailable,
      callback: () => openInPhotoshop(node, 'original')
    })
    items.push({
      content: `Start Fresh in Photoshop${suffix}`,
      disabled: unavailable,
      callback: () => openInPhotoshop(node, 'fresh')
    })
  } else {
    items.push({
      content: `Open in Photoshop${suffix}`,
      disabled: unavailable,
      callback: () => openInPhotoshop(node, 'new')
    })
  }

  const count = node.imgs.length
  if (count >= 2 && count <= MAX_BATCH_OPEN) {
    items.push({
      content: `Open all ${count} in Photoshop${suffix}`,
      disabled: unavailable,
      callback: () => openAllInPhotoshop(node)
    })
  }

  return items
}

/**
 * Installs the legacy `getExtraMenuOptions` monkeypatch on a node
 * prototype — only when {@link supportsGetNodeMenuItems} is false, and only
 * once per prototype (a node-def reload calls `beforeRegisterNodeDef` again
 * for already-registered types, which would otherwise chain the patch
 * through itself and duplicate menu items on every reload).
 * @param {typeof import('../../../scripts/app.js').LGraphNode} nodeType
 */
export function installLegacyFallback(nodeType) {
  if (supportsGetNodeMenuItems()) return
  const proto = nodeType.prototype
  if (proto[LEGACY_PATCH_FLAG]) return
  proto[LEGACY_PATCH_FLAG] = true

  const original = proto.getExtraMenuOptions
  proto.getExtraMenuOptions = function (canvas, options) {
    const result = original?.call(this, canvas, options)
    const items = getNodeMenuItems(this)
    if (items.length) {
      if (options.length) options.push(null)
      options.push(...items)
    }
    return result
  }
}
