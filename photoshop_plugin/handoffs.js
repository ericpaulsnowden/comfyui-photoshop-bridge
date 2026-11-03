/**
 * @file The document <-> handoff registry (docs/PROTOCOL.md §3, §5;
 * PLAN.md §5): tracks which open Photoshop documents correspond to which
 * ComfyUI handoffs, drives the open/cancel side of the websocket protocol,
 * and is the shared entry point the save listener and both "Send"
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
 *
 * {@link startDocumentCloseWatcher} (gallery overhaul, 2026-07-22) polls
 * every {@link DOC_CLOSE_CHECK_MS} whether each tracked document is still
 * open and tells the server the moment one closes (`document_closed`) — the
 * ComfyUI-side gallery's real "is this still open?" signal for a Tier-2
 * handoff, replacing an old client-only elapsed-time guess that had no
 * plugin involvement at all.
 */

const { app, core } = require('photoshop')
const uxp = require('uxp')
const { localFileSystem, formats } = uxp.storage

const { connection, pathToFileUrl } = require('./connection.js')
const { logInfo, logWarn, logError, describeError } = require('./log.js')
const { runExport } = require('./exporter.js')
const { uploadEdit, uploadLayeredPsd } = require('./uploader.js')

/**
 * @typedef {'editing' | 'exporting' | 'sent' | 'save_failed' | 'export_failed' | 'upload_failed'} CpsbLocalHandoffStatus
 * Local, panel-facing status for a tracked document — distinct from (and
 * coarser than) the server's own `meta.json` status enum (PROTOCOL.md §1);
 * this plugin only ever deals with documents it has successfully opened.
 * `save_failed` means a manual Send found unsaved changes on a handoff
 * whose delivery depends on the saved file (see
 * {@link deliveryDependsOnSavedFile}), could not save the document first,
 * and therefore sent NOTHING — never stale bytes.
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
 * @property {boolean} wantsLayeredPsd - Mirrors `open_handoff.wants_layered_psd`
 * (docs/PROTOCOL.md §3, §6d) — `true` for a Photoshop Annotate node handoff,
 * whose managed PSD copy carries a paintable "Instructions" layer.
 * Consulted by `deliverEdit` in two ways. In `'remote'` mode, `true` means
 * the save pipeline uploads `sandboxFile`'s own raw PSD bytes (`kind:
 * "psd"`) instead of running the flat-PNG export. In BOTH modes, `true`
 * means the handoff's real payload is the SAVED FILE (remote: the bytes the
 * plugin reads; local: the managed PSD the server's Annotate node re-opens
 * after the flat-PNG upload lands), so a manual Send saves an
 * unsaved document first ({@link deliveryDependsOnSavedFile}).
 * @property {import('uxp').storage.File | null} sandboxFile - The REMOTE-mode
 * sandbox copy `openRemote` downloaded into — the same file Photoshop's own
 * Cmd/Ctrl+S overwrites in place on every save — re-read directly for the
 * layered-PSD upload path (no export/flatten needed: Photoshop already
 * wrote the real, current, layered document there). `null` in `'local'`
 * mode (nothing was downloaded; `path` points at the real, shared-
 * filesystem file instead).
 * @property {string | null} lastError - The reason the most recent
 * `deliverEdit` stopped before sending (`status === 'save_failed'`), else
 * `null`. Cleared at the start of every delivery.
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
 * (remote mode), records the document<->handoff mapping (including
 * `msg.wants_layered_psd` and, in remote mode, the sandbox `File` entry
 * `deliverEdit` later re-reads for the layered-PSD upload path — docs/
 * PROTOCOL.md §6d), and replies `opened` / `open_failed` (docs/PROTOCOL.md
 * §3).
 * @param {import('./connection.js').CpsbOpenHandoffMessage} msg
 * @returns {Promise<void>}
 */
async function openHandoff(msg) {
  const handoffId = msg.handoff_id
  try {
    const isLocal = connection.getState().localMode === true
    const opened = isLocal
      ? { doc: await openLocal(msg.psd_path), file: null }
      : await openRemote(handoffId, msg.psd_path)
    const doc = opened.doc
    /** @type {CpsbHandoffRecord} */
    const record = {
      handoffId,
      mode: isLocal ? 'local' : 'remote',
      path: isLocal ? normalizePath(msg.psd_path) : null,
      documentId: doc.id,
      docTitle: doc.title,
      status: 'editing',
      wantsLayeredPsd: Boolean(msg.wants_layered_psd),
      sandboxFile: opened.file,
      lastError: null
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
 * the handoff's managed PSD copy directly at its real path -- whatever name
 * the server derived for it (product-owner requirement 2026-07-18: named
 * after the handoff's origin file, e.g. `Eric-Headshot.psd`, rather than the
 * literal `source.psd` every handoff used to get). This function never
 * hardcodes a filename at all, so it inherits the derived name automatically.
 * @param {string} psdPath - Absolute path from `open_handoff.psd_path`.
 * @returns {Promise<import('photoshop').Document>}
 */
async function openLocal(psdPath) {
  const entry = await localFileSystem.getEntryWithUrl(pathToFileUrl(psdPath))
  return core.executeAsModal(() => app.open(entry), { commandName: 'ComfyUI: open handoff' })
}

/**
 * Best-effort basename of a SERVER-side path string. `psd_path` in
 * `open_handoff` is built from the ComfyUI server's own filesystem
 * (`str(Path.resolve())`, Python) and may therefore use either `/` or `\`
 * separators depending on the server's OS -- REMOTE mode means this plugin
 * can be running on a DIFFERENT OS than the server, so both must be handled,
 * not just whichever separator this platform's own `path` module would use.
 * @param {string | undefined | null} serverPath
 * @returns {string | null} The final path segment, or `null` if *serverPath*
 * is missing, empty, or has no usable segment.
 */
function basenameOfServerPath(serverPath) {
  if (!serverPath) return null
  const segments = String(serverPath)
    .replace(/\\/g, '/')
    .split('/')
    .filter(Boolean)
  return segments.length ? segments[segments.length - 1] : null
}

/**
 * Gets (or lazily creates) a named subfolder of *parent*. Mirrors
 * `getHandoffsSandboxFolder`'s own get-or-create pattern.
 * @param {import('uxp').storage.Folder} parent
 * @param {string} name
 * @returns {Promise<import('uxp').storage.Folder>}
 */
async function getOrCreateSubfolder(parent, name) {
  try {
    return await parent.getEntry(name)
  } catch (_error) {
    return await parent.createFolder(name)
  }
}

/**
 * Remote-mode open: no shared filesystem, so the PSD bytes are downloaded
 * over the plugin's websocket (`connection.requestFile`, PROTOCOL.md §3:
 * `request_file`/`file_chunk`/`file_error`) and written into a PER-HANDOFF
 * subfolder of this plugin's own sandbox (`handoffs/<handoffId>/<basename>`)
 * before opening — a plain Cmd/Ctrl+S then saves in place to that sandbox
 * copy exactly as in Tier 1 (PLAN.md §5).
 *
 * The written filename is *psdPath*'s own basename (product-owner requirement
 * 2026-07-18: name the file after its origin, e.g. `Eric-Headshot.psd`, so
 * Photoshop's document TITLE is no longer the opaque handoff id) — falling
 * back to `${handoffId}.psd` (the old behavior) when *psdPath* is missing or
 * has no usable basename. The PER-HANDOFF subfolder (rather than writing
 * straight into the shared `handoffs` sandbox root, as before) is what keeps
 * two DIFFERENT handoffs that happen to derive the SAME basename (e.g. both
 * opened from "Eric-Headshot.jpg") from overwriting each other's file.
 *
 * This used to be a plain HTTP `fetch` GET of `file_url` (`/cpsb/file/<id>`)
 * — UXP's runtime blocks cleartext `http://` to a non-localhost host (the
 * whole reason cross-machine use needs this fix at all: the plugin's
 * control `ws://` connection to a remote ComfyUI works fine, but an HTTP
 * `fetch()` to that same remote host does not), so the download now rides
 * the identical, already-open websocket instead of a second HTTP call.
 *
 * Returns the sandbox `File` entry alongside the opened `Document` (not just
 * the document) so `openHandoff` can stash it on the handoff's record —
 * `deliverEdit`'s layered-PSD upload path (docs/PROTOCOL.md §6d) re-reads
 * this SAME file after a save, since a plain Cmd/Ctrl+S in Photoshop
 * overwrites it in place with the real, current, layered document; no
 * separate export step is needed the way the flat-PNG path needs one.
 * @param {string} handoffId
 * @param {string} [psdPath] - The server-side `psd_path` from `open_handoff`,
 * used only to recover its basename (see above).
 * @returns {Promise<{doc: import('photoshop').Document, file: import('uxp').storage.File}>}
 */
async function openRemote(handoffId, psdPath) {
  const bytes = await connection.requestFile(handoffId)
  const handoffsFolder = await getHandoffsSandboxFolder()
  const handoffFolder = await getOrCreateSubfolder(handoffsFolder, handoffId)
  const basename = basenameOfServerPath(psdPath) || `${handoffId}.psd`
  const file = await handoffFolder.createFile(basename, { overwrite: true })
  await file.write(bytes.buffer, { format: formats.binary })
  const doc = await core.executeAsModal(() => app.open(file), { commandName: 'ComfyUI: open handoff' })
  return { doc, file }
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
/**
 * Handoff ids whose documents were observed CLOSED but whose
 * `document_closed` report has not yet been delivered to the server. Every
 * prune path feeds this set (see {@link pruneClosedHandoffs}), and
 * {@link startDocumentCloseWatcher} flushes it whenever the connection is
 * actually up. Two real loss modes forced this indirection (a plain
 * send-inside-the-prune-callback lost the signal both ways): (1)
 * `connection.send` silently DROPS messages while the socket is down (its
 * own documented best-effort posture) yet the record was pruned regardless,
 * so a document closed during a server restart/network blip was never
 * reported after reconnect; (2) `getActiveHandoffs()` — called on every
 * panel render — runs the SAME prune without any callback, so a render
 * landing inside the watcher's 5s window consumed the closed record before
 * the watcher ever saw it. Either way `plugin_doc_open` stayed true/null
 * server-side and the gallery's "Closed without saving" chip — the entire
 * point of the feature — never appeared.
 * @type {Set<string>}
 */
const pendingCloseReports = new Set()

/**
 * Drops any tracked handoff whose Photoshop document has been CLOSED since we
 * opened it (the "clear when no longer active" behavior). A record with no
 * `documentId` yet (mid-open) is left alone so an in-flight open isn't pruned.
 * Silent — no `notifyChanged()` — so it's safe to call from inside a render
 * (which `getActiveHandoffs` is); the manual clear path notifies explicitly.
 * EVERY pruned handoff is queued on {@link pendingCloseReports} — reporting
 * is decoupled from pruning precisely so no caller (renders included) can
 * consume a closure without the server eventually hearing about it.
 * @returns {number} How many were pruned.
 */
function pruneClosedHandoffs() {
  let removed = 0
  for (const record of Array.from(byHandoffId.values())) {
    if (record.documentId != null && findOpenDocument(record) == null) {
      pendingCloseReports.add(record.handoffId)
      logInfo(`handoff ${record.handoffId}: document "${record.docTitle}" closed`)
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
 * How often {@link startDocumentCloseWatcher} checks whether tracked
 * handoffs' documents are still open, in milliseconds.
 */
const DOC_CLOSE_CHECK_MS = 5000

/**
 * Periodically (a) runs the same closed-document prune the panel's own
 * renders run, and (b) FLUSHES {@link pendingCloseReports} to the server as
 * `document_closed` messages — but only while the connection is actually
 * up, retrying on later ticks otherwise, so a closure observed during a
 * disconnect still reaches the server after reconnect (the server's handoff
 * records persist across its restarts via meta.json, so a late report still
 * lands on the right handoff; an unknown id is just a logged warning
 * server-side). This is the ComfyUI gallery's REAL "is this still open?"
 * signal for a Tier-2 handoff (gallery overhaul, 2026-07-22), replacing the
 * old client-only "editing for over an hour" guess. Started once at module
 * load; this module has no teardown (mirrors `saveListener.js`'s own
 * permanent top-level registration), so a plain top-level `setInterval`
 * running for the plugin's whole lifetime is appropriate.
 */
function startDocumentCloseWatcher() {
  setInterval(() => {
    pruneClosedHandoffs()
    if (pendingCloseReports.size === 0) return
    if (connection.getState().status !== 'connected') return // retry next tick
    for (const handoffId of Array.from(pendingCloseReports)) {
      connection.send({ type: 'document_closed', handoff_id: handoffId })
      pendingCloseReports.delete(handoffId)
    }
  }, DOC_CLOSE_CHECK_MS)
}

startDocumentCloseWatcher()

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
 * Reads the current on-disk bytes of a REMOTE-mode handoff's sandbox PSD
 * copy (docs/PROTOCOL.md §6d, remote Tier-2 layered annotate) — the exact
 * file `openRemote` wrote, which Photoshop's own plain Cmd/Ctrl+S save (the
 * same one that fires `saveListener.js`'s `save` notification, which runs
 * AFTER Photoshop confirms the write) has since overwritten in place, still
 * carrying every layer — including the user's painted "Instructions" layer
 * — untouched. Unlike `runExport`'s duplicate-flatten-export pipeline, this
 * never touches Photoshop's DOM at all (plain file I/O against a file
 * that's already fully written), so no `core.executeAsModal` wrapper is
 * needed. It trusts that the file IS current: a manual Send gets that from
 * {@link ensureDocumentSaved} before calling this; the save listener gets it
 * from the save that triggered it.
 * @param {import('uxp').storage.File} file
 * @returns {Promise<Uint8Array>}
 */
async function readSandboxPsdBytes(file) {
  const contents = await file.read({ format: formats.binary })
  return new Uint8Array(/** @type {ArrayBuffer} */ (contents))
}

/**
 * How long after one of OUR OWN programmatic saves ({@link
 * saveDocumentBeforeSend}) completes that a `save` notification for that
 * document is still treated as that save's echo and ignored by the save
 * listener. While the save is in flight the suppression has no deadline at
 * all (`Infinity`); this grace only covers a notification that Photoshop
 * delivers AFTER `executeAsModal` has resolved (UXP may defer a listener
 * callback until the modal scope ends). Deliberately SHORT, because the two
 * ways to be wrong are not symmetric: a late echo that slips through just
 * delivers the same bytes a second time (the server dedups an identical
 * edit — docs/PROTOCOL.md §2 `POST /cpsb/upload`), whereas a window that is
 * too long would swallow a REAL Cmd/Ctrl+S the user makes right afterwards.
 * Same order of magnitude as `saveListener.js`'s own 500ms debounce.
 */
const OWN_SAVE_NOTIFICATION_GRACE_MS = 1000

/**
 * documentId -> epoch-ms deadline until which a `save` notification for that
 * document is the echo of a save THIS MODULE issued (`Infinity` while the
 * save is in flight). Read by the save listener through
 * {@link isOwnSaveNotification}; written only by {@link saveDocumentBeforeSend}.
 * Keyed by document id (not handoff id) because that is what the listener
 * resolves from the notification descriptor.
 * @type {Map<number, number>}
 */
const ownSaveSuppressedUntil = new Map()

/**
 * handoffId -> the save-before-send already in flight for it, so two Sends
 * fired back to back (a double-click on the panel button, the menu command
 * plus the button) share ONE `doc.save()` instead of the second one
 * colliding with the first's modal scope.
 * @type {Map<string, Promise<void>>}
 */
const saveBeforeSendInFlight = new Map()

/**
 * Whether a `save` notification for *documentId* is the echo of a save this
 * module issued itself (so the save listener must NOT start a second
 * delivery for it — the manual Send that caused the save is already
 * delivering). Self-clearing once the grace window has passed.
 * @param {number} documentId
 * @returns {boolean}
 */
function isOwnSaveNotification(documentId) {
  const until = ownSaveSuppressedUntil.get(documentId)
  if (until === undefined) return false
  if (Date.now() < until) return true
  ownSaveSuppressedUntil.delete(documentId)
  return false
}

/**
 * Whether delivering *record* depends on the document's SAVED FILE rather
 * than on a fresh export of the live document. True exactly for a
 * `wantsLayeredPsd` (Annotate) handoff, in both modes:
 *
 * - REMOTE: {@link readSandboxPsdBytes} reads the sandbox PSD straight off
 *   disk — whatever Photoshop last wrote there is what gets uploaded.
 * - LOCAL: the plugin uploads a flat PNG exported from the live document,
 *   but the server's Annotate node does not consume that PNG: the upload
 *   only marks the edit as arrived, after which `cpsb.annotate.
 *   _read_ps_saved_psd` re-opens the managed PSD on disk for the
 *   `Instructions` layer (PROTOCOL.md §6d). Unsaved strokes are in the PNG
 *   but not in that file.
 *
 * Every other handoff (the plain Edit-in-Photoshop bridge node, Compose,
 * Load PSD, Run Photoshop Action) is consumed from the exported PNG, which
 * {@link runExport} produces from `doc.duplicate()` of the LIVE document —
 * saved or not — so it never has this problem, and must NOT be saved here
 * (for an `edit_in_place` handoff the saved file is the user's own original).
 * @param {CpsbHandoffRecord} record
 * @returns {boolean}
 */
function deliveryDependsOnSavedFile(record) {
  if (!record.wantsLayeredPsd) return false
  return record.mode === 'local' || Boolean(record.sandboxFile)
}

/**
 * Saves *doc* (no dialog) inside `core.executeAsModal`, marking the document
 * as "our own save in flight" so the save listener ignores the `save`
 * notification this save produces. The mark is released
 * {@link OWN_SAVE_NOTIFICATION_GRACE_MS} after the save settles — on failure
 * as well as success, so a stray notification from a half-finished save can
 * never start a stale delivery either.
 * @param {import('photoshop').Document} doc
 * @returns {Promise<void>}
 * @throws Whatever `executeAsModal` / `Document.save()` throws (read-only
 * file, modal unavailable or cancelled, ...).
 */
async function saveDocumentBeforeSend(doc) {
  ownSaveSuppressedUntil.set(doc.id, Infinity)
  try {
    await core.executeAsModal(
      async () => {
        await doc.save()
      },
      { commandName: 'ComfyUI: save before send' }
    )
  } finally {
    ownSaveSuppressedUntil.set(doc.id, Date.now() + OWN_SAVE_NOTIFICATION_GRACE_MS)
  }
}

/**
 * Makes the document's on-disk copy current before a manual Send reads it.
 * `Document.saved` is `true` only when there are no unsaved changes; any
 * other value — `false`, or `undefined` on a Photoshop build that does not
 * expose the property — is treated as "may be dirty" and saved, because an
 * unnecessary save of an unchanged document is harmless while a skipped one
 * uploads stale bytes. Concurrent callers for the same handoff share one save.
 * @param {CpsbHandoffRecord} record
 * @param {import('photoshop').Document} doc
 * @returns {Promise<boolean>} `true` if a save was performed (or joined),
 * `false` if the document was already saved.
 * @throws If the save fails — the caller must then send nothing.
 */
async function ensureDocumentSaved(record, doc) {
  if (doc.saved === true) return false
  let pending = saveBeforeSendInFlight.get(record.handoffId)
  if (!pending) {
    logInfo(
      `handoff ${record.handoffId}: "${record.docTitle}" has unsaved changes — saving before send`
    )
    pending = saveDocumentBeforeSend(doc).finally(() => {
      saveBeforeSendInFlight.delete(record.handoffId)
    })
    saveBeforeSendInFlight.set(record.handoffId, pending)
  }
  await pending
  return true
}

/**
 * Runs the full "send this document's current state back to ComfyUI"
 * pipeline for a tracked handoff: an immediate `save_detected` notification
 * (informational — docs/PROTOCOL.md §3), then either the flat-PNG export
 * pipeline (exporter.js) + upload, or — for a REMOTE-mode handoff whose
 * `open_handoff` carried `wants_layered_psd: true` (docs/PROTOCOL.md §6d) —
 * the sandbox document's own raw PSD bytes, read directly and uploaded as
 * `kind: "psd"` instead (`uploader.js`'s `uploadLayeredPsd`). LOCAL mode
 * always takes the flat-PNG path regardless of `wantsLayeredPsd`: the
 * shared filesystem already gives the server this handoff's real, layered
 * save with no upload at all, so there is nothing for the layered-PSD
 * transport to do there. Shared by the save listener (automatic, on a real
 * Photoshop save) and both "Send" entry points (the plugin command and the
 * panel's per-document button) — neither of those callers needs to know
 * this sequence exists.
 *
 * SAVE-BEFORE-SEND (docs/PROTOCOL.md §6d): when the handoff's delivery
 * depends on the saved file ({@link deliveryDependsOnSavedFile}) and the
 * call is a MANUAL Send, the document is saved first if it has unsaved
 * changes — otherwise the previous on-disk contents would be delivered while
 * the panel reports success, silently dropping everything painted since the
 * last save. If that save fails the delivery STOPS: status `save_failed`,
 * `record.lastError` set, an error logged, and nothing is sent or uploaded.
 * The save's own `save` notification is suppressed (see
 * {@link isOwnSaveNotification}) so one Send never delivers twice.
 *
 * @param {string} handoffId
 * @param {{afterNativeSave?: boolean}} [options] `afterNativeSave: true` is
 * passed ONLY by the save listener: its trigger is a save Photoshop has just
 * completed, so the file is current by definition, and attempting a save of
 * our own from inside that notification could collide with the one still
 * finishing. That path behaves exactly as it did before the dirty check existed.
 * @returns {Promise<CpsbLocalHandoffStatus | undefined>} The record's final
 * status (so a caller can react to `save_failed`), or `undefined` when the
 * handoff is unknown or its document is no longer open.
 */
async function deliverEdit(handoffId, { afterNativeSave = false } = {}) {
  const record = byHandoffId.get(handoffId)
  if (!record) {
    logWarn(`deliverEdit: unknown handoff ${handoffId}`)
    return undefined
  }
  const doc = findOpenDocument(record)
  if (!doc) {
    logWarn(
      `deliverEdit: document for handoff ${handoffId} ("${record.docTitle}") is no longer open`
    )
    return undefined
  }
  record.lastError = null
  record.status = 'exporting'
  notifyChanged()
  if (!afterNativeSave && deliveryDependsOnSavedFile(record)) {
    try {
      await ensureDocumentSaved(record, doc)
    } catch (error) {
      record.lastError = describeError(error)
      record.status = 'save_failed'
      logError(
        `could not save "${record.docTitle}" before sending handoff ${handoffId} — nothing was ` +
          `sent: ${record.lastError}`
      )
      notifyChanged()
      return record.status
    }
  }
  connection.send({ type: 'save_detected', handoff_id: handoffId })
  try {
    let uploaded
    if (record.mode === 'remote' && record.wantsLayeredPsd && record.sandboxFile) {
      const psdBytes = await readSandboxPsdBytes(record.sandboxFile)
      uploaded = await uploadLayeredPsd(handoffId, psdBytes)
    } else {
      const pngBytes = await runExport(doc)
      uploaded = await uploadEdit(handoffId, pngBytes)
    }
    record.status = uploaded ? 'sent' : 'upload_failed'
    if (uploaded) {
      logInfo(`sent edit for handoff ${handoffId} ("${record.docTitle}")`)
    }
  } catch (error) {
    record.status = 'export_failed'
    logError(`export failed for handoff ${handoffId} ("${record.docTitle}"): ${describeError(error)}`)
  }
  notifyChanged()
  return record.status
}

module.exports = {
  registryEvents,
  openHandoff,
  cancelHandoff,
  findByDocumentId,
  findByPath,
  getActiveHandoffs,
  clearAllHandoffs,
  deliverEdit,
  isOwnSaveNotification
}
