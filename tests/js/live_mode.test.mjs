/**
 * photoshop_plugin/liveMode.js — the reconnect baseline (release review
 * item 8). The real liveMode.js (and log.js) are loaded into a fresh VM
 * context; `photoshop`, the websocket `connection` and the poll timer are
 * stubs, so a test drives ticks by hand and reads what was SENT.
 */

import assert from 'node:assert/strict'
import test from 'node:test'

import { deferred, flush, loadCjs } from './harness.mjs'

/** A stand-in for ConnectionManager: statechange events + getState/send. */
function makeConnection() {
  const target = new EventTarget()
  const connection = {
    status: 'connected',
    sent: [],
    addEventListener: (...args) => target.addEventListener(...args),
    getState: () => ({ status: connection.status }),
    send: (message) => connection.sent.push(message),
    /** Mirrors ConnectionManager._setStatus(): set, then announce. */
    setStatus(status) {
      connection.status = status
      target.dispatchEvent(new CustomEvent('statechange', { detail: { status } }))
    }
  }
  return connection
}

/** Boots liveMode.js against a fake Photoshop with one open document. */
function setup() {
  const doc = { id: 7, title: 'sketch.psd', width: 1000, height: 800, activeHistoryState: { id: 100 } }
  const connection = makeConnection()
  const captures = [] // deferred per getPixels call when `holdCapture` is set
  const h = {
    doc,
    connection,
    captures,
    holdCapture: false,
    tickFn: null,
    frames: () => connection.sent.filter((m) => m.type === 'live_frame'),
    async tick() {
      h.tickFn()
      await flush()
    },
    /** The user paints: the document gets a new history state. */
    stroke() {
      doc.activeHistoryState = { id: doc.activeHistoryState.id + 1 }
    }
  }
  const photoshop = {
    app: { documents: [doc], activeDocument: doc },
    core: { executeAsModal: async (fn) => fn() },
    imaging: {
      getPixels: async () => {
        if (h.holdCapture) {
          const d = deferred()
          captures.push(d)
          await d.promise
        }
        return { imageData: { dispose() {} } }
      },
      encodeImageData: async () => 'JPEGBASE64'
    }
  }
  const globals = {
    EventTarget,
    CustomEvent,
    Date,
    setInterval: (fn) => {
      h.tickFn = fn
      return 1
    },
    clearInterval: () => {
      h.tickFn = null
    }
  }
  h.mod = loadCjs(
    'photoshop_plugin/liveMode.js',
    { photoshop, './connection.js': { connection }, './log.js': 'real' },
    globals
  )
  return h
}

test('baseline: start sends one frame, an unchanged canvas sends no more', async () => {
  const h = setup()
  assert.equal(h.mod.startLive(), true)
  await h.tick()
  assert.equal(h.frames().length, 1)
  await h.tick()
  await h.tick()
  assert.equal(h.frames().length, 1, 'unchanged canvas is skipped')
  h.stroke()
  await h.tick()
  assert.equal(h.frames().length, 2, 'a stroke is sent')
})

test('disconnect + reconnect WITHOUT a new stroke re-sends a baseline frame', async () => {
  const h = setup()
  h.mod.startLive()
  await h.tick()
  assert.equal(h.frames().length, 1)

  h.connection.setStatus('disconnected')
  await h.tick()
  assert.equal(h.frames().length, 1, 'nothing is sent while disconnected')

  h.connection.setStatus('connecting')
  h.connection.setStatus('connected') // handshake done: a NEW server connection
  await h.tick()
  assert.equal(h.frames().length, 2, 'the new connection gets the current canvas')
  assert.equal(h.frames()[1].doc_title, 'sketch.psd')

  await h.tick()
  assert.equal(h.frames().length, 2, 'and only one baseline, not one per tick')
})

test('a reconnect that never reports "disconnected" (server-base change) also re-baselines', async () => {
  const h = setup()
  h.mod.startLive()
  await h.tick()
  h.connection.setStatus('connecting') // _reconnectNow() goes straight to connecting
  h.connection.setStatus('connected')
  await h.tick()
  assert.equal(h.frames().length, 2)
})

test('the baseline waits for the tick: the connect event itself sends nothing', async () => {
  const h = setup()
  h.mod.startLive()
  await h.tick()
  h.connection.setStatus('disconnected')
  h.connection.setStatus('connected')
  await flush()
  assert.equal(h.frames().length, 1)
})

test('while still disconnected the baseline is not consumed', async () => {
  const h = setup()
  h.mod.startLive()
  await h.tick()
  h.connection.setStatus('disconnected')
  h.connection.setStatus('connected')
  h.connection.status = 'connecting' // the link flapped again before the next poll
  await h.tick()
  assert.equal(h.frames().length, 1)
  h.connection.setStatus('connected')
  await h.tick()
  assert.equal(h.frames().length, 2)
})

test('a stroke made while disconnected is sent once on reconnect (no duplicate baseline)', async () => {
  const h = setup()
  h.mod.startLive()
  await h.tick()
  h.connection.setStatus('disconnected')
  h.stroke()
  await h.tick()
  assert.equal(h.frames().length, 1)
  h.connection.setStatus('connected')
  await h.tick()
  await h.tick()
  assert.equal(h.frames().length, 2)
})

test('a reconnect with no live session changes nothing; a later start still baselines', async () => {
  const h = setup()
  h.connection.setStatus('disconnected')
  h.connection.setStatus('connected')
  assert.equal(h.mod.getLiveState().active, false)
  h.mod.startLive()
  await h.tick()
  assert.equal(h.frames().length, 1)
})

test('a reconnect after Live Mode was stopped does not resume sending', async () => {
  const h = setup()
  h.mod.startLive()
  await h.tick()
  h.mod.stopLive()
  h.connection.setStatus('disconnected')
  h.connection.setStatus('connected')
  assert.equal(h.tickFn, null, 'the poll timer is gone')
  assert.equal(h.frames().length, 1)
})

test('a reconnect during a capture re-baselines once the capture lands', async () => {
  const h = setup()
  h.holdCapture = true
  h.mod.startLive()
  h.tickFn() // capture starts, held inside getPixels
  await flush()
  assert.equal(h.captures.length, 1)

  h.connection.setStatus('disconnected')
  h.connection.setStatus('connected')
  h.holdCapture = false
  h.captures[0].resolve()
  await flush()
  assert.equal(h.frames().length, 1, 'the in-flight capture is sent on the new connection')

  await h.tick()
  assert.equal(h.frames().length, 2, 'then the reset marker re-sends the baseline once')
  await h.tick()
  assert.equal(h.frames().length, 2)
})

test('connection state is read from the connection, not mirrored', async () => {
  const h = setup()
  h.mod.startLive()
  h.connection.status = 'disconnected' // no statechange event delivered
  await h.tick()
  assert.equal(h.frames().length, 0, 'the tick still asks the connection')
  h.connection.status = 'connected'
  await h.tick()
  assert.equal(h.frames().length, 1)
})
