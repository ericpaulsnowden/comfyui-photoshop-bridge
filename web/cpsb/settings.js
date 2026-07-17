/**
 * @file Registers the frontend-only `cpsb.*` ComfyUI settings (PROTOCOL.md
 * §8) and provides read/write helpers: two boolean toggles, plus a
 * read-only version-info row. Distinct from the backend-persisted settings
 * exposed via `GET/POST /cpsb/settings` (`api.getBackendSettings` /
 * `api.updateBackendSettings`), which are not edited from this frontend.
 *
 * Registration shape verified against `Comfy-Org/ComfyUI_frontend`
 * `src/types/comfy.ts` (`ComfyExtension.settings: SettingParams[]`) and
 * `src/platform/settings/types.ts`; read/write against
 * `src/types/extensionTypes.ts` (`ExtensionManager.setting`).
 *
 * Read-only display, investigated for the version-info row: ComfyUI's
 * settings API has no dedicated "readonly" input type —
 * `src/platform/settings/types.ts`'s `SettingInputType` union is `boolean |
 * number | slider | knob | combo | radio | text | image | color | url |
 * hidden | backgroundImage`, none of which is display-only, and `hidden`
 * means "not shown in the panel at all" (used throughout `coreSettings.ts`
 * for internal state), not "shown but disabled." However `FormItem.type`
 * (same file) is typed `SettingInputType | SettingCustomRenderer`, where
 * `SettingCustomRenderer = (name, setter, value, attrs) => HTMLElement` — a
 * *function* is a first-class alternative to a data-type string.
 * `src/components/common/FormItem.vue` confirms this is a deliberate,
 * fully-supported branch, not an accident: `getFormComponent` routes
 * `typeof item.type === 'function'` to a dedicated `CustomFormValue`
 * component (`getFormAttrs` wraps it as `attrs.renderFunction`), and
 * `src/components/common/CustomFormValue.vue` mounts the returned element
 * directly (`container.appendChild(element)`) — no `<input>`/toggle/select
 * is ever created. That is genuinely read-only (no control to disable),
 * strictly cleaner than the documented fallback of a disabled `type: 'text'`
 * input, so this file uses the custom-renderer form for the version row
 * (see `renderVersionRow` below).
 */

import { app } from '../../../scripts/app.js'
import { warn, FRONTEND_VERSION } from './api.js'
import * as state from './state.js'
import { el, injectStyles } from './ui.js'

/**
 * @typedef {Object} CpsbSettingParams
 * Minimal shape of a ComfyUI `SettingParams` entry actually used here: two
 * boolean toggles, plus one purely-informational row whose `type` is a
 * "custom renderer" function instead of a data-type string (see the file
 * header for the verified source references).
 * @property {string} id - Dot-namespaced, e.g. `"cpsb.autoQueue"`.
 * @property {string} name - Row label in the settings panel.
 * @property {"boolean" | ((name: string, setter: (v: unknown) => void, value: unknown, attrs?: Record<string, unknown>) => HTMLElement)} type
 * @property {boolean | string} defaultValue
 * @property {string[]} category - `[tab, group, rowLabel]`.
 * @property {string} [tooltip]
 */

/**
 * Custom read-only "form control" for the version-info settings row —
 * `SettingCustomRenderer`'s signature is `(name, setter, value, attrs) =>
 * HTMLElement` (see file header); this ignores all four arguments by
 * design, since the row has nothing to set, only something to show. Reads
 * live data at *call* time (this build's own {@link FRONTEND_VERSION} plus
 * `state.getServerVersion()`, populated once `/cpsb/status` resolves)
 * rather than baking a value in at module-load time — `CustomFormValue.vue`
 * re-invokes this whenever the settings row re-renders (at minimum, once on
 * first mount, which in practice is always after the initial status fetch
 * completes, since that fetch is kicked off at `setup()` and a user cannot
 * open the settings panel before the page has finished loading).
 * @returns {HTMLElement}
 */
function renderVersionRow() {
  injectStyles()
  const serverVersion = state.getServerVersion()
  const text = `Backend v${serverVersion || '?'} • Frontend v${FRONTEND_VERSION}`
  return el('span', { className: 'cpsb-version-display', text })
}

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
  },
  {
    id: 'cpsb.versionInfo',
    name: 'Version',
    type: renderVersionRow,
    defaultValue: '',
    category: ['Photoshop Bridge', 'General', 'Version'],
    tooltip:
      'The Photoshop Bridge backend (Python, this ComfyUI server) and ' +
      'frontend (this browser tab) versions. If they differ, you likely ' +
      'updated only one half — restart the ComfyUI server for a new ' +
      'backend, or hard-refresh the browser for a new frontend.'
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
