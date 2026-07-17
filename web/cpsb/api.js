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
 * @typedef {"load_image" | "terminal_output" | "bridge_node"} CpsbOriginKind
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
 * @typedef {Object} CpsbMaskFileRef
 * A mask file as recorded on a `meta.json` edit entry (PROTOCOL.md §1/§4).
 * Unlike {@link CpsbImageRef}, no `subfolder`/`type` is carried: the mask is
 * always written "beside the edit" it belongs to, i.e. into that edit's own
 * handoff folder — combine with {@link editSubfolder} for a full reference.
 * @property {string} filename
 */

/**
 * @typedef {Object} CpsbEdit
 * @property {string} filename - `edit_%03d.png`, arrival order.
 * @property {number} ts
 * @property {CpsbFidelity} fidelity
 * @property {CpsbSiblingOutput | null} [sibling_output]
 * @property {CpsbMaskFileRef | null} [mask] - The extracted-channel mask
 * recorded for this edit (PROTOCOL.md §1/§4), or absent/`null` when none was
 * extracted (no qualifying channel, Tier 2 remote mode, or a non-fatal
 * extraction failure). Missing entirely on edits recorded before this field
 * existed. Same handoff folder as `filename` — see {@link editSubfolder}.
 */

/**
 * @typedef {Object} CpsbHandoffMeta
 * Mirrors `meta.json` (PROTOCOL.md §1) exactly.
 * @property {string} handoff_id
 * @property {string} origin_node_id
 * @property {CpsbOriginKind} origin_kind
 * @property {string} workflow_name
 * @property {CpsbImageRef} source
 * @property {string | null} [source_hash]
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
 * @property {string} photoshop_path
 * @property {number} debounce_ms
 * @property {number} cleanup_days
 * @property {boolean} sibling_outputs
 * @property {string} managed_folder_name
 * @property {string} mask_channel_name
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
 * @property {CpsbImageRef | null} mask - Full reference to the extracted
 * mask image (PROTOCOL.md §5), or `null` when this edit had no extractable
 * mask (§4). Unlike {@link CpsbEdit.mask}, this carries the complete
 * `{filename, subfolder, type}` triple since the event payload must be
 * self-contained. Mask *consumption* is entirely backend-side (the
 * Photoshop Bridge node's MASK output, PROTOCOL.md §6) — this frontend only
 * surfaces the mask's presence (gallery.js's "MASK" chip); pasteback.js is
 * intentionally unchanged.
 */

/**
 * @typedef {Object} CpsbStatusEvent
 * Payload of the `cpsb.status` websocket message (PROTOCOL.md §5).
 * @property {string} handoff_id
 * @property {string} origin_node_id
 * @property {CpsbStatus} status
 */

/**
 * @typedef {Object} CpsbTier2Event
 * Payload of the `cpsb.tier2` websocket message (PROTOCOL.md §5).
 * @property {boolean} connected
 * @property {string | null} ps_version
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
 * Builds the URL for GET `/cpsb/file/{handoffId}` (the raw `source.psd`).
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
