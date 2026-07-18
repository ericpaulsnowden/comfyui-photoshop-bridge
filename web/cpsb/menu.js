/**
 * @file Context-menu integration (PROTOCOL.md §8). Registers ONE top-level
 * "Open in Photoshop" item on any node whose `node.imgs` is non-empty — or,
 * as of PROTOCOL.md §6b, any `PhotoshopLoadPSD` node regardless of
 * `node.imgs` (see {@link getNodeMenuItems}'s own doc comment and
 * loadpsd.js) — it acts directly (plus a sibling "Open all N in Photoshop"
 * when applicable) when the node has no active handoff, or opens a submenu
 * containing "Edit Original in Photoshop" / "Start Fresh in Photoshop" (+
 * "Open all N", when applicable) when one exists.
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
 * ROOT CAUSE of "Edit Original"/"Start Fresh" appearing separated by other
 * packs' items in a user's screenshot: the whole node context menu is ONE
 * flat, unnamespaced array assembled by concatenating three independently-
 * ordered sources, with no grouping/section-header mechanism at any stage —
 * verified end to end in `ComfyUI_frontend`:
 *   1. `LGraphCanvas.prototype.getNodeMenuOptions` (`LGraphCanvas.ts` ~line
 *      8537-8660) builds litegraph's native items (Properties/Title/Mode/
 *      Colors/Shapes/Remove/…), then at ~line 8625-8628 calls
 *      `node.getExtraMenuOptions?.(this, options)` — the classic, still
 *      fully-supported per-node hook every legacy-style pack (including our
 *      own `installLegacyFallback` below, and, going by the reported
 *      screenshot, evidently pysssss/KJNodes' relevant features too) chains
 *      onto — and PREPENDS its result before the native items
 *      (`options = extra.concat(options)`), i.e. every `getExtraMenuOptions`
 *      contributor's items land at the very TOP of the menu, in prototype-
 *      patch chain order (which pack patched a given node type's prototype
 *      last — itself a function of custom_nodes directory scan order, not
 *      anything any one pack controls).
 *   2. `useContextMenuTranslation.ts`'s `getNodeMenuOptionsWithExtensions`
 *      (~line 68-93) then appends `app.collectNodeMenuItems(node)` — every
 *      extension's `getNodeMenuItems(node)` hook result, our own included —
 *      AFTER all of the above (so after native items AND every
 *      `getExtraMenuOptions` contributor, i.e. at the very BOTTOM by
 *      default). `collectNodeMenuItems` itself (`app.ts` ~line 2149-2153) is
 *      `invokeExtensions('getNodeMenuItems', node).flat()` — a flat
 *      concatenation in EXTENSION-REGISTRATION order, again not ours to
 *      control, and, critically, ANOTHER bucket a pack's items can land in
 *      independently of its `getExtraMenuOptions` items (an actively
 *      maintained pack can easily have both legacy and modern-hook code for
 *      different features).
 *   3. Finally `legacyMenuCompat.extractLegacyItems(...)` appends items from
 *      any pack that monkeypatched `LGraphCanvas.prototype.getNodeMenuOptions`
 *      itself (canvas-level, yet another independent bucket).
 * Litegraph's `ContextMenu` (`ContextMenu.ts` `addItem`, ~line 240-383)
 * then renders every entry in that single merged array as an equally-styled
 * flat row — no per-extension header, indentation, or divider. So even
 * though OUR OWN two items are always contiguous with each other (both
 * always come from one call to `getNodeMenuItems` below, in whichever of
 * the two buckets we're using — never both, see above), THEIR shared
 * position in that undifferentiated, registration-order-dependent list can
 * land anywhere relative to every other pack's equally-flat, equally
 * ungrouped items. Two flat top-level rows with nothing visually anchoring
 * them together will always read as "scattered among everything else" in a
 * 15-25 row menu — this is inherent to the flat single-array design, not a
 * bug in any one contributing pack, and no ordering trick fixes it (we
 * don't control global registration order, nor do other packs). The durable
 * fix is structural: collapse to ONE top-level entry, which — regardless of
 * how many unrelated rows land above, below, or (for other packs, if they
 * also emit ≥2 flat rows) *between* whatever surrounds our old position —
 * can no longer itself be "split apart", because its two actions now only
 * ever render together, in their own popout, on hover/click of that single
 * row.
 *
 * Submenu mechanism (`IContextMenuValue.has_submenu`/`.submenu`) verified
 * against `interfaces.ts` (~line 447-474: `submenu?: {options, callback?,
 * title?, extra?, …}`, `options` being the same array shape the top-level
 * menu itself takes) and `ContextMenu.ts` `addItem`/`inner_onclick`
 * (~line 273: either `submenu` or `has_submenu` adds the `.has_submenu` CSS
 * class + `aria-haspopup`; ~line 308-315: pointer-hover auto-open
 * (`inner_over`) checks `has_submenu` specifically, so BOTH fields are
 * needed together for full native-feeling behaviour; ~line 363-376: a click
 * with `value.submenu` set constructs a new `ContextMenu` from
 * `value.submenu.options` — this is item-SHAPE-driven, with no branch
 * anywhere on which mechanism contributed the item). Since both our
 * registration paths (the modern `getNodeMenuItems` hook and the legacy
 * `getExtraMenuOptions` fallback below) ultimately feed the exact same
 * merged array into this one rendering pipeline, a `has_submenu`/`submenu`
 * item behaves identically either way — confirmed, not assumed; no
 * adjacent-flat-items fallback is needed for the legacy path.
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
import * as compose from './compose.js'
import * as loadpsd from './loadpsd.js'
import * as open from './open.js'
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
 * @returns {import('./api.js').CpsbOriginKind} `load_image`, `load_psd`, or
 * `terminal_output` — `bridge_node` is only ever assigned server-side, when
 * the handoff is created by a Photoshop Bridge node's own execution, never
 * from a menu click (see PROTOCOL.md §6).
 */
export function deriveOriginKind(node) {
  // Explicit allowlist entry (PROTOCOL.md §6b): Load PSD's `psd` COMBO is a
  // plain combo, not one flagged `image_upload`/etc. in its INPUT_TYPES —
  // its upload affordance is the hand-rolled widget loadpsd.js attaches
  // from nodeCreated, which {@link captureImageUploadType}'s node-def scan
  // (below) has no way to see. Checked before the generic detection so this
  // node type is never accidentally reclassified as `load_image` by the
  // `nodeHasImageUploadWidget` fallback.
  if (loadpsd.isLoadPsdNode(node)) return 'load_psd'
  const typeKey = node.comfyClass || node.type
  if (typeKey && imageUploadNodeTypes.has(typeKey)) return 'load_image'
  if (nodeHasImageUploadWidget(node)) return 'load_image'
  return 'terminal_output'
}

/**
 * The `{filename, subfolder, type}` a right-click open request should use
 * for *node*. A Load PSD node has no `node.imgs` to read from at all in the
 * common case (PROTOCOL.md §6b: its hand-rolled widget bypasses the stock
 * image-preview pipeline entirely — see loadpsd.js's header) — its file
 * lives on the `psd` COMBO widget's value instead, always `type: "input"`.
 * A Compose node (`PhotoshopComposePSD`) likewise never populates
 * `node.imgs` (it returns plain `IMAGE`/`MASK`/`STRING` tensors, not a
 * SaveImage-style UI preview) — its file is whatever `compose.js`'s
 * `cpsb.compose_written` handler last recorded on the node (build brief item
 * 4: reopening a "Don't open" run's output). Checked BEFORE the `node.imgs`
 * fallback so a stale/absent `node.imgs` on a Compose node never masks a
 * genuinely known written file. Every other node type keeps the original
 * `node.imgs`-based lookup.
 * @param {import('../../../scripts/app.js').LGraphNode} node
 * @param {number} imageIndex
 * @returns {import('./api.js').CpsbImageRef | null}
 */
function resolveImageRef(node, imageIndex) {
  if (loadpsd.isLoadPsdNode(node)) return loadpsd.getPsdFileRef(node)
  if (compose.isComposePsdNode(node)) return compose.getWrittenFileRef(node)
  return api.parseImageRef(node.imgs?.[imageIndex]?.src)
}

/**
 * Every currently-displayed image on *node*, parsed to
 * `{filename, subfolder, type}` — unlike {@link resolveImageRef} (which
 * addresses exactly one `node.imgs` slot by index, for building an actual
 * open request), this is used by {@link activeHandoffMatchesNode} to check
 * a candidate source against ALL of a batch node's slots at once: batches
 * are real, and the slot that matches a given handoff's source is not
 * necessarily index 0.
 * @param {import('../../../scripts/app.js').LGraphNode} node
 * @returns {import('./api.js').CpsbImageRef[]}
 */
function getAllCurrentImageRefs(node) {
  if (!Array.isArray(node.imgs)) return []
  const refs = []
  for (let i = 0; i < node.imgs.length; i++) {
    const ref = api.parseImageRef(node.imgs[i]?.src)
    if (ref) refs.push(ref)
  }
  return refs
}

/**
 * @param {import('./api.js').CpsbImageRef | null | undefined} a
 * @param {import('./api.js').CpsbImageRef | null | undefined} b
 * @returns {boolean}
 */
function sameSourceRef(a, b) {
  return (
    !!a &&
    !!b &&
    a.filename === b.filename &&
    (a.subfolder || '') === (b.subfolder || '') &&
    (a.type || '') === (b.type || '')
  )
}

/**
 * The source-identity gate for the Edit-Original/Start-Fresh submenu (field
 * report, verbatim: "Images I didn't expect on my mac had both edit
 * original and start fresh in the menu. And if you click edit original on
 * an image that doesn't have an original it will open a different psd and
 * show a different preview"). `state.getActiveHandoffForNode` only matches
 * on `origin_node_id` + workflow + status (see its own doc comment) — NOTHING
 * there checks that the handoff's SOURCE FILE is still what the node
 * currently shows. Node ids get reused across sessions/workflows (and an
 * empty workflow name is a wildcard on BOTH sides — unsaved workflows), so a
 * handoff that is genuinely still server-side "active" (a round trip from an
 * earlier session, or one belonging to an entirely different unsaved
 * workflow) can latch onto whatever node now happens to reuse that id. This
 * is the check that keeps such a handoff from ever reaching the menu as if
 * it were this node's own — see {@link getNodeMenuItems} for where it's
 * applied and what "rejected" falls back to.
 *
 * Origin-kind-specific because each kind's "identity" lives in a different
 * place:
 * - `load_psd`: the node has no `node.imgs` at all (loadpsd.js's header —
 *   its hand-rolled widget bypasses the stock image-preview pipeline
 *   entirely), so identity is the `psd` COMBO's CURRENT selection
 *   ({@link loadpsd.getPsdFileRef}) vs. the handoff's recorded `source`.
 * - `load_image` / `terminal_output`: identity is whichever image(s) the
 *   node currently displays — checked against EVERY slot in `node.imgs` via
 *   {@link getAllCurrentImageRefs}, not just index 0 (batches are real).
 * - `bridge_node` (PhotoshopBridge / Compose / Annotate — PROTOCOL.md §6c):
 *   deliberately NO gate, always matches. These handoffs are created by the
 *   node's own backend execution, never by a menu click ({@link
 *   deriveOriginKind} never itself produces `bridge_node`), a Compose node
 *   in particular never populates `node.imgs` at all (compose.js's header),
 *   and staleness for this kind is already handled server-side
 *   (`cpsb/routes.py`'s `_resolve_source`/source_hash-based auto-supersede
 *   on `mode:"new"`, and `HandoffManager.find_active_for_node`'s own
 *   workflow-scoped node-id lookup) — node identity IS the correct key for
 *   this kind. Gating on `node.imgs` here would incorrectly hide the
 *   submenu for every Compose/Annotate/Bridge node with a real edit in
 *   flight (build brief item 4: `MODE_DONT_OPEN` re-open relies on exactly
 *   this "no node.imgs, still a real file" state).
 * - Unknown/missing `origin_kind`, or a handoff with no recorded `source` at
 *   all: fails OPEN (treated as still matching). A server newer or older
 *   than this frontend build must never silently make the submenu vanish —
 *   the worst case on a false positive here is the pre-existing staleness
 *   bug this function exists to fix, not a new failure mode; the server's
 *   own 409 chooser (`open.js`) remains the authoritative backstop
 *   regardless of what the menu guesses.
 * @param {import('../../../scripts/app.js').LGraphNode} node
 * @param {import('./api.js').CpsbHandoffMeta} meta
 * @returns {boolean}
 */
function activeHandoffMatchesNode(node, meta) {
  if (!meta.origin_kind || !meta.source) return true // fail open — see doc comment above
  switch (meta.origin_kind) {
    case 'load_psd': {
      const current = loadpsd.getPsdFileRef(node)
      return !!current && current.filename === meta.source.filename
    }
    case 'load_image':
    case 'terminal_output':
      return getAllCurrentImageRefs(node).some((ref) => sameSourceRef(ref, meta.source))
    case 'bridge_node':
      return true
    default:
      return true // unknown future origin_kind — fail open, see doc comment above
  }
}

/**
 * Opens a single image on a node via the shared `open.js` flow — the 409
 * Edit Original / Start Fresh chooser, the PROTOCOL.md §2/§7 428
 * remote-open confirm, and server-message error toasts all live there
 * (`open.openInteractive`), shared verbatim with gallery.js's card actions.
 * This function only contributes what is menu-specific: deriving the
 * request body from the clicked node.
 *
 * For a Load PSD node, the body also carries two node-widget-derived fields —
 * for every mode ('new'/'original'/'fresh' all funnel through here), since the
 * backend only ever consults them for a `load_psd` open (`cpsb/routes.py`) and
 * ignores them otherwise; every other node type simply never sends them:
 * - `edit_in_place` — the node's `edit_original` widget (PROTOCOL.md §6b
 *   "Edit-original option", `loadpsd.getEditOriginal`).
 * - `trigger_policy` — the node's `on_save` widget (PROTOCOL.md §6b
 *   "Save-trigger policy", `loadpsd.getOnSave`), which decides whether a save
 *   in Photoshop re-runs the workflow, only ingests, or is ignored outright.
 *   Both are read at OPEN time and persisted onto the handoff, so changing the
 *   widget on an ALREADY-open handoff has no effect until the next open —
 *   the same contract `edit_in_place` has always had. The field is OMITTED
 *   entirely when the widget is absent (a workflow saved before the option
 *   existed) so the server applies its own default rather than this file
 *   duplicating that string.
 * @param {import('../../../scripts/app.js').LGraphNode} node
 * @param {"new" | "original" | "fresh"} mode
 * @param {number} [imageIndex]
 */
async function openInPhotoshop(node, mode, imageIndex = node.imageIndex ?? 0) {
  const ref = resolveImageRef(node, imageIndex)
  if (!ref) {
    ui.showToast({
      severity: 'error',
      summary: 'Could not open in Photoshop',
      detail: loadpsd.isLoadPsdNode(node)
        ? 'Upload a PSD/PSB file to this node first.'
        : compose.isComposePsdNode(node)
          ? 'Run this node at least once first.'
          : 'This image has no resolvable file reference.'
    })
    return
  }
  const isLoadPsd = loadpsd.isLoadPsdNode(node)
  const onSave = isLoadPsd ? loadpsd.getOnSave(node) : undefined
  await open.openInteractive({
    filename: ref.filename,
    subfolder: ref.subfolder,
    type: ref.type,
    origin_node_id: String(node.id),
    origin_kind: deriveOriginKind(node),
    workflow_name: state.getWorkflowName(),
    mode,
    ...(isLoadPsd ? { edit_in_place: loadpsd.getEditOriginal(node) } : {}),
    ...(onSave === undefined ? {} : { trigger_policy: onSave })
  })
}

/**
 * "Open all N in Photoshop": opens every image on the node without
 * interrupting the batch with a per-image dialog — an image that already
 * has an active handoff is silently re-opened (`mode: "original"`) instead
 * of popping the Edit Original / Start Fresh chooser N times. The
 * PROTOCOL.md §2 remote-open confirm is likewise resolved at most ONCE for
 * the whole batch, not per image: client locality can't change between
 * images in the same call, and {@link open.openWithRemoteConfirm} persists
 * an "allow" to `localStorage` before the next iteration's request even
 * goes out, so only the first 428 in the loop can ever show the dialog. If
 * the user declines it, {@link open.RemoteOpenCancelled} aborts the whole
 * batch immediately rather than continuing to the next image.
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
      await open.openWithRemoteConfirm(body)
      opened++
    } catch (error) {
      if (error instanceof open.RemoteOpenCancelled) break
      if (error instanceof api.CpsbApiError && error.status === 409) {
        try {
          await open.openWithRemoteConfirm({ ...body, mode: 'original' })
          opened++
          continue
        } catch (retryError) {
          if (retryError instanceof open.RemoteOpenCancelled) break
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
 * Builds the context-menu item(s) for one node. Shared by both registration
 * paths (the modern `getNodeMenuItems` hook and the legacy
 * `getExtraMenuOptions` monkeypatch) — and, per this file's header, a
 * `has_submenu`/`submenu` item built here renders identically in both, so
 * neither path needs its own variant.
 *
 * Always exactly ONE top-level entry (see this file's header for why: the
 * shared node-menu array has no grouping mechanism, so two-or-more flat
 * siblings from the same extension read as scattered among every other
 * pack's equally-flat items). With no active handoff it acts directly
 * ("Open in Photoshop", plus a sibling "Open all N" when applicable — there
 * is nothing to disambiguate, so no submenu is needed). With an active
 * handoff, the SAME top-level label opens a submenu grouping "Edit
 * Original"/"Start Fresh" (plus "Open all N" when applicable) — the two
 * choices a 409 from `/cpsb/open` would otherwise force the user through a
 * dialog to make (see {@link openInPhotoshop}'s 409 handling), now reachable
 * directly from the menu instead.
 *
 * When `state.js` already knows neither tier is reachable (Tier 1 gated by
 * the server or the PROTOCOL.md §7 client-side non-localhost check, and no
 * Tier 2 plugin connected), every item — including the top-level one, which
 * also disables its submenu, since drilling into an all-disabled submenu
 * would serve no purpose — is shown disabled with an inline reason rather
 * than left clickable to fail every time. `tier2Connected` in particular is
 * updated instantly by the dedicated `cpsb.tier2` event, so this is not
 * meaningfully stale. This is deliberately a courtesy, not the source of
 * truth: `/cpsb/open` still authoritatively decides per request (see the 503
 * handling in {@link openInPhotoshop}), since a menu built from
 * slightly-stale client state could otherwise under- or over-disable.
 *
 * The `node.imgs?.length` gate below is allowlist-extended for Load PSD
 * nodes (PROTOCOL.md §6b): that node type's hand-rolled upload widget never
 * populates `node.imgs` (see loadpsd.js's header — it deliberately doesn't
 * hook into the stock image-preview pipeline), so without this exception a
 * freshly-added Load PSD node would never offer "Open in Photoshop" at all.
 * It is likewise allowlist-extended for a `PhotoshopComposePSD` node that has
 * RECORDED a written filename (`compose.hasWrittenFile`, populated by the
 * `cpsb.compose_written` event `compose.js` subscribes to) — build brief
 * item 4: a Compose node run in `MODE_DONT_OPEN` ("Don't open (composite
 * only)") never opens Photoshop and never populates `node.imgs` either, so
 * without this it would have no way to re-open the file it already wrote.
 * `count` below naturally falls back to 0 for both allowlisted cases, so the
 * "Open all N" batch item — a concept that doesn't apply to either a
 * single-file COMBO or a single composed PSD — simply never appears for
 * them, no extra branching needed there. The re-open itself goes through the
 * exact same `mode: 'new'` → `open.openInteractive` → `POST /cpsb/open` path
 * as every other node's right-click "Open in Photoshop" ({@link
 * openInPhotoshop}/{@link resolveImageRef}), so it inherits the same
 * client-locality confirm and Tier 1/Tier 2 handling for free.
 * @param {import('../../../scripts/app.js').LGraphNode} node
 * @returns {import('../../../scripts/app.js').IContextMenuValue[]}
 */
export function getNodeMenuItems(node) {
  const hasComposeWrittenFile = compose.isComposePsdNode(node) && compose.hasWrittenFile(node)
  if (!node.imgs?.length && !loadpsd.isLoadPsdNode(node) && !hasComposeWrittenFile) return []

  // state.getActiveHandoffForNode only matches on node id + workflow +
  // status (see its own doc comment) -- activeHandoffMatchesNode is the
  // additional source-identity gate that keeps a handoff belonging to a
  // DIFFERENT image (stale session, or a different unsaved workflow) from
  // ever being offered as "this node's" Edit Original / Start Fresh. A
  // rejected match is treated exactly like "no active handoff" below.
  const candidateHandoff = state.getActiveHandoffForNode(String(node.id))
  const activeHandoff =
    candidateHandoff && activeHandoffMatchesNode(node, candidateHandoff)
      ? candidateHandoff
      : undefined
  const tierInfo = state.getTierInfo()
  const unavailable = !tierInfo.tier1Effective && !tierInfo.tier2Connected
  const suffix = unavailable ? ' (unavailable)' : ''

  const count = node.imgs?.length ?? 0
  const batchItem =
    count >= 2 && count <= MAX_BATCH_OPEN
      ? {
          content: `Open all ${count} in Photoshop${suffix}`,
          disabled: unavailable,
          callback: () => openAllInPhotoshop(node)
        }
      : null

  if (!activeHandoff) {
    const items = [
      {
        content: `Open in Photoshop${suffix}`,
        disabled: unavailable,
        callback: () => openInPhotoshop(node, 'new')
      }
    ]
    if (batchItem) items.push(batchItem)
    return items
  }

  const submenuOptions = [
    {
      content: `Edit Original in Photoshop${suffix}`,
      disabled: unavailable,
      callback: () => openInPhotoshop(node, 'original')
    },
    {
      content: `Start Fresh in Photoshop${suffix}`,
      disabled: unavailable,
      callback: () => openInPhotoshop(node, 'fresh')
    }
  ]
  if (batchItem) submenuOptions.push(batchItem)

  return [
    {
      content: `Open in Photoshop${suffix}`,
      disabled: unavailable,
      has_submenu: true,
      submenu: { options: submenuOptions }
    }
  ]
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
