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
import * as loadpsd from './cpsb/loadpsd.js'
import * as compose from './cpsb/compose.js'
import * as ui from './cpsb/ui.js'
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

/**
 * This extension's own GitHub repo — the target of the About-panel badge
 * below (PROTOCOL.md doesn't cover the About panel; the field itself is
 * `Comfy-Org/ComfyUI_frontend`'s `ComfyExtension.aboutPageBadges`).
 */
const REPO_URL = 'https://github.com/ericpaulsnowden/comfyui-photoshop-bridge'

let versionMismatchChecked = false

/**
 * One-time check, run once the initial `/cpsb/status` fetch resolves:
 * compares the backend's reported version against this frontend build's own
 * (PROTOCOL.md §9 only specifies the plugin<->backend `hello`/`hello_ack`
 * exchange; this is the analogous frontend<->backend check the doc doesn't
 * itself define — a product decision, not a protocol requirement). A
 * mismatch almost always means only one half of an update was applied —
 * e.g. `git pull` without restarting the ComfyUI server, or a stale browser
 * tab left open across a frontend update. The gallery header
 * (`gallery.js` `buildVersionMismatchNotice`) surfaces the same condition
 * persistently; this toast is deliberately one-time only.
 */
function checkVersionMismatch() {
  if (versionMismatchChecked) return
  versionMismatchChecked = true
  const serverVersion = state.getServerVersion()
  if (!serverVersion || serverVersion === api.FRONTEND_VERSION) return
  ui.showToast({
    severity: 'warn',
    summary: 'Photoshop Bridge version mismatch',
    detail:
      `backend v${serverVersion}, frontend v${api.FRONTEND_VERSION} — if ` +
      'you just updated, restart the ComfyUI server (backend) or ' +
      'hard-refresh the browser (frontend).'
  })
}

app.registerExtension({
  name: 'cpsb.PhotoshopBridge',
  settings: SETTINGS,
  aboutPageBadges: [
    {
      label: `Photoshop Bridge v${api.FRONTEND_VERSION}`,
      url: REPO_URL,
      icon: 'pi pi-github'
    }
  ],

  /**
   * Fires once, after the canvas is created but before any node type is
   * registered or any node instance exists (`Comfy-Org/ComfyUI_frontend`
   * `src/types/comfy.ts` `ComfyExtension.init` doc comment: "Called after
   * the canvas is created but before nodes are added" — call order
   * confirmed live in `src/scripts/app.ts` `ComfyApp.setup()`:
   * `invokeExtensionsAsync('init')` precedes `registerNodes()`, itself where
   * `beforeRegisterNodeDef` fires per type, which in turn precedes
   * `invokeExtensionsAsync('setup')` — all of which complete before a
   * restored workflow's own nodes are deserialized). Used only for
   * loadpsd.js's one-time File/FormData feature-detection, which must
   * finish before the first possible `nodeCreated` call below.
   */
  init() {
    safely('loadpsd.init', () => loadpsd.init())
  },

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
   * badge hook (badges.js) and, for Load PSD nodes only, the hand-rolled
   * upload button + drag-and-drop (loadpsd.js — PROTOCOL.md §6b: the stock
   * IMAGEUPLOAD widget can't accept `.psd`/`.psb`; see loadpsd.js's header
   * for why).
   */
  nodeCreated(node) {
    safely('badges.installBadgeHook', () => badges.installBadgeHook(node))
    safely('loadpsd.attachUploadWidget', () => loadpsd.attachUploadWidget(node))
    safely('compose.attachAutoGrowInputs', () => compose.attachAutoGrowInputs(node))
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
    // Piggybacks on the same (idempotent — see state.js) initState() call
    // rather than a second independent fetch, running once it resolves.
    safely('checkVersionMismatch', () => state.initState().then(checkVersionMismatch))
  }
})
