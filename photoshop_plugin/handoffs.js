/**
 * @file The document <-> handoff registry (docs/PROTOCOL.md §3, §5;
 * PLAN.md §5): tracks which open Photoshop documents correspond to which
 * ComfyUI handoffs, drives the open/cancel side of the websocket protocol,
 * and is the shared entry point the save listener and both "Send back now"
 * actions (the plugin command and the panel's per-document button) use to
 * run the export+upload pipeline.
 *
 * Persistence across plugin reloads is intentionally NOT implemented: if
 * the plugin or Photoshop restarts, this registry starts empty. Documents
 * still open from a prior session are simply no longer tracked — the
 * user's recourse is "Edit Original" from the ComfyUI side, which issues a
 * fresh `open_handoff` and re-establishes the mapping. This matches
 * PLAN.md §5's own scoping ("persistence of the map across plugin reloads
 * NOT required") and keeps this module's state as plain in-memory maps with
 * no on-disk format of its own to keep in sync with the backend.
 */

const { app, core } = require('photoshop')
const uxp = require('uxp')
const { localFileSystem, formats } = uxp.storage

const { connection, pathToFileUrl } = require('./connection.js')
const { logInfo, logWarn, logError, describeError } = require('./log.js')
const { runExport } = require('./exporter.js')
const { uploadEdit } = require('./uploader.js')

/**
 * @typedef {'editing' | 'exporting' | 'sent' | 'export_failed' | 'upload_failed'} CpsbLocalHandoffStatus
 * Local, panel-facing status for a tracked document — distinct from (and
 * coarser than) the server's own `meta.json` status enum (PROTOCOL.md §1);
 * this plugin only ever deals with documents it has successfully opened.
 */

/**
 * @typedef {Object} CpsbHandoffRecord
 * @property {string} handoffId
 * @property {'local' | 'remote'} mode
 * @property {string | null} path - Normalized local path (local mode only).
 * @property {number | null} documentId - Photoshop's own document id, set as
 * soon as `open_handoff` succeeds, in both modes.
 * @property {string} docTitle
 * @property {CpsbLocalHandoffStatus} status
 */

/** @type {Map<string, CpsbHandoffRecord>} handoffId -> record */
const byHandoffId = new Map()
/** @type {Map<string, CpsbHandoffRecord>} normalized local path -> record */
const byPath = new Map()
/** @type {Map<number, CpsbHandoffRecord>} Photoshop documentId -> record */
const byDocumentId = new Map()

/**
 * Fires a `change` event (detail: the current handoff list) whenever the
 * registry gains/loses an entry or a record's status changes, for the
 * panel to re-render.
 */
const registryEvents = new EventTarget()

/** @returns {void} */
function notifyChanged() {
  registryEvents.dispatchEvent(new CustomEvent('change', { detail: getActiveHandoffs() }))
}

/**
 * @param {string} path
 * @returns {string}
 */
function normalizePath(path) {
  return path.replace(/\\/g, '/')
}

/** @type {Promise<import('uxp').storage.Folder> | null} */
let sandboxFolderPromise = null

/**
 * The plugin-data folder remote-mode handoffs are downloaded into. Lazily
 * created once and reused; `getDataFolder()` is documented as persistent
 * across host-app version upgrades, so this folder (and any leftover files
 * in it from a prior session) can outlive one Photoshop run.
 * @returns {Promise<import('uxp').storage.Folder>}
 */
function getHandoffsSandboxFolder() {
  if (!sandboxFolderPromise) {
    sandboxFolderPromise = (async () => {
      const root = await localFileSystem.getDataFolder()
      try {
        return await root.getEntry('handoffs')
      } catch (_error) {
        return await root.createFolder('handoffs')
      }
    })()
  }
  return sandboxFolderPromise
}

/**
 * Handles a server `open_handoff` command: opens the PSD directly (local
 * mode, shared filesystem) or downloads it into the plugin's sandbox first
 * (remote mode), records the document<->handoff mapping, and replies
 * `opened` / `open_failed` (docs/PROTOCOL.md §3).
 * @param {import('./connection.js').CpsbOpenHandoffMessage} msg
 * @returns {Promise<void>}
 */
async function openHandoff(msg) {
  const handoffId = msg.handoff_id
  try {
    const isLocal = connection.getState().localMode === true
    const doc = isLocal ? await openLocal(msg.psd_path) : await openRemote(handoffId)
    /** @type {CpsbHandoffRecord} */
    const record = {
      handoffId,
      mode: isLocal ? 'local' : 'remote',
      path: isLocal ? normalizePath(msg.psd_path) : null,
      documentId: doc.id,
      docTitle: doc.title,
      status: 'editing'
    }
    byHandoffId.set(handoffId, record)
    if (record.path) byPath.set(record.path, record)
    byDocumentId.set(doc.id, record)
    connection.send({ type: 'opened', handoff_id: handoffId, document_id: doc.id })
    logInfo(`opened handoff ${handoffId} as "${doc.title}" (${record.mode} mode)`)
    notifyChanged()
  } catch (error) {
    const message = describeError(error)
    logError(`open_handoff failed for ${handoffId}: ${message}`)
    connection.send({ type: 'open_failed', handoff_id: handoffId, error: message })
  }
}

/**
 * Local-mode open: the plugin and ComfyUI share a filesystem, so this opens
 * `source.psd` directly at its real path.
 * @param {string} psdPath - Absolute path from `open_handoff.psd_path`.
 * @returns {Promise<import('photoshop').Document>}
 */
async function openLocal(psdPath) {
  const entry = await localFileSystem.getEntryWithUrl(pathToFileUrl(psdPath))
  return core.executeAsModal(() => app.open(entry), { commandName: 'ComfyUI: open handoff' })
}

/**
 * Remote-mode open: no shared filesystem, so the PSD bytes are downloaded
 * over the plugin's websocket (`connection.requestFile`, PROTOCOL.md §3:
 * `request_file`/`file_chunk`/`file_error`) and written into this plugin's
 * own sandbox folder before opening — a plain Cmd/Ctrl+S then saves in
 * place to that sandbox copy exactly as in Tier 1 (PLAN.md §5).
 *
 * This used to be a plain HTTP `fetch` GET of `file_url` (`/cpsb/file/<id>`)
 * — UXP's runtime blocks cleartext `http://` to a non-localhost host (the
 * whole reason cross-machine use needs this fix at all: the plugin's
 * control `ws://` connection to a remote ComfyUI works fine, but an HTTP
 * `fetch()` to that same remote host does not), so the download now rides
 * the identical, already-open websocket instead of a second HTTP call.
 * @param {string} handoffId
 * @returns {Promise<import('photoshop').Document>}
 */
async function openRemote(handoffId) {
  const bytes = await connection.requestFile(handoffId)
  const folder = await getHandoffsSandboxFolder()
  const file = await folder.createFile(`${handoffId}.psd`, { overwrite: true })
  await file.write(bytes.buffer, { format: formats.binary })
  return core.executeAsModal(() => app.open(file), { commandName: 'ComfyUI: open handoff' })
}

/**
 * Handles a server `handoff_cancelled` command: stops tracking the
 * document, if any. Does not touch the open Photoshop document itself —
 * the user keeps whatever they were doing, ComfyUI just no longer expects
 * an edit back for it.
 * @param {import('./connection.js').CpsbHandoffCancelledMessage} msg
 * @returns {void}
 */
function cancelHandoff(msg) {
  const record = byHandoffId.get(msg.handoff_id)
  if (!record) return
  byHandoffId.delete(record.handoffId)
  if (record.path) byPath.delete(record.path)
  if (record.documentId != null) byDocumentId.delete(record.documentId)
  logInfo(`handoff ${record.handoffId} cancelled — no longer tracking "${record.docTitle}"`)
  notifyChanged()
}

connection.addEventListener('message', (event) => {
  const msg = /** @type {CustomEvent} */ (event).detail
  if (msg.type === 'open_handoff') {
    // openHandoff never rejects (it catches internally and replies
    // open_failed), so this fire-and-forget call is safe.
    openHandoff(msg)
  } else if (msg.type === 'handoff_cancelled') {
    cancelHandoff(msg)
  }
})

/**
 * @param {number} documentId
 * @returns {CpsbHandoffRecord | undefined}
 */
function findByDocumentId(documentId) {
  return byDocumentId.get(documentId)
}

/**
 * @param {string} path
 * @returns {CpsbHandoffRecord | undefined}
 */
function findByPath(path) {
  return byPath.get(normalizePath(path))
}

/**
 * Drops any tracked handoff whose Photoshop document has been CLOSED since we
 * opened it (the "clear when no longer active" behavior). A record with no
 * `documentId` yet (mid-open) is left alone so an in-flight open isn't pruned.
 * Silent — no `notifyChanged()` — so it's safe to call from inside a render
 * (which `getActiveHandoffs` is); the manual clear path notifies explicitly.
 * @returns {number} How many were pruned.
 */
function pruneClosedHandoffs() {
  let removed = 0
  for (const record of Array.from(byHandoffId.values())) {
    if (record.documentId != null && findOpenDocument(record) == null) {
      byHandoffId.delete(record.handoffId)
      if (record.path) byPath.delete(record.path)
      byDocumentId.delete(record.documentId)
      removed += 1
    }
  }
  return removed
}

/**
 * @returns {CpsbHandoffRecord[]} Currently tracked handoffs, closed-document
 * ones pruned first so the panel never accretes stale rows for documents the
 * user already closed.
 */
function getActiveHandoffs() {
  pruneClosedHandoffs()
  return Array.from(byHandoffId.values())
}

/**
 * Forgets ALL tracked handoffs (the panel's manual "Clear" button). Local
 * only — the ComfyUI server's own handoff records are untouched (they age out
 * via its cleanup window); this just stops the plugin from tracking/listing
 * them. A subsequent save on a since-forgotten document simply won't
 * auto-deliver (re-open from ComfyUI to track it again).
 * @returns {void}
 */
function clearAllHandoffs() {
  byHandoffId.clear()
  byPath.clear()
  byDocumentId.clear()
  logInfo('cleared all tracked handoffs (panel Clear)')
  notifyChanged()
}

/**
 * @param {CpsbHandoffRecord} record
 * @returns {import('photoshop').Document | null} The live Document object
 * for this record, or null if it's no longer among `app.documents` (closed
 * since we opened it).
 */
function findOpenDocument(record) {
  if (record.documentId != null) {
    for (let i = 0; i < app.documents.length; i++) {
      if (app.documents[i].id === record.documentId) return app.documents[i]
    }
  }
  return null
}

/**
 * Runs the full "send this document's current state back to ComfyUI"
 * pipeline for a tracked handoff: an immediate `save_detected` notification
 * (informational — docs/PROTOCOL.md §3), then the export pipeline
 * (exporter.js) and the multipart upload (uploader.js). Shared by the save
 * listener (automatic, on a real Photoshop save) and both "Send back now"
 * entry points (the plugin command and the panel's per-document button) —
 * neither of those callers needs to know this sequence exists.
 * @param {string} handoffId
 * @returns {Promise<void>}
 */
async function deliverEdit(handoffId) {
  const record = byHandoffId.get(handoffId)
  if (!record) {
    logWarn(`deliverEdit: unknown handoff ${handoffId}`)
    return
  }
  const doc = findOpenDocument(record)
  if (!doc) {
    logWarn(
      `deliverEdit: document for handoff ${handoffId} ("${record.docTitle}") is no longer open`
    )
    return
  }
  connection.send({ type: 'save_detected', handoff_id: handoffId })
  record.status = 'exporting'
  notifyChanged()
  try {
    const pngBytes = await runExport(doc)
    const uploaded = await uploadEdit(handoffId, pngBytes)
    record.status = uploaded ? 'sent' : 'upload_failed'
    if (uploaded) {
      logInfo(`sent edit for handoff ${handoffId} ("${record.docTitle}")`)
    }
  } catch (error) {
    record.status = 'export_failed'
    logError(`export failed for handoff ${handoffId} ("${record.docTitle}"): ${describeError(error)}`)
  }
  notifyChanged()
}

module.exports = {
  registryEvents,
  openHandoff,
  cancelHandoff,
  findByDocumentId,
  findByPath,
  getActiveHandoffs,
  clearAllHandoffs,
  deliverEdit
}
