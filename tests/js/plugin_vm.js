'use strict'
/**
 * Node-VM loader for the Photoshop UXP plugin's CommonJS modules.
 *
 * The plugin is plain CommonJS (`require('./x.js')`, `module.exports`) that
 * only ever runs inside Photoshop's UXP runtime, so there is nothing to
 * `require()` it from a normal Node test. This loads the REAL source files
 * into a fresh `vm` context with a custom `require` that:
 *
 *   - hands out fakes for the host modules (`photoshop`, `uxp`),
 *   - hands out caller-supplied mocks for any sibling module named in
 *     `mocks` (keyed by basename, e.g. `'uploader.js'`),
 *   - loads every other relative module from `photoshop_plugin/` for real.
 *
 * Module state is per-`loadPluginWorld()` call, so every scenario starts from
 * a pristine registry (handoffs.js keeps its record maps at module scope).
 *
 * The context's clock is injectable: modules see `Date.now()` as
 * `world.clock.now`, so debounce / grace windows can be crossed without
 * sleeping. `setInterval` is a no-op (handoffs.js starts a 5 s document-close
 * poll at import time that must not keep Node alive).
 */

const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')

// CPSB_PLUGIN_DIR lets a mutation check point the same scenarios at another
// copy of the plugin (e.g. the pre-fix files from `git show HEAD:...`).
const PLUGIN_DIR = process.env.CPSB_PLUGIN_DIR
  ? path.resolve(process.env.CPSB_PLUGIN_DIR)
  : path.resolve(__dirname, '..', '..', 'photoshop_plugin')

/**
 * @param {{photoshop: object, uxp: object, mocks?: Record<string, object>, clock?: {now: number}}} options
 * @returns {{load: (name: string) => any, clock: {now: number}, console: {warnings: string[]}}}
 */
function loadPluginWorld({ photoshop, uxp, mocks = {}, clock = { now: 1_000_000 } }) {
  const warnings = []

  class FakeDate extends Date {
    static now() {
      return clock.now
    }
  }

  const sandbox = {
    Date: FakeDate,
    EventTarget,
    CustomEvent,
    console: { warn: (message) => warnings.push(String(message)), log() {}, error() {} },
    setTimeout,
    clearTimeout,
    setInterval: () => 0,
    clearInterval: () => {}
  }
  const context = vm.createContext(sandbox)

  /** @type {Map<string, any>} */
  const cache = new Map()

  function require_(spec) {
    if (spec === 'photoshop') return photoshop
    if (spec === 'uxp') return uxp
    const base = path.basename(spec)
    if (Object.prototype.hasOwnProperty.call(mocks, base)) return mocks[base]
    return load(base)
  }

  function load(name) {
    if (cache.has(name)) return cache.get(name).exports
    const file = path.join(PLUGIN_DIR, name)
    const source = fs.readFileSync(file, 'utf8')
    const wrapper = vm.runInContext(
      `(function (exports, require, module, __filename, __dirname) {${source}\n})`,
      context,
      { filename: file }
    )
    const module_ = { exports: {} }
    cache.set(name, module_)
    wrapper(module_.exports, require_, module_, file, PLUGIN_DIR)
    return module_.exports
  }

  return { load, clock, console: { warnings } }
}

module.exports = { loadPluginWorld, PLUGIN_DIR }
