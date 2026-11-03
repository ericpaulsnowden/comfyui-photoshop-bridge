/**
 * web/cpsb/live.js — the Refine recovery paths (release review item 5).
 *
 * Every test loads the real live.js into a fresh VM context and drives it the
 * way ComfyUI does: `cpsb.*` handlers, `status` events, and a scripted
 * `app.queuePrompt`. Observation is through the stubs only: how many times
 * `queuePrompt` ran, and every write to a Refine Source node's `mode`.
 */

import assert from 'node:assert/strict'
import test from 'node:test'

import { deferred, flush, loadEsm } from './harness.mjs'

const NEVER = 2 // litegraph: muted
const BYPASS = 4 // litegraph: bypassed
const ALWAYS = 0

/** A litegraph-ish node whose `mode` writes are recorded. */
function makeNode(comfyClass, mode, widgets = []) {
  let current = mode
  const node = { comfyClass, widgets, writes: [] }
  Object.defineProperty(node, 'mode', {
    enumerable: true,
    configurable: true,
    get: () => current,
    set: (value) => {
      node.writes.push(value)
      current = value
    }
  })
  return node
}

/**
 * Builds one live.js instance + its fake ComfyUI.
 * `queuePrompt` behaviours are scripted per call: 'resolve' (default),
 * 'reject', 'throw', or 'defer' (returns a promise the test settles).
 */
async function setup({ withSources = true } = {}) {
  const liveCanvas = makeNode('PhotoshopLiveCanvas', ALWAYS, [{ name: 'auto_queue', value: 'On' }])
  const mutedSource = makeNode('PhotoshopRefineSource', NEVER)
  const bypassedSource = makeNode('PhotoshopRefineSource', BYPASS)
  const nodes = withSources ? [liveCanvas, mutedSource, bypassedSource] : [liveCanvas]

  const script = []
  const pending = []
  const h = {
    liveCanvas,
    mutedSource,
    bypassedSource,
    calls: 0,
    warnings: [],
    handlers: {},
    pending,
    /** The next queuePrompt call behaves like `kind` (default 'resolve'). */
    script(...kinds) {
      script.push(...kinds)
    },
    status(remaining) {
      comfyApi.dispatchEvent(
        new CustomEvent('status', { detail: { exec_info: { queue_remaining: remaining } } })
      )
    }
  }

  const app = {
    graph: { _nodes: nodes },
    queuePrompt(_number) {
      h.calls += 1
      const kind = script.shift() ?? 'resolve'
      if (kind === 'throw') throw new Error('queuePrompt exploded synchronously')
      if (kind === 'reject') return Promise.reject(new Error('queue rejected'))
      if (kind === 'defer') {
        const d = deferred()
        pending.push(d)
        return d.promise
      }
      return Promise.resolve(true)
    }
  }
  const comfyApi = new EventTarget()
  const cpsbApi = {
    warn: (...args) => h.warnings.push(args),
    onLive: (cb) => (h.handlers.live = cb),
    onLivePrompt: (cb) => (h.handlers.livePrompt = cb),
    onLiveCreativity: (cb) => (h.handlers.liveCreativity = cb),
    onRefine: (cb) => (h.handlers.refine = cb)
  }

  const mod = await loadEsm(
    'web/cpsb/live.js',
    {
      '../../../scripts/app.js': { app },
      '../../../scripts/api.js': { api: comfyApi },
      './api.js': cpsbApi
    },
    { EventTarget, CustomEvent }
  )
  mod.init()
  h.live = () => h.handlers.live()
  h.refine = () => h.handlers.refine()
  return h
}

const modes = (h) => [h.mutedSource.mode, h.bypassedSource.mode]
const writes = (h) => [h.mutedSource.writes, h.bypassedSource.writes]

test('baseline: a normal refine un-mutes, then restores exactly once on drain', async () => {
  const h = await setup()
  h.refine()
  assert.equal(h.calls, 1)
  assert.deepEqual(modes(h), [ALWAYS, ALWAYS])
  await flush() // queuePrompt resolved
  h.status(1)
  assert.deepEqual(modes(h), [ALWAYS, ALWAYS], 'still refining while the queue is busy')
  h.status(0)
  assert.deepEqual(modes(h), [NEVER, BYPASS])
  assert.deepEqual(writes(h), [[ALWAYS, NEVER], [ALWAYS, BYPASS]])
})

test('a rejected refine enqueue restores the modes and ends the pause', async () => {
  const h = await setup()
  h.script('reject')
  h.refine()
  assert.deepEqual(modes(h), [ALWAYS, ALWAYS])
  await flush()
  assert.deepEqual(modes(h), [NEVER, BYPASS])
  assert.deepEqual(writes(h), [[ALWAYS, NEVER], [ALWAYS, BYPASS]])
  assert.ok(h.warnings.some((w) => String(w[0]).includes('queuePrompt failed')))
  // The pause is over: a live trigger queues instead of deferring forever.
  const before = h.calls
  h.live()
  assert.equal(h.calls, before + 1)
})

test('a rejected refine enqueue resumes the live run owed from the pause, once', async () => {
  const h = await setup()
  h.script('defer')
  h.refine()
  h.live() // arrives during the pause -> trailing, not queued
  assert.equal(h.calls, 1)
  h.pending[0].reject(new Error('nope'))
  await flush()
  assert.deepEqual(modes(h), [NEVER, BYPASS])
  assert.equal(h.calls, 2, 'the trailing live run fired')
  // ...and only once: a later idle status must not fire it again.
  h.status(0)
  assert.equal(h.calls, 2)
})

test('a synchronous queuePrompt throw during refine restores at once', async () => {
  const h = await setup()
  h.script('throw')
  h.refine() // must not throw out of the handler
  assert.deepEqual(modes(h), [NEVER, BYPASS], 'restored synchronously')
  assert.deepEqual(writes(h), [[ALWAYS, NEVER], [ALWAYS, BYPASS]])
  assert.ok(h.warnings.some((w) => String(w[0]).includes('queuePrompt failed')))
  const before = h.calls
  h.live()
  assert.equal(h.calls, before + 1, 'live loop not wedged by a stuck in-flight flag')
})

test('a synchronous throw on a live fire releases the in-flight state', async () => {
  const h = await setup()
  h.script('throw')
  h.live()
  assert.equal(h.calls, 1)
  h.live() // pre-fix: deferred forever behind a never-cleared queueInFlight
  assert.equal(h.calls, 2)
})

test('a rejected live fire releases the in-flight state', async () => {
  const h = await setup()
  h.script('reject')
  h.live()
  await flush()
  h.live()
  assert.equal(h.calls, 2)
})

test('early drain: queued and finished before queuePrompt settles -> restore at settle, once', async () => {
  const h = await setup()
  h.script('defer')
  h.refine()
  h.status(1)
  h.status(0) // the whole job ran while the enqueue promise was still pending
  assert.deepEqual(modes(h), [ALWAYS, ALWAYS], 'not restored while the enqueue is in flight')
  h.pending[0].resolve(true)
  await flush()
  assert.deepEqual(modes(h), [NEVER, BYPASS])
  assert.deepEqual(writes(h), [[ALWAYS, NEVER], [ALWAYS, BYPASS]], 'one un-mute, one restore')
  const before = h.calls
  h.live()
  assert.equal(h.calls, before + 1, 'refine pause is over')
})

test('early drain also fires the live run owed from the pause', async () => {
  const h = await setup()
  h.script('defer')
  h.refine()
  h.live()
  h.status(1)
  h.status(0)
  h.pending[0].resolve(true)
  await flush()
  assert.equal(h.calls, 2)
  assert.deepEqual(modes(h), [NEVER, BYPASS])
})

test('early drain then a late rejection restores only once', async () => {
  const h = await setup()
  h.script('defer')
  h.refine()
  h.status(1)
  h.status(0)
  h.pending[0].reject(new Error('post-submit bookkeeping failed'))
  await flush()
  assert.deepEqual(modes(h), [NEVER, BYPASS])
  assert.deepEqual(writes(h), [[ALWAYS, NEVER], [ALWAYS, BYPASS]])
})

test('a stale idle status while the enqueue is in flight does not end the refine', async () => {
  const h = await setup()
  h.script('defer')
  h.refine()
  h.status(0) // old news from before our job was accepted
  assert.deepEqual(modes(h), [ALWAYS, ALWAYS])
  h.pending[0].resolve(true)
  await flush()
  assert.deepEqual(modes(h), [ALWAYS, ALWAYS], 'queue never went busy: our job is still coming')
  h.status(1)
  h.status(0)
  assert.deepEqual(modes(h), [NEVER, BYPASS])
  assert.deepEqual(writes(h), [[ALWAYS, NEVER], [ALWAYS, BYPASS]])
})

test('after a failure recovery a late drain never re-applies an old mode', async () => {
  const h = await setup()
  h.script('reject')
  h.refine()
  await flush()
  assert.deepEqual(modes(h), [NEVER, BYPASS])
  h.mutedSource.mode = ALWAYS // the user un-mutes it by hand afterwards
  h.mutedSource.writes.length = 0
  h.status(1)
  h.status(0)
  assert.equal(h.mutedSource.mode, ALWAYS)
  assert.deepEqual(h.mutedSource.writes, [])
})

test('overlapping refines: the first failing does not restore under the second', async () => {
  const h = await setup()
  h.script('defer', 'defer')
  h.refine()
  h.refine() // double click
  assert.equal(h.calls, 2)
  h.pending[0].reject(new Error('first failed'))
  await flush()
  assert.deepEqual(modes(h), [ALWAYS, ALWAYS], 'the second refine is not serialised yet')
  h.pending[1].resolve(true)
  await flush()
  h.status(1)
  h.status(0)
  // The ORIGINAL modes come back (the second click must not record "unmuted").
  assert.deepEqual(modes(h), [NEVER, BYPASS])
  assert.deepEqual(writes(h), [[ALWAYS, NEVER], [ALWAYS, BYPASS]])
})

test('overlapping refines: the last one failing recovers', async () => {
  const h = await setup()
  h.script('defer', 'defer')
  h.refine()
  h.refine()
  h.pending[0].resolve(true)
  await flush()
  assert.deepEqual(modes(h), [ALWAYS, ALWAYS])
  h.pending[1].reject(new Error('second failed'))
  await flush()
  assert.deepEqual(modes(h), [NEVER, BYPASS])
})

test('a failed live fire does not disturb a refine in progress', async () => {
  const h = await setup()
  h.script('defer', 'defer')
  h.live() // live fire still in flight...
  h.refine() // ...when Refine is clicked
  h.pending[0].reject(new Error('the live one failed'))
  await flush()
  assert.deepEqual(modes(h), [ALWAYS, ALWAYS], 'refine still running')
  h.pending[1].resolve(true)
  await flush()
  h.status(1)
  h.status(0)
  assert.deepEqual(modes(h), [NEVER, BYPASS])
})

test('early drain on a plain live fire resumes the trailing run', async () => {
  const h = await setup()
  h.script('defer')
  h.live()
  h.live() // newer frame while the enqueue is in flight -> trailing
  assert.equal(h.calls, 1)
  h.status(1)
  h.status(0)
  assert.equal(h.calls, 1, 'not while the enqueue is in flight')
  h.pending[0].resolve(true)
  await flush()
  assert.equal(h.calls, 2)
})

test('a throw while un-muting leaves nothing un-muted and nothing paused', async () => {
  const h = await setup()
  // The second source refuses the un-mute write.
  Object.defineProperty(h.bypassedSource, 'mode', {
    get: () => BYPASS,
    set: () => {
      throw new Error('mode is read-only')
    },
    configurable: true
  })
  h.refine() // handler catches + warns
  assert.equal(h.calls, 0, 'nothing queued')
  assert.equal(h.mutedSource.mode, NEVER, 'the first source was put back')
  assert.ok(h.warnings.some((w) => String(w[0]).includes('cpsb.refine')))
  h.live()
  assert.equal(h.calls, 1, 'live loop not left paused')
})

test('no Refine Source node: warns, queues nothing, pauses nothing', async () => {
  const h = await setup({ withSources: false })
  h.refine()
  assert.equal(h.calls, 0)
  assert.ok(h.warnings.some((w) => String(w[0]).includes('no "Photoshop Refine Source" node')))
  h.live()
  assert.equal(h.calls, 1)
})

test('a rejected refine enqueue while an earlier live job still runs leaves the owed run for the drain', async () => {
  const h = await setup()
  h.live() // a live fire, accepted and now executing
  await flush()
  h.status(1) // ComfyUI: that job is running
  h.script('defer')
  h.refine() // Refine clicked while the queue is busy
  h.live() // arrives during the pause -> trailing
  assert.equal(h.calls, 2)
  h.pending[0].reject(new Error('nope'))
  await flush()
  assert.deepEqual(modes(h), [NEVER, BYPASS], 'the failed refine restored the modes')
  assert.equal(h.calls, 2, 'no second run while one is still executing (single-slot rule)')
  h.status(0) // the earlier job drains
  assert.equal(h.calls, 3, 'the drain fires the owed run')
})
