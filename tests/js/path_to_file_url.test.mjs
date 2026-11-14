/**
 * photoshop_plugin/connection.js pathToFileUrl: the server's path -> `file:`
 * URL. Checked under WHATWG URL parsing (UXP's own parser is unverified): the
 * URL must parse back to exactly the original path, whatever characters the
 * folder names hold.
 */

import assert from 'node:assert/strict'
import test from 'node:test'

import { loadCjs } from './harness.mjs'

const { pathToFileUrl } = loadCjs(
  'photoshop_plugin/connection.js',
  { './log.js': 'real', './prefs.js': {}, uxp: { versions: {}, host: {}, storage: {} } },
  { EventTarget, CustomEvent }
)

/** What a WHATWG parser makes of the URL: [host, decoded path]. */
function parsed(url) {
  const u = new URL(url)
  return [u.host, decodeURIComponent(u.pathname)]
}

test('POSIX path keeps its shape', () => {
  assert.equal(pathToFileUrl('/Users/me/in/a.png'), 'file:/Users/me/in/a.png')
})

test('Windows path: backslashes become slashes, drive colon stays', () => {
  assert.equal(pathToFileUrl('C:\\Users\\me\\in\\a.png'), 'file:/C:/Users/me/in/a.png')
  assert.deepEqual(parsed(pathToFileUrl('C:\\Users\\me\\a.png')), ['', '/C:/Users/me/a.png'])
})

test('UNC path keeps host as the authority', () => {
  const url = pathToFileUrl('\\\\nas\\share\\My Art\\a.png')
  assert.equal(url, 'file://nas/share/My%20Art/a.png')
  assert.deepEqual(parsed(url), ['nas', '/share/My Art/a.png'])
})

test('a # in a folder name does not truncate the path', () => {
  assert.deepEqual(parsed(pathToFileUrl('C:\\Projects #2\\in\\a.png')), [
    '',
    '/C:/Projects #2/in/a.png'
  ])
})

test('a % in a folder name survives, including one that looks like an escape', () => {
  assert.deepEqual(parsed(pathToFileUrl('/data/100% done/a.png')), ['', '/data/100% done/a.png'])
  assert.deepEqual(parsed(pathToFileUrl('/data/50%20x/a.png')), ['', '/data/50%20x/a.png'])
})

test('? in a folder name is not read as a query', () => {
  assert.deepEqual(parsed(pathToFileUrl('/data/what?/a.png')), ['', '/data/what?/a.png'])
})

test('spaces and non-ASCII are percent-encoded and round-trip', () => {
  const path = 'C:\\Users\\Zoë Müller\\Мои файлы\\日本語 folder\\a.png'
  const url = pathToFileUrl(path)
  assert.match(url, /^file:\/C:\/Users\/Zo%C3%AB%20M%C3%BCller\//)
  assert.deepEqual(parsed(url), ['', '/C:/Users/Zoë Müller/Мои файлы/日本語 folder/a.png'])
})
