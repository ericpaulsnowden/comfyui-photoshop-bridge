/**
 * @file Listens for Photoshop's native `save` notification and, for any
 * saved document this plugin is tracking, runs the deliver-edit pipeline
 * (handoffs.js). Deliberately thin: identity resolution and debouncing live
 * here; the export+upload sequence itself lives in handoffs.js so this
 * listener and the "Send back now" entry points share one implementation.
 */

const { app, action } = require('photoshop')

const { findByDocumentId, deliverEdit } = require('./handoffs.js')
const { logError, describeError } = require('./log.js')

/** Debounce window per document (quality bar: "Debounce double-fires... 500ms"). */
const DEBOUNCE_MS = 500

/** @type {Map<number, number>} documentId -> ts of the last accepted save. */
const lastAcceptedSave = new Map()

/**
 * @param {number} documentId
 * @returns {boolean} True if a save for this document was already accepted
 * within the debounce window (and this one should therefore be ignored).
 * The timestamp is recorded only when a save is accepted — a rejected save
 * must not slide the window forward, or a steady stream of sub-500ms
 * double-fires would keep refreshing the window and starve every later
 * save that is legitimately far from the last ACCEPTED one.
 */
function isDebounced(documentId) {
  const now = Date.now()
  const last = lastAcceptedSave.get(documentId)
  if (last !== undefined && now - last < DEBOUNCE_MS) {
    return true
  }
  lastAcceptedSave.set(documentId, now)
  return false
}

/**
 * Resolves which document a `save` notification's descriptor refers to.
 *
 * `// VERIFY(spike-7):` whether the `save` event descriptor reliably
 * carries a `documentID` field is not confirmed by any Adobe reference page
 * found during implementation — the official event-listener examples cover
 * `open`/`select`/etc. generically, and the one first-party sample that
 * reads `descriptor.documentID` after a notification (the `vanilla-js-sample`
 * plugin, AdobeDocs/uxp-photoshop-plugin-samples) does so for
 * `open`/`newDocument`/`close`/`duplicate`/`select`, not `save` specifically.
 * This function is therefore written defensively, exactly per PLAN.md §5 /
 * docs/SPIKES.md spike 7: prefer `descriptor.documentID` when present,
 * otherwise fall back to `app.activeDocument`. Resolve this comment by
 * running spike 7's procedure (docs/SPIKES.md) against a real Photoshop
 * install with multiple documents open, only some of which are ours; if the
 * descriptor turns out to always carry the id, simplify this function to
 * drop the fallback branch (and re-check whether it stays race-safe if not).
 * @param {import('photoshop').ActionDescriptor} descriptor
 * @returns {number | null}
 */
function resolveSavedDocumentId(descriptor) {
  if (descriptor && typeof (/** @type {any} */ (descriptor).documentID) === 'number') {
    return /** @type {any} */ (descriptor).documentID
  }
  try {
    const active = app.activeDocument
    return active ? active.id : null
  } catch (_error) {
    // Thrown when no document is open at all — nothing to resolve.
    return null
  }
}

/**
 * @param {import('photoshop').ActionDescriptor} descriptor
 * @returns {Promise<void>}
 */
async function handleSave(descriptor) {
  const documentId = resolveSavedDocumentId(descriptor)
  if (documentId == null) return
  if (isDebounced(documentId)) return
  const record = findByDocumentId(documentId)
  if (!record) return // Not a document this plugin opened — ignore.
  await deliverEdit(record.handoffId)
}

/**
 * Registers the `save` notification listener. Call once, from
 * `entrypoints.plugin.create()` (index.js), so it is active for the whole
 * Photoshop session regardless of whether the panel is ever opened.
 *
 * Signature note: Adobe's current reference for `action.addNotificationListener`
 * (`ps-reference/media/photoshopaction.md`) documents it as
 * `addNotificationListener(events: string[], callback)` — a plain array of
 * event-name strings. PLAN.md §5 and docs/SPIKES.md spike 7 both describe
 * calling it as `addNotificationListener([{event: 'save'}], ...)` (an array
 * of descriptor objects); that shape does not match the verified reference
 * and is not used here — see this plugin's implementation report for the
 * full discrepancy.
 * @returns {void}
 */
function startSaveListener() {
  action.addNotificationListener(['save'], (_eventName, descriptor) => {
    handleSave(descriptor).catch((error) => {
      logError(`save listener failed: ${describeError(error)}`)
    })
  })
}

module.exports = { startSaveListener }
