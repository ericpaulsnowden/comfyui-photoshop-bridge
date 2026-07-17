/**
 * @file Client-side handoff cache. Seeded from `GET /cpsb/status` at setup
 * and kept in sync by re-fetching on every `cpsb.status` / `cpsb.updated`
 * event (the payloads of those events are partial — PROTOCOL.md §5 — so a
 * full re-sync is simpler and more robust than hand-merging deltas; handoff
 * volume is inherently low and the server already caps the list at 200).
 *
 * This module answers "does node X have an active handoff" for menu
 * building (`menu.js`), holds the reverse-chronological list + tier status
 * for the sidebar gallery (`gallery.js`), and resolves a handoff's original
 * source ref for `pasteback.js`'s batch-slot matching. It does not itself
 * paste images into nodes or draw badges — `pasteback.js` and `badges.js`
 * subscribe to the raw `cpsb.*` events independently via `api.js` for that,
 * since they need each event's own payload, not the resynced cache.
 */

import { app } from '../../../scripts/app.js'
import * as api from './api.js'

/** Handoff statuses that count as "this node has an active round trip". */
const ACTIVE_STATUSES = new Set(['pending', 'editing', 'edited'])

/** A handoff sitting in `editing` longer than this is considered Stale. */
const STALE_MS = 60 * 60 * 1000

/** @type {Map<string, import('./api.js').CpsbHandoffMeta>} */
const handoffsById = new Map()

// Optimistic defaults until the first /cpsb/status response lands: menu.js
// only uses these to *disable* an item early as a courtesy (see menu.js),
// and /cpsb/open remains the authoritative gate either way — so defaulting
// to "available" avoids a menu item flashing disabled for the brief window
// before the initial fetch resolves, in exchange for the (harmless) reverse
// case of a click that fails with a clear error instead.
const tier = {
  tier1Available: true,
  tier1Reason: /** @type {string | null} */ (null),
  tier2Connected: false,
  psVersion: /** @type {string | null} */ (null)
}

/** @type {Set<() => void>} */
const listeners = new Set()

let initPromise = /** @type {Promise<void> | null} */ (null)
let refreshPromise = /** @type {Promise<void> | null} */ (null)

function notify() {
  for (const listener of listeners) {
    try {
      listener()
    } catch (error) {
      api.warn('state subscriber threw', error)
    }
  }
}

function applyStatusResponse(
  /** @type {import('./api.js').CpsbStatusResponse} */ response
) {
  handoffsById.clear()
  for (const meta of response.handoffs ?? []) {
    handoffsById.set(meta.handoff_id, meta)
  }
  tier.tier1Available = !!response.tier1_available
  tier.tier1Reason = response.tier1_reason ?? null
  tier.tier2Connected = !!response.tier2_connected
  tier.psVersion = response.ps_version ?? null
}

/**
 * Re-fetches `/cpsb/status` and replaces the cache. Concurrent calls share
 * one in-flight request.
 * @returns {Promise<void>}
 */
export function refresh() {
  if (refreshPromise) return refreshPromise
  refreshPromise = api
    .getStatus()
    .then((response) => {
      applyStatusResponse(response)
      notify()
    })
    .catch((error) => {
      api.warn('failed to refresh /cpsb/status', error)
    })
    .finally(() => {
      refreshPromise = null
    })
  return refreshPromise
}

/**
 * Seeds the cache from `/cpsb/status` and subscribes to the events that
 * invalidate it. Safe to call more than once — subsequent calls return the
 * same promise as the first.
 * @returns {Promise<void>}
 */
export function initState() {
  if (initPromise) return initPromise
  // Attach listeners synchronously before the first await so no event can
  // arrive un-observed while the initial fetch is in flight; the handlers
  // just trigger a full refresh, so an event racing the initial fetch is
  // self-healing either way.
  api.onStatusChanged(() => {
    refresh()
  })
  api.onUpdated(() => {
    refresh()
  })
  api.onTier2Changed((detail) => {
    tier.tier2Connected = !!detail.connected
    tier.psVersion = detail.ps_version ?? null
    notify()
  })
  initPromise = refresh()
  return initPromise
}

/**
 * @param {() => void} callback - Invoked (no args) whenever the cache
 * changes; re-read state via the getters below.
 * @returns {() => void} Unsubscribe function.
 */
export function subscribe(callback) {
  listeners.add(callback)
  return () => listeners.delete(callback)
}

/**
 * @param {string} handoffId
 * @returns {import('./api.js').CpsbHandoffMeta | undefined}
 */
export function getHandoffById(handoffId) {
  return handoffsById.get(handoffId)
}

/**
 * @returns {string | undefined} The active workflow's base filename, best
 * effort — `undefined` when unavailable (unsaved workflow, or a frontend
 * without `extensionManager.workflow`). Shared by `menu.js` (the
 * `workflow_name` field of every `/cpsb/open` body) and
 * {@link getActiveHandoffForNode} (workflow scoping) so both always use one
 * implementation.
 */
export function getWorkflowName() {
  try {
    return app.extensionManager?.workflow?.activeWorkflow?.filename || undefined
  } catch {
    return undefined
  }
}

/**
 * Mirrors the backend's workflow-scoping rule exactly: when BOTH the
 * current workflow name and the candidate handoff's `workflow_name` are
 * non-empty they must be equal; when EITHER is empty, it counts as a match
 * (wildcard fallback, so handoffs from unsaved/unnamed workflows are never
 * orphaned behind a name they don't have).
 * @param {string | undefined} candidateName
 * @returns {boolean}
 */
function workflowMatches(candidateName) {
  const current = getWorkflowName() || ''
  const candidate = candidateName || ''
  if (!current || !candidate) return true
  return current === candidate
}

/**
 * The most recent non-terminal handoff for a node, if any — used to decide
 * between "Open in Photoshop" and "Edit Original / Start Fresh" in the
 * context menu. Scoped by workflow name via {@link workflowMatches}: node
 * ids are only unique within one workflow, so without this, node "17" in
 * workflow B would cross-match an active handoff belonging to node "17" in
 * workflow A.
 * @param {string} nodeId
 * @returns {import('./api.js').CpsbHandoffMeta | undefined}
 */
export function getActiveHandoffForNode(nodeId) {
  let best
  for (const meta of handoffsById.values()) {
    if (meta.origin_node_id !== nodeId) continue
    if (!ACTIVE_STATUSES.has(meta.status)) continue
    if (!workflowMatches(meta.workflow_name)) continue
    if (!best || meta.created_ts > best.created_ts) best = meta
  }
  return best
}

/**
 * Full handoff list, newest first (server order is preserved verbatim).
 * @returns {import('./api.js').CpsbHandoffMeta[]}
 */
export function getAllHandoffs() {
  return Array.from(handoffsById.values())
}

/**
 * Whether the page is being browsed via a non-local hostname (e.g. ComfyUI
 * started with `--listen`, reached by LAN address). PROTOCOL.md §7: this is
 * an INFORMATIONAL signal only — it must never gate Tier 1, because Tier 1
 * only requires Photoshop on the SERVER's machine, and a LAN hostname says
 * nothing about where the server (or the user) physically is. A common
 * false positive is browsing the same machine that runs both ComfyUI and
 * Photoshop through its LAN address.
 * @returns {boolean}
 */
export function isRemoteBrowsingLikely() {
  const host = window.location.hostname
  return host !== '' && host !== 'localhost' && host !== '127.0.0.1' && host !== '::1'
}

/**
 * @typedef {Object} CpsbTierInfo
 * @property {boolean} tier1Available - Server-reported availability — the
 * sole authority on Tier 1 (PROTOCOL.md §7); no client-side gating applies.
 * @property {string | null} tier1Reason - Server reason when unavailable.
 * @property {boolean} tier1Effective - Equal to `tier1Available`; retained
 * as the name menu.js branches on.
 * @property {string} tier1EffectiveReason - Human-readable reason to show in
 * a disabled-menu-item tooltip; empty string when Tier 1 is available.
 * @property {boolean} tier2Connected
 * @property {string | null} psVersion
 */

/**
 * @returns {CpsbTierInfo}
 */
export function getTierInfo() {
  const tier1Effective = tier.tier1Available
  const tier1EffectiveReason = tier1Effective
    ? ''
    : tier.tier1Reason || 'Photoshop is not available on this server.'
  return {
    tier1Available: tier.tier1Available,
    tier1Reason: tier.tier1Reason,
    tier1Effective,
    tier1EffectiveReason,
    tier2Connected: tier.tier2Connected,
    psVersion: tier.psVersion
  }
}

/**
 * Derives the display status for a handoff, materializing the client-only
 * "stale" pseudo-status (PROTOCOL.md §1: `editing` and `updated_ts` older
 * than 1h; never sent by the server).
 * @param {import('./api.js').CpsbHandoffMeta} meta
 * @returns {import('./api.js').CpsbStatus | "stale"}
 */
export function getDisplayStatus(meta) {
  if (meta.status === 'editing' && Date.now() - meta.updated_ts * 1000 > STALE_MS) {
    return 'stale'
  }
  return meta.status
}
