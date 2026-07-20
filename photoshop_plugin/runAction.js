/**
 * @file The plugin side of the `PhotoshopAction` ComfyUI node's ("the
 * capstone", Eric's backlog) `run_action` websocket message: plays a SAVED
 * Photoshop Action, by name, from a named Action Set, against the document
 * a preceding `open_handoff` opened, then runs the SAME export+upload
 * pipeline a manual save uses (`handoffs.js`'s `deliverEdit` — imported, not
 * duplicated). Not yet in `docs/PROTOCOL.md` §3 — see the implementation
 * report for the exact `run_action` / `action_ok` / `action_error` message
 * shapes to fold in there.
 *
 * **Playing an Action: the typed `app.actionTree` API, not a guessed
 * batchPlay descriptor.** Research for this feature initially went looking
 * for a raw `batchPlay` "play" descriptor (the classic
 * `{_obj: "play", _target: [{_ref: "action", _name: "..."}, {_ref:
 * "actionSet", _name: "..."}]}` shape other Photoshop scripting ecosystems
 * use) but found no authoritative source confirming that exact shape for
 * UXP specifically. Instead, UXP's `photoshop` module exposes a properly
 * DOCUMENTED, TYPED API for this — confirmed against the official
 * `@adobe-uxp-types/photoshop` TypeScript declarations (not a guess):
 * `app.actionTree: ActionSet[]` (app-wide, not tied to any Document);
 * `ActionSet { name, actions: Action[], play(): Promise<void> }` (`play()`
 * runs EVERY action in the set, in order — not what this file wants);
 * `Action { name, parent: ActionSet, play(): Promise<void> }` (`play()`
 * plays exactly this one Action — what this file uses). Two independent
 * Adobe Creative Cloud Developer Forums threads use this identical
 * `app.actionTree.find(...)` / `actionSet.actions.find(...)` / `.play()`
 * pattern in real, running plugin code (one of them a live bug report, PS
 * 23.0.2/23.1, confirming the mechanism DOES execute the Action — the bug
 * in that thread is about dialog freezing, covered below, not about this
 * API shape being wrong). `core.executeAsModal` wrapping is required (Adobe's
 * own reference: any command that may modify the document/application state
 * must run inside one) — the exact same requirement `exporter.js` and
 * `handoffs.js` already satisfy for every other Photoshop DOM mutation in
 * this plugin. `app.activeDocument = doc` (making the target document the
 * one the Action actually runs against — Actions, like the Actions panel
 * itself, always operate on whatever is CURRENTLY active, with no per-call
 * "target this specific document" parameter) is confirmed WRITABLE by the
 * same TypeScript declarations (`activeDocument: Document;`, no `readonly`)
 * and by a dedicated forum thread's own accepted answer.
 *
 * **Known, honest, UNRESOLVED risk — read before relying on this in
 * production (this is the live-Photoshop spike this feature still needs,
 * see the implementation report):** a Creative Cloud Developer Forums thread
 * ("Playing a Photoshop action via (UXP) JavaScript: Dialogs are freezing")
 * reports that an Action containing a step with an interactive dialog
 * enabled (their example: Gaussian Blur with its dialog showing) FREEZES
 * inside `executeAsModal` on PS 23.1+ — no working fix was found in that
 * thread. This file cannot detect or recover from that: if it happens, this
 * async function simply never resolves, `deliverEdit` never runs, and no
 * `action_ok`/`action_error` reply is ever sent — the ComfyUI-side node's
 * OWN `timeout_seconds` bound is the only backstop (it stops the workflow
 * from waiting forever; it cannot un-stick Photoshop itself). Users must
 * ensure every step of an Action meant to run through this node has its
 * dialog toggle (the small dialog-box icon in the Actions panel's own
 * checkbox column, per step) turned OFF.
 */

const { app, core } = require('photoshop')

const { connection } = require('./connection.js')
const { getActiveHandoffs, deliverEdit } = require('./handoffs.js')
const { logInfo, logWarn, logError, describeError } = require('./log.js')

/**
 * Bound on waiting for `open_handoff`'s own async document-open to finish
 * populating `handoffs.js`'s registry. `run_action` (server, `cpsb/actions.py`)
 * is sent as a SECOND, separate websocket message immediately after
 * `open_handoff`'s own send resolves server-side — but `open_handoff`'s
 * PLUGIN-side handler (`handoffs.js` `openHandoff`) is async (file I/O,
 * `app.open()`) and its own `connection.js` dispatch is fire-and-forget
 * (`openHandoff(msg)`, not awaited), so `run_action` can genuinely arrive
 * before that open has finished. Polling briefly for the registry entry
 * (rather than failing immediately on a benign ordering race) is simpler and
 * safer than trying to correlate the two messages by some new id — worth
 * flagging for live verification alongside the batchPlay-adjacent risk
 * above: this codebase has no other case of two related server->plugin
 * messages sent back-to-back like this, so the actual real-world race
 * window (network + `app.open()` latency for the target file) is unverified
 * against a real Photoshop.
 */
const RECORD_WAIT_TIMEOUT_MS = 15000
const RECORD_WAIT_POLL_MS = 150

/**
 * @param {number} ms
 * @returns {Promise<void>}
 */
function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

/**
 * Polls `handoffs.js`'s registry for `handoffId` until it has a live
 * `documentId` (i.e. `open_handoff` finished successfully) or
 * `RECORD_WAIT_TIMEOUT_MS` elapses. See {@link RECORD_WAIT_TIMEOUT_MS} for
 * why this waits at all instead of failing on a missing record immediately.
 * @param {string} handoffId
 * @returns {Promise<import('./handoffs.js').CpsbHandoffRecord | null>}
 */
async function waitForHandoffRecord(handoffId) {
  const deadline = Date.now() + RECORD_WAIT_TIMEOUT_MS
  do {
    const record = getActiveHandoffs().find((candidate) => candidate.handoffId === handoffId)
    if (record && record.documentId != null) return record
    await delay(RECORD_WAIT_POLL_MS)
  } while (Date.now() < deadline)
  return null
}

/**
 * @param {number} documentId
 * @returns {import('photoshop').Document | null}
 */
function findDocumentById(documentId) {
  for (let i = 0; i < app.documents.length; i++) {
    if (app.documents[i].id === documentId) return app.documents[i]
  }
  return null
}

/**
 * Finds a named Action inside a named Action Set via `app.actionTree` (see
 * this file's own doc comment for the sourcing behind this exact shape).
 * @param {string} actionSetName
 * @param {string} actionName
 * @returns {import('photoshop').Action | null}
 */
function findAction(actionSetName, actionName) {
  const actionSet = app.actionTree.find((candidate) => candidate.name === actionSetName)
  if (!actionSet) return null
  return actionSet.actions.find((candidate) => candidate.name === actionName) || null
}

/**
 * Handles a server `run_action` command (`cpsb/actions.py`'s `PhotoshopAction`
 * node): plays `msg.action_name` from `msg.action_set` against the document
 * open for `msg.handoff_id`, then delivers the result exactly like a manual
 * save would (`deliverEdit`), and finally acks with `action_ok` or
 * `action_error` (Photoshop's own error text, or this function's own
 * diagnostic, on failure — so a bad Action/Set name surfaces clearly instead
 * of the node just spinning until `timeout_seconds`).
 * @param {{type: 'run_action', handoff_id: string, action_name: string, action_set: string}} msg
 * @returns {Promise<void>}
 */
async function runAction(msg) {
  const handoffId = msg.handoff_id
  const actionName = msg.action_name
  const actionSetName = msg.action_set
  try {
    const record = await waitForHandoffRecord(handoffId)
    if (!record) {
      throw new Error(
        `No open document for handoff ${handoffId} -- the open may have failed, or not ` +
          'finished, before this Action request arrived'
      )
    }
    const doc = findDocumentById(record.documentId)
    if (!doc) {
      throw new Error(`Document for handoff ${handoffId} ("${record.docTitle}") is no longer open`)
    }

    const action = findAction(actionSetName, actionName)
    if (!action) {
      throw new Error(
        `Action "${actionName}" not found in set "${actionSetName}" -- check the exact ` +
          'names in Photoshop\'s Actions panel (case-sensitive, no cross-set search)'
      )
    }

    logInfo(`playing action "${actionName}" (set "${actionSetName}") for handoff ${handoffId}`)
    await core.executeAsModal(
      async () => {
        // Actions operate on whichever document is ACTIVE (like pressing
        // Play in the Actions panel) -- there is no per-call "target this
        // document" parameter, so the target must be made active first.
        app.activeDocument = doc
        await action.play()
      },
      { commandName: `ComfyUI: run action "${actionName}"` }
    )

    // Same export+upload pipeline a manual save uses -- `record` is the
    // SAME object `deliverEdit` mutates `.status` on (both come from
    // handoffs.js's one shared `byHandoffId` map), so re-reading it after
    // the call reflects the real outcome without a second lookup.
    await deliverEdit(handoffId)
    if (record.status === 'sent') {
      connection.send({ type: 'action_ok', handoff_id: handoffId })
      logInfo(`action "${actionName}" delivered for handoff ${handoffId}`)
    } else {
      connection.send({
        type: 'action_error',
        handoff_id: handoffId,
        error: `Action "${actionName}" played, but delivering the result failed (${record.status})`
      })
    }
  } catch (error) {
    const message = describeError(error)
    logError(`run_action failed for ${handoffId}: ${message}`)
    connection.send({ type: 'action_error', handoff_id: handoffId, error: message })
  }
}

connection.addEventListener('message', (event) => {
  const msg = /** @type {CustomEvent} */ (event).detail
  if (msg.type === 'run_action') {
    // runAction never rejects (it catches internally and replies
    // action_error), so this fire-and-forget call is safe -- same
    // convention as handoffs.js's own open_handoff/handoff_cancelled
    // listener right next to this one's sibling registration.
    runAction(msg).catch((error) => {
      logWarn(`unexpected error from runAction: ${describeError(error)}`)
    })
  }
})

module.exports = { runAction }
