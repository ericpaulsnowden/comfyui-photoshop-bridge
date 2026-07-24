/**
 * @file Thin client for every `/cpsb/*` HTTP route plus typed subscription
 * helpers for the `cpsb.*` websocket events, both defined in
 * `docs/PROTOCOL.md`. This is the only module that talks to the network or
 * touches `api.addEventListener` directly — everything else in `cpsb/`
 * depends on this file rather than on `../../../scripts/api.js` so the wire
 * format stays defined in exactly one place.
 *
 * Verified against `Comfy-Org/ComfyUI_frontend` (module paths, `fetchApi`,
 * `addEventListener`/`dispatchCustomEvent` semantics) — see the implementation
 * report for exact file/line references.
 */

import { api } from '../../../scripts/api.js'
import { FRONTEND_VERSION } from './version.js'

/**
 * Re-exported so every other `cpsb/` module reads this build's own version
 * through the same `import * as api from './api.js'` it already uses for
 * everything else (this file's header: "the only module that talks to the
 * network... everything else depends on this file"), instead of importing
 * `version.js` directly in three more places. Compare against
 * {@link CpsbStatusResponse}'s `server_version` for the PROTOCOL.md §9
 * version-mismatch check (state.js `getServerVersion` / cpsb.js `setup`).
 */
export { FRONTEND_VERSION }

/** Set to `true` locally to enable verbose [cpsb] console logging. */
export const DEBUG = false

/**
 * Logs only when {@link DEBUG} is true. Keeps the console clean for the
 * common case while leaving a single switch for troubleshooting.
 * @param {...unknown} args
 */
export function debugLog(...args) {
  if (DEBUG) console.log('[cpsb]', ...args)
}

/**
 * Always-on warning, used for the "one console.warn per degraded feature"
 * requirement when a ComfyUI frontend API is missing or behaves
 * unexpectedly.
 * @param {...unknown} args
 */
export function warn(...args) {
  console.warn('[cpsb]', ...args)
}

/**
 * @typedef {"input" | "output" | "temp"} CpsbFileType
 * Matches the `type` triple used by ComfyUI's own `/view` and `/upload/image`
 * routes (PROTOCOL.md §2).
 */

/**
 * @typedef {"load_image" | "terminal_output" | "bridge_node" | "load_psd" | "manual_send"} CpsbOriginKind
 */

/**
 * @typedef {"pending" | "editing" | "edited" | "cancelled" | "discarded" | "superseded" | "error"} CpsbStatus
 * "Stale" is intentionally absent — it is derived client-side (PROTOCOL.md
 * §1: `editing` and `updated_ts` older than 1h), never sent by the server.
 */

/**
 * @typedef {"composite" | "recomposite" | "plugin"} CpsbFidelity
 */

/**
 * @typedef {Object} CpsbImageRef
 * @property {string} filename
 * @property {string} subfolder
 * @property {CpsbFileType} type
 */

/**
 * @typedef {Object} CpsbSiblingOutput
 * @property {string} filename
 * @property {string} subfolder
 */

/**
 * @typedef {Object} CpsbEdit
 * @property {string} filename - `edit_%03d.png`, arrival order.
 * @property {number} ts
 * @property {CpsbFidelity} fidelity
 * @property {CpsbSiblingOutput | null} [sibling_output]
 */
// NOTE: mask-channel extraction was REMOVED from the protocol (PROTOCOL.md
// §4 removal note, owner's call 2026-07-17) — edits no longer carry a `mask`
// field, and this frontend reads none. A legacy `meta.json` written by a
// pre-removal backend may still contain one; it is simply ignored here.

/**
 * @typedef {Object} CpsbHandoffMeta
 * Mirrors `meta.json` (PROTOCOL.md §1) exactly.
 * @property {string} handoff_id
 * @property {string} origin_node_id
 * @property {CpsbOriginKind} origin_kind
 * @property {string} workflow_name
 * @property {CpsbImageRef} source
 * @property {string | null} [source_hash]
 * @property {string} [psd_filename] - The managed PSD copy's own on-disk
 * filename, DERIVED from the origin filename's stem (PROTOCOL.md §1, v0.5.26
 * — `Eric-Headshot.jpg` → `Eric-Headshot.psd`), not the literal `source.psd`
 * every handoff used before that change. Absent only on a `meta.json`
 * written before this field existed, which the backend itself reads back as
 * `source.psd` (`HandoffManager`'s own fallback) — so treat a missing value
 * here the same way. This frontend doesn't currently address the managed
 * PSD by name (thumbnails/edits go through {@link thumbUrl}/{@link viewUrl},
 * neither of which needs it), so the field is documented here for
 * completeness with `meta.json` rather than consumed anywhere yet.
 * @property {string | null} [managed_dir] - The `managed_folder_name`
 * (PROTOCOL.md §1/§2) in effect when this handoff was created — the actual
 * folder under `input/` its files live in, which is `null` only for a
 * handoff recovered from a `meta.json` predating this field. Use
 * {@link editSubfolder}, never a hardcoded literal, to build the subfolder
 * for one of this handoff's edits.
 * @property {number} created_ts
 * @property {number} updated_ts
 * @property {CpsbStatus} status
 * @property {string | null} error
 * @property {CpsbEdit[]} edits
 * @property {boolean} [edit_in_place] - Load PSD "edit original" handoffs
 * (PROTOCOL.md §6b): the handoff edits the user's real file in place rather
 * than a managed copy. Absent/false on every other handoff.
 * @property {string | null} [original_path] - Absolute path of the user's
 * PSD when `edit_in_place` is true; `null`/absent otherwise.
 * @property {boolean | null} [plugin_doc_open] - The Tier-2 plugin's own
 * report of whether this handoff's Photoshop document is still open
 * (gallery overhaul, 2026-07-22) — `true`/`false` once a plugin has reported
 * either way, `null`/absent when none ever has (Tier-1-only handoffs, or
 * before any plugin connected this session). `state.js`'s
 * `getDisplayStatus` reads this to derive a real "closed without saving"
 * signal for an `editing` handoff, replacing the old client-only elapsed-
 * time guess.
 */

/**
 * @typedef {Object} CpsbOpenRequest
 * @property {string} filename
 * @property {string} subfolder
 * @property {CpsbFileType} type
 * @property {string} origin_node_id
 * @property {CpsbOriginKind} origin_kind
 * @property {string} [workflow_name]
 * @property {"new" | "original" | "fresh"} mode
 * @property {boolean} [client_remote_ok] - Acknowledges the PROTOCOL.md §2/§7
 * client-locality gate; default `false` server-side when omitted. Only set
 * this `true` once the user has agreed to open Photoshop on the server's
 * machine (menu.js remembers that choice per-browser in `localStorage`).
 * @property {boolean} [edit_in_place] - Only meaningful when `origin_kind` is
 * `"load_psd"` (PROTOCOL.md §6b): open and edit the user's ACTUAL selected
 * PSD in place rather than a managed copy. Defaults `false` server-side when
 * omitted.
 */

/**
 * @typedef {Object} CpsbOpenResponse
 * @property {string} handoff_id
 * @property {1 | 2} tier
 * @property {"pending"} status
 */

/**
 * @typedef {Object} CpsbClientRemoteBody
 * Body of the 428 response from `/cpsb/open` (PROTOCOL.md §2/§7): the Tier 1
 * path would be used, but the requesting client isn't on the server's
 * machine and the request didn't set `client_remote_ok`.
 * @property {string} error
 * @property {"client_remote"} reason
 * @property {string} server_name - The server machine's hostname
 * (`platform.node()`), for the "Photoshop will open on <server_name>" confirm.
 */

/**
 * @typedef {Object} CpsbUploadResponse
 * @property {true} ok
 * @property {string} filename
 * @property {string} subfolder
 * @property {CpsbFileType} type
 */

/**
 * @typedef {Object} CpsbStatusResponse
 * @property {string} server_version - The backend's semver string
 * (PROTOCOL.md §2/§9). Compare against {@link FRONTEND_VERSION} for the
 * version-mismatch warning (state.js `getServerVersion` / cpsb.js `setup`).
 * @property {boolean} tier1_available
 * @property {string | null} tier1_reason
 * @property {boolean} tier2_connected
 * @property {string | null} ps_version
 * @property {CpsbHandoffMeta[]} handoffs - Newest first, max 200.
 */

/**
 * @typedef {Object} CpsbBackendSettings
 * Exactly the PROTOCOL.md §2 `GET/POST /cpsb/settings` object — no
 * `mask_channel_name`: it left the contract with the §4 mask-extraction
 * removal.
 * @property {string} photoshop_path
 * @property {number} debounce_ms
 * @property {number} cleanup_days
 * @property {boolean} sibling_outputs
 * @property {string} managed_folder_name
 */

/**
 * @typedef {Object} CpsbUpdatedEvent
 * Payload of the `cpsb.updated` websocket message (PROTOCOL.md §5).
 * @property {string} handoff_id
 * @property {string} origin_node_id
 * @property {CpsbOriginKind} origin_kind
 * @property {string} filename
 * @property {string} subfolder
 * @property {CpsbFileType} type
 * @property {CpsbFidelity} fidelity
 * @property {CpsbSiblingOutput | null} sibling_output
 */

/**
 * @typedef {Object} CpsbStatusEvent
 * Payload of the `cpsb.status` websocket message (PROTOCOL.md §5).
 * @property {string} handoff_id
 * @property {string} origin_node_id
 * @property {CpsbStatus} status
 * @property {boolean | null} [plugin_doc_open] - Mirrors
 * `CpsbHandoffMeta.plugin_doc_open` at emit time. Load-bearing for
 * `badges.js`: a `status: "editing"` event carrying `false` is a
 * document-CLOSED report (`document_closed` → `set_plugin_doc_open`), which
 * must clear the node badge, not (re)create it. Absent on events from a
 * backend predating the field — treated like `null`/unknown.
 */

/**
 * @typedef {Object} CpsbTier2Event
 * Payload of the `cpsb.tier2` websocket message (PROTOCOL.md §5).
 * @property {boolean} connected
 * @property {string | null} ps_version
 */

/**
 * @typedef {Object} CpsbComposeWrittenEvent
 * Payload of the `cpsb.compose_written` websocket message. NOT part of
 * `docs/PROTOCOL.md`'s handoff lifecycle -- this event carries no
 * `handoff_id`/`origin_kind` and implies no handoff at all. Emitted by
 * `PhotoshopComposePSD` (`cpsb/compose_psd.py`) immediately after it writes
 * a PSD to disk, for all three `mode` values alike, so the frontend can show
 * "Written: <filename>" on the node even in `MODE_DONT_OPEN` ("Don't open
 * (composite only)"), which never opens Photoshop and never creates a
 * handoff -- the only reason this event exists (closing the reported gap:
 * "for 'don't open' how do I later find and open the file?").
 * @property {string} node_id - The compose node's own id (matches
 * `String(node.id)`, NOT an `origin_node_id` of any handoff).
 * @property {string} filename - Bare filename (input-dir-relative
 * convention), suitable for `viewUrl`-style addressing but NOT a
 * server-side absolute path -- see {@link path} for that.
 * @property {string} path - The just-written file's FULL, absolute,
 * server-side path (`Path.resolve()`d by
 * `cpsb.compose_psd._emit_compose_written`), added so the frontend's "Copy
 * Path" button (`web/cpsb/compose.js`) can copy something a remote
 * (different-machine) user could actually use to find the file on the
 * ComfyUI machine, unlike the deliberately-bare {@link filename}.
 * @property {string} subfolder - Always `""` -- see
 * `cpsb.compose_psd._emit_compose_written`'s docstring for the one accepted
 * exception (an `existing_psd_path` override pointing outside `input/`).
 * @property {CpsbFileType} type - Always `"input"`.
 */

/**
 * @typedef {Object} CpsbBrowseDirEntry
 * A subdirectory (or browse root) entry from `GET /cpsb/fs/list`
 * (STANDARD-fs-browse.md).
 * @property {string} name
 * @property {string} [path] - Absolute, server-side. Present ONLY for a
 * `dir === "ROOTS"` listing's entries (each one is independently rooted, so
 * the client can't derive it by joining) — absent for a real directory's
 * entries, which are names-only; join with the response's own `dir` + `sep`.
 */

/**
 * @typedef {Object} CpsbBrowseFileEntry
 * A `.psd`/`.psb` file entry from `GET /cpsb/fs/list` (STANDARD-fs-browse.md)
 * — names-only, like {@link CpsbBrowseDirEntry}; join with the response's
 * own `dir` + `sep` for the full path.
 * @property {string} name
 * @property {number} size - Bytes.
 * @property {number} mtime - Unix seconds.
 */

/**
 * @typedef {Object} CpsbBrowseResponse
 * `GET /cpsb/fs/list`'s response shape (`cpsb/routes.py` `fs_list_route`) —
 * STANDARD-fs-browse.md, the cross-plugin "server filesystem Browse"
 * contract shared with cprb/epsnodes (migrated 2026-07-19 off the old
 * `GET /cpsb/browse`, `path` param, full-path-per-entry shape). Either a
 * real directory's contents, or (when `dir === "ROOTS"`) the virtual ROOTS
 * listing (the user's home directory, ComfyUI's own input directory, and
 * platform-specific drives/volumes) used by the "Browse..." dialog
 * (`web/cpsb/browse.js`) for `PhotoshopComposePSD.existing_psd_path`.
 * @property {string} dir - The resolved absolute directory, or the literal
 * `"ROOTS"` sentinel for the roots listing.
 * @property {string | null} parent - `null` for the roots listing, or when
 * `dir` is already a filesystem root (its own parent).
 * @property {string} sep - This server's `os.sep` — build/display paths with
 * this, never a hardcoded `/` or `\`, since the ComfyUI machine may not be
 * the same platform as the browser (PROTOCOL.md's two-machine setup).
 * @property {CpsbBrowseDirEntry[]} dirs - Sorted case-insensitively by name.
 * @property {CpsbBrowseFileEntry[]} files - `.psd`/`.psb` only (narrowable
 * via `ext`), sorted case-insensitively by name; always empty for the roots
 * listing.
 * @property {boolean} truncated - `true` iff this listing hit the server's
 * per-request entry cap.
 */

/**
 * Error thrown by every helper in this module for a non-2xx response.
 * Callers that need to branch on the exact status (e.g. the 409
 * existing-handoff response from `/cpsb/open`) should catch this type and
 * read {@link CpsbApiError#status} / {@link CpsbApiError#body}.
 */
export class CpsbApiError extends Error {
  /**
   * @param {string} message
   * @param {number} status
   * @param {unknown} body - Parsed JSON error body, or `null` if unparseable.
   */
  constructor(message, status, body) {
    super(message)
    this.name = 'CpsbApiError'
    this.status = status
    this.body = body
  }
}

/**
 * The most useful human-readable message an error can yield, for toast
 * `detail` fields. Preference order: the SERVER's own `{"error": ...}` body
 * message (PROTOCOL.md §2 — every `/cpsb/*` error carries one; this is what
 * `request()` already promotes into {@link CpsbApiError#message}, restated
 * here from `.body` so the guarantee is explicit and survives any future
 * message-mangling), then `Error#message`, then a string coercion. Every UI
 * error path (gallery.js, open.js) routes through this so a failure always
 * shows the real reason — "Source image not found: x.png", "Handoff is
 * cancelled, not accepting uploads" — never a bare generic failure line.
 * @param {unknown} error
 * @returns {string}
 */
export function errorMessage(error) {
  if (error instanceof CpsbApiError) {
    const body = error.body
    if (body && typeof body === 'object' && typeof body.error === 'string' && body.error) {
      return body.error
    }
    return error.message
  }
  if (error instanceof Error) return error.message
  return String(error)
}

/**
 * Issues a `/cpsb/*` request through `api.fetchApi` and normalizes errors.
 * @param {string} route - e.g. `"/cpsb/open"`.
 * @param {RequestInit} [options]
 * @returns {Promise<any>} Parsed JSON body.
 * @throws {CpsbApiError}
 */
async function request(route, options) {
  const response = await api.fetchApi(route, options)
  let body = null
  try {
    body = await response.json()
  } catch {
    body = null
  }
  if (!response.ok) {
    const message =
      (body && typeof body === 'object' && body.error) ||
      `${route} failed with HTTP ${response.status}`
    throw new CpsbApiError(message, response.status, body)
  }
  return body
}

/**
 * POST `/cpsb/open` — create (or re-open) a handoff and launch Photoshop.
 * On a 409 the returned error's `.body` is
 * `{error, existing_handoff_id}` (PROTOCOL.md §2); on a 428 it is
 * {@link CpsbClientRemoteBody}; on a 503 it is
 * `{error, tier1_available, tier2_connected}`.
 * @param {CpsbOpenRequest} body
 * @returns {Promise<CpsbOpenResponse>}
 * @throws {CpsbApiError}
 */
export async function openHandoff(body) {
  return request('/cpsb/open', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  })
}

/**
 * POST `/cpsb/upload` — deliver edited pixels for a handoff (manual
 * drag-and-drop import from the gallery; the UXP plugin uses this same route
 * but that path is not exercised by this frontend).
 * @param {string} handoffId
 * @param {File | Blob} file - PNG image data.
 * @param {"plugin" | "manual"} [source]
 * @returns {Promise<CpsbUploadResponse>}
 * @throws {CpsbApiError}
 */
export async function uploadEdit(handoffId, file, source = 'manual') {
  const formData = new FormData()
  formData.append('handoff_id', handoffId)
  formData.append('image', file, file.name || 'edit.png')
  formData.append('source', source)
  return request('/cpsb/upload', { method: 'POST', body: formData })
}

/**
 * POST `/upload/image` — ComfyUI's own core upload route, not a `/cpsb/*`
 * one, but centralized here anyway per this file's header ("the only
 * module that talks to the network... so the wire format stays defined in
 * exactly one place"). Used by loadpsd.js for the Load PSD node's
 * hand-rolled upload widget (PROTOCOL.md §6b): the stock IMAGEUPLOAD widget
 * hardcodes `accept="image/png,image/jpeg,image/webp"` and silently drops
 * anything else (`Comfy-Org/ComfyUI_frontend` `src/utils/mediaUploadUtil.ts`
 * `ACCEPTED_IMAGE_TYPES`), so a `.psd`/`.psb` can only reach the server
 * through this route, called directly — see loadpsd.js's header for the
 * full citation trail.
 *
 * Request/response shape verified against every current core caller of
 * this route (`src/composables/node/useNodeImageUpload.ts`,
 * `src/extensions/core/load3d/Load3dUtils.ts`,
 * `src/extensions/core/webcamCapture.ts`): `multipart/form-data` with an
 * `image` file part plus `subfolder`/`type` fields, no `Content-Type`
 * header set (the browser fills in the multipart boundary itself) —
 * `api.fetchApi` passes a `FormData` body through to `fetch` untouched, so
 * this works exactly like {@link uploadEdit} above. Reuses the same
 * `request()` helper, so a non-2xx response throws {@link CpsbApiError}
 * the same way every other function in this file does. The JSON response
 * is `{name, subfolder, type}` — notably `name`, NOT `filename`, unlike
 * every `/cpsb/*` response in this file; that mismatch is normalized away
 * here so callers only ever see this file's own {@link CpsbImageRef} shape.
 * @param {File} file
 * @param {{subfolder?: string, type?: CpsbFileType}} [options]
 * @returns {Promise<CpsbImageRef>}
 * @throws {CpsbApiError}
 */
export async function uploadInputFile(file, { subfolder = '', type = 'input' } = {}) {
  const formData = new FormData()
  formData.append('image', file, file.name || 'upload.psd')
  formData.append('subfolder', subfolder)
  formData.append('type', type)
  const data = await request('/upload/image', { method: 'POST', body: formData })
  return {
    filename: data?.name ?? file.name,
    subfolder: data?.subfolder ?? subfolder,
    type: /** @type {CpsbFileType} */ (data?.type ?? type)
  }
}

/**
 * POST `/cpsb/cancel/{handoffId}` — cancel a pending/editing handoff and
 * unblock any waiting Photoshop Bridge node.
 * @param {string} handoffId
 * @returns {Promise<{ok: true}>}
 * @throws {CpsbApiError}
 */
export async function cancelHandoff(handoffId) {
  return request(`/cpsb/cancel/${encodeURIComponent(handoffId)}`, {
    method: 'POST'
  })
}

/**
 * POST `/cpsb/discard/{handoffId}` — gallery "Discard" for a stale handoff.
 * @param {string} handoffId
 * @returns {Promise<{ok: true}>}
 * @throws {CpsbApiError}
 */
export async function discardHandoff(handoffId) {
  return request(`/cpsb/discard/${encodeURIComponent(handoffId)}`, {
    method: 'POST'
  })
}

/**
 * GET `/cpsb/status` — tier availability, plugin connection, and the full
 * handoff list. Used both for the initial `state.js` seed and for
 * re-synchronizing after any `cpsb.status` / `cpsb.updated` event.
 * @returns {Promise<CpsbStatusResponse>}
 * @throws {CpsbApiError}
 */
export async function getStatus() {
  return request('/cpsb/status', { method: 'GET' })
}

/**
 * GET `/cpsb/fs/list` — list a directory (or, with *dir* set to `"ROOTS"`,
 * the virtual browse roots; omitted/empty resolves to this pack's own
 * default directory) on the ComfyUI machine's filesystem. STANDARD-fs-
 * browse.md's cross-plugin contract (migrated 2026-07-19 off the old
 * `GET /cpsb/browse`, whose `path` param this function used to send).
 * Backs the "Browse..." dialog (`web/cpsb/browse.js`) for
 * `PhotoshopComposePSD.existing_psd_path`. Deliberately NOT gated by the
 * client-locality confirm `/cpsb/open` uses — see `cpsb/routes.py`'s
 * `fs_list_route`/its section header for why a read-only listing doesn't
 * carry that gate's "wrong machine" concern (cpsb's `FS_LIST_LOCAL_ONLY`
 * build-time flag is `False`).
 * @param {string} [dir] - Omit/empty for this pack's default directory, or
 * `"ROOTS"` for the virtual top-level listing.
 * @returns {Promise<CpsbBrowseResponse>}
 * @throws {CpsbApiError} 400 if *dir* is set but isn't `"ROOTS"` and isn't an
 * existing absolute directory.
 */
export async function browseDirectory(dir = '') {
  const params = new URLSearchParams()
  if (dir) params.set('dir', dir)
  const query = params.toString()
  return request(`/cpsb/fs/list${query ? `?${query}` : ''}`, { method: 'GET' })
}

/**
 * GET `/cpsb/settings` — backend-persisted settings (distinct from the
 * ComfyUI-settings-API `cpsb.*` frontend preferences in `settings.js`).
 * @returns {Promise<CpsbBackendSettings>}
 * @throws {CpsbApiError}
 */
export async function getBackendSettings() {
  return request('/cpsb/settings', { method: 'GET' })
}

/**
 * POST `/cpsb/settings` — merge partial updates into the backend-persisted
 * settings.
 * @param {Partial<CpsbBackendSettings>} partial
 * @returns {Promise<CpsbBackendSettings>}
 * @throws {CpsbApiError}
 */
export async function updateBackendSettings(partial) {
  return request('/cpsb/settings', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(partial)
  })
}

/**
 * `managed_folder_name`'s documented default (PROTOCOL.md §1/§2) — used only
 * as a last-resort fallback in {@link editSubfolder} for a `meta.json`
 * recovered before the `managed_dir` field existed (`managed_dir: null`).
 * Every handoff created going forward always carries its own `managed_dir`,
 * so this constant is never the primary source of truth, only a documented
 * fallback for old data — the managed folder itself remains fully
 * server-configurable and is never otherwise assumed by this frontend.
 */
const DEFAULT_MANAGED_FOLDER_NAME = 'photoshop'

/**
 * The subfolder every edit of *meta* lives under. All of a handoff's edits
 * share one on-disk folder for its whole lifetime (PROTOCOL.md §1), so this
 * is valid for any of `meta.edits`, not just the latest. Derived from the
 * handoff's own recorded `managed_dir` — never a hardcoded literal — so this
 * keeps working regardless of the server's configured `managed_folder_name`
 * (default `"photoshop"`, but admin-configurable and not necessarily
 * `"cpsb"`, a name this folder has never had in the current protocol).
 * @param {CpsbHandoffMeta} meta
 * @returns {string}
 */
export function editSubfolder(meta) {
  return `${meta.managed_dir || DEFAULT_MANAGED_FOLDER_NAME}/${meta.handoff_id}`
}

/**
 * Builds the URL for GET `/cpsb/thumb/{handoffId}` (the original-image
 * thumbnail, `orig_thumb.png`). Not fetched via `request()` since the
 * response is a PNG, not JSON — intended for direct use as an `<img src>`.
 * @param {string} handoffId
 * @returns {string}
 */
export function thumbUrl(handoffId) {
  return api.apiURL(`/cpsb/thumb/${encodeURIComponent(handoffId)}`)
}

/**
 * Builds the URL for GET `/cpsb/file/{handoffId}` (the handoff's raw managed
 * PSD file, whatever name the server derived for it).
 * Included for completeness with every route in PROTOCOL.md §2; this
 * frontend never fetches it itself — it exists solely for the UXP plugin's
 * remote-mode download.
 * @param {string} handoffId
 * @returns {string}
 */
export function fileUrl(handoffId) {
  return api.apiURL(`/cpsb/file/${encodeURIComponent(handoffId)}`)
}

/**
 * Builds a ComfyUI `/view` URL for an edited-image thumbnail
 * (`edit_00N.png`), per PROTOCOL.md §2 ("Edited-image thumbnails are fetched
 * via ComfyUI's own /view"). Not a `/cpsb/*` route, but the only other
 * endpoint this extension's UI needs to address an image.
 * @param {CpsbImageRef} ref
 * @returns {string}
 */
export function viewUrl({ filename, subfolder = '', type = 'input' }) {
  const params = new URLSearchParams({ filename, subfolder, type })
  return api.apiURL(`/view?${params.toString()}`)
}

/**
 * The inverse of {@link viewUrl}: parses `{filename, subfolder, type}` out
 * of a rendered `<img>`'s `src` (always a
 * `/view?filename=...&subfolder=...&type=...` URL for images ComfyUI itself
 * populated `node.imgs` from). Shared by `menu.js` (deriving the open
 * request from the clicked image) and `pasteback.js` (matching which slot of
 * a batch node an edit belongs to). Deliberately robust: reads whatever
 * query params exist rather than assuming a fixed order or that all three
 * are present.
 * @param {string | undefined} src
 * @returns {CpsbImageRef | null}
 */
export function parseImageRef(src) {
  if (!src) return null
  try {
    const url = new URL(src, window.location.href)
    const filename = url.searchParams.get('filename')
    if (!filename) return null
    return {
      filename,
      subfolder: url.searchParams.get('subfolder') || '',
      // ComfyUI's /view defaults to "output" when the param is omitted.
      type: /** @type {CpsbFileType} */ (url.searchParams.get('type') || 'output')
    }
  } catch (error) {
    debugLog('failed to parse image src as a URL', src, error)
    return null
  }
}

/**
 * Subscribes to the `cpsb.updated` websocket event (an edit arrived).
 * @param {(detail: CpsbUpdatedEvent) => void} callback
 * @returns {() => void} Unsubscribe function.
 */
export function onUpdated(callback) {
  /** @param {CustomEvent<CpsbUpdatedEvent>} event */
  const handler = (event) => callback(event.detail)
  api.addEventListener('cpsb.updated', handler)
  return () => api.removeEventListener('cpsb.updated', handler)
}

/**
 * Subscribes to the `cpsb.status` websocket event (handoff lifecycle
 * transition).
 * @param {(detail: CpsbStatusEvent) => void} callback
 * @returns {() => void} Unsubscribe function.
 */
export function onStatusChanged(callback) {
  /** @param {CustomEvent<CpsbStatusEvent>} event */
  const handler = (event) => callback(event.detail)
  api.addEventListener('cpsb.status', handler)
  return () => api.removeEventListener('cpsb.status', handler)
}

/**
 * @typedef {Object} CpsbLiveEvent
 * Payload of the `cpsb.live` websocket event (PROTOCOL.md §5, realtime
 * drawing M1/M2): a new live-drawing frame landed in the server's
 * keep-latest slot.
 * @property {number} seq - The server-side frame counter —
 * `PhotoshopLiveCanvas.IS_CHANGED`'s cache key.
 * @property {string} doc_title
 */

/**
 * Subscribes to the `cpsb.live` websocket event (a live-drawing frame
 * arrived — `live.js`'s coalesced auto-queue loop hangs off this).
 * @param {(detail: CpsbLiveEvent) => void} callback
 * @returns {() => void} Unsubscribe function.
 */
export function onLive(callback) {
  /** @param {CustomEvent<CpsbLiveEvent>} event */
  const handler = (event) => callback(event.detail)
  api.addEventListener('cpsb.live', handler)
  return () => api.removeEventListener('cpsb.live', handler)
}

/**
 * @typedef {Object} CpsbLivePromptEvent
 * Payload of the `cpsb.liveprompt` websocket event (PROTOCOL.md §5, realtime
 * drawing prompt control): the user edited the plugin panel's Live prompt
 * field. The text itself is deliberately NOT carried — `PhotoshopLivePrompt`
 * reads it server-side at execute time; consumers only need the nudge.
 * @property {boolean} has_prompt - Whether the panel field is now non-empty.
 */

/**
 * Subscribes to the `cpsb.liveprompt` websocket event (the panel prompt
 * changed — `live.js` re-queues on it just like a new frame).
 * @param {(detail: CpsbLivePromptEvent) => void} callback
 * @returns {() => void} Unsubscribe function.
 */
export function onLivePrompt(callback) {
  /** @param {CustomEvent<CpsbLivePromptEvent>} event */
  const handler = (event) => callback(event.detail)
  api.addEventListener('cpsb.liveprompt', handler)
  return () => api.removeEventListener('cpsb.liveprompt', handler)
}

/**
 * @typedef {Object} CpsbLiveCreativityEvent
 * Payload of the `cpsb.livecreativity` websocket event (realtime drawing
 * creativity slider): the user moved the preview panel's Creativity slider.
 * @property {number} creativity - The new value, 0.0..1.0. The node maps it
 * onto a denoise band server-side; consumers only need the re-render nudge.
 */

/**
 * Subscribes to the `cpsb.livecreativity` websocket event (the creativity
 * slider changed — `live.js` re-queues on it just like a frame or a prompt).
 * @param {(detail: CpsbLiveCreativityEvent) => void} callback
 * @returns {() => void} Unsubscribe function.
 */
export function onLiveCreativity(callback) {
  /** @param {CustomEvent<CpsbLiveCreativityEvent>} event */
  const handler = (event) => callback(event.detail)
  api.addEventListener('cpsb.livecreativity', handler)
  return () => api.removeEventListener('cpsb.livecreativity', handler)
}

/**
 * Subscribes to the `cpsb.tier2` websocket event (plugin connection state
 * changed).
 * @param {(detail: CpsbTier2Event) => void} callback
 * @returns {() => void} Unsubscribe function.
 */
export function onTier2Changed(callback) {
  /** @param {CustomEvent<CpsbTier2Event>} event */
  const handler = (event) => callback(event.detail)
  api.addEventListener('cpsb.tier2', handler)
  return () => api.removeEventListener('cpsb.tier2', handler)
}

/**
 * Subscribes to the `cpsb.compose_written` websocket event (a
 * `PhotoshopComposePSD` node just wrote a PSD to disk -- see
 * {@link CpsbComposeWrittenEvent}'s own doc comment for why this exists
 * outside the handoff lifecycle every other `on*` helper here concerns
 * itself with).
 * @param {(detail: CpsbComposeWrittenEvent) => void} callback
 * @returns {() => void} Unsubscribe function.
 */
export function onComposeWritten(callback) {
  /** @param {CustomEvent<CpsbComposeWrittenEvent>} event */
  const handler = (event) => callback(event.detail)
  api.addEventListener('cpsb.compose_written', handler)
  return () => api.removeEventListener('cpsb.compose_written', handler)
}
