/**
 * web/cpsb/state.js — subscribers (the mounted gallery) must be notified when
 * the user switches workflow tabs, because workflow-scoped views such as the
 * gallery's Reveal button depend on the active workflow and the frontend
 * emits no extension-visible event for a tab switch. The real state.js runs
 * with a fake interval timer the test ticks by hand.
 */

import assert from 'node:assert/strict'
import test from 'node:test'

import { loadEsm } from './harness.mjs'

async function setup() {
  let active = 'A'
  const app = { extensionManager: { workflow: { get activeWorkflow() { return { filename: active } } } } }
  const timers = new Map()
  let nextId = 1
  const globals = {
    setInterval: (fn) => { timers.set(nextId, fn); return nextId++ },
    clearInterval: (id) => { timers.delete(id) }
  }
  const api = { onStatusChanged() {}, onUpdated() {}, onTier2Changed() {}, warn() {} }
  const state = await loadEsm('web/cpsb/state.js', { '../../../scripts/app.js': { app }, './api.js': api }, globals)
  const tick = () => { for (const fn of [...timers.values()]) fn() }
  return { state, tick, timers, switchTo: (name) => { active = name } }
}

test('a workflow switch notifies subscribers; no switch does not', async () => {
  const { state, tick, switchTo } = await setup()
  let notified = 0
  state.subscribe(() => { notified++ })
  tick()
  assert.equal(notified, 0)
  switchTo('B')
  tick()
  assert.equal(notified, 1)
  tick()
  assert.equal(notified, 1)
  switchTo('A')
  tick()
  assert.equal(notified, 2)
})

test('the watch stops when the last subscriber leaves', async () => {
  const { state, timers } = await setup()
  const off1 = state.subscribe(() => {})
  const off2 = state.subscribe(() => {})
  assert.equal(timers.size, 1)
  off1()
  assert.equal(timers.size, 1)
  off2()
  assert.equal(timers.size, 0)
})
