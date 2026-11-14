/**
 * web/cpsb/badges.js: the "Editing in Photoshop…" badge set must be derived
 * from the /cpsb/status cache as well as live events -- a reloaded ComfyUI
 * tab, or a terminal event missed while the browser's websocket was down,
 * would otherwise leave badges missing or spinning forever. The badge set is
 * observed through its repaint timer (it runs only while a badge exists).
 */

import assert from 'node:assert/strict'
import test from 'node:test'

import { loadEsm } from './harness.mjs'

async function setup(initial = []) {
  const handoffs = new Map(initial.map((h) => [h.handoff_id, h]))
  const cb = { status: () => {}, reconnected: () => {}, stateChanged: () => {} }
  const timer = { tick: null, cleared: false }
  const calls = { refresh: 0, cancelled: [] }
  const active = { workflow: '' }
  const api = {
    warn: () => {},
    onStatusChanged: (fn) => (cb.status = fn),
    onUpdated: () => {},
    onReconnected: (fn) => (cb.reconnected = fn),
    cancelHandoff: async (id) => {
      calls.cancelled.push(id)
    }
  }
  const state = {
    subscribe: (fn) => (cb.stateChanged = fn),
    getHandoffById: (id) => handoffs.get(id),
    workflowMatches: (name) => !active.workflow || !name || name === active.workflow,
    getAllHandoffs: () => Array.from(handoffs.values()),
    refresh: async () => {
      calls.refresh += 1
    }
  }
  const badges = await loadEsm(
    'web/cpsb/badges.js',
    {
      '../../../scripts/app.js': { app: { graph: { setDirtyCanvas: () => {} } } },
      './api.js': api,
      './state.js': state,
      './ui.js': {}
    },
    {
      setInterval: (fn) => {
        timer.tick = fn
        timer.cleared = false
        return 1
      },
      clearInterval: () => {
        timer.cleared = true
        timer.tick = null
      }
    }
  )
  badges.init()
  /** True while the badge repaint timer would still be running. */
  const spinning = () => {
    if (timer.tick) timer.tick()
    return timer.tick !== null
  }
  return { handoffs, cb, calls, spinning, active, badges }
}

const editing = { handoff_id: 'h1', origin_node_id: '7', status: 'editing' }

test('a handoff already editing in the cache gets a badge (ComfyUI tab reload)', async () => {
  const { handoffs, cb, spinning } = await setup()
  assert.equal(spinning(), false)
  handoffs.set('h1', { ...editing })
  cb.stateChanged() // initState's first fetch landed
  assert.equal(spinning(), true)
})

test('an editing handoff whose plugin document is closed gets no badge', async () => {
  const { handoffs, cb, spinning } = await setup()
  handoffs.set('h1', { ...editing, plugin_doc_open: false })
  cb.stateChanged()
  assert.equal(spinning(), false)
})

test('a badge set by a live event is cleared once the cache says the handoff ended (missed terminal event)', async () => {
  const { handoffs, cb, spinning } = await setup()
  cb.status({ origin_node_id: '7', handoff_id: 'h1', status: 'editing' })
  assert.equal(spinning(), true)
  handoffs.set('h1', { ...editing, status: 'cancelled' })
  cb.stateChanged()
  assert.equal(spinning(), false)
})

test('a live badge is not cleared by a cache that does not know the handoff yet', async () => {
  const { cb, spinning } = await setup()
  cb.status({ origin_node_id: '7', handoff_id: 'h1', status: 'editing' })
  cb.stateChanged()
  assert.equal(spinning(), true)
})

test('a ComfyUI websocket reconnect re-syncs /cpsb/status', async () => {
  const { cb, calls } = await setup()
  cb.reconnected()
  assert.equal(calls.refresh, 1)
})

test("another workflow's editing handoff on the same node id gets no badge and no cancel", async () => {
  const { handoffs, cb, calls, spinning, active, badges } = await setup()
  active.workflow = 'B.json'
  handoffs.set('wfA', { ...editing, handoff_id: 'wfA-edit', workflow_name: 'A.json' })
  cb.stateChanged()
  assert.equal(spinning(), false)

  const node = { id: 7, size: [300, 120], flags: {}, widgets: [] }
  badges.installBadgeHook(node)
  let drawn = 0
  const ctx = new Proxy({}, { get: (_t, prop) => (prop === 'measureText' ? () => ({ width: 80 }) : () => { drawn += 1 }) })
  node.onDrawForeground(ctx, null, null)
  assert.equal(drawn, 0, 'nothing drawn on the unrelated node')
  node.onMouseDown({}, [150, 60], null)
  node.onMouseDown({}, [280, 10], null)
  assert.deepEqual(calls.cancelled, [])

  // Switching to workflow A: state notifies, the next reconcile adds the badge.
  active.workflow = 'A.json'
  cb.stateChanged()
  assert.equal(spinning(), true)
})
