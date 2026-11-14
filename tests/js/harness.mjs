/**
 * The ONE Node-VM loader for the frontend (`web/cpsb/*.js`, ES modules) and
 * the Photoshop plugin (`photoshop_plugin/*.js`, CommonJS) sources.
 *
 * The REAL source files are loaded unmodified into a `vm` context, with every
 * import / `require` served from stubs the test supplies, so a test exercises
 * the shipped code and observes it through its stubs (calls made, node modes
 * written, frames sent) — module-private state is never reached into.
 *
 *   - `loadWorld({...})`  — a plugin "world": ONE context and ONE module
 *     registry, so modules that `require` each other share singletons
 *     (handoffs.js keeps its record maps at module scope). Use it for
 *     anything that loads more than one real plugin file.
 *   - `loadCjs(path, requires, globals)` — shorthand: one world, one module.
 *   - `loadEsm(path, imports, globals)`  — one web ES module.
 *
 * Every context gets the same sandbox defaults (see `makeSandbox`), each
 * overridable per call. ES modules need `node --experimental-vm-modules`
 * (tests/test_plugin_js.py passes it). `CPSB_JS_ROOT` — read only here —
 * points the loader at another checkout, to prove a test fails against the
 * unfixed sources.
 *
 * Cross-realm trap: an Error built in the TEST is not `instanceof Error`
 * inside the VM. Build errors the code under test will see with
 * `world.makeError(message, props)`.
 */

import fs from 'node:fs'
import path from 'node:path'
import vm from 'node:vm'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))

/** The repository root whose sources are loaded (override: CPSB_JS_ROOT). */
export const repoRoot = process.env.CPSB_JS_ROOT || path.resolve(here, '..', '..')

const PLUGIN_DIR = 'photoshop_plugin'

/**
 * @param {string} relPath - Repo-relative source path.
 * @returns {string}
 */
function readSource(relPath) {
  return fs.readFileSync(path.join(repoRoot, relPath), 'utf8')
}

/**
 * The globals every sandbox starts with: the real timers, `EventTarget` and
 * `CustomEvent` (UXP and browsers have them, a bare VM does not), a no-op
 * `setInterval` (handoffs.js starts a 5 s poll at import time that must not
 * keep Node alive), and the real `console`. `clock` swaps in a `Date` whose
 * `now()` reads `clock.now`, so a debounce / grace window can be crossed
 * without sleeping. `captureWarnings` routes `console.warn` into
 * `warnings` instead of the terminal. `globals` overrides any of it.
 * @param {{globals?: object, clock?: {now: number}, warnings?: string[]}} options
 * @returns {object}
 */
function makeSandbox({ globals = {}, clock, warnings } = {}) {
  const sandbox = {
    console,
    EventTarget,
    CustomEvent,
    setTimeout,
    clearTimeout,
    setInterval: () => 0,
    clearInterval: () => {}
  }
  if (clock) {
    sandbox.Date = class FakeDate extends Date {
      static now() {
        return clock.now
      }
    }
  }
  if (warnings) {
    sandbox.console = { warn: (message) => warnings.push(String(message)), log() {}, error() {} }
  }
  return { ...sandbox, ...globals }
}

/**
 * Loads an ES module source with every import served from `imports`.
 * @param {string} relPath - e.g. 'web/cpsb/live.js'.
 * @param {Record<string, Record<string, unknown>>} imports - specifier ->
 * the named exports to serve for it (exactly as written in the import).
 * @param {Record<string, unknown>} [globals] - overrides for the sandbox defaults.
 * @returns {Promise<Record<string, any>>} The module namespace.
 */
export async function loadEsm(relPath, imports, globals = {}) {
  const context = vm.createContext(makeSandbox({ globals }))
  const mod = new vm.SourceTextModule(readSource(relPath), {
    context,
    identifier: relPath
  })
  const served = new Map()
  await mod.link((specifier) => {
    if (!(specifier in imports)) {
      throw new Error(`${relPath}: unexpected import "${specifier}"`)
    }
    if (!served.has(specifier)) {
      const exportsObj = imports[specifier]
      const names = Object.keys(exportsObj)
      served.set(
        specifier,
        new vm.SyntheticModule(
          names,
          function evaluate() {
            for (const name of names) this.setExport(name, exportsObj[name])
          },
          { context }
        )
      )
    }
    return served.get(specifier)
  })
  await mod.evaluate()
  return mod.namespace
}

/**
 * A plugin world: ONE context and ONE module registry for CommonJS sources
 * (UXP style: `require` + `module.exports`). A `require` is served from:
 *   - `imports` — host modules, by exact specifier (`photoshop`, `uxp`);
 *   - `mocks`   — relative modules, by basename (`'./uploader.js'` and
 *     `'uploader.js'` are the same key).
 * A value of `'real'` loads that `photoshop_plugin/` file for real, into the
 * same context and registry (so it shares singletons with everything else).
 * Any other specifier throws "unexpected require".
 *
 * @param {object} [options]
 * @param {Record<string, unknown>} [options.imports] - host modules.
 * @param {Record<string, unknown>} [options.mocks] - relative modules or 'real'.
 * @param {Record<string, unknown>} [options.globals] - overrides for the sandbox defaults.
 * @param {{now: number}} [options.clock] - injectable `Date.now()`.
 * @param {boolean} [options.captureWarnings] - collect `console.warn` in `world.warnings`.
 * @returns {{
 *   load: (file: string) => any, clock?: {now: number}, warnings: string[],
 *   makeError: (message: string, props?: object) => Error
 * }} `load` takes a bare plugin file name (`'handoffs.js'`) or a repo-relative
 * path, and returns its `module.exports`; `makeError` builds an Error that is
 * `instanceof Error` inside the world.
 */
export function loadWorld({ imports = {}, mocks = {}, globals = {}, clock, captureWarnings = false } = {}) {
  const warnings = []
  const context = vm.createContext(
    makeSandbox({ globals, clock, warnings: captureWarnings ? warnings : undefined })
  )
  const table = { ...imports }
  for (const [key, value] of Object.entries(mocks)) table[path.basename(key)] = value
  /** @type {Map<string, {exports: any}>} module registry, by basename */
  const registry = new Map()

  function resolve(requirer, specifier) {
    const key = specifier.startsWith('.') ? path.basename(specifier) : specifier
    if (!(key in table)) throw new Error(`${requirer}: unexpected require("${specifier}")`)
    return table[key] === 'real' ? load(key) : table[key]
  }

  function load(file) {
    const relPath = file.includes('/') ? file : `${PLUGIN_DIR}/${file}`
    const name = path.basename(relPath)
    if (registry.has(name)) return registry.get(name).exports
    const absolute = path.join(repoRoot, relPath)
    const wrapper = vm.compileFunction(
      readSource(relPath),
      ['exports', 'require', 'module', '__filename', '__dirname'],
      { parsingContext: context, filename: absolute }
    )
    const module = { exports: {} }
    registry.set(name, module) // before running: a require cycle sees the partial exports
    wrapper(module.exports, (specifier) => resolve(relPath, specifier), module, absolute, path.dirname(absolute))
    return module.exports
  }

  const makeError = vm.runInContext('(message, props) => Object.assign(new Error(message), props)', context)
  return { load, clock, warnings, makeError }
}

/**
 * Loads one CommonJS source: a single-module world (see `loadWorld`).
 * @param {string} relPath - e.g. 'photoshop_plugin/liveMode.js'.
 * @param {Record<string, unknown>} requires - specifier -> value `require`
 * returns (host modules by name, relative ones by './name.js'; `'real'` loads
 * the real file).
 * @param {Record<string, unknown>} [globals] - overrides for the sandbox defaults.
 * @returns {Record<string, any>} `module.exports`.
 */
export function loadCjs(relPath, requires, globals = {}) {
  const entries = Object.entries(requires)
  const imports = Object.fromEntries(entries.filter(([key]) => !key.startsWith('.')))
  const mocks = Object.fromEntries(entries.filter(([key]) => key.startsWith('.')))
  return loadWorld({ imports, mocks, globals }).load(relPath)
}

/**
 * Lets every pending microtask and zero-delay callback run.
 * @returns {Promise<void>}
 */
export function flush() {
  return new Promise((resolve) => setImmediate(resolve))
}

/**
 * A promise with its resolvers exposed.
 * @returns {{promise: Promise<any>, resolve: (v?: any) => void, reject: (e?: any) => void}}
 */
export function deferred() {
  let resolve
  let reject
  const promise = new Promise((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}
