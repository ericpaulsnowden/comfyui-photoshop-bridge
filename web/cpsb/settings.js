/**
 * @file Registers the two frontend-only `cpsb.*` ComfyUI settings
 * (PROTOCOL.md §8) and provides read/write helpers. Distinct from the
 * backend-persisted settings exposed via `GET/POST /cpsb/settings`
 * (`api.getBackendSettings` / `api.updateBackendSettings`), which are not
 * edited from this frontend.
 *
 * Registration shape verified against `Comfy-Org/ComfyUI_frontend`
 * `src/types/comfy.ts` (`ComfyExtension.settings: SettingParams[]`) and
 * `src/platform/settings/types.ts`; read/write against
 * `src/types/extensionTypes.ts` (`ExtensionManager.setting`).
 */

import { app } from '../../../scripts/app.js'
import { warn } from './api.js'

/**
 * @typedef {Object} CpsbSettingParams
 * Minimal shape of a ComfyUI `SettingParams` entry — only the fields this
 * extension actually uses (both settings are simple booleans).
 * @property {string} id - Dot-namespaced, e.g. `"cpsb.autoQueue"`.
 * @property {string} name - Row label in the settings panel.
 * @property {"boolean"} type
 * @property {boolean} defaultValue
 * @property {string[]} category - `[tab, group, rowLabel]`.
 * @property {string} [tooltip]
 */

/**
 * Passed verbatim as the `settings` field of the single
 * `app.registerExtension(...)` call in `cpsb.js`. A plain data field like
 * this is inert on any frontend that predates the settings API, so no
 * feature-detection is needed here — only at the point where a setting's
 * value is actually read or written (below).
 * @type {CpsbSettingParams[]}
 */
export const SETTINGS = [
  {
    id: 'cpsb.autoQueue',
    name: 'Re-queue the workflow when an edit returns',
    type: 'boolean',
    defaultValue: true,
    category: ['Photoshop Bridge', 'General', 'Re-queue after edit'],
    tooltip:
      'When on: as soon as an edit comes back from Photoshop, the workflow ' +
      'automatically re-queues — ComfyUI’s own caching means only the ' +
      'changed node and whatever is downstream of it actually re-run, not ' +
      'the whole graph. When off: the edit still lands on the node right ' +
      'away, but silently — nothing re-runs until you queue the workflow ' +
      'yourself. Applies to Load Image nodes and Photoshop Bridge nodes; ' +
      'has no effect for output-only (terminal) nodes.'
  },
  {
    id: 'cpsb.showUpgradeBanner',
    name: 'Show Photoshop panel upgrade banner',
    type: 'boolean',
    defaultValue: true,
    category: ['Photoshop Bridge', 'General', 'Show upgrade banner'],
    tooltip:
      'Show a dismissible banner in the Photoshop Edits sidebar tab ' +
      'suggesting the ComfyUI panel plugin for Photoshop (instant round ' +
      'trips, remote ComfyUI support).'
  }
]

/**
 * @param {string} id
 * @param {boolean} fallback
 * @returns {boolean}
 */
function readBooleanSetting(id, fallback) {
  const settingApi = app.extensionManager?.setting
  if (!settingApi || typeof settingApi.get !== 'function') return fallback
  try {
    const value = settingApi.get(id)
    return typeof value === 'boolean' ? value : fallback
  } catch (error) {
    warn(`failed to read setting "${id}", using default`, error)
    return fallback
  }
}

/**
 * @param {string} id
 * @param {boolean} value
 */
function writeBooleanSetting(id, value) {
  const settingApi = app.extensionManager?.setting
  if (!settingApi || typeof settingApi.set !== 'function') {
    warn(
      `cannot persist setting "${id}" — app.extensionManager.setting is ` +
        'unavailable on this frontend; the in-memory default will be used ' +
        'until the page reloads'
    )
    return
  }
  try {
    settingApi.set(id, value)
  } catch (error) {
    warn(`failed to write setting "${id}"`, error)
  }
}

/**
 * @returns {boolean} Current value of `cpsb.autoQueue` (default `true`).
 */
export function getAutoQueue() {
  return readBooleanSetting('cpsb.autoQueue', true)
}

/**
 * @returns {boolean} Current value of `cpsb.showUpgradeBanner` (default `true`).
 */
export function getShowUpgradeBanner() {
  return readBooleanSetting('cpsb.showUpgradeBanner', true)
}

/**
 * @param {boolean} value
 */
export function setShowUpgradeBanner(value) {
  writeBooleanSetting('cpsb.showUpgradeBanner', value)
}
