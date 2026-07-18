/**
 * @file Plugin-scope entry point (PLAN.md §5): wires up `entrypoints.setup()`
 * for the "ComfyUI" panel and the "Send" command, and starts the
 * persistent connection manager from `plugin.create()` — not from the
 * panel's `show()` — so the websocket connection to ComfyUI is established
 * as soon as Photoshop launches (manifest.json sets `host.data.loadEvent`
 * to `"startup"` so the plugin loads at launch rather than lazily) and
 * survives the panel being closed, or never opened at all.
 *
 * This is the panel document's classic `<script src="index.js">` (panel.html).
 * Its top-level boot sequence is deliberately structured to be
 * SELF-DIAGNOSING, because the panel previously rendered bare with no
 * indication of why:
 *   - The very first thing it does — depending on nothing but `require('uxp')`
 *     (a host module every UXP sample proves loads) and the DOM — is paint
 *     the running plugin version into the always-visible `#cpsb-version`
 *     banner. That is proof that fresh code loaded after a reload.
 *   - Everything else (local `require()`s, `entrypoints.setup()`) runs inside
 *     a single try/catch that writes any thrown error into the visible
 *     `#cpsb-fatal` element instead of leaving the panel silently blank.
 * So the next screenshot is unambiguous: version shown + no error → JS ran;
 * version + an error → that's the cause; NOTHING at all (the static "Plugin
 * loading…" placeholder never changed) → the document/script itself did not
 * load, which is a structural/manifest problem, not a code one.
 */

const uxp = require('uxp')

/**
 * Best-effort write into a panel element. Never throws — if the DOM or the
 * element is unavailable, it silently no-ops, so the proof-of-life path
 * itself cannot become a new failure mode.
 * @param {string} id - Target element id.
 * @param {string} text - Text content to set.
 * @param {string} [className] - If given, replaces the element's class.
 * @param {boolean} [show] - If true, force the element visible.
 * @returns {void}
 */
function setPanelText(id, text, className, show) {
  try {
    const el = document.getElementById(id)
    if (!el) return
    el.textContent = text
    if (className !== undefined) el.className = className
    if (show) el.style.display = 'block'
  } catch (_error) {
    // DOM not ready / unavailable — proof-of-life is strictly best-effort.
  }
}

// Proof of life, painted the instant this script runs.
setPanelText('cpsb-version', `Plugin v${uxp.versions.plugin}`, 'cpsb-version')

try {
  bootstrap()
} catch (error) {
  const message = (error && error.message) || String(error)
  setPanelText('cpsb-fatal', `Plugin error: ${message}`, 'cpsb-fatal', true)
  // Surface to the UDT console as well — this is a genuine fault.
  console.warn(`[cpsb] fatal during bootstrap: ${message}`)
}

/**
 * Loads the rest of the plugin's modules and registers its entrypoints.
 * Runs inside the guarded block above so a failed local `require()` (e.g. a
 * module path that doesn't resolve) or a throw from `entrypoints.setup()`
 * surfaces in `#cpsb-fatal` rather than vanishing.
 * @returns {void}
 */
function bootstrap() {
  const { entrypoints } = uxp
  const { app } = require('photoshop')
  // Local modules use CommonJS require/module.exports with explicit `.js`
  // extensions — the exact multi-file pattern documented in Adobe's UXP
  // "Importing Modules" tutorial (require("./file.js") + module.exports),
  // which the runtime supports directly with no bundler. ES `import` is the
  // form UXP does NOT support, and is not used anywhere here.
  const { connection } = require('./connection.js')
  // Requiring handoffs.js at load time (not lazily) matters: its module body
  // registers the connection's open_handoff/handoff_cancelled listener.
  const { findByDocumentId, findByPath, deliverEdit } = require('./handoffs.js')
  const { startSaveListener } = require('./saveListener.js')
  const { initPanel } = require('./panel.js')
  const { logError, describeError } = require('./log.js')

  /**
   * Implements the "Send" Plugins-menu command: exports and uploads
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
      logError('"Send": no active document')
      return
    }
    let record = findByDocumentId(doc.id)
    if (!record) {
      try {
        record = findByPath(doc.path)
      } catch (_error) {
        // Some Photoshop versions throw on `.path` for a never-saved
        // document rather than returning "" — the id lookup above is the
        // primary key, so a throwing path getter just means no fallback match.
      }
    }
    if (!record) {
      logError(`"Send": "${doc.title}" is not a document ComfyUI opened`)
      return
    }
    await deliverEdit(record.handoffId)
  }

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
            logError(`"Send" command failed: ${describeError(error)}`)
          }
        }
      }
    }
  })
}
