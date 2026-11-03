/**
 * Tiny Node-VM harness for the frontend (`web/cpsb/*.js`, ES modules) and the
 * Photoshop plugin (`photoshop_plugin/*.js`, CommonJS) sources.
 *
 * The REAL source files are loaded unmodified into a fresh `vm` context, with
 * every import / `require` served from stubs the test supplies, so a test
 * exercises the shipped code and observes it through its stubs (calls made,
 * node modes written, frames sent) — module-private state is never reached
 * into. Each `load*` call builds a brand-new context, so tests are isolated.
 *
 * ES modules need `node --experimental-vm-modules` (tests/test_plugin_js.py
 * passes it). `CPSB_JS_ROOT` points the loader at another checkout — used to
 * prove a test fails against the unfixed sources.
 */

import fs from 'node:fs'
import path from 'node:path'
import vm from 'node:vm'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))

/** The repository root whose sources are loaded (override: CPSB_JS_ROOT). */
export const repoRoot = process.env.CPSB_JS_ROOT || path.resolve(here, '..', '..')

/**
 * @param {string} relPath - Repo-relative source path.
 * @returns {string}
 */
function readSource(relPath) {
  return fs.readFileSync(path.join(repoRoot, relPath), 'utf8')
}

/**
 * Loads an ES module source with every import served from `imports`.
 * @param {string} relPath - e.g. 'web/cpsb/live.js'.
 * @param {Record<string, Record<string, unknown>>} imports - specifier ->
 * the named exports to serve for it (exactly as written in the import).
 * @param {Record<string, unknown>} [globals] - extra globals for the context.
 * @returns {Promise<Record<string, any>>} The module namespace.
 */
export async function loadEsm(relPath, imports, globals = {}) {
  const context = vm.createContext({ console, ...globals })
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
 * Loads a CommonJS source (UXP style: `require` + `module.exports`).
 * @param {string} relPath - e.g. 'photoshop_plugin/liveMode.js'.
 * @param {Record<string, unknown>} requires - specifier -> value `require`
 * returns. A value of `'real'` loads that repo file for real (same loader,
 * same stubs), for dependency-free helpers such as log.js.
 * @param {Record<string, unknown>} [globals] - extra globals for the context
 * (setInterval, EventTarget, CustomEvent, ... are NOT implicit: the sandbox
 * only has the JS built-ins).
 * @returns {Record<string, any>} `module.exports`.
 */
export function loadCjs(relPath, requires, globals = {}) {
  const context = vm.createContext({ console, ...globals })
  const fn = vm.compileFunction(readSource(relPath), ['require', 'module', 'exports'], {
    parsingContext: context,
    filename: relPath
  })
  const module = { exports: {} }
  const require = (specifier) => {
    if (!(specifier in requires)) {
      throw new Error(`${relPath}: unexpected require("${specifier}")`)
    }
    const value = requires[specifier]
    if (value === 'real') {
      const dir = path.posix.dirname(relPath)
      return loadCjs(path.posix.join(dir, specifier), {}, globals)
    }
    return value
  }
  fn(require, module, module.exports)
  return module.exports
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
