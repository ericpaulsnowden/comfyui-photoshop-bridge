/**
 * @file Per-DOCUMENT persistence for the preview panel's live controls
 * (prompt text + creativity level), so a plugin/Photoshop reload restores
 * what the user last used **with that specific file** (owner ask 2026-07-24:
 * "tie to the file so you don't get it doing strange things to the wrong
 * file"). One localStorage JSON map, keyed by document identity:
 *
 * - A SAVED document keys by its absolute `path` — stable across sessions,
 *   renames of the window, and reopens.
 * - A never-saved document falls back to `title:<title>` — the best identity
 *   that exists for it (its id is session-scoped and its path throws). Two
 *   unsaved "Untitled-1"s on different days would collide, but an unsaved
 *   scratch doc carrying over a prompt is at worst mildly surprising, never
 *   wrong-file data loss.
 *
 * Entries carry a timestamp and the map is pruned to the newest
 * {@link MAX_ENTRIES} on every save, so it can't grow unboundedly. All
 * storage access is best-effort (mirrors prefs.js): a missing/broken
 * localStorage degrades to session-only behavior, never an error.
 */

const { logWarn, describeError } = require('./log.js')

/** localStorage key for the whole per-document settings map. */
const STORAGE_KEY = 'cpsb.liveDocSettings'

/** Newest-N cap on stored documents (pruned by timestamp on save). */
const MAX_ENTRIES = 50

/**
 * @typedef {Object} CpsbLiveDocSettings
 * @property {string} prompt - The panel prompt text ('' = cleared).
 * @property {string | null} creativityKey - 'low'|'medium'|'high', or null
 * when the user never picked a level for this document.
 */

/**
 * A stable storage key for *doc*, or `null` when no identity is readable.
 * @param {import('photoshop').Document | null | undefined} doc
 * @returns {string | null}
 */
function makeDocKey(doc) {
  if (!doc) return null
  try {
    const path = doc.path
    if (typeof path === 'string' && path) return path
  } catch (_error) {
    // Never-saved docs throw on .path in some Photoshop versions — fall
    // through to the title key.
  }
  try {
    const title = doc.title
    if (typeof title === 'string' && title) return `title:${title}`
  } catch (_error) {
    // No readable identity at all (closing doc?) — caller skips persistence.
  }
  return null
}

/**
 * @returns {Record<string, {prompt: string, creativityKey: string | null, ts: number}>}
 */
function readMap() {
  try {
    if (typeof localStorage === 'undefined') return {}
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return {}
    const parsed = JSON.parse(raw)
    return parsed && typeof parsed === 'object' ? parsed : {}
  } catch (_error) {
    return {}
  }
}

/**
 * The stored settings for *docKey*, or `null` when none exist.
 * @param {string | null} docKey
 * @returns {CpsbLiveDocSettings | null}
 */
function getDocSettings(docKey) {
  if (!docKey) return null
  const entry = readMap()[docKey]
  if (!entry || typeof entry !== 'object') return null
  return {
    prompt: typeof entry.prompt === 'string' ? entry.prompt : '',
    creativityKey: typeof entry.creativityKey === 'string' ? entry.creativityKey : null
  }
}

/**
 * Persists *settings* under *docKey* (no-op without a key), pruning the map
 * to the newest {@link MAX_ENTRIES}.
 * @param {string | null} docKey
 * @param {CpsbLiveDocSettings} settings
 * @returns {void}
 */
function saveDocSettings(docKey, settings) {
  if (!docKey) return
  try {
    if (typeof localStorage === 'undefined') return
    const map = readMap()
    map[docKey] = {
      prompt: settings.prompt,
      creativityKey: settings.creativityKey,
      ts: Date.now()
    }
    const keys = Object.keys(map)
    if (keys.length > MAX_ENTRIES) {
      keys
        .sort((a, b) => (map[a].ts || 0) - (map[b].ts || 0))
        .slice(0, keys.length - MAX_ENTRIES)
        .forEach((k) => delete map[k])
    }
    localStorage.setItem(STORAGE_KEY, JSON.stringify(map))
  } catch (error) {
    logWarn(`could not persist live-control settings (${describeError(error)})`)
  }
}

module.exports = { makeDocKey, getDocSettings, saveDocSettings }
