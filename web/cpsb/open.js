/**
 * @file The ONE shared `/cpsb/open` flow. Every open call site in this
 * extension — menu.js's single-image open, menu.js's "Open all N" batch
 * loop, gallery.js's card Re-open and "Open fresh copy" — routes through
 * this module rather than calling `api.openHandoff` directly, so the
 * PROTOCOL.md §2 interactive protocol around that route behaves identically
 * everywhere:
 *
 *  - **428 `client_remote`** (§2/§7 client-locality gate): confirm
 *    "Photoshop will open on <server_name>", remember an affirmative
 *    per-browser in `localStorage`, retry the SAME request with
 *    `client_remote_ok: true`. This lived in menu.js first — which is
 *    exactly how the field bug happened: gallery.js's Re-open bypassed it,
 *    so a remote browser's reopen died on the raw 428 with an error toast
 *    instead of showing the confirm. Centralizing here makes that class of
 *    bug structurally impossible.
 *  - **409 existing-handoff**: the Edit Original / Start Fresh chooser, then
 *    a re-call with the chosen mode ({@link openInteractive} only — the
 *    batch path resolves 409s silently with `mode:"original"` instead, per
 *    its own doc).
 *  - **503 neither-tier / 404 / anything else**: an error toast that always
 *    carries the SERVER's own message (`api.errorMessage`), never a generic
 *    failure line.
 *
 * Moved verbatim from menu.js (the 428/confirm/localStorage half) so both
 * halves of the UI share it; menu.js keeps only what is genuinely
 * menu-specific (node → request-body derivation, item building).
 */

import * as api from './api.js'
import * as state from './state.js'
import * as ui from './ui.js'

/**
 * localStorage key remembering an affirmative answer to the PROTOCOL.md §2
 * remote-open confirm below, so a given browser is asked at most once. Only
 * the affirmative is ever written — there is no stored "no".
 */
const REMOTE_OPEN_ALLOWED_KEY = 'cpsb.remoteOpenAllowed'

/** Guards the warning below so a broken `localStorage` never spams the console. */
let localStorageWarned = false

/**
 * @param {unknown} error
 */
function warnLocalStorageUnavailable(error) {
  if (localStorageWarned) return
  localStorageWarned = true
  api.warn(
    'localStorage is unavailable; the "open Photoshop on a different ' +
      'computer" choice will not be remembered between opens',
    error
  )
}

/**
 * @returns {boolean} Whether this browser has already been told, and
 * agreed, that Photoshop opens on the ComfyUI server's machine
 * (PROTOCOL.md §2) — only the affirmative is ever persisted.
 * Feature-detected: some browser configurations (storage disabled by
 * policy, certain private-browsing modes) throw on `localStorage` access
 * rather than simply leaving it absent.
 */
function isRemoteOpenAllowed() {
  try {
    return window.localStorage.getItem(REMOTE_OPEN_ALLOWED_KEY) === '1'
  } catch (error) {
    warnLocalStorageUnavailable(error)
    return false
  }
}

/**
 * Persists the user's "Open on <server_name>" choice so this browser is
 * never asked again (PROTOCOL.md §2: "choice remembered per-browser in
 * localStorage; only the affirmative is persisted").
 */
function rememberRemoteOpenAllowed() {
  try {
    window.localStorage.setItem(REMOTE_OPEN_ALLOWED_KEY, '1')
  } catch (error) {
    warnLocalStorageUnavailable(error)
  }
}

/**
 * Thrown by {@link openWithRemoteConfirm} when the user dismisses or
 * cancels the PROTOCOL.md §2 remote-open confirm. Callers catch this
 * specifically and stop silently — declining is a deliberate answer, not a
 * failure, so it shows no toast.
 */
export class RemoteOpenCancelled extends Error {
  constructor() {
    super('Remote-open confirm was cancelled')
    this.name = 'RemoteOpenCancelled'
  }
}

/**
 * Adds `client_remote_ok: true` to an open-request body when the user has
 * already agreed, in this browser, to open Photoshop on the server's
 * machine AND the page looks remotely browsed
 * ({@link state.isRemoteBrowsingLikely}) — a pure optimization so the
 * 428-then-retry round trip in {@link openWithRemoteConfirm} never happens
 * for a choice that's already known. Never required for correctness: a
 * body that omits the flag (e.g. an `isRemoteBrowsingLikely()`
 * false-negative — PROTOCOL.md §7 notes hostname can't perfectly determine
 * locality) still reaches the same outcome via the 428 branch below, just
 * one extra request later.
 * @param {import('./api.js').CpsbOpenRequest} body
 * @returns {import('./api.js').CpsbOpenRequest}
 */
function withProactiveRemoteFlag(body) {
  if (isRemoteOpenAllowed() && state.isRemoteBrowsingLikely()) {
    return { ...body, client_remote_ok: true }
  }
  return body
}

/**
 * POSTs `/cpsb/open`, transparently resolving the PROTOCOL.md §2/§7
 * client-locality confirm. A 428 with `body.reason === "client_remote"` is
 * caught here: if this browser already agreed ({@link isRemoteOpenAllowed}),
 * the SAME open is retried immediately with `client_remote_ok: true`
 * (covers the case where {@link withProactiveRemoteFlag} didn't already
 * send it proactively, e.g. an `isRemoteBrowsingLikely()` false-negative);
 * otherwise the user is asked once via {@link ui.chooseDialog}, and on
 * "allow" the choice is persisted and the same retry happens. Every other
 * status (409, 503, or anything else) is rethrown untouched — this
 * function's only job is the 428 branch, so it composes transparently with
 * a caller's own 409/503 handling, including a 409-driven re-call that then
 * hits 428.
 * @param {import('./api.js').CpsbOpenRequest} body
 * @returns {Promise<import('./api.js').CpsbOpenResponse>}
 * @throws {RemoteOpenCancelled} The user declined the confirm.
 * @throws {import('./api.js').CpsbApiError} Any other non-2xx response.
 */
export async function openWithRemoteConfirm(body) {
  try {
    return await api.openHandoff(withProactiveRemoteFlag(body))
  } catch (error) {
    if (
      error instanceof api.CpsbApiError &&
      error.status === 428 &&
      error.body &&
      typeof error.body === 'object' &&
      error.body.reason === 'client_remote'
    ) {
      const serverName = error.body.server_name
      if (!isRemoteOpenAllowed()) {
        const choice = await ui.chooseDialog({
          title: 'Photoshop is on a different computer',
          message:
            `Photoshop will open on “${serverName}” — the machine running ` +
            'ComfyUI — not on this computer. If you’re not at that machine, ' +
            'cancel and install the Photoshop panel plugin there instead. ' +
            '(Remote opening on THIS computer is planned.)',
          choices: [{ label: `Open on ${serverName}`, value: 'allow', primary: true }]
        })
        if (choice !== 'allow') throw new RemoteOpenCancelled()
        rememberRemoteOpenAllowed()
      }
      return api.openHandoff({ ...body, client_remote_ok: true })
    }
    throw error
  }
}

/**
 * @param {import('./api.js').CpsbApiError["body"]} body - 503 response body.
 * @returns {string}
 */
function describeUnavailable(body) {
  if (body && typeof body === 'object' && body.error) return String(body.error)
  return 'Neither Photoshop (Tier 1) nor the Photoshop panel plugin (Tier 2) is available.'
}

/**
 * The full interactive open flow for a SINGLE open request — the one entry
 * point for menu.js's single-image opens and every gallery card action:
 *
 *  1. {@link openWithRemoteConfirm} (428 handled inside).
 *  2. Success → "Opening in Photoshop…" toast (customizable summary).
 *  3. Declined remote confirm → silent stop (deliberate user answer).
 *  4. 409 → Edit Original / Start Fresh chooser → SAME flow re-runs with
 *     the chosen mode (so a 409-then-428 chain still confirms correctly).
 *  5. 503 → "Photoshop not available" with the server's reason.
 *  6. Anything else (404 unknown/inactive handoff, 404 missing source, 400,
 *     network) → error toast with the server's own message
 *     (`api.errorMessage`) — G1c: never a generic failure.
 *
 * @param {import('./api.js').CpsbOpenRequest} body
 * @param {{successSummary?: string}} [options]
 * @returns {Promise<boolean>} Whether an open was actually issued (`false`
 * on decline/dismiss/error) — callers that need to chain UI on success.
 */
export async function openInteractive(body, { successSummary = 'Opening in Photoshop…' } = {}) {
  try {
    await openWithRemoteConfirm(body)
    // Toast only after the POST succeeds — the 409 path below shows its own
    // chooser instead (and the recursive re-call toasts exactly once), a
    // declined remote-open confirm shows no toast at all, and a 503/other
    // failure must not be preceded by a false "Opening…".
    ui.showToast({
      severity: 'info',
      summary: successSummary,
      detail: 'ComfyUI will watch this file and bring back your edits automatically.'
    })
    // No further UI here by design — the cpsb.status/cpsb.updated events
    // drive the node badge (badges.js) and the gallery (gallery.js).
    return true
  } catch (error) {
    if (error instanceof RemoteOpenCancelled) return false
    if (error instanceof api.CpsbApiError && error.status === 409) {
      const choice = await ui.chooseDialog({
        title: 'Already editing this image',
        message:
          'An edit is already in progress for this node. Continue editing ' +
          'the same Photoshop document, or start over from the current image?',
        choices: [
          { label: 'Edit Original', value: 'original', primary: true },
          { label: 'Start Fresh', value: 'fresh' }
        ]
      })
      if (choice === 'original' || choice === 'fresh') {
        return openInteractive({ ...body, mode: choice }, { successSummary })
      }
      return false
    }
    if (error instanceof api.CpsbApiError && error.status === 503) {
      ui.showToast({
        severity: 'error',
        summary: 'Photoshop not available',
        detail: describeUnavailable(error.body)
      })
      return false
    }
    ui.showToast({
      severity: 'error',
      summary: 'Failed to open in Photoshop',
      detail: api.errorMessage(error)
    })
    return false
  }
}
