'use strict'
/**
 * Scenarios for "manual Send must not deliver stale bytes" (review item 6;
 * docs/PROTOCOL.md §6d, "Save before send").
 *
 * Runs the REAL handoffs.js / saveListener.js / log.js in a Node VM
 * (plugin_vm.js) against fakes of Photoshop, UXP storage, the websocket
 * connection, the exporter and the uploader. Each scenario builds a fresh
 * "world", opens the handoff through the real `openHandoff` (so the sandbox
 * file wiring is exercised too), then drives `deliverEdit` and/or the save
 * listener, and asserts on the ORDERED event log.
 *
 * Invoked by tests/test_send_save_js.py, which prints one JSON line per run:
 *   { "<scenario>": { "ok": true } | { "ok": false, "error": "..." }, ... }
 */

const assert = require('node:assert/strict')

const { loadPluginWorld } = require('./plugin_vm.js')

const OLD_BYTES = 'PSD-OLD: what was on disk before the stroke'
const NEW_BYTES = 'PSD-NEW: includes the unsaved stroke'
const GRACE_MS = 1000 // OWN_SAVE_NOTIFICATION_GRACE_MS in handoffs.js
const HANDOFF = 'h1'
const DOC_ID = 7

const text = (bytes) => Buffer.from(bytes).toString('utf8')
const flush = async () => {
  for (let i = 0; i < 12; i++) await new Promise((resolve) => setImmediate(resolve))
}

function makeFolder() {
  const entries = new Map()
  return {
    entries,
    async getEntry(name) {
      if (!entries.has(name)) throw new Error(`no such entry: ${name}`)
      return entries.get(name)
    },
    async createFolder(name) {
      const folder = makeFolder()
      entries.set(name, folder)
      return folder
    },
    async createFile(name) {
      const file = { name, contents: Buffer.alloc(0) }
      file.read = async () => {
        world.events.push('read')
        const copy = Buffer.from(file.contents)
        return copy.buffer.slice(copy.byteOffset, copy.byteOffset + copy.byteLength)
      }
      file.write = async (data) => {
        file.contents = Buffer.from(data)
      }
      entries.set(name, file)
      return file
    }
  }
}

/** The world currently under test (scenarios run one at a time). */
let world

/**
 * @param {{mode?: 'remote'|'local', layered?: boolean, saved?: boolean|undefined,
 *   saveFails?: boolean, modalFails?: boolean, notifyDuringSave?: boolean}} [options]
 */
async function makeWorld(options = {}) {
  const {
    mode = 'remote',
    layered = true,
    notifyDuringSave = false
  } = options
  // `saved: undefined` must stay undefined (a host that lacks Document.saved),
  // so it is read by key presence rather than through a destructuring default.
  const saved = 'saved' in options ? options.saved : false
  let modalFails = false
  let saveFails = options.saveFails === true

  const events = []
  const uploads = []
  const sent = []
  const counts = { save: 0, modal: 0 }
  let notificationListener = null
  let sandboxFile = null
  const localMode = mode === 'local'

  const doc = {
    id: DOC_ID,
    title: 'Annotate.psd',
    async save() {
      events.push('save:begin')
      counts.save += 1
      await Promise.resolve()
      if (notifyDuringSave) notificationListener('save', { documentID: DOC_ID })
      await new Promise((resolve) => setImmediate(resolve))
      if (saveFails) throw new Error('Could not save: the file is read-only')
      // Photoshop overwrites the sandbox/managed PSD in place.
      if (sandboxFile) sandboxFile.contents = Buffer.from(NEW_BYTES)
      doc.saved = true
      events.push('save:end')
    }
  }
  if (saved !== undefined) doc.saved = saved

  const photoshop = {
    app: {
      documents: [doc],
      get activeDocument() {
        return doc
      },
      async open(entry) {
        if (entry && entry.contents !== undefined) sandboxFile = entry
        return doc
      }
    },
    core: {
      async executeAsModal(fn) {
        counts.modal += 1
        events.push('modal:enter')
        if (modalFails) throw new Error('The command is not currently available (modal cancelled)')
        try {
          return await fn({ isCancelled: false })
        } finally {
          events.push('modal:exit')
        }
      }
    },
    action: {
      addNotificationListener(_names, callback) {
        notificationListener = callback
      }
    }
  }

  const dataFolder = makeFolder()
  const uxp = {
    storage: {
      localFileSystem: {
        getDataFolder: async () => dataFolder,
        getEntryWithUrl: async (url) => ({ nativePath: url })
      },
      formats: { binary: 'binary' }
    }
  }

  const connectionListeners = {}
  const mocks = {
    'connection.js': {
      pathToFileUrl: (p) => `file://${p}`,
      connection: {
        send(message) {
          sent.push(message)
          events.push(`send:${message.type}`)
        },
        getState: () => ({ localMode, status: 'connected' }),
        requestFile: async () => Uint8Array.from(Buffer.from(OLD_BYTES)),
        addEventListener(type, callback) {
          connectionListeners[type] = callback
        }
      }
    },
    'exporter.js': {
      runExport: async () => {
        events.push('export')
        return Uint8Array.of(137, 80, 78, 71)
      }
    },
    'uploader.js': {
      uploadEdit: async (id, bytes) => {
        events.push('upload:png')
        uploads.push({ kind: 'png', id, bytes: Array.from(bytes) })
        return true
      },
      uploadLayeredPsd: async (id, bytes) => {
        events.push('upload:psd')
        uploads.push({ kind: 'psd', id, text: text(bytes) })
        return true
      }
    }
  }

  const loaded = loadPluginWorld({ photoshop, uxp, mocks })
  const handoffs = loaded.load('handoffs.js')
  const log = loaded.load('log.js')
  const saveListener = loaded.load('saveListener.js')
  saveListener.startSaveListener()

  await handoffs.openHandoff({
    type: 'open_handoff',
    handoff_id: HANDOFF,
    psd_path: '/srv/comfy/input/cpsb/h1/Annotate.psd',
    wants_layered_psd: layered
  })
  if (mode === 'local') {
    // Local mode shares the filesystem: there is no sandbox copy to patch.
    sandboxFile = null
  } else {
    assert.ok(sandboxFile, 'remote open must have produced a sandbox file')
    assert.equal(text(sandboxFile.contents), OLD_BYTES)
  }
  events.length = 0
  sent.length = 0
  counts.save = 0
  counts.modal = 0
  modalFails = options.modalFails === true // only AFTER the open's own modal scope

  world = {
    events,
    uploads,
    sent,
    counts,
    doc,
    handoffs,
    log,
    clock: loaded.clock,
    record: () => handoffs.getActiveHandoffs()[0],
    notify: () => notificationListener('save', { documentID: DOC_ID }),
    setSaveFails: (value) => {
      saveFails = value
    },
    closeDocument: () => {
      photoshop.app.documents.splice(0, photoshop.app.documents.length)
    }
  }
  return world
}

const scenarios = {
  // ---- the bug: remote + layered ------------------------------------------

  async remote_layered_unsaved_saves_first_then_uploads_fresh_bytes() {
    const w = await makeWorld({ mode: 'remote', layered: true, saved: false })
    const status = await w.handoffs.deliverEdit(HANDOFF)
    assert.equal(w.uploads.length, 1)
    // The regression: pre-fix this was OLD_BYTES (the stale on-disk file).
    assert.equal(w.uploads[0].text, NEW_BYTES)
    assert.equal(status, 'sent')
    assert.deepEqual(w.events, [
      'modal:enter',
      'save:begin',
      'save:end',
      'modal:exit',
      'send:save_detected',
      'read',
      'upload:psd'
    ])
    assert.equal(w.counts.save, 1)
    assert.equal(w.record().status, 'sent')
    assert.equal(w.record().lastError, null)
  },

  async remote_layered_saved_document_is_not_saved_again() {
    const w = await makeWorld({ mode: 'remote', layered: true, saved: true })
    const status = await w.handoffs.deliverEdit(HANDOFF)
    assert.equal(status, 'sent')
    assert.deepEqual(w.events, ['send:save_detected', 'read', 'upload:psd'])
    assert.equal(w.counts.save, 0)
    assert.equal(w.counts.modal, 0)
    assert.equal(w.uploads[0].text, OLD_BYTES) // the disk copy IS current
  },

  async remote_layered_unknown_saved_state_is_saved_defensively() {
    // A Photoshop build that does not expose Document.saved: never skip the save.
    const w = await makeWorld({ mode: 'remote', layered: true, saved: undefined })
    assert.equal(w.doc.saved, undefined)
    await w.handoffs.deliverEdit(HANDOFF)
    assert.equal(w.counts.save, 1)
    assert.equal(w.uploads[0].text, NEW_BYTES)
  },

  // ---- double delivery: the save listener must ignore our own save --------

  async own_save_notification_during_the_save_does_not_deliver_twice() {
    const w = await makeWorld({ layered: true, saved: false, notifyDuringSave: true })
    await w.handoffs.deliverEdit(HANDOFF)
    await flush()
    assert.equal(w.uploads.length, 1, 'echo during the save must be ignored')
    assert.equal(w.counts.save, 1)
  },

  async own_save_notification_arriving_after_the_save_settles_does_not_deliver_twice() {
    const w = await makeWorld({ layered: true, saved: false })
    await w.handoffs.deliverEdit(HANDOFF)
    w.clock.now += GRACE_MS - 1 // late, but inside the grace window
    w.notify()
    await flush()
    assert.equal(w.uploads.length, 1)
  },

  async real_native_save_after_the_grace_window_still_delivers() {
    const w = await makeWorld({ layered: true, saved: false })
    await w.handoffs.deliverEdit(HANDOFF)
    assert.equal(w.uploads.length, 1)
    w.clock.now += GRACE_MS + 1
    w.notify() // the user's own Cmd/Ctrl+S
    await flush()
    assert.equal(w.uploads.length, 2, 'suppression must expire, not swallow real saves')
    assert.equal(w.counts.save, 1, 'the listener path never saves on its own')
    assert.equal(w.uploads[1].text, NEW_BYTES)
  },

  async native_save_path_never_triggers_a_save_even_if_the_flag_lags() {
    // The listener's trigger is a save Photoshop just finished; doc.saved may
    // not have flipped yet. The dirty check must be skipped there.
    const w = await makeWorld({ layered: true, saved: false })
    w.notify()
    await flush()
    assert.equal(w.counts.save, 0)
    assert.equal(w.counts.modal, 0)
    assert.deepEqual(w.events, ['send:save_detected', 'read', 'upload:psd'])
    assert.equal(w.uploads[0].text, OLD_BYTES)
  },

  async two_sends_at_once_share_a_single_save() {
    const w = await makeWorld({ layered: true, saved: false })
    const [a, b] = await Promise.all([
      w.handoffs.deliverEdit(HANDOFF),
      w.handoffs.deliverEdit(HANDOFF)
    ])
    assert.equal(a, 'sent')
    assert.equal(b, 'sent')
    assert.equal(w.counts.save, 1)
    assert.equal(w.counts.modal, 1)
    assert.ok(w.uploads.every((upload) => upload.text === NEW_BYTES))
  },

  // ---- failure: never upload stale bytes ----------------------------------

  async save_failure_sends_nothing_and_reports_save_failed() {
    const w = await makeWorld({ layered: true, saved: false, saveFails: true })
    const status = await w.handoffs.deliverEdit(HANDOFF) // must not throw
    assert.equal(status, 'save_failed')
    assert.equal(w.uploads.length, 0, 'no upload of stale bytes')
    assert.ok(!w.events.includes('read'), 'the stale file must not even be read')
    assert.ok(!w.events.includes('send:save_detected'), 'no save_detected for a send that never happened')
    assert.equal(w.record().status, 'save_failed')
    assert.match(w.record().lastError, /read-only/)
    const errors = w.log.getLogLines().filter((line) => line.level === 'error')
    assert.equal(errors.length, 1)
    assert.match(errors[0].message, /nothing was sent/)
    assert.match(errors[0].message, /read-only/)
    // A stray notification from the half-finished save must not deliver either.
    w.notify()
    await flush()
    assert.equal(w.uploads.length, 0)
  },

  async rejected_modal_scope_sends_nothing_and_reports_save_failed() {
    const w = await makeWorld({ layered: true, saved: false, modalFails: true })
    const status = await w.handoffs.deliverEdit(HANDOFF)
    assert.equal(status, 'save_failed')
    assert.equal(w.counts.save, 0)
    assert.equal(w.uploads.length, 0)
    assert.match(w.record().lastError, /modal cancelled/)
  },

  async a_failed_send_can_be_retried_once_the_problem_is_fixed() {
    const w = await makeWorld({ layered: true, saved: false, saveFails: true })
    assert.equal(await w.handoffs.deliverEdit(HANDOFF), 'save_failed')
    // Fix the cause (the file is no longer read-only) and Send again.
    w.setSaveFails(false)
    w.clock.now += GRACE_MS + 1
    const status = await w.handoffs.deliverEdit(HANDOFF)
    assert.equal(status, 'sent')
    assert.equal(w.record().lastError, null)
    assert.equal(w.uploads.length, 1)
    assert.equal(w.uploads[0].text, NEW_BYTES)
  },

  // ---- local mode: server reads the managed PSD after the PNG lands ------

  async local_layered_unsaved_saves_before_exporting() {
    const w = await makeWorld({ mode: 'local', layered: true, saved: false })
    const status = await w.handoffs.deliverEdit(HANDOFF)
    assert.equal(status, 'sent')
    assert.deepEqual(w.events, [
      'modal:enter',
      'save:begin',
      'save:end',
      'modal:exit',
      'send:save_detected',
      'export',
      'upload:png'
    ])
    assert.equal(w.counts.save, 1)
  },

  async local_layered_saved_document_is_not_saved_again() {
    const w = await makeWorld({ mode: 'local', layered: true, saved: true })
    await w.handoffs.deliverEdit(HANDOFF)
    assert.equal(w.counts.save, 0)
    assert.deepEqual(w.events, ['send:save_detected', 'export', 'upload:png'])
  },

  async local_layered_save_failure_sends_nothing() {
    const w = await makeWorld({ mode: 'local', layered: true, saved: false, saveFails: true })
    assert.equal(await w.handoffs.deliverEdit(HANDOFF), 'save_failed')
    assert.equal(w.uploads.length, 0)
    assert.ok(!w.events.includes('export'))
  },

  // ---- flat (non-layered) handoffs export the LIVE document: untouched ----

  async remote_flat_unsaved_exports_live_document_and_never_saves() {
    const w = await makeWorld({ mode: 'remote', layered: false, saved: false })
    const status = await w.handoffs.deliverEdit(HANDOFF)
    assert.equal(status, 'sent')
    assert.equal(w.counts.save, 0)
    assert.equal(w.counts.modal, 0)
    assert.deepEqual(w.events, ['send:save_detected', 'export', 'upload:png'])
  },

  async local_flat_unsaved_exports_live_document_and_never_saves() {
    const w = await makeWorld({ mode: 'local', layered: false, saved: false })
    const status = await w.handoffs.deliverEdit(HANDOFF)
    assert.equal(status, 'sent')
    assert.equal(w.counts.save, 0)
    assert.deepEqual(w.events, ['send:save_detected', 'export', 'upload:png'])
  },

  // ---- existing guard rails stay put --------------------------------------

  async unknown_handoff_is_a_noop() {
    const w = await makeWorld({ layered: true, saved: false })
    assert.equal(await w.handoffs.deliverEdit('nope'), undefined)
    assert.deepEqual(w.events, [])
  },

  async closed_document_is_a_noop() {
    const w = await makeWorld({ layered: true, saved: false })
    w.closeDocument()
    assert.equal(await w.handoffs.deliverEdit(HANDOFF), undefined)
    assert.equal(w.counts.save, 0)
    assert.deepEqual(w.events, [])
  }
}

async function main() {
  const results = {}
  for (const [name, scenario] of Object.entries(scenarios)) {
    try {
      await scenario()
      results[name] = { ok: true }
    } catch (error) {
      results[name] = { ok: false, error: String((error && error.stack) || error) }
    }
  }
  process.stdout.write(JSON.stringify(results))
}

if (require.main === module) {
  main()
}

module.exports = { scenarios }
