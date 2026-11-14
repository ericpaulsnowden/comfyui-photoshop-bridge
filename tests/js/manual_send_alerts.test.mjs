/**
 * photoshop_plugin/manualSend.js + layerReceiver.js: failures of "Send to
 * ComfyUI" / refined-layer placement must reach the user as an alert (like
 * the sibling "Send edit back" command), not only the plugin log.
 */

import assert from 'node:assert/strict'
import test from 'node:test'

import { flush, loadCjs } from './harness.mjs'

/** A DOM element stub: every method a dialog build touches, picking "Whole Document". */
function fakeElement() {
  return {
    style: {},
    appendChild() {},
    setAttribute() {},
    addEventListener() {},
    remove() {},
    close() {},
    uxpShowModal: async () => 'document'
  }
}

function setupSend({ doc = { title: 'Doc' }, exportFails = false, pushResult = 'h1' } = {}) {
  const alerts = []
  const photoshop = {
    app: { activeDocument: doc },
    core: { showAlert: async (message) => alerts.push(message), executeAsModal: async (fn) => fn() },
    constants: {}
  }
  const { sendToComfyUI } = loadCjs(
    'photoshop_plugin/manualSend.js',
    {
      photoshop,
      './log.js': 'real',
      './exporter.js': {
        runExport: async () => {
          if (exportFails) throw new Error('export boom')
          return new Uint8Array([1])
        },
        exportFlattenedDocument: async () => new Uint8Array([1])
      },
      './uploader.js': { pushManualSend: async () => pushResult },
      './connection.js': { connection: { getState: () => ({ status: 'connected' }) } }
    },
    { document: { createElement: fakeElement, body: fakeElement() } }
  )
  return { sendToComfyUI, alerts }
}

test('Send to ComfyUI with no open document alerts', async () => {
  const { sendToComfyUI, alerts } = setupSend({ doc: null })
  await sendToComfyUI()
  assert.equal(alerts.length, 1)
  assert.match(alerts[0], /no document is open/)
})

test('Send to ComfyUI export failure alerts and says nothing was sent', async () => {
  const { sendToComfyUI, alerts } = setupSend({ exportFails: true })
  await sendToComfyUI()
  assert.equal(alerts.length, 1)
  assert.match(alerts[0], /export boom/)
  assert.match(alerts[0], /nothing was sent/)
})

test('Send to ComfyUI delivery failure (push returned null) alerts', async () => {
  const { sendToComfyUI, alerts } = setupSend({ pushResult: null })
  await sendToComfyUI()
  assert.equal(alerts.length, 1)
  assert.match(alerts[0], /could not be delivered/)
})

test('a successful Send to ComfyUI shows no alert', async () => {
  const { sendToComfyUI, alerts } = setupSend()
  await sendToComfyUI()
  assert.equal(alerts.length, 0)
})

test('a failed refined-layer placement alerts', async () => {
  const alerts = []
  let onMessage
  const connection = { addEventListener: (type, fn) => type === 'message' && (onMessage = fn) }
  loadCjs(
    'photoshop_plugin/layerReceiver.js',
    {
      photoshop: { core: { showAlert: async (m) => alerts.push(m) } },
      './connection.js': { connection, base64Decode: () => new Uint8Array([1]) },
      './log.js': 'real',
      './addAsLayer.js': {
        addImageAsLayer: async () => {
          throw new Error('place boom')
        }
      },
      './prefs.js': { getRefinedLayerMode: () => 'stack' }
    }
  )
  onMessage({
    detail: { type: 'add_layer_chunk', transfer_id: 't', total: 1, data_b64: 'AA==', layer_name: 'L' }
  })
  await flush()
  assert.equal(alerts.length, 1)
  assert.match(alerts[0], /place boom/)
})
