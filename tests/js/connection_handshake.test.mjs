/**
 * photoshop_plugin/connection.js _completeHandshake awaits the local-mode
 * probe. A socket that closes during that await (superseded with close code
 * 4000, a ping timeout, the user pressing Disconnect) must keep the state its
 * own close handling set -- not be overwritten with "connected".
 */

import assert from 'node:assert/strict'
import test from 'node:test'

import { deferred, flush, loadCjs } from './harness.mjs'

class FakeWebSocket {
  static instances = []
  constructor(url) {
    this.url = url
    this.readyState = 1
    this.sent = []
    FakeWebSocket.instances.push(this)
  }
  send(data) {
    this.sent.push(JSON.parse(data))
  }
  close() {
    this.readyState = 3
  }
  serverSends(message) {
    this.onmessage({ data: JSON.stringify(message) })
  }
}

/** Boots a connection that has received hello_ack and is parked in the probe. */
function bootInProbe() {
  FakeWebSocket.instances = []
  const probe = deferred()
  const { connection } = loadCjs(
    'photoshop_plugin/connection.js',
    {
      './log.js': 'real',
      './prefs.js': { ensureMaximizeCompatibility: async () => {} },
      uxp: {
        versions: { plugin: '1', uxp: '1' },
        host: { version: '1' },
        storage: { localFileSystem: { getEntryWithUrl: () => probe.promise } }
      }
    },
    {
      WebSocket: FakeWebSocket,
      EventTarget,
      CustomEvent,
      setTimeout: (fn, ms) => setTimeout(fn, ms).unref(),
      clearTimeout
    }
  )
  connection.connect()
  const socket = FakeWebSocket.instances[0]
  socket.onopen()
  socket.serverSends({ type: 'hello_ack', server_version: '1', input_cpsb_path: '/x' })
  return { connection, socket, probe }
}

test('a socket superseded (close 4000) during the probe stays in standby, not "connected"', async () => {
  const { connection, socket, probe } = bootInProbe()
  socket.readyState = 3
  socket.onclose({ code: 4000, reason: 'replaced by a new connection' })
  const before = connection.getState()
  assert.equal(before.status, 'disconnected')
  assert.equal(before.standby, 'superseded')
  probe.reject(new Error('remote'))
  await flush()
  const after = connection.getState()
  assert.equal(after.status, 'disconnected')
  assert.equal(after.standby, 'superseded')
  assert.equal(socket.sent.some((m) => m.type === 'ready'), false)
})

test('pressing Disconnect during the probe stays disconnected, not "connected"', async () => {
  const { connection, socket, probe } = bootInProbe()
  connection.disconnect()
  const before = connection.getState()
  assert.notEqual(before.status, 'connected')
  probe.reject(new Error('remote'))
  await flush()
  const after = connection.getState()
  assert.equal(after.status, before.status)
  assert.equal(after.standby, before.standby)
  assert.equal(socket.sent.some((m) => m.type === 'ready'), false)
})

test('a handshake whose socket survives the probe still completes', async () => {
  const { connection, socket, probe } = bootInProbe()
  probe.reject(new Error('remote'))
  await flush()
  assert.equal(connection.getState().status, 'connected')
  assert.equal(JSON.stringify(socket.sent.at(-1)), JSON.stringify({ type: 'ready', local_mode: false }))
})
