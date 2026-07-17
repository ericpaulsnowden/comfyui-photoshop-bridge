/**
 * @file Hand-rolled upload widget for the "Load PSD" node (`PhotoshopLoadPSD`,
 * PROTOCOL.md §6b): a button widget + hidden file input + node-level
 * drag-and-drop, all funneling into a raw POST to ComfyUI's own
 * `/upload/image` and a refresh of the node's `psd` COMBO widget. Required
 * because ComfyUI's stock IMAGEUPLOAD widget cannot accept `.psd`/`.psb` at
 * all — confirmed in `Comfy-Org/ComfyUI_frontend`
 * `src/utils/mediaUploadUtil.ts`: `ACCEPTED_IMAGE_TYPES =
 * 'image/png,image/jpeg,image/webp'`, hardcoded and threaded straight into
 * both the file-input `accept` attribute and a `fileFilter` that silently
 * drops anything else (`src/renderer/extensions/vueNodes/widgets/composables/useImageUploadWidget.ts`,
 * `src/composables/node/useNodeImageUpload.ts`) — there is no input-spec
 * option that overrides it. `research-psd-loading.md` §1 independently
 * confirms this and cites the proven bypass pattern this file follows:
 * leafiy/comfyui_psd_smart_object `web/js/psd_mockup_upload.js`.
 *
 * Verified against `Comfy-Org/ComfyUI_frontend` (frontend v1.48.3 @
 * 3be998a, 2026-07-17):
 * - Upload request/response shape: every current core caller of
 *   `/upload/image` (`src/composables/node/useNodeImageUpload.ts`
 *   `uploadFile`, `src/extensions/core/load3d/Load3dUtils.ts` `uploadFile`,
 *   `src/extensions/core/webcamCapture.ts`) POSTs `multipart/form-data` via
 *   `api.fetchApi` with no `Content-Type` header set (the browser fills in
 *   the boundary itself), and reads the JSON response's `.name` field — NOT
 *   `.filename`, unlike every `/cpsb/*` route in this package. See api.js's
 *   `uploadInputFile` (added alongside this file) for where that detail is
 *   centralized.
 * - Combo refresh: `src/utils/litegraphUtil.ts` `addToComboValues` — push
 *   the new filename into `widget.options.values` if not already present;
 *   the caller then sets `.value` and fires `.callback` itself
 *   (`useImageUploadWidget.ts`'s `onUploadComplete`). Not importable as-is
 *   (an internal `@/utils/...` path, unreachable from this package's `web/`
 *   bundle — see api.js's own header for why this package only ever
 *   imports from `../../../scripts/app.js` / `api.js`), so
 *   {@link addComboValue} below is a from-scratch equivalent, not a copy.
 * - Node-level drag-and-drop is a real, non-Vue-only mechanism:
 *   `src/scripts/app.ts` `addDropHandler`'s canvas-wide `dragover`/`drop`
 *   listeners call `graph.getNodeOnPos(...)` then `node.onDragOver`/
 *   `node.onDragDrop` directly — the same kind of plain instance-level hook
 *   this package's badges.js already chains for `onDrawForeground` et al.
 *   A truthy `onDragOver` also sets `app.dragOverNode`, which
 *   `src/services/litegraphService.ts` `setupStrokeStyles` already renders
 *   as a dodgerblue node-border highlight for ANY node automatically — so
 *   this file adds no drag-over visual of its own.
 * - `ComfyExtension.init` vs `.setup` (`src/types/comfy.ts` doc comments;
 *   call order confirmed in `src/scripts/app.ts` `ComfyApp.setup()`):
 *   `init` fires after the canvas exists but before any node TYPE is even
 *   registered, let alone any node instance created; `setup` fires later
 *   still, but both complete before a restored workflow's own nodes are
 *   deserialized (that happens outside `ComfyApp.setup()` entirely, driven
 *   by workflow-persistence code once the app shell is up). {@link init}
 *   below uses the `init` hook — the more semantically-correct of the two
 *   per its own doc comment ("initialisation, e.g. loading resources") —
 *   for a one-time feature-detection check that must complete before the
 *   first possible `nodeCreated` call; either hook would in fact run early
 *   enough, but `init` is the one actually meant for this.
 *
 * The `psd` COMBO widget itself is NOT created here — it comes from the
 * node's own `INPUT_TYPES` (backend, out of scope per "YOU OWN web/ ONLY"),
 * the same way LoadImage's own file combo does. This file only adds the
 * extra upload affordance and reads/writes that widget's `.value` /
 * `.options.values`.
 */

import * as api from './api.js'
import * as ui from './ui.js'

/**
 * Class id for the Load PSD node (PROTOCOL.md §6b: "unique id — plain
 * 'LoadPSD' collides with other packs"). The backend (`cpsb/nodes.py`, out
 * of scope for this file) registers the node class under this exact
 * string via `NODE_CLASS_MAPPINGS`; it must match verbatim.
 */
export const LOAD_PSD_NODE_TYPE = 'PhotoshopLoadPSD'

/** Extensions this widget accepts (PROTOCOL.md §6b). */
const ACCEPTED_EXTENSIONS = ['.psd', '.psb']

/** `<input type="file" accept="...">` value, comma-joined per the HTML spec. */
const ACCEPT_ATTR = ACCEPTED_EXTENSIONS.join(',')

/** Guards {@link init}'s capability warning so it is logged at most once. */
let capabilityWarned = false

/**
 * Whether this browser's File/FormData APIs — everything the upload path
 * needs — are present. Set once by {@link init}; every ComfyUI-supported
 * browser has both, so this only ever goes false in some future/embedded
 * WebView this package has not been tested against.
 */
let uploadSupported = true

/**
 * @param {import('../../../scripts/app.js').LGraphNode} node
 * @returns {boolean}
 */
export function isLoadPsdNode(node) {
  return (node.comfyClass || node.type) === LOAD_PSD_NODE_TYPE
}

/**
 * @param {import('../../../scripts/app.js').LGraphNode} node
 * @returns {import('../../../scripts/app.js').IBaseWidget | undefined} The
 * node's `psd` COMBO widget (PROTOCOL.md §6b), if present. Excluding
 * `type === 'button'` keeps this from ever matching {@link attachUploadWidget}'s
 * own upload button even if some future revision happened to name it the
 * same — defensive, mirrors menu.js's `nodeHasImageUploadWidget`.
 */
export function getPsdWidget(node) {
  return node.widgets?.find((w) => w.name === 'psd' && w.type !== 'button')
}

/**
 * @param {import('../../../scripts/app.js').LGraphNode} node
 * @returns {import('./api.js').CpsbImageRef | null} The currently-selected
 * PSD, or `null` when the node has no psd widget yet or nothing is selected
 * (an empty combo before the first upload — expected for a freshly-added
 * Load PSD node). Always `subfolder: ""`, `type: "input"`: every value this
 * widget can ever hold was itself just uploaded to the root of the input
 * directory (see {@link ingestFile}), exactly mirroring how LoadImage's own
 * combo lists bare filenames for root-level input files (PROTOCOL.md §6b:
 * "COMBO of .psd/.psb files in the input directory, refreshed like
 * LoadImage's combo").
 */
export function getPsdFileRef(node) {
  const widget = getPsdWidget(node)
  const filename = widget?.value
  if (!filename || typeof filename !== 'string') return null
  return { filename, subfolder: '', type: 'input' }
}

/**
 * @param {string | undefined} filename
 * @returns {boolean}
 */
function hasAcceptedExtension(filename) {
  const lower = filename?.toLowerCase?.() ?? ''
  return ACCEPTED_EXTENSIONS.some((ext) => lower.endsWith(ext))
}

/**
 * Pushes *value* into a combo widget's option list if not already present —
 * a from-scratch equivalent of the verified
 * `Comfy-Org/ComfyUI_frontend` `src/utils/litegraphUtil.ts`
 * `addToComboValues` (see this file's header for why it isn't imported
 * directly). Does not select *value* — callers set `.value` themselves.
 * @param {import('../../../scripts/app.js').IBaseWidget} widget
 * @param {string} value
 */
function addComboValue(widget, value) {
  if (!widget.options) widget.options = {}
  if (!Array.isArray(widget.options.values)) widget.options.values = []
  if (!widget.options.values.includes(value)) {
    widget.options.values.push(value)
  }
}

/**
 * Uploads *file* via {@link api.uploadInputFile}, then refreshes and selects
 * it on the psd COMBO widget. This is a different flow from pasteback.js's
 * `pasteToWidget`: that one lands an EDIT arriving from Photoshop on a
 * (possibly different) node's widget; this is the Load PSD node's OWN file
 * widget reacting to a direct user upload of the SOURCE file, so it lives
 * here rather than there.
 * @param {import('../../../scripts/app.js').LGraphNode} node
 * @param {import('../../../scripts/app.js').IBaseWidget} psdWidget
 * @param {import('../../../scripts/app.js').IBaseWidget} uploadWidget
 * @param {File} file
 */
async function ingestFile(node, psdWidget, uploadWidget, file) {
  if (!hasAcceptedExtension(file.name)) {
    ui.showToast({
      severity: 'error',
      summary: 'Not a PSD/PSB file',
      detail: `"${file.name}" — only .psd and .psb files are accepted.`
    })
    return
  }
  if (node.__cpsbPsdUploading) {
    // Mirrors Comfy-Org/ComfyUI_frontend's own guard for the stock upload
    // widget (src/composables/node/useNodeImageUpload.ts handleUploadBatch:
    // "Upload already in progress").
    ui.showToast({
      severity: 'warn',
      summary: 'A PSD upload is already in progress for this node'
    })
    return
  }

  node.__cpsbPsdUploading = true
  const originalLabel = uploadWidget.label
  uploadWidget.label = 'Uploading…'
  node.graph?.setDirtyCanvas(true, false)

  try {
    const ref = await api.uploadInputFile(file, { subfolder: '', type: 'input' })
    addComboValue(psdWidget, ref.filename)
    psdWidget.value = ref.filename
    try {
      psdWidget.callback?.(ref.filename)
    } catch (error) {
      api.warn('psd combo widget callback threw after a PSD upload', error)
    }
    node.graph?.setDirtyCanvas(true, false)
    ui.showToast({ severity: 'success', summary: 'PSD uploaded', detail: ref.filename })
  } catch (error) {
    api.warn(`PSD upload failed for node ${node.id}`, error)
    ui.showToast({
      severity: 'error',
      summary: 'PSD upload failed',
      detail: error instanceof Error ? error.message : String(error)
    })
  } finally {
    node.__cpsbPsdUploading = false
    uploadWidget.label = originalLabel
    node.graph?.setDirtyCanvas(true, false)
  }
}

/**
 * Creates a hidden `<input type="file">` scoped to one node, wired to call
 * *onSelect* with the chosen file. Not appended to `document.body`: a
 * detached `<input>` still opens the native file picker on `.click()` in
 * every browser ComfyUI itself targets — the same assumption
 * `Comfy-Org/ComfyUI_frontend` `src/composables/node/useNodeFileInput.ts`
 * relies on for the stock upload widget. Cleanup is chained onto
 * `node.onRemoved` the same way that file does, so this never leaks one
 * detached input per Load PSD node ever created in the session.
 * @param {import('../../../scripts/app.js').LGraphNode} node
 * @param {(file: File) => void} onSelect
 * @returns {() => void} Opens the native file picker.
 */
function createFileInput(node, onSelect) {
  let fileInput = document.createElement('input')
  fileInput.type = 'file'
  fileInput.accept = ACCEPT_ATTR
  fileInput.onchange = () => {
    const file = fileInput?.files?.[0]
    // Reset so re-selecting the exact same filename still fires onchange
    // (Comfy-Org/ComfyUI_frontend useNodeFileInput.ts does the same).
    if (fileInput) fileInput.value = ''
    if (file) onSelect(file)
  }

  const originalOnRemoved = node.onRemoved
  node.onRemoved = function () {
    if (fileInput) {
      fileInput.onchange = null
      fileInput = null
    }
    return originalOnRemoved?.call(this)
  }

  return () => fileInput?.click()
}

/**
 * @param {DragEvent} event
 * @returns {boolean} Whether the drag carries at least one file. Mirrors
 * `Comfy-Org/ComfyUI_frontend` `src/composables/node/useNodeDragAndDrop.ts`
 * `hasFiles` (`items[].kind === 'file'`) — deliberately NOT gated by MIME
 * type: `dataTransfer.items[].type` is a guess the OS/browser makes from
 * the file's extension, and research-psd-loading.md §1/UNCONFIRMED notes
 * the value for a dragged `.psd` is unverified and "unlikely image/*" —
 * gating on it here risks silently rejecting the exact files this widget
 * exists to accept. The real extension check happens in {@link ingestFile}
 * once `File.name` is available (drop time only — the spec keeps
 * `getAsFile`/`.files` unavailable during dragover).
 */
function isDraggingFiles(event) {
  const items = event?.dataTransfer?.items
  if (!items) return false
  return Array.from(items).some((item) => item.kind === 'file')
}

/**
 * Installs chained `onDragOver`/`onDragDrop` node hooks — verified live in
 * `Comfy-Org/ComfyUI_frontend` `src/scripts/app.ts` `addDropHandler` (see
 * this file's header) as a real, canvas-level mechanism, not a Vue-only
 * one, so it composes safely with every other chained node hook this
 * package installs (badges.js's `onDrawForeground`/`onMouseDown`/etc.).
 * @param {import('../../../scripts/app.js').LGraphNode} node
 * @param {(file: File) => void} onFile
 */
function installDragAndDrop(node, onFile) {
  const originalDragOver = node.onDragOver
  node.onDragOver = function (event) {
    if (isDraggingFiles(event)) return true
    return originalDragOver ? originalDragOver.call(this, event) : false
  }

  const originalDragDrop = node.onDragDrop
  node.onDragDrop = function (event) {
    const file = event?.dataTransfer?.files?.[0]
    if (file) {
      // Claimed unconditionally, even if the file turns out to be the wrong
      // extension (ingestFile shows its own toast for that): letting
      // ComfyUI's generic drop handling also see the same file afterward
      // would only risk a second, unrelated, more confusing failure path
      // for a file type it was never going to handle correctly anyway.
      onFile(file)
      return true
    }
    return originalDragDrop ? originalDragDrop.call(this, event) : false
  }
}

/**
 * Attaches the hand-rolled upload affordance to one `PhotoshopLoadPSD` node
 * instance: a button widget opening a hidden file input, plus node-level
 * drag-and-drop — both funneling into the same {@link ingestFile} upload +
 * combo-refresh path. Call from `nodeCreated`. Idempotent per node instance
 * (matching badges.js's `installBadgeHook` convention) and a no-op for any
 * other node type.
 * @param {import('../../../scripts/app.js').LGraphNode} node
 */
export function attachUploadWidget(node) {
  if (!isLoadPsdNode(node)) return
  if (node.__cpsbPsdUploadAttached) return
  node.__cpsbPsdUploadAttached = true

  if (!uploadSupported) return // already warned once, in init()

  const psdWidget = getPsdWidget(node)
  if (!psdWidget) {
    api.warn(
      `node ${node.id} is a ${LOAD_PSD_NODE_TYPE} but has no "psd" combo ` +
        'widget to attach the upload button to (PROTOCOL.md §6b)'
    )
    return
  }

  // uploadWidget is assigned immediately below; onFile/openFileDialog only
  // ever run later, in response to a user action (a click or a drop), by
  // which point the closure's captured `uploadWidget` binding is always set.
  /** @type {import('../../../scripts/app.js').IBaseWidget} */
  let uploadWidget
  const onFile = (file) => ingestFile(node, psdWidget, uploadWidget, file)
  const openFileDialog = createFileInput(node, onFile)

  uploadWidget = node.addWidget(
    'button',
    'psd_upload',
    'psd_upload',
    () => openFileDialog(),
    // canvasOnly matches the stock upload button's own choice
    // (useImageUploadWidget.ts) so this renders identically regardless of
    // which node-rendering mode (canvas vs. Vue) is active.
    { serialize: false, canvasOnly: true }
  )
  uploadWidget.label = 'Choose PSD to upload'

  installDragAndDrop(node, onFile)
}

/**
 * One-time feature-detection, called from cpsb.js's `init()` — the
 * `ComfyExtension.init` hook (see this file's header for why it, not
 * `setup()`, is the right place). `attachUploadWidget` reads the resulting
 * flag rather than re-checking per node, so a degraded environment gets
 * exactly one `[cpsb]` warning (this project's established
 * one-warning-per-degraded-feature convention — see api.js `warn`,
 * menu.js `warnLocalStorageUnavailable`, ui.js's toast/dialog fallbacks)
 * instead of one per Load PSD node.
 */
export function init() {
  uploadSupported = typeof File !== 'undefined' && typeof FormData !== 'undefined'
  if (!uploadSupported && !capabilityWarned) {
    capabilityWarned = true
    api.warn(
      'File/FormData are unavailable on this frontend — the Load PSD ' +
        'node’s upload button and drag-and-drop will not work'
    )
  }
}
