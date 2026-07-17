/**
 * @file Plugin-scope entry point (PLAN.md §5): wires up `entrypoints.setup()`
 * for the "ComfyUI" panel and the "Send back now" command, and starts the
 * persistent connection manager from `plugin.create()` — not from the
 * panel's `show()` — so the websocket connection to ComfyUI is established
 * as soon as Photoshop launches (manifest.json sets `host.data.loadEvent`
 * to `"startup"` specifically to make that possible — by default a UXP
 * plugin loads lazily, only when its panel is shown or a command is run,
 * which would defeat the point of a persistent connection) and survives the
 * panel being closed, or never opened at all, for the rest of the session.
 */

const { entrypoints } = require('uxp')
const { app } = require('photoshop')

// Local modules use CommonJS require/module.exports throughout — the module
// pattern every AdobeDocs sample ships (e.g. io-websocket-example/plugin/
// index.js: `const { entrypoints } = require("uxp")` loaded from a classic
// <script> tag). <script type="module"> in a UXP panel is undocumented, so
// this plugin does not rely on it.
const { connection } = require('./connection.js')
// Requiring handoffs.js at load time (not lazily) matters: its module body
// registers the connection's open_handoff/handoff_cancelled listener.
const { findByDocumentId, findByPath, deliverEdit } = require('./handoffs.js')
const { startSaveListener } = require('./saveListener.js')
const { initPanel } = require('./panel.js')
const { logError, describeError } = require('./log.js')

entrypoints.setup({
  plugin: {
    create() {
      try {
        connection.start()
        startSaveListener()
      } catch (error) {
        logError(`plugin.create failed: ${describeError(error)}`)
      }
    }
  },
  panels: {
    comfyui: {
      create() {
        try {
          initPanel()
        } catch (error) {
          logError(`panel create failed: ${describeError(error)}`)
        }
      }
    }
  },
  commands: {
    sendBackNow: {
      async run() {
        try {
          await sendBackNowForActiveDocument()
        } catch (error) {
          logError(`"Send back now" command failed: ${describeError(error)}`)
        }
      }
    }
  }
})

/**
 * Implements the "Send back now" Plugins-menu command: exports and uploads
 * the active document's current state without waiting for a save event
 * (PLAN.md §2, §5 — the missed-save safety net, e.g. for a user who only
 * ever uses Export/Save a Copy rather than a plain Cmd/Ctrl+S).
 * @returns {Promise<void>}
 */
async function sendBackNowForActiveDocument() {
  /** @type {import('photoshop').Document | null} */
  let doc
  try {
    doc = app.activeDocument
  } catch (_error) {
    doc = null
  }
  if (!doc) {
    logError('"Send back now": no active document')
    return
  }
  let record = findByDocumentId(doc.id)
  if (!record) {
    try {
      record = findByPath(doc.path)
    } catch (_error) {
      // Some Photoshop versions throw on `.path` for a never-saved document
      // rather than returning "" — the id lookup above is the primary key,
      // so a throwing path getter simply means there is no fallback match.
    }
  }
  if (!record) {
    logError(`"Send back now": "${doc.title}" is not a document ComfyUI opened`)
    return
  }
  await deliverEdit(record.handoffId)
}
