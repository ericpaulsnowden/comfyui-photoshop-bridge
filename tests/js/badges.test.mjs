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
  const calls = { refresh: 0 }
  const api = {
    warn: () => {},
    onStatusChanged: (fn) => (cb.status = fn),
    onUpdated: () => {},
    onReconnected: (fn) => (cb.reconnected = fn)
  }
  const state = {
    subscribe: (fn) => (cb.stateChanged = fn),
    getHandoffById: (id) => handoffs.get(id),
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
  return { handoffs, cb, calls, spinning }
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
