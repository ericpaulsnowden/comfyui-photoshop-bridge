/**
 * photoshop_plugin/uploader.js + connection.js — the server's transfer-limit
 * rejection reasons (docs/PROTOCOL.md §3 "Transfer limits"). A `too_large`
 * upload_error must NOT be retried: the identical bytes would only be
 * streamed across twice more and refused again. `busy` (transient) and
 * `malformed` keep the existing retry behaviour.
 *
 * The real uploader.js (and log.js) run in a fresh VM context against a stub
 * connection that records every transfer attempt; connection.js runs against
 * a fake WebSocket, so the reason is observed arriving from a real
 * `manual_push_error` / `refine_error` frame.
 */

import assert from 'node:assert/strict'
import test from 'node:test'

import { loadCjs } from './harness.mjs'

/** Zero-delay timers: the uploader's 1 s / 2 s retry backoff would otherwise slow the suite. */
const instantTimers = {
  setTimeout: (fn) => setImmediate(fn),
  clearTimeout: () => {}
}

/**
 * Boots uploader.js in REMOTE mode against a connection whose every transfer
 * is rejected the way the server would reject it.
 * @param {string | undefined} reason - The `upload_error`/`manual_push_error` reason.
 */
function setupUploader(reason) {
  const attempts = []
  const reject = (what) => {
    attempts.push(what)
    // A plain object, not an Error: an Error built in this realm is not
    // `instanceof Error` inside the VM, and uploader.js only reads
    // `.message` / `.reason` from it anyway (as connection.js sets them).
    throw { message: `rejected (${reason})`, reason }
  }
  const connection = {
    getState: () => ({ localMode: false }),
    uploadEditOverWs: async (_handoffId, _bytes, kind) => reject(`upload_edit:${kind}`),
    pushManualSendOverWs: async () => reject('manual_push')
  }
  const uploader = loadCjs(
    'photoshop_plugin/uploader.js',
    { './connection.js': { connection }, './log.js': 'real' },
    instantTimers
  )
  return { uploader, attempts }
}

const BYTES = new Uint8Array([1, 2, 3, 4])

test('a too_large upload_error is not retried (flat PNG upload)', async () => {
  const { uploader, attempts } = setupUploader('too_large')
  assert.equal(await uploader.uploadEdit('h1', BYTES), false)
  assert.deepEqual(attempts, ['upload_edit:png'])
})

test('a too_large upload_error is not retried (layered PSD upload)', async () => {
  const { uploader, attempts } = setupUploader('too_large')
  assert.equal(await uploader.uploadLayeredPsd('h1', BYTES), false)
  assert.deepEqual(attempts, ['upload_edit:psd'])
})

test('busy and malformed upload_errors are still retried', async () => {
  for (const reason of ['busy', 'malformed', undefined]) {
    const { uploader, attempts } = setupUploader(reason)
    assert.equal(await uploader.uploadEdit('h1', BYTES), false)
    assert.equal(attempts.length, 3, `reason ${reason} should use all 3 attempts`)
  }
})

test('unknown_handoff / inactive are still not retried', async () => {
  for (const reason of ['unknown_handoff', 'inactive']) {
    const { uploader, attempts } = setupUploader(reason)
    assert.equal(await uploader.uploadEdit('h1', BYTES), false)
    assert.equal(attempts.length, 1, `reason ${reason} should not be retried`)
  }
})

test('a too_large manual_push_error is not retried', async () => {
  const { uploader, attempts } = setupUploader('too_large')
  assert.equal(await uploader.pushManualSend('Layer 1', BYTES), null)
  assert.deepEqual(attempts, ['manual_push'])
})

/** A WebSocket stand-in: records sent frames; the test plays the server via `onmessage`. */
class FakeWebSocket {
  static instances = []

  constructor(url) {
    this.url = url
    this.readyState = 1 // OPEN: the transfer methods only check this
    this.sent = []
    FakeWebSocket.instances.push(this)
  }

  send(data) {
    this.sent.push(JSON.parse(data))
  }

  close() {}

  /** Delivers one server frame the way UXP's WebSocket would. */
  serverSends(message) {
    this.onmessage({ data: JSON.stringify(message) })
  }
}

function setupConnection() {
  FakeWebSocket.instances = []
  const { connection } = loadCjs(
    'photoshop_plugin/connection.js',
    {
      './log.js': 'real',
      './prefs.js': { ensureMaximizeCompatibility: async () => {} },
      uxp: {
        versions: { plugin: '0.0.0', uxp: '0.0' },
        host: { version: '0' },
        storage: { localFileSystem: {} } // only touched by the handshake, never here
      }
    },
    {
      WebSocket: FakeWebSocket,
      EventTarget,
      CustomEvent,
      // Unref'd so a pending reconnect/transfer timer never keeps Node alive.
      setTimeout: (fn, ms) => setTimeout(fn, ms).unref(),
      clearTimeout
    }
  )
  connection.connect()
  const socket = FakeWebSocket.instances.at(-1)
  return { connection, socket }
}

test('manual_push_error carries its reason onto the rejected Error', async () => {
  const { connection, socket } = setupConnection()
  const pushed = connection.pushManualSendOverWs('push-1', BYTES, 'Layer 1')
  assert.ok(socket.sent.some((m) => m.type === 'manual_push' && m.push_id === 'push-1'))
  // Expectation attached BEFORE the frame arrives: the reply rejects synchronously.
  const rejected = assert.rejects(pushed, (error) => error.reason === 'too_large')
  socket.serverSends({
    type: 'manual_push_error',
    push_id: 'push-1',
    error: 'Transfer exceeds the 1073741824-character size limit',
    reason: 'too_large'
  })
  await rejected
  connection.disconnect()
})

test('refine_error carries its reason onto the rejected Error', async () => {
  const { connection, socket } = setupConnection()
  const refined = connection.pushRefineOverWs('refine-1', BYTES)
  const rejected = assert.rejects(refined, (error) => error.reason === 'too_large')
  socket.serverSends({
    type: 'refine_error',
    refine_id: 'refine-1',
    error: 'Transfer declares 5000 chunks; the limit is 4096',
    reason: 'too_large'
  })
  await rejected
  connection.disconnect()
})

test('a reason-less manual_push_error (older server) still rejects, with no reason', async () => {
  const { connection, socket } = setupConnection()
  const pushed = connection.pushManualSendOverWs('push-2', BYTES, 'Layer 1')
  const rejected = assert.rejects(
    pushed,
    (error) => error.message === 'Invalid base64' && !('reason' in error)
  )
  socket.serverSends({ type: 'manual_push_error', push_id: 'push-2', error: 'Invalid base64' })
  await rejected
  connection.disconnect()
})
