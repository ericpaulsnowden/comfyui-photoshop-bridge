/**
 * web/cpsb/state.js refresh(): a `cpsb.status` event that fires AFTER a
 * /cpsb/status request was issued must still get a fresh fetch, otherwise the
 * cache keeps what the server answered BEFORE the change (a card shows
 * "Pending" instead of "Editing" until the next event).
 */

import assert from 'node:assert/strict'
import test from 'node:test'

import { deferred, flush, loadEsm } from './harness.mjs'

/** A fake server: each GET answers with the status it holds when the request is ISSUED. */
async function setup() {
  const server = { status: 'pending' }
  const calls = []
  let onStatus = () => {}
  const api = {
    warn: () => {},
    getStatus: () => {
      const answer = { handoffs: [{ handoff_id: 'h1', status: server.status }] }
      const reply = deferred()
      calls.push(reply)
      return reply.promise.then(() => answer)
    },
    onStatusChanged: (cb) => {
      onStatus = cb
    },
    onUpdated: () => {},
    onTier2Changed: () => {}
  }
  const state = await loadEsm('web/cpsb/state.js', {
    '../../../scripts/app.js': { app: {} },
    './api.js': api
  })
  return { state, server, calls, fire: () => onStatus({ handoff_id: 'h1' }) }
}

test('an event during an in-flight fetch triggers one follow-up fetch, so the cache converges', async () => {
  const { state, server, calls, fire } = await setup()
  state.initState() // request #1 issued while the server says pending
  server.status = 'editing' // the plugin opened the doc; server emits cpsb.status
  fire()
  calls[0].resolve() // request #1 answers with the stale "pending"
  await flush()
  assert.equal(calls.length, 2, 'a follow-up fetch must be issued')
  calls[1].resolve()
  await flush()
  assert.equal(state.getHandoffById('h1').status, 'editing')
})

test('a burst of events during one fetch coalesces into a single follow-up fetch', async () => {
  const { state, calls, fire } = await setup()
  state.initState()
  fire()
  fire()
  fire()
  calls[0].resolve()
  await flush()
  assert.equal(calls.length, 2, 'one extra fetch, not one per event')
  calls[1].resolve()
  await flush()
  assert.equal(calls.length, 2)
})

test('with no event during the fetch there is no follow-up', async () => {
  const { state, calls } = await setup()
  const done = state.initState()
  calls[0].resolve()
  await done
  assert.equal(calls.length, 1)
})
