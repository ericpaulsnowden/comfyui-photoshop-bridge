/**
 * Manual Send must never deliver stale bytes (review item 6;
 * docs/PROTOCOL.md §6d, "Save before send").
 *
 * The bug: for an Annotate (`wants_layered_psd`) handoff, `deliverEdit` in
 * photoshop_plugin/handoffs.js read the sandbox PSD straight off disk without
 * saving the open document first, so strokes painted since the last Cmd/Ctrl+S
 * never left Photoshop while the panel reported "sent". The fix saves an
 * unsaved document (inside `executeAsModal`) before a MANUAL Send reads it,
 * suppresses the save listener's echo of that save so one Send delivers once,
 * and refuses to send anything when the save fails.
 *
 * Runs the REAL handoffs.js / saveListener.js / log.js in one `loadWorld`
 * against fakes of Photoshop, UXP storage, the websocket connection, the
 * exporter and the uploader. Each test builds a fresh world, opens the handoff
 * through the real `openHandoff` (so the sandbox file wiring is exercised too),
 * then drives `deliverEdit` and/or the save listener, and asserts on the
 * ORDERED event log.
 *
 * What this can NOT prove (only real Photoshop can): that `Document.saved`
 * flips on a painted stroke and back on a save, that `Document.save()` shows no
 * dialog, and when Photoshop delivers the `save` notification relative to the
 * save promise -- see the manual steps in docs/PROTOCOL.md §6d.
 */

import assert from 'node:assert/strict'
import test from 'node:test'

import { loadWorld } from './harness.mjs'

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

  const loaded = loadWorld({
    imports: { photoshop, uxp },
    mocks: { ...mocks, 'log.js': 'real', 'handoffs.js': 'real' },
    clock: { now: 1_000_000 }
  })
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

// ---- the bug: remote + layered ------------------------------------------

test('remote layered unsaved saves first then uploads fresh bytes', async () => {
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
})

test('remote layered saved document is not saved again', async () => {
  const w = await makeWorld({ mode: 'remote', layered: true, saved: true })
  const status = await w.handoffs.deliverEdit(HANDOFF)
  assert.equal(status, 'sent')
  assert.deepEqual(w.events, ['send:save_detected', 'read', 'upload:psd'])
  assert.equal(w.counts.save, 0)
  assert.equal(w.counts.modal, 0)
  assert.equal(w.uploads[0].text, OLD_BYTES) // the disk copy IS current
})

test('remote layered unknown saved state is saved defensively', async () => {
  // A Photoshop build that does not expose Document.saved: never skip the save.
  const w = await makeWorld({ mode: 'remote', layered: true, saved: undefined })
  assert.equal(w.doc.saved, undefined)
  await w.handoffs.deliverEdit(HANDOFF)
  assert.equal(w.counts.save, 1)
  assert.equal(w.uploads[0].text, NEW_BYTES)
})

// ---- double delivery: the save listener must ignore our own save --------

test('own save notification during the save does not deliver twice', async () => {
  const w = await makeWorld({ layered: true, saved: false, notifyDuringSave: true })
  await w.handoffs.deliverEdit(HANDOFF)
  await flush()
  assert.equal(w.uploads.length, 1, 'echo during the save must be ignored')
  assert.equal(w.counts.save, 1)
})

test('own save notification arriving after the save settles does not deliver twice', async () => {
  const w = await makeWorld({ layered: true, saved: false })
  await w.handoffs.deliverEdit(HANDOFF)
  w.clock.now += GRACE_MS - 1 // late, but inside the grace window
  w.notify()
  await flush()
  assert.equal(w.uploads.length, 1)
})

test('real native save after the grace window still delivers', async () => {
  const w = await makeWorld({ layered: true, saved: false })
  await w.handoffs.deliverEdit(HANDOFF)
  assert.equal(w.uploads.length, 1)
  w.clock.now += GRACE_MS + 1
  w.notify() // the user's own Cmd/Ctrl+S
  await flush()
  assert.equal(w.uploads.length, 2, 'suppression must expire, not swallow real saves')
  assert.equal(w.counts.save, 1, 'the listener path never saves on its own')
  assert.equal(w.uploads[1].text, NEW_BYTES)
})

test('native save path never triggers a save even if the flag lags', async () => {
  // The listener's trigger is a save Photoshop just finished; doc.saved may
  // not have flipped yet. The dirty check must be skipped there.
  const w = await makeWorld({ layered: true, saved: false })
  w.notify()
  await flush()
  assert.equal(w.counts.save, 0)
  assert.equal(w.counts.modal, 0)
  assert.deepEqual(w.events, ['send:save_detected', 'read', 'upload:psd'])
  assert.equal(w.uploads[0].text, OLD_BYTES)
})

test('two sends at once share a single save', async () => {
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
})

// ---- failure: never upload stale bytes ----------------------------------

test('save failure sends nothing and reports save failed', async () => {
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
})

test('rejected modal scope sends nothing and reports save failed', async () => {
  const w = await makeWorld({ layered: true, saved: false, modalFails: true })
  const status = await w.handoffs.deliverEdit(HANDOFF)
  assert.equal(status, 'save_failed')
  assert.equal(w.counts.save, 0)
  assert.equal(w.uploads.length, 0)
  assert.match(w.record().lastError, /modal cancelled/)
})

test('a failed send can be retried once the problem is fixed', async () => {
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
})

// ---- local mode: server reads the managed PSD after the PNG lands ------

test('local layered unsaved saves before exporting', async () => {
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
})

test('local layered saved document is not saved again', async () => {
  const w = await makeWorld({ mode: 'local', layered: true, saved: true })
  await w.handoffs.deliverEdit(HANDOFF)
  assert.equal(w.counts.save, 0)
  assert.deepEqual(w.events, ['send:save_detected', 'export', 'upload:png'])
})

test('local layered save failure sends nothing', async () => {
  const w = await makeWorld({ mode: 'local', layered: true, saved: false, saveFails: true })
  assert.equal(await w.handoffs.deliverEdit(HANDOFF), 'save_failed')
  assert.equal(w.uploads.length, 0)
  assert.ok(!w.events.includes('export'))
})

// ---- flat (non-layered) handoffs export the LIVE document: untouched ----

test('remote flat unsaved exports live document and never saves', async () => {
  const w = await makeWorld({ mode: 'remote', layered: false, saved: false })
  const status = await w.handoffs.deliverEdit(HANDOFF)
  assert.equal(status, 'sent')
  assert.equal(w.counts.save, 0)
  assert.equal(w.counts.modal, 0)
  assert.deepEqual(w.events, ['send:save_detected', 'export', 'upload:png'])
})

test('local flat unsaved exports live document and never saves', async () => {
  const w = await makeWorld({ mode: 'local', layered: false, saved: false })
  const status = await w.handoffs.deliverEdit(HANDOFF)
  assert.equal(status, 'sent')
  assert.equal(w.counts.save, 0)
  assert.deepEqual(w.events, ['send:save_detected', 'export', 'upload:png'])
})

// ---- existing guard rails stay put --------------------------------------

test('unknown handoff is a noop', async () => {
  const w = await makeWorld({ layered: true, saved: false })
  assert.equal(await w.handoffs.deliverEdit('nope'), undefined)
  assert.deepEqual(w.events, [])
})

test('closed document is a noop', async () => {
  const w = await makeWorld({ layered: true, saved: false })
  w.closeDocument()
  assert.equal(await w.handoffs.deliverEdit(HANDOFF), undefined)
  assert.equal(w.counts.save, 0)
  assert.deepEqual(w.events, [])
})
