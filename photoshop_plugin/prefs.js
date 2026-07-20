/**
 * @file Session-scoped, best-effort auto-fix for Photoshop's "Maximize PSD
 * Compatibility" file-handling preference (docs/SPIKES.md spike 8). Without
 * it set to Always, Photoshop (a) pops a compatibility dialog on every save
 * of a layered PSD, and (b) — the part that actually affects fidelity, not
 * just annoyance — this bridge reads back the flattened Maximize-Compatibility
 * COMPOSITE embedded in a PSD rather than genuine layered content whenever
 * that composite is missing, so getting this preference right matters beyond
 * the dialog itself.
 *
 * Uses the typed `photoshop` module Preferences API
 * (`app.preferences.fileHandling.maximizeCompatibility`) rather than a
 * hand-guessed `batchPlay` descriptor. Confidence: HIGH, from independently
 * agreeing sources —
 *   1. Adobe's own UXP Photoshop API reference documents `app.preferences`
 *      (`Preferences` class, min version 24.0 — comfortably inside this
 *      plugin's own `minVersion: 24.4.0`), its `fileHandling` property
 *      (`PreferencesFileHandling`), that class's `maximizeCompatibility`
 *      property, and the `constants` module's `MaximizeCompatibility` enum
 *      (developer.adobe.com/photoshop/uxp/2022/ps_reference/classes/
 *      preferences/, .../preferences/preferencesfilehandling/, and
 *      .../modules/constants/).
 *   2. The community-maintained UXP TypeScript declarations
 *      (github.com/bubblydoo/uxp-toolkit, packages/types-photoshop) show
 *      `maximizeCompatibility` as an explicit `get`/`set` pair (i.e.
 *      documented read-write, not read-only) on `PreferencesFileHandling`,
 *      typed as `Constants.MaximizeCompatibility` with underlying string
 *      values `ALWAYS = 'queryAlways'`, `ASK = 'queryAsk'`,
 *      `NEVER = 'queryNever'`.
 *   3. Those exact string values match Photoshop's long-documented
 *      ExtendScript `QueryStateType` enum for this same preference
 *      (`app.preferences.maximizeCompatibility` in the DOM/ExtendScript
 *      API) — an independent cross-check that this is the real underlying
 *      representation, not a doc-generator guess.
 *   4. Adobe's `executeAsModal` reference is explicit that a modal scope is
 *      required for "creating or modifying documents, or even updating UI
 *      or preference state" — hence the write below is wrapped in
 *      `core.executeAsModal`, mirroring exporter.js/handoffs.js's existing
 *      use of the same API for document mutations. Reads are not wrapped —
 *      only the write needs modal scope.
 *
 * VERIFY(spike-8): all of the above is sourced from documentation and
 * cross-referenced type declarations, never exercised against a real
 * Photoshop from this environment (no Photoshop here — see docs/SPIKES.md
 * spike 8, still "Not started" as of this writing). The API SHAPE is
 * high-confidence; what remains genuinely unverified until run live is (a)
 * that the write actually takes effect and survives a Photoshop restart,
 * and (b) that no additional manifest permission is needed beyond what this
 * plugin already requests. If wrong, the failure mode is a caught
 * exception (see `ensureMaximizeCompatibility` below) — never a silent
 * corruption of the user's preferences, since this only ever calls the
 * documented setter with a documented enum value, never a raw descriptor.
 */

const { app, core, constants } = require('photoshop')
const { logInfo, logWarn, describeError } = require('./log.js')

/**
 * localStorage key for the user's opt-out of the auto-fix (panel Advanced
 * section checkbox). Same persistence pattern as connection.js's
 * `SERVER_BASE_STORAGE_KEY` — a synchronous, best-effort read/write that
 * degrades to in-session-only if `localStorage` is unavailable in this UXP
 * target.
 */
const AUTO_FIX_STORAGE_KEY = 'cpsb.autoMaxCompat'

/**
 * Whether this plugin session has already made its one attempt (successful,
 * failed, or skipped because the toggle was off) at fixing the preference.
 * Mirrors the "safe to call more than once — only the first call has any
 * effect" idiom already used in this plugin (connection.js's
 * `ConnectionManager._started`, panel.js's module-level `initialized`), so
 * connection.js can call {@link ensureMaximizeCompatibility} unconditionally
 * on every connect/reconnect without needing its own bookkeeping.
 *
 * Deliberate trade-off: this flag is consumed on the FIRST call regardless
 * of whether the toggle was on or off at the time. If a user leaves the
 * toggle off through the first connect and turns it on later in the same
 * Photoshop session, the auto-fix will NOT retry until the next Photoshop
 * restart — merely reconnecting isn't enough, since `_attempted` is already
 * true. This keeps the "once per session" behavior simple and predictable
 * (exactly one attempt, one log line, ever) rather than reactive to the
 * checkbox; the checkbox itself always reflects its true persisted state,
 * and the user can always set the real Photoshop preference by hand via
 * Preferences → File Handling in the meantime.
 * @type {boolean}
 */
let _attempted = false

/**
 * Reads whether the auto-fix is enabled (panel Advanced checkbox). Defaults
 * to ON when nothing has been persisted yet — the design calls for a
 * visible, reversible opt-OUT (some users may not want their global
 * Photoshop prefs changed silently), not an opt-in that most users would
 * never discover.
 * @returns {boolean}
 */
function isAutoFixEnabled() {
  try {
    if (typeof localStorage === 'undefined') return true
    const stored = localStorage.getItem(AUTO_FIX_STORAGE_KEY)
    // Unset (first run, or localStorage cleared) -> default ON. Only a
    // persisted "0" (explicit prior opt-out) turns it off.
    return stored === null ? true : stored !== '0'
  } catch (_error) {
    return true
  }
}

/**
 * Persists the auto-fix toggle, best-effort — a failure here (localStorage
 * absent in this UXP target) is non-fatal: the new setting still applies
 * for this session, it just won't survive a Photoshop restart. Mirrors
 * connection.js's `persistServerBase`.
 * @param {boolean} enabled
 * @returns {void}
 */
function setAutoFixEnabled(enabled) {
  try {
    if (typeof localStorage !== 'undefined') {
      localStorage.setItem(AUTO_FIX_STORAGE_KEY, enabled ? '1' : '0')
    }
  } catch (error) {
    logWarn(
      `could not persist the Maximize Compatibility auto-fix setting (${describeError(error)}) — it will reset when Photoshop restarts`
    )
  }
}

/**
 * The plugin's one attempt, per session, at setting Photoshop's Maximize PSD
 * Compatibility preference to Always. Call from connection.js once the
 * websocket handshake completes (transition to `'connected'`). Safe to call
 * unconditionally on every connect/reconnect — only the first call in this
 * plugin session does anything; see `_attempted`.
 *
 * NEVER throws: every failure (missing API, a rejected `executeAsModal`,
 * anything) is caught and logged as a warning, not an error — a failed
 * preference write must never break the connect flow. The bridge still
 * works either way; the user just keeps seeing Photoshop's own save dialog
 * occasionally, exactly as before this feature existed.
 * @returns {Promise<void>}
 */
async function ensureMaximizeCompatibility() {
  if (_attempted) return
  _attempted = true

  if (!isAutoFixEnabled()) {
    logInfo('Maximize PSD Compatibility auto-fix is off (Advanced setting) — leaving Photoshop\'s preference as-is')
    return
  }

  try {
    const current = app.preferences.fileHandling.maximizeCompatibility
    if (current === constants.MaximizeCompatibility.ALWAYS) {
      logInfo('Maximize PSD Compatibility: already Always')
      return
    }
    await core.executeAsModal(
      async () => {
        app.preferences.fileHandling.maximizeCompatibility = constants.MaximizeCompatibility.ALWAYS
      },
      { commandName: 'ComfyUI: enable Maximize PSD Compatibility' }
    )
    logInfo('Set Maximize PSD Compatibility → Always')
  } catch (error) {
    logWarn(
      `could not set Maximize PSD Compatibility automatically (${describeError(error)}) — set it by hand in Preferences → File Handling → Maximize PSD Compatibility → Always if save dialogs keep appearing`
    )
  }
}

module.exports = { isAutoFixEnabled, setAutoFixEnabled, ensureMaximizeCompatibility }
