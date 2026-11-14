/**
 * web/cpsb/pasteback.js — a `cpsb.updated` edit must only land in the
 * workflow that owns the handoff. Every workflow tab shares one `app.graph`
 * and node ids are small per-workflow integers, so a bare id lookup would
 * paste workflow A's edit into workflow B's node of the same id and
 * auto-queue the wrong workflow. `workflow_name: ""` is a documented
 * wildcard (PROTOCOL.md §1) and must still paste.
 *
 * The real pasteback.js AND the real state.js run together (state.js's
 * scoping rule is the one under test); only app/api/ui/settings are stubs.
 */

import assert from 'node:assert/strict'
import test from 'node:test'

import { loadEsm } from './harness.mjs'

/**
 * Boots both modules; the handoff `h1` (origin node 17) belongs to
 * `handoffWorkflow` while `activeWorkflow` is the open one.
 * `pendingStatus` leaves /cpsb/status unresolved (startup: the cache has not
 * loaded) until the returned `resolveStatus(known)` is called; `known` false
 * resolves it WITHOUT the handoff.
 * @param {{activeWorkflow: string, handoffWorkflow: string, origin?: string, pendingStatus?: boolean}} opts
 */
async function setup({ activeWorkflow, handoffWorkflow, origin = 'load_image', pendingStatus = false }) {
  // The open graph has its own LoadImage with id 17 showing its own photo.
  const node17 = {
    id: 17,
    widgets: [{ name: 'image', type: 'combo', value: 'own_photo.png [input]' }],
    imgs: []
  }
  const calls = { queued: 0, toasts: [], actionToasts: [] }
  const app = {
    graph: { getNodeById: (id) => (id === 17 ? node17 : null), setDirtyCanvas() {} },
    nodeOutputs: {},
    extensionManager: { workflow: { activeWorkflow: { filename: activeWorkflow } } },
    queuePrompt: async () => {
      calls.queued++
    }
  }
  const statusBody = (known) => ({
    handoffs: known
      ? [
          {
            handoff_id: 'h1',
            origin_node_id: '17',
            origin_kind: origin,
            workflow_name: handoffWorkflow,
            status: 'editing',
            source: null
          }
        ]
      : []
  })
  let resolveGate
  const gate = new Promise((resolve) => {
    resolveGate = resolve
  })
  let updatedHandler
  const api = {
    onUpdated: (cb) => {
      updatedHandler = cb
    },
    onStatusChanged() {},
    onTier2Changed() {},
    getStatus: () => (pendingStatus ? gate : Promise.resolve(statusBody(true))),
    parseImageRef: () => null,
    viewUrl: () => 'view-url',
    warn: console.warn,
    debugLog() {}
  }
  const ui = {
    showToast: (t) => calls.toasts.push(t),
    showActionToast: (t) => calls.actionToasts.push(t)
  }
  const globals = { Image: class {}, window: {} }
  const state = await loadEsm(
    'web/cpsb/state.js',
    { '../../../scripts/app.js': { app }, './api.js': api },
    globals
  )
  if (pendingStatus) state.initState() // fetch stays pending
  else await state.refresh() // seeds the handoff table from the stubbed /cpsb/status
  const pasteback = await loadEsm(
    'web/cpsb/pasteback.js',
    {
      '../../../scripts/app.js': { app },
      './api.js': api,
      './settings.js': { getAutoQueue: () => true },
      './state.js': state,
      './ui.js': ui
    },
    globals
  )
  pasteback.init()
  const fire = () =>
    updatedHandler({
      handoff_id: 'h1',
      origin_node_id: '17',
      origin_kind: origin,
      filename: 'edit_001.png',
      subfolder: 'photoshop/h1',
      type: 'input'
    })
  const resolveStatus = async (known) => {
    resolveGate(statusBody(known))
    for (let i = 0; i < 10; i++) await Promise.resolve() // let the .then chains run
  }
  return { node17, calls, fire, resolveStatus }
}

const EDIT = 'photoshop/h1/edit_001.png [input]'

test('same workflow: the edit is written to the node widget and the workflow is queued', async () => {
  const { node17, calls, fire } = await setup({ activeWorkflow: 'A', handoffWorkflow: 'A' })
  fire()
  assert.equal(node17.widgets[0].value, EDIT)
  assert.equal(calls.queued, 1)
})

test('other workflow: node untouched, nothing queued, a toast says the edit waits in the gallery', async () => {
  const { node17, calls, fire } = await setup({ activeWorkflow: 'B', handoffWorkflow: 'A' })
  fire()
  assert.equal(node17.widgets[0].value, 'own_photo.png [input]')
  assert.deepEqual(node17.imgs, [])
  assert.equal(calls.queued, 0)
  assert.equal(calls.toasts.length, 1)
  assert.match(calls.toasts[0].detail, /"A"/)
  assert.match(calls.toasts[0].detail, /gallery/)
})

test('other workflow: terminal_output and load_psd origins are not touched either', async () => {
  for (const origin of ['terminal_output', 'load_psd']) {
    const { node17, calls, fire } = await setup({
      activeWorkflow: 'B',
      handoffWorkflow: 'A',
      origin
    })
    fire()
    assert.deepEqual(node17.imgs, [], origin)
    assert.equal(calls.queued, 0, origin)
    assert.equal(calls.actionToasts.length, 0, origin)
    assert.equal(calls.toasts.length, 1, origin)
  }
})

test('wildcard: a handoff with an empty workflow_name is written and queued', async () => {
  const { node17, calls, fire } = await setup({ activeWorkflow: 'B', handoffWorkflow: '' })
  fire()
  assert.equal(node17.widgets[0].value, EDIT)
  assert.equal(calls.queued, 1)
})

test('startup: an edit arriving before the status cache loads is not written blind (other workflow)', async () => {
  const { node17, calls, fire, resolveStatus } = await setup({
    activeWorkflow: 'B',
    handoffWorkflow: 'A',
    pendingStatus: true
  })
  fire()
  await resolveStatus(true)
  assert.equal(node17.widgets[0].value, 'own_photo.png [input]')
  assert.equal(calls.queued, 0)
  assert.equal(calls.toasts.length, 1)
  assert.match(calls.toasts[0].detail, /"A"/)
})

test('startup: an edit arriving before the cache loads is applied once it resolves to this workflow', async () => {
  const { node17, calls, fire, resolveStatus } = await setup({
    activeWorkflow: 'A',
    handoffWorkflow: 'A',
    pendingStatus: true
  })
  fire()
  assert.equal(node17.widgets[0].value, 'own_photo.png [input]') // not decided yet
  await resolveStatus(true)
  assert.equal(node17.widgets[0].value, EDIT)
  assert.equal(calls.queued, 1)
})

test('a handoff still unknown after the refresh is not written; the gallery toast is shown', async () => {
  const { node17, calls, fire, resolveStatus } = await setup({
    activeWorkflow: 'B',
    handoffWorkflow: 'A',
    pendingStatus: true
  })
  fire()
  await resolveStatus(false)
  assert.equal(node17.widgets[0].value, 'own_photo.png [input]')
  assert.equal(calls.queued, 0)
  assert.equal(calls.toasts.length, 1)
  assert.match(calls.toasts[0].detail, /gallery/)
})
