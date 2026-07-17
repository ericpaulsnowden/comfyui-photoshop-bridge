/**
 * @file Entry point for the comfyui-photoshop-bridge frontend extension.
 * ComfyUI auto-imports every top-level `.js` file under `WEB_DIRECTORY`
 * (`./web`, set by the Python backend) — this is the only such file; every
 * other module lives under `cpsb/` and is wired together here into exactly
 * one `app.registerExtension(...)` call, as required so the modern
 * `getNodeMenuItems` hook and the legacy `getExtraMenuOptions` monkeypatch
 * (installed conditionally by `menu.js`) are never both active for the same
 * node — see `cpsb/menu.js` for why that would duplicate menu items.
 *
 * Each hook below defensively catches errors from its module so one broken
 * sub-feature (e.g. an unexpected node-definition shape, or a missing
 * frontend API on some future/past ComfyUI version) never prevents the
 * others from loading, and never surfaces as an uncaught exception during
 * ComfyUI's own startup or node registration.
 */

import { app } from '../../scripts/app.js'
import * as api from './cpsb/api.js'
import * as state from './cpsb/state.js'
import * as menu from './cpsb/menu.js'
import * as pasteback from './cpsb/pasteback.js'
import * as badges from './cpsb/badges.js'
import * as gallery from './cpsb/gallery.js'
import { SETTINGS } from './cpsb/settings.js'

/**
 * Runs `fn`, logging and swallowing any thrown/rejected error with the
 * project's `[cpsb]` prefix instead of letting it propagate.
 * @param {string} label
 * @param {() => unknown} fn
 */
function safely(label, fn) {
  try {
    const result = fn()
    if (result && typeof result.catch === 'function') {
      result.catch((error) => api.warn(`${label} failed`, error))
    }
  } catch (error) {
    api.warn(`${label} failed`, error)
  }
}

app.registerExtension({
  name: 'cpsb.PhotoshopBridge',
  settings: SETTINGS,

  /**
   * Fires once per node type at startup (and again for any type re-sent by
   * `app.reloadNodeDefs()`). Captures image-upload-widget metadata for
   * `menu.js`'s origin-kind derivation and installs the legacy context-menu
   * fallback when this frontend lacks the modern `getNodeMenuItems` hook.
   */
  beforeRegisterNodeDef(nodeType, nodeData) {
    safely('menu.captureImageUploadType', () => menu.captureImageUploadType(nodeData))
    safely('menu.installLegacyFallback', () => menu.installLegacyFallback(nodeType))
  },

  /**
   * Modern context-menu hook (PROTOCOL.md §8). Always provided — it is a
   * no-op on frontends that don't invoke it, which is exactly how
   * `menu.js` decides whether the legacy fallback above is needed at all.
   */
  getNodeMenuItems(node) {
    try {
      return menu.getNodeMenuItems(node)
    } catch (error) {
      api.warn('menu.getNodeMenuItems failed', error)
      return []
    }
  },

  /**
   * Fires once per node instance. Installs the chained `onDrawForeground`
   * badge hook (badges.js).
   */
  nodeCreated(node) {
    safely('badges.installBadgeHook', () => badges.installBadgeHook(node))
  },

  /**
   * Fires once, after ComfyUI has finished starting up. Wires the
   * `cpsb.updated` paste-back handler, the node-badge event subscriptions,
   * the sidebar gallery tab, and seeds the client-side handoff cache from
   * `GET /cpsb/status`.
   */
  async setup() {
    safely('pasteback.init', () => pasteback.init())
    safely('badges.init', () => badges.init())
    safely('gallery.registerGalleryTab', () => gallery.registerGalleryTab())
    safely('state.initState', () => state.initState())
  }
})
