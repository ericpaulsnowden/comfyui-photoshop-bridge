/**
 * @file The persistent websocket client to ComfyUI's `/cpsb/ws` route
 * (docs/PROTOCOL.md §3), implementing the hello/hello_ack/ready handshake,
 * ping/pong keepalive, and indefinite exponential-backoff reconnection.
 *
 * This module is started from `entrypoints.plugin.create()` (index.js), not
 * from the panel's `show()` — research on this project (research-photoshop.md)
 * confirms a UXP panel's JS context and any open websocket survive the panel
 * being hidden, but panel lifecycle hooks are simply the wrong place to
 * *start* something that must run whether or not the panel is ever opened.
 * `plugin.create()` fires once at plugin load regardless of panel
 * visibility, which is what PLAN.md §5 calls for.
 *
 * Connection state is exposed as a small `EventTarget` (`connection`) rather
 * than a bespoke pub/sub implementation — `EventTarget` is a standard,
 * UXP-supported web platform class, not a library.
 */

const { logInfo, logWarn, logError, describeError } = require('./log.js')
const { ensureMaximizeCompatibility } = require('./prefs.js')

const uxp = require('uxp')
const { localFileSystem } = uxp.storage

/**
 * Default ComfyUI server base as `host:port` (docs/PROTOCOL.md §3). The plugin
 * derives its WebSocket URL (`ws://<base>/cpsb/ws`) and HTTP origin
 * (`http://<base>`) from this single value, so `host:port` is the one source
 * of truth for the connection target. The default keeps existing localhost
 * users on the exact same target as before; it is now user-configurable (panel
 * Advanced section, `setServerBase`) so Photoshop on one machine can reach
 * ComfyUI on another.
 */
const DEFAULT_SERVER_BASE = 'localhost:8188'

/**
 * localStorage key the configured server base persists under.
 *
 * Persistence choice: UXP for Photoshop exposes a Web-standard, SYNCHRONOUS
 * `localStorage`, which fits this manager far better than an async plugin-data
 * JSON file would — the persisted base must be known BEFORE the first
 * connection attempt, and localStorage can be read inline in the constructor
 * with no async bootstrap (the existing `start()`/`_open()` flow is entirely
 * synchronous; an async file read would force it to become async or to open
 * once on the default and then reconnect). ASSUMPTION to verify on real
 * Photoshop: that `localStorage` both exists AND survives a Photoshop restart
 * for this plugin. Every access is guarded (try/catch) so that if it is
 * unavailable the plugin degrades to in-session-only — the setting still
 * applies and reconnects for this session, it just won't persist across a
 * restart.
 */
const SERVER_BASE_STORAGE_KEY = 'cpsb.serverBase'

/** Exponential backoff schedule in ms, capping at 10s forever (PLAN.md §5). */
const BACKOFF_STEPS_MS = [1000, 2000, 5000, 10000]

/** `WebSocket.readyState` value meaning "open" (docs/reference-js WebSocket page). */
const WS_READY_STATE_OPEN = 1

/**
 * Application close code the SERVER uses when a NEW plugin connection displaces
 * this one (cpsb/routes.py: `ws.close(code=4000, "replaced by a new
 * connection")`). The server allows only one plugin socket at a time. When this
 * plugin receives it, another Photoshop (e.g. on another machine, both pointed
 * at the same ComfyUI) has taken over — so this plugin STANDS BY instead of
 * auto-reconnecting, which would kick the other one off and start an infinite
 * tug-of-war. The user re-claims with the panel's Connect button.
 */
const WS_CLOSE_REPLACED = 4000

/**
 * Chunk size, in base64 CHARACTERS, for both `request_file`'s `file_chunk`
 * replies and this plugin's own `upload_edit` frames (docs/PROTOCOL.md §3,
 * cross-machine file transfer). Matches `cpsb/routes.py`'s `_WS_CHUNK_CHARS`
 * in spirit, not in a load-bearing way -- either side can use a different
 * chunk size independently, since reassembly is just "concatenate every
 * chunk's `data_b64` in `seq` order, then base64-decode once at the end."
 */
const WS_TRANSFER_CHUNK_CHARS = 700_000

/**
 * How long to wait for a `request_file` download to fully arrive, or an
 * `upload_edit` to be acknowledged (`upload_ok`/`upload_error`), before
 * giving up (docs/PROTOCOL.md §3). Generous relative to a layered PSD that
 * can run into the tens of MB over a LAN/WAN link.
 */
const TRANSFER_TIMEOUT_MS = 60000

/** Base64 alphabet, standard (RFC 4648 §4), used by the hand-rolled codec
 * below -- see its own doc comment for why this isn't `btoa`/`atob`. */
const BASE64_CHARS = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/'

/**
 * Encodes `bytes` to a standard, padded base64 string.
 *
 * Hand-rolled rather than relying on `btoa` (which operates on a JS
 * "binary string", not a byte array, and would need its own conversion loop
 * either way) or a Node-style `Buffer` (not guaranteed present in UXP) --
 * this plugin already hand-rolls other binary primitives it needs rather
 * than assuming runtime APIs beyond what UXP is documented to support (see
 * exporter.js's PNG encoder: crc32/adler32/zlibStore/pngChunk). Pairs with
 * {@link base64Decode}.
 * @param {Uint8Array} bytes
 * @returns {string}
 */
function base64Encode(bytes) {
  let result = ''
  for (let i = 0; i < bytes.length; i += 3) {
    const b0 = bytes[i]
    const hasB1 = i + 1 < bytes.length
    const hasB2 = i + 2 < bytes.length
    const b1 = hasB1 ? bytes[i + 1] : 0
    const b2 = hasB2 ? bytes[i + 2] : 0
    result += BASE64_CHARS[b0 >> 2]
    result += BASE64_CHARS[((b0 & 0x03) << 4) | (b1 >> 4)]
    result += hasB1 ? BASE64_CHARS[((b1 & 0x0f) << 2) | (b2 >> 6)] : '='
    result += hasB2 ? BASE64_CHARS[b2 & 0x3f] : '='
  }
  return result
}

/**
 * Decodes a standard, padded base64 string back to bytes. Pairs with
 * {@link base64Encode}; not hardened against arbitrary/malformed input
 * (whitespace, non-alphabet characters) since every caller here only ever
 * feeds it a string this plugin itself produced or that the server produced
 * with the identical scheme (`cpsb/routes.py`'s `base64.b64encode`).
 * @param {string} b64
 * @returns {Uint8Array}
 */
function base64Decode(b64) {
  const clean = b64.replace(/=+$/, '')
  const byteLength = Math.floor((clean.length * 3) / 4)
  const out = new Uint8Array(byteLength)
  let pos = 0
  for (let i = 0; i < clean.length; i += 4) {
    const c0 = BASE64_CHARS.indexOf(clean[i])
    const c1 = BASE64_CHARS.indexOf(clean[i + 1])
    const c2 = i + 2 < clean.length ? BASE64_CHARS.indexOf(clean[i + 2]) : -1
    const c3 = i + 3 < clean.length ? BASE64_CHARS.indexOf(clean[i + 3]) : -1
    out[pos++] = (c0 << 2) | (c1 >> 4)
    if (c2 >= 0) out[pos++] = ((c1 & 0x0f) << 4) | (c2 >> 2)
    if (c3 >= 0) out[pos++] = ((c2 & 0x03) << 6) | c3
  }
  return out
}

/**
 * Slices `data` into <= {@link WS_TRANSFER_CHUNK_CHARS}-character pieces, in
 * order. Always returns at least one element (`['']` for empty input).
 * @param {string} data
 * @returns {string[]}
 */
function splitIntoChunks(data) {
  if (!data) return ['']
  const chunks = []
  for (let i = 0; i < data.length; i += WS_TRANSFER_CHUNK_CHARS) {
    chunks.push(data.slice(i, i + WS_TRANSFER_CHUNK_CHARS))
  }
  return chunks
}

/**
 * @typedef {Object} CpsbHelloMessage
 * @property {'hello'} type
 * @property {string} plugin_version
 * @property {string} ps_version
 * @property {string} uxp_version
 */

/**
 * @typedef {Object} CpsbHelloAckMessage
 * @property {'hello_ack'} type
 * @property {string} server_version
 * @property {string} input_cpsb_path - Absolute path, server's filesystem view.
 */

/**
 * @typedef {Object} CpsbReadyMessage
 * @property {'ready'} type
 * @property {boolean} local_mode
 */

/** @typedef {Object} CpsbPingMessage
 * @property {'ping'} type */

/** @typedef {Object} CpsbPongMessage
 * @property {'pong'} type */

/**
 * @typedef {Object} CpsbOpenHandoffMessage
 * @property {'open_handoff'} type
 * @property {string} handoff_id
 * @property {string} psd_path - Absolute, server-side path to the handoff's
 * managed PSD copy (named after its origin file, not literally `source.psd`
 * -- product-owner requirement 2026-07-18). Used directly to open in local
 * mode; in remote mode this plugin never opens the path itself (no shared
 * filesystem) but still basenames it to name the locally-downloaded copy
 * the same thing (`handoffs.js`'s `openRemote`).
 * @property {string} file_url - e.g. `/cpsb/file/<id>` (remote mode).
 * @property {boolean} [wants_layered_psd] - Remote Tier-2 layered annotate
 * (docs/PROTOCOL.md §6d): mirrors the handoff's own
 * `HandoffMeta.wants_layered_psd`. `true` only for a Photoshop Annotate
 * node handoff, whose managed PSD copy carries a paintable "Instructions"
 * layer -- `handoffs.js`'s save pipeline uses this, together with the
 * connection's own REMOTE/LOCAL mode, to decide whether to upload the
 * document's own raw PSD bytes (`uploader.js`'s `uploadLayeredPsd`) instead
 * of the ordinary flattened PNG. Absent or `false` (including from a server
 * build that predates this field) means "flat PNG, as always."
 */

/**
 * @typedef {Object} CpsbHandoffCancelledMessage
 * @property {'handoff_cancelled'} type
 * @property {string} handoff_id
 */

/**
 * @typedef {Object} CpsbRequestFileMessage
 * @property {'request_file'} type
 * @property {string} handoff_id
 */

/**
 * @typedef {Object} CpsbFileChunkMessage
 * @property {'file_chunk'} type
 * @property {string} handoff_id
 * @property {number} seq
 * @property {number} total
 * @property {string} data_b64 - One slice of the FULL file's base64 encoding
 * (docs/PROTOCOL.md §3) -- concatenate every chunk's `data_b64` in `seq`
 * order, THEN base64-decode once; chunks are never independently decodable.
 */

/**
 * @typedef {Object} CpsbFileErrorMessage
 * @property {'file_error'} type
 * @property {string} handoff_id
 * @property {string} error
 */

/**
 * @typedef {Object} CpsbUploadEditMessage
 * @property {'upload_edit'} type
 * @property {string} handoff_id
 * @property {number} seq
 * @property {number} total
 * @property {string} data_b64 - Same full-encode-then-slice scheme as
 * {@link CpsbFileChunkMessage}, in the opposite direction.
 * @property {'plugin'} fidelity
 * @property {'png' | 'psd'} kind - Remote Tier-2 layered annotate
 * (docs/PROTOCOL.md §6d): `"png"` is the original flat-PNG transport;
 * `"psd"` is the document's own raw PSD bytes, sent only for a handoff
 * whose `open_handoff` carried `wants_layered_psd: true`. A server build
 * that predates this field treats a missing `kind` as `"png"`.
 */

/**
 * @typedef {Object} CpsbUploadOkMessage
 * @property {'upload_ok'} type
 * @property {string} handoff_id
 */

/**
 * @typedef {Object} CpsbUploadErrorMessage
 * @property {'upload_error'} type
 * @property {string} handoff_id
 * @property {string} error
 * @property {'unknown_handoff' | 'inactive' | 'invalid_image' | 'malformed'} [reason] -
 * Mirrors `POST /cpsb/upload`'s HTTP status codes: `unknown_handoff`/
 * `inactive` are the 404/409 equivalents (never worth retrying identical
 * bytes); `invalid_image`/`malformed` are retryable.
 */

/**
 * @typedef {CpsbHelloAckMessage | CpsbPingMessage | CpsbOpenHandoffMessage | CpsbHandoffCancelledMessage | CpsbFileChunkMessage | CpsbFileErrorMessage | CpsbUploadOkMessage | CpsbUploadErrorMessage} CpsbServerMessage
 * Every message type the server can send (docs/PROTOCOL.md §3). Messages
 * with an unrecognized `type` are ignored for forward compatibility, per
 * the contract ("Unknown types are ignored").
 */

/**
 * @typedef {Object} CpsbConnectionState
 * @property {'disconnected' | 'connecting' | 'connected'} status
 * @property {string} url
 * @property {string | null} serverVersion
 * @property {boolean | null} localMode - `null` until the handshake
 * completes at least once.
 * @property {string | null} lastError - Human-readable description of the
 * most recent connection failure (constructor throw, failed attempt, or
 * dropped connection); `null` once connected.
 * @property {boolean} lastErrorBlocking - Whether `lastError` is blocking
 * (user must act — a permission denial) vs transient (server not up yet /
 * retrying). The panel surfaces blocking errors prominently and keeps
 * transient ones inside Advanced.
 * @property {'superseded' | 'manual' | null} standby - Non-null when the
 * plugin is intentionally idle awaiting an explicit Connect: `'superseded'`
 * (another plugin took over this ComfyUI) or `'manual'` (user disconnected).
 * Null during normal auto-connect/retry.
 * @property {number} attempts - Consecutive failed connection attempts
 * since the last successful handshake.
 * @property {number | null} nextRetryAt - Epoch ms of the next scheduled
 * reconnect attempt, or `null` when none is pending (connected or mid-attempt).
 */

/**
 * Converts an absolute filesystem path (as sent by the server — may be
 * POSIX or Windows-style) into a `file:` URL suitable for
 * `localFileSystem.getEntryWithUrl()` / `createEntryWithUrl()`. Adobe's own
 * reference examples consistently use a single slash immediately after the
 * scheme, followed by the absolute path as-is (e.g. `file:/Users/name/...`,
 * `file:/Users/user/Documents/tmp`) rather than the triple-slash `file:///`
 * form — see `uxp-api/reference-js/.../persistent-file-storage/file-system-provider.md`.
 * Windows paths are normalized to forward slashes first
 * (`C:\foo\bar` -> `file:/C:/foo/bar`).
 * @param {string} absolutePath
 * @returns {string}
 */
function pathToFileUrl(absolutePath) {
  const normalized = absolutePath.replace(/\\/g, '/')
  return normalized.startsWith('/') ? `file:${normalized}` : `file:/${normalized}`
}

/**
 * Normalizes a user-entered server address to a bare `host:port` base. Accepts
 * forgiving forms — `192.168.1.50:8188`, `http://192.168.1.50:8188`,
 * `ws://host:8188/cpsb/ws`, `localhost:8188`, or a bare host (port defaults to
 * ComfyUI's 8188) — by stripping any scheme and any path/query/fragment and
 * keeping only the authority. Throws an `Error` with a clear, user-facing
 * message on empty or malformed input; the panel surfaces that message inline.
 * @param {string} input
 * @returns {string} `host:port`
 */
function normalizeServerBase(input) {
  if (typeof input !== 'string') {
    throw new Error('Server address must be text like "192.168.1.50:8188"')
  }
  let s = input.trim()
  if (!s) throw new Error('Enter a server address, e.g. "192.168.1.50:8188"')
  // Strip a leading scheme (http://, https://, ws://, wss://, …) if present.
  s = s.replace(/^[a-zA-Z][a-zA-Z0-9+.-]*:\/\//, '')
  // Keep only the authority — drop any /path, ?query, or #fragment.
  s = s.replace(/[/?#].*$/, '')
  if (!s) throw new Error(`"${input}" has no host part`)
  // Split host from an optional :port on the LAST colon.
  let host = s
  let port = '8188'
  const colon = s.lastIndexOf(':')
  if (colon !== -1) {
    host = s.slice(0, colon)
    port = s.slice(colon + 1)
  }
  if (!host) throw new Error(`"${input}" has no host part`)
  if (!/^[A-Za-z0-9.\-]+$/.test(host)) {
    throw new Error(`"${host}" is not a valid host name or IP address`)
  }
  if (!/^[0-9]+$/.test(port)) {
    throw new Error(`Port "${port}" must be a number`)
  }
  const portNum = Number(port)
  if (portNum < 1 || portNum > 65535) {
    throw new Error(`Port ${portNum} is out of range (1–65535)`)
  }
  return `${host}:${portNum}`
}

/**
 * Reads the persisted server base, falling back to the default if nothing is
 * stored or localStorage is unavailable / holds a bad value. See
 * `SERVER_BASE_STORAGE_KEY` for the persistence rationale and assumptions.
 * @returns {string} `host:port`
 */
function loadPersistedServerBase() {
  try {
    const stored =
      typeof localStorage !== 'undefined' && localStorage.getItem(SERVER_BASE_STORAGE_KEY)
    if (stored) return normalizeServerBase(stored)
  } catch (_error) {
    // localStorage unavailable, or a corrupt stored value — use the default.
  }
  return DEFAULT_SERVER_BASE
}

/**
 * Persists the server base, best-effort. A failure here (localStorage absent
 * in this UXP target) is non-fatal: the new base still applies for this
 * session, it just won't survive a Photoshop restart.
 * @param {string} base - Already-normalized `host:port`.
 * @returns {void}
 */
function persistServerBase(base) {
  try {
    if (typeof localStorage !== 'undefined') {
      localStorage.setItem(SERVER_BASE_STORAGE_KEY, base)
    }
  } catch (error) {
    logWarn(
      `could not persist server address (${describeError(error)}) — it will reset when Photoshop restarts`
    )
  }
}

/**
 * Manages the single websocket connection to ComfyUI. Import the shared
 * `connection` singleton below rather than constructing this directly.
 */
class ConnectionManager extends EventTarget {
  constructor() {
    super()
    /**
     * Active server base as `host:port` — the single source of truth for the
     * target URLs (`getWsUrl()`/`getHttpOrigin()`). Loaded from persistence
     * (or the default) at construction; changed only via `setServerBase()`.
     * @type {string}
     */
    this._serverBase = loadPersistedServerBase()
    /** @type {'disconnected' | 'connecting' | 'connected'} */
    this.status = 'disconnected'
    /** @type {string | null} */
    this.serverVersion = null
    /** @type {boolean | null} */
    this.localMode = null
    /** @type {string | null} */
    this.lastError = null
    /** Consecutive failed attempts since the last successful handshake. */
    this.attempts = 0
    /** @type {number | null} Epoch ms of the next scheduled reconnect. */
    this.nextRetryAt = null
    /** @type {WebSocket | null} */
    this._socket = null
    /** @type {ReturnType<typeof setTimeout> | null} */
    this._reconnectTimer = null
    /** @type {string | null} Last failure detail, to console-warn only on change. */
    this._lastFailureDetail = null
    /** @type {string | null} Detail stashed by an `error` event, if the runtime provided any. */
    this._socketErrorDetail = null
    /**
     * Whether the current `lastError` is BLOCKING (user must act) rather than
     * transient (just wait / retrying). True only for the constructor-throw
     * path in `_open` — a manifest/network-permission denial, the one failure
     * the user has to fix. A connection-refused / server-not-up close (code
     * 1006, the "ComfyUI is still starting" case) is transient, so this stays
     * false. The panel uses it to decide whether the error breaks out of the
     * Advanced section (blocking) or stays tucked inside it (transient).
     * @type {boolean}
     */
    this._lastErrorBlocking = false
    /**
     * Non-null when the plugin is intentionally NOT connected and NOT
     * auto-retrying, awaiting an explicit user Connect:
     *  - `'superseded'`: the server closed us (code 4000) because another
     *    plugin took over this ComfyUI; standing by avoids a reconnect
     *    tug-of-war between two machines.
     *  - `'manual'`: the user pressed Disconnect.
     * `null` means normal auto-connect/auto-retry behavior.
     * @type {'superseded' | 'manual' | null}
     */
    this._standby = null
    this._started = false
    /**
     * In-flight `request_file` downloads awaiting reassembly, keyed by
     * `handoff_id` (docs/PROTOCOL.md §3). See {@link requestFile}.
     * @type {Map<string, {chunks: string[], received: number, total: number | null, resolve: (bytes: Uint8Array) => void, reject: (error: Error) => void, timer: ReturnType<typeof setTimeout>}>}
     */
    this._pendingDownloads = new Map()
    /**
     * In-flight `upload_edit` uploads awaiting an `upload_ok`/`upload_error`
     * ack, keyed by `handoff_id`. See {@link uploadEditOverWs}.
     * @type {Map<string, {resolve: () => void, reject: (error: Error) => void, timer: ReturnType<typeof setTimeout>}>}
     */
    this._pendingUploads = new Map()
    /**
     * In-flight `manual_push` sends awaiting a `manual_push_ok`/
     * `manual_push_error` ack, keyed by the plugin's own `push_id` (no
     * `handoff_id` exists until the server creates one) — "send a
     * layer/document to ComfyUI", 2026-07-23. See {@link pushManualSendOverWs}.
     * @type {Map<string, {resolve: (handoffId: string) => void, reject: (error: Error) => void, timer: ReturnType<typeof setTimeout>}>}
     */
    this._pendingPushes = new Map()
  }

  /**
   * Starts the connection manager. Safe to call more than once — only the
   * first call has any effect, so `index.js` can call it unconditionally
   * from `plugin.create()`.
   * @returns {void}
   */
  start() {
    if (this._started) return
    this._started = true
    this._open()
  }

  /**
   * User-initiated Disconnect (the panel's "cancel"): stop connecting, stop
   * auto-retrying, and tear down any socket. The plugin stays off until an
   * explicit {@link connect}. Use it to bow out of a two-machine tug-of-war,
   * or to stop a stuck retry loop.
   * @returns {void}
   */
  disconnect() {
    this._standby = 'manual'
    this._teardownSocket()
    if (this._reconnectTimer) {
      clearTimeout(this._reconnectTimer)
      this._reconnectTimer = null
    }
    this.attempts = 0
    this.nextRetryAt = null
    this.lastError = null
    this._lastFailureDetail = null
    this._lastErrorBlocking = false
    this.serverVersion = null
    this.localMode = null
    logInfo('disconnected by user — standing by until Connect')
    this._setStatus('disconnected')
  }

  /**
   * User-initiated Connect / Take over: clears any standby (manual or
   * superseded) and (re)connects to the current server base. When another
   * Photoshop currently holds the connection, this reclaims it — the server
   * hands the single plugin slot to the newest connector; the previously
   * connected plugin then stands by rather than fighting back.
   * @returns {void}
   */
  connect() {
    this._standby = null
    this._started = true
    this._reconnectNow()
  }

  /**
   * Detaches handlers from the current socket and closes it. Handlers are
   * removed BEFORE closing so the imminent close does not run `_onClose`
   * (which would record a failure and schedule its own reconnect). No-op when
   * there is no socket.
   * @returns {void}
   */
  _teardownSocket() {
    // Any in-flight requestFile()/uploadEditOverWs() call can never complete
    // over a socket that's about to be torn down — fail it now rather than
    // making the caller wait out its own timeout. No-op when nothing is
    // pending (the common case).
    this._rejectAllPending('Connection closed')
    if (!this._socket) return
    const socket = this._socket
    this._socket = null
    socket.onopen = null
    socket.onmessage = null
    socket.onclose = null
    socket.onerror = null
    try {
      socket.close()
    } catch (_error) {
      // Already closing/closed — nothing to do.
    }
  }

  /**
   * Fails every in-flight {@link requestFile}/{@link uploadEditOverWs} call
   * with *reason* and clears both pending maps. Called whenever the socket
   * is known to be gone (`_teardownSocket`) or just closed (`_onClose`) —
   * either way, no more `file_chunk`/`upload_ok`/etc. will ever arrive for
   * these on this connection.
   * @param {string} reason
   * @returns {void}
   */
  _rejectAllPending(reason) {
    for (const pending of this._pendingDownloads.values()) {
      clearTimeout(pending.timer)
      pending.reject(new Error(reason))
    }
    this._pendingDownloads.clear()
    for (const pending of this._pendingUploads.values()) {
      clearTimeout(pending.timer)
      pending.reject(new Error(reason))
    }
    this._pendingUploads.clear()
    for (const pending of this._pendingPushes.values()) {
      clearTimeout(pending.timer)
      pending.reject(new Error(reason))
    }
    this._pendingPushes.clear()
  }

  /**
   * The active server base (`host:port`) the plugin connects to. Prefill the
   * panel's server field with this.
   * @returns {string}
   */
  getServerBase() {
    return this._serverBase
  }

  /**
   * WebSocket URL derived from the current server base (docs/PROTOCOL.md §3).
   * @returns {string}
   */
  getWsUrl() {
    return `ws://${this._serverBase}/cpsb/ws`
  }

  /**
   * HTTP origin derived from the current server base, for the `/cpsb/file/*`
   * and `/cpsb/upload` routes (docs/PROTOCOL.md §2). Replaces the former
   * module-level `HTTP_ORIGIN` constant with a getter so the origin tracks a
   * reconfigured server base — every consumer (handoffs.js, uploader.js) calls
   * this per request rather than capturing a value at load time.
   * @returns {string}
   */
  getHttpOrigin() {
    return `http://${this._serverBase}`
  }

  /**
   * Points the plugin at a (possibly different) ComfyUI server. Validates and
   * normalizes the input to `host:port` — throwing a clear `Error` on
   * empty/malformed input, which the panel surfaces inline — persists it, and
   * performs a clean reconnect against the new URL. `getState().url` reflects
   * the new target immediately.
   * @param {string} value - e.g. `192.168.1.50:8188` or `http://host:8188`.
   * @returns {void}
   */
  setServerBase(value) {
    const normalized = normalizeServerBase(value)
    this._serverBase = normalized
    persistServerBase(normalized)
    logInfo(`server address set to ${normalized} — reconnecting`)
    this._reconnectNow()
  }

  /**
   * Tears down the current socket and any pending reconnect timer, resets all
   * backoff/handshake state to a clean slate, and immediately opens a fresh
   * connection against the current server base. Used by `setServerBase()`.
   * @returns {void}
   */
  _reconnectNow() {
    if (this._reconnectTimer) {
      clearTimeout(this._reconnectTimer)
      this._reconnectTimer = null
    }
    this._teardownSocket()
    this.attempts = 0
    this.nextRetryAt = null
    this.lastError = null
    this._lastFailureDetail = null
    this._lastErrorBlocking = false
    this._socketErrorDetail = null
    this.serverVersion = null
    this.localMode = null
    // Ensure a reconnect works even if `setServerBase` is somehow called
    // before `start()` (start() is normally called first, from plugin.create).
    this._started = true
    this._open()
  }

  /** @returns {void} */
  _open() {
    this.nextRetryAt = null
    this._socketErrorDetail = null
    this._setStatus('connecting')
    /** @type {WebSocket} */
    let socket
    try {
      socket = new WebSocket(this.getWsUrl())
    } catch (error) {
      // Constructor-throw path. This is where a manifest-permission denial
      // surfaces (UXP's WebSocket is documented to throw from the
      // constructor, and permission problems reject at creation) — the
      // thrown message is surfaced verbatim so "Permission denied" in the
      // log/panel is clearly distinguishable from the onclose path below
      // (connection refused / server absent, which reach `_onClose` with a
      // close code instead).
      this._recordFailure(describeError(error))
      // Permission/constructor failures are the one BLOCKING case — the user
      // has to fix the plugin's network permission; surfaced prominently.
      this._lastErrorBlocking = true
      this._scheduleReconnect()
      this._setStatus('disconnected')
      return
    }
    this._socket = socket
    socket.onopen = () => this._onOpen()
    socket.onmessage = (event) => {
      // Deliberately not awaited: `_onMessage` catches every error it can
      // encounter internally, so this never produces an unhandled rejection.
      this._onMessage(event)
    }
    socket.onclose = (event) => this._onClose(event)
    socket.onerror = (event) => {
      // The WebSocket spec gives `error` events little to no detail and
      // `close` always follows, so all failure bookkeeping/scheduling lives
      // in `_onClose`. If this runtime DID attach a message to the error
      // event, stash it so the close-path failure text can include it.
      const detail =
        event && (/** @type {any} */ (event).message || (/** @type {any} */ (event).error && /** @type {any} */ (event).error.message))
      if (detail) this._socketErrorDetail = String(detail)
    }
  }

  /** @returns {void} */
  _onOpen() {
    try {
      /** @type {CpsbHelloMessage} */
      const hello = {
        type: 'hello',
        plugin_version: uxp.versions.plugin,
        ps_version: uxp.host.version,
        uxp_version: uxp.versions.uxp
      }
      this.send(hello)
    } catch (error) {
      logError(`failed to send hello: ${describeError(error)}`)
    }
  }

  /**
   * @param {MessageEvent} event
   * @returns {Promise<void>}
   */
  async _onMessage(event) {
    /** @type {CpsbServerMessage | null} */
    let msg = null
    try {
      msg = JSON.parse(/** @type {string} */ (event.data))
    } catch (error) {
      logWarn(`ignoring non-JSON WebSocket frame: ${describeError(error)}`)
      return
    }
    try {
      await this._handleMessage(msg)
    } catch (error) {
      logError(`error handling "${msg && msg.type}" message: ${describeError(error)}`)
    }
  }

  /**
   * @param {CpsbServerMessage | null} msg
   * @returns {Promise<void>}
   */
  async _handleMessage(msg) {
    if (!msg || typeof msg.type !== 'string') return
    if (msg.type === 'hello_ack') {
      await this._completeHandshake(/** @type {CpsbHelloAckMessage} */ (msg))
      return
    }
    if (msg.type === 'ping') {
      this.send(/** @type {CpsbPongMessage} */ ({ type: 'pong' }))
      return
    }
    if (msg.type === 'file_chunk') {
      this._onFileChunk(/** @type {CpsbFileChunkMessage} */ (msg))
      return
    }
    if (msg.type === 'file_error') {
      this._onFileError(/** @type {CpsbFileErrorMessage} */ (msg))
      return
    }
    if (msg.type === 'upload_ok') {
      this._onUploadResult(/** @type {CpsbUploadOkMessage} */ (msg).handoff_id, null, undefined)
      return
    }
    if (msg.type === 'upload_error') {
      const errorMsg = /** @type {CpsbUploadErrorMessage} */ (msg)
      this._onUploadResult(errorMsg.handoff_id, errorMsg.error || 'Upload rejected', errorMsg.reason)
      return
    }
    if (msg.type === 'manual_push_ok') {
      const okMsg = /** @type {{push_id: string, handoff_id: string}} */ (msg)
      this._onManualPushResult(okMsg.push_id, okMsg.handoff_id, null)
      return
    }
    if (msg.type === 'manual_push_error') {
      const errorMsg = /** @type {{push_id: string, error: string}} */ (msg)
      this._onManualPushResult(errorMsg.push_id, null, errorMsg.error || 'Push rejected')
      return
    }
    // open_handoff / handoff_cancelled / anything future and unrecognized —
    // not this module's concern. handoffs.js listens for these.
    this.dispatchEvent(new CustomEvent('message', { detail: msg }))
  }

  /**
   * Accumulates one `file_chunk` for its `handoff_id`'s in-flight
   * {@link requestFile} call; once every chunk (`0..total-1`) has arrived,
   * concatenates them in `seq` order and resolves with the decoded bytes. A
   * chunk for a `handoff_id` with no pending download (already timed out,
   * already resolved, or simply unexpected) is ignored.
   * @param {CpsbFileChunkMessage} msg
   * @returns {void}
   */
  _onFileChunk(msg) {
    const pending = this._pendingDownloads.get(msg.handoff_id)
    if (!pending) return
    if (pending.chunks[msg.seq] === undefined) pending.received += 1
    pending.chunks[msg.seq] = msg.data_b64
    pending.total = msg.total
    if (pending.total === null || pending.received < pending.total) return
    this._pendingDownloads.delete(msg.handoff_id)
    clearTimeout(pending.timer)
    try {
      pending.resolve(base64Decode(pending.chunks.join('')))
    } catch (error) {
      pending.reject(error instanceof Error ? error : new Error(String(error)))
    }
  }

  /**
   * Fails the in-flight {@link requestFile} call for `msg.handoff_id`, if
   * any (see {@link _onFileChunk} for the "if any" rationale).
   * @param {CpsbFileErrorMessage} msg
   * @returns {void}
   */
  _onFileError(msg) {
    const pending = this._pendingDownloads.get(msg.handoff_id)
    if (!pending) return
    this._pendingDownloads.delete(msg.handoff_id)
    clearTimeout(pending.timer)
    pending.reject(new Error(msg.error || 'Server reported a file error'))
  }

  /**
   * Resolves or rejects the in-flight {@link uploadEditOverWs} call for
   * `handoffId` (`upload_ok` -> resolve, `upload_error` -> reject with
   * `error.reason` set to *reason* when the server sent one, so
   * `uploader.js` can decide whether retrying makes sense). A result for a
   * `handoffId` with no pending upload is ignored.
   * @param {string} handoffId
   * @param {string | null} errorMessage - `null` for `upload_ok`.
   * @param {string | undefined} reason
   * @returns {void}
   */
  _onUploadResult(handoffId, errorMessage, reason) {
    const pending = this._pendingUploads.get(handoffId)
    if (!pending) return
    this._pendingUploads.delete(handoffId)
    clearTimeout(pending.timer)
    if (errorMessage === null) {
      pending.resolve()
      return
    }
    const error = new Error(errorMessage)
    if (reason) /** @type {any} */ (error).reason = reason
    pending.reject(error)
  }

  /**
   * Resolves or rejects the in-flight {@link pushManualSendOverWs} call for
   * `pushId` (`manual_push_ok` -> resolve with the server's new
   * `handoffId`, `manual_push_error` -> reject). A result for a `pushId`
   * with no pending push is ignored — mirrors {@link _onUploadResult}.
   * @param {string} pushId
   * @param {string | null} handoffId - The new handoff id on success, `null`
   * for `manual_push_error`.
   * @param {string | null} errorMessage - `null` for `manual_push_ok`.
   * @returns {void}
   */
  _onManualPushResult(pushId, handoffId, errorMessage) {
    const pending = this._pendingPushes.get(pushId)
    if (!pending) return
    this._pendingPushes.delete(pushId)
    clearTimeout(pending.timer)
    if (errorMessage === null) {
      pending.resolve(/** @type {string} */ (handoffId))
      return
    }
    pending.reject(new Error(errorMessage))
  }

  /**
   * @param {CpsbHelloAckMessage} msg
   * @returns {Promise<void>}
   */
  async _completeHandshake(msg) {
    this.serverVersion = msg.server_version
    if (msg.server_version !== uxp.versions.plugin) {
      // docs/PROTOCOL.md §9: during 0.x a plugin/server version mismatch
      // warns but never refuses the connection. Ring buffer only — the
      // panel's version line is the user-facing surface for this.
      logWarn(
        `version mismatch: plugin v${uxp.versions.plugin} vs server ` +
          `v${msg.server_version} — update whichever is behind (connection is fine)`
      )
    }
    this.localMode = await this._probeLocalMode(msg.input_cpsb_path)
    /** @type {CpsbReadyMessage} */
    const ready = { type: 'ready', local_mode: this.localMode }
    this.send(ready)
    this.attempts = 0
    this.nextRetryAt = null
    this.lastError = null
    this._lastFailureDetail = null
    this._lastErrorBlocking = false
    this._standby = null
    this._setStatus('connected')
    logInfo(
      `connected to server ${msg.server_version} (${this.localMode ? 'local' : 'remote'} mode)`
    )
    // Fire-and-forget: this plugin's one-per-session attempt at fixing
    // Photoshop's "Maximize PSD Compatibility" preference (docs/SPIKES.md
    // spike 8). Deliberately NOT awaited — ensureMaximizeCompatibility()
    // never throws (every failure is caught and logged internally as a
    // warning) and its own `executeAsModal` round-trip must not delay this
    // handshake completing or hold up whatever the caller of `_onMessage`
    // does next.
    ensureMaximizeCompatibility()
  }

  /**
   * Probes whether `input_cpsb_path` exists on this machine's filesystem —
   * the local/remote mode decision from the handshake (docs/PROTOCOL.md §3).
   * @param {string} inputCpsbPath - Absolute path, from `hello_ack`.
   * @returns {Promise<boolean>} True (local mode) if this filesystem can see
   * that exact path directly; false (remote mode) otherwise.
   */
  async _probeLocalMode(inputCpsbPath) {
    try {
      await localFileSystem.getEntryWithUrl(pathToFileUrl(inputCpsbPath))
      return true
    } catch (_error) {
      // Expected in remote mode (no shared filesystem) — not a fault.
      return false
    }
  }

  /**
   * @param {CloseEvent} event
   * @returns {void}
   */
  _onClose(event) {
    const wasConnected = this.status === 'connected'
    this._socket = null
    this.localMode = null
    // The socket is gone either way (superseded, dropped, or refused) — no
    // `file_chunk`/`upload_ok`/etc. is ever coming for whatever was still
    // in flight. Note this is the natural-close path (handlers still
    // attached); a manual disconnect/reconnect goes through
    // `_teardownSocket`, which does the equivalent cleanup itself since
    // handlers are stripped before close() there and this handler never runs.
    this._rejectAllPending('Connection closed')
    // Every close-path failure is transient (server not up yet / refused /
    // dropped) — the "just wait, it's retrying" case, NOT something the user
    // must act on. Only the constructor-throw path in `_open` is blocking.
    this._lastErrorBlocking = false
    const code = event ? event.code : undefined
    const reason = event ? event.reason : ''
    // Match the server's replace-close by code OR reason. Real UXP delivers
    // code 4000 + this reason (observed in the field); the reason match is
    // insurance against a client that surfaces the coded close as a bare 1006
    // but still carries the reason text.
    const superseded =
      code === WS_CLOSE_REPLACED ||
      (typeof reason === 'string' && reason.indexOf('replaced by a new connection') !== -1)
    if (superseded) {
      // Another plugin (e.g. Photoshop on another machine, same ComfyUI) took
      // over the server's single plugin slot. STAND BY instead of reconnecting
      // — auto-reconnecting here would kick the other one off and spin up an
      // endless two-machine tug-of-war. The user re-claims with Connect.
      this._standby = 'superseded'
      // Not an error — a state. The panel's standby line carries the message;
      // clearing lastError keeps it out of the Advanced diagnostic line.
      this.lastError = null
      logInfo('superseded by another plugin connection — standing by')
      this._setStatus('disconnected')
      return
    }
    if (wasConnected) {
      // An established connection dropped — not a failed attempt, so the
      // attempt counter stays at 0 and the next retry starts the backoff
      // schedule from the beginning.
      this.lastError = `connection lost (code ${code}${reason ? `, ${reason}` : ''})`
      logWarn(`disconnected from server (${this.lastError}) — reconnecting`)
    } else {
      // A connection attempt that never opened (or died before hello_ack).
      // Connection-refused / server-absent both surface here, typically as
      // close code 1006 with no reason — distinguishable in the log from a
      // permission denial, which throws from the constructor instead.
      let detail = `connection failed (code ${code}${reason ? `, ${reason}` : ''})`
      if (this._socketErrorDetail) detail += `; ${this._socketErrorDetail}`
      this._recordFailure(detail)
    }
    // Schedule BEFORE announcing the state change so the statechange
    // payload already carries nextRetryAt for the panel's countdown.
    this._scheduleReconnect()
    this._setStatus('disconnected')
  }

  /**
   * Records one failed connection attempt: bumps the attempt counter, sets
   * `lastError`, and writes the attempt (with the actual exception/close
   * detail) to the ring buffer. Only a NEW failure message reaches
   * `console.warn` (via `logError`); repeats of the same message go to the
   * ring buffer only (`logWarn`) — reconnecting forever every 10s must not
   * spam the console with an identical line each time.
   * @param {string} detail
   * @returns {void}
   */
  _recordFailure(detail) {
    this.attempts += 1
    this.lastError = detail
    const message = `connect attempt ${this.attempts} failed: ${detail}`
    if (detail === this._lastFailureDetail) {
      logWarn(message)
    } else {
      logError(message)
    }
    this._lastFailureDetail = detail
  }

  /** @returns {void} */
  _scheduleReconnect() {
    // Standing by (superseded by another plugin, or user-disconnected) means
    // no auto-retry — only an explicit `connect()` brings it back.
    if (this._standby) return
    if (this._reconnectTimer) return
    const delay =
      BACKOFF_STEPS_MS[Math.min(Math.max(this.attempts - 1, 0), BACKOFF_STEPS_MS.length - 1)]
    this.nextRetryAt = Date.now() + delay
    this._reconnectTimer = setTimeout(() => {
      this._reconnectTimer = null
      this._open()
    }, delay)
  }

  /**
   * Sends one JSON control message. Drops (with a log line) if the socket
   * isn't open — every caller treats delivery as best-effort, since the
   * reconnect loop re-establishes full state via the next handshake anyway.
   * @param {Record<string, unknown>} message
   * @returns {void}
   */
  send(message) {
    if (!this._socket || this._socket.readyState !== WS_READY_STATE_OPEN) {
      logWarn(`dropped "${message.type}" message — socket not open`)
      return
    }
    this._socket.send(JSON.stringify(message))
  }

  /**
   * Downloads `handoffId`'s edit-target PSD over this websocket
   * (docs/PROTOCOL.md §3: `request_file` -> one or more `file_chunk`s ->
   * reassembled bytes, or a `file_error`). Used by `handoffs.js`'s
   * `openRemote` in place of a plain HTTP `fetch` GET, which UXP's runtime
   * blocks for a non-localhost host even though the identical-origin
   * `ws://` connection this method rides on works fine.
   *
   * Rejects immediately (no message sent) if the socket isn't currently
   * open, on `file_error` from the server, if the download doesn't finish
   * within {@link TRANSFER_TIMEOUT_MS}, or if the connection closes/resets
   * while the download is in flight (see {@link _rejectAllPending}).
   * @param {string} handoffId
   * @returns {Promise<Uint8Array>}
   */
  requestFile(handoffId) {
    if (!this._socket || this._socket.readyState !== WS_READY_STATE_OPEN) {
      return Promise.reject(new Error('Not connected to the ComfyUI server'))
    }
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this._pendingDownloads.delete(handoffId)
        reject(new Error(`request_file for ${handoffId} timed out after ${TRANSFER_TIMEOUT_MS}ms`))
      }, TRANSFER_TIMEOUT_MS)
      this._pendingDownloads.set(handoffId, { chunks: [], received: 0, total: null, resolve, reject, timer })
      this.send(/** @type {CpsbRequestFileMessage} */ ({ type: 'request_file', handoff_id: handoffId }))
    })
  }

  /**
   * Uploads `bytes` for `handoffId` over this websocket, split into
   * {@link WS_TRANSFER_CHUNK_CHARS}-character base64 `upload_edit` chunks
   * (docs/PROTOCOL.md §3), resolving once the server acks `upload_ok`. Used
   * by `uploader.js` in REMOTE mode in place of a plain HTTP `fetch` POST
   * (the same UXP cleartext-to-remote-host restriction {@link requestFile}
   * exists for) — for both the original flat-PNG upload and the newer
   * layered-PSD one (`kind`, remote Tier-2 layered annotate,
   * docs/PROTOCOL.md §6d): the chunking/reassembly scheme is identical
   * either way, only the server-side interpretation of the reassembled
   * bytes differs.
   *
   * Rejects immediately (no messages sent) if the socket isn't currently
   * open, on `upload_error` from the server (the rejected `Error` carries a
   * `.reason` when the server sent one — see `CpsbUploadErrorMessage`), if
   * no ack arrives within {@link TRANSFER_TIMEOUT_MS}, or if the connection
   * closes/resets mid-upload.
   * @param {string} handoffId
   * @param {Uint8Array} bytes
   * @param {'png' | 'psd'} [kind] - Defaults to `'png'`, the original
   * transport.
   * @returns {Promise<void>}
   */
  uploadEditOverWs(handoffId, bytes, kind = 'png') {
    if (!this._socket || this._socket.readyState !== WS_READY_STATE_OPEN) {
      return Promise.reject(new Error('Not connected to the ComfyUI server'))
    }
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this._pendingUploads.delete(handoffId)
        reject(new Error(`upload_edit for ${handoffId} timed out after ${TRANSFER_TIMEOUT_MS}ms`))
      }, TRANSFER_TIMEOUT_MS)
      this._pendingUploads.set(handoffId, { resolve, reject, timer })

      const chunks = splitIntoChunks(base64Encode(bytes))
      const total = chunks.length
      for (let seq = 0; seq < total; seq++) {
        this.send(
          /** @type {CpsbUploadEditMessage} */ ({
            type: 'upload_edit',
            handoff_id: handoffId,
            seq,
            total,
            data_b64: chunks[seq],
            fidelity: 'plugin',
            kind
          })
        )
      }
    })
  }

  /**
   * Sends *bytes* as a brand-new "send a layer/document to ComfyUI" push
   * (2026-07-23) — the reverse of every other transfer in this file: there
   * is no existing `handoff_id` to correlate by, since this transfer is
   * what CREATES one. Chunked identically to {@link uploadEditOverWs}
   * (same `WS_TRANSFER_CHUNK_CHARS` split), correlated instead by *pushId*,
   * a caller-generated id used purely for THIS transfer's own reassembly
   * bookkeeping (server-side: `cpsb.routes._PendingPush`) — never persisted,
   * never becomes the real handoff id. Resolves with the server's newly
   * minted `handoff_id` on `manual_push_ok`; rejects on `manual_push_error`,
   * timeout, or a mid-transfer disconnect (`_rejectAllPending`), mirroring
   * every other transfer method's failure posture in this file.
   * @param {string} pushId - Caller-generated, unique per call (a fresh
   * random string is enough — nothing here interprets it beyond using it
   * as a Map key both here and server-side for correlation).
   * @param {Uint8Array} bytes
   * @param {string} title - Becomes the new handoff's `source.filename`
   * (`cpsb/routes.py`'s `_handle_manual_push_chunk`), which the ComfyUI
   * gallery shows as the card's title.
   * @returns {Promise<string>} The new `handoff_id`.
   */
  pushManualSendOverWs(pushId, bytes, title) {
    if (!this._socket || this._socket.readyState !== WS_READY_STATE_OPEN) {
      return Promise.reject(new Error('Not connected to the ComfyUI server'))
    }
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this._pendingPushes.delete(pushId)
        reject(new Error(`manual_push ${pushId} timed out after ${TRANSFER_TIMEOUT_MS}ms`))
      }, TRANSFER_TIMEOUT_MS)
      this._pendingPushes.set(pushId, { resolve, reject, timer })

      const chunks = splitIntoChunks(base64Encode(bytes))
      const total = chunks.length
      for (let seq = 0; seq < total; seq++) {
        this.send({
          type: 'manual_push',
          push_id: pushId,
          seq,
          total,
          data_b64: chunks[seq],
          title
        })
      }
    })
  }

  /** @returns {CpsbConnectionState} */
  getState() {
    return {
      status: this.status,
      url: this.getWsUrl(),
      serverVersion: this.serverVersion,
      localMode: this.localMode,
      lastError: this.lastError,
      lastErrorBlocking: this._lastErrorBlocking,
      standby: this._standby,
      attempts: this.attempts,
      nextRetryAt: this.nextRetryAt
    }
  }

  /**
   * @param {'disconnected' | 'connecting' | 'connected'} status
   * @returns {void}
   */
  _setStatus(status) {
    this.status = status
    this.dispatchEvent(new CustomEvent('statechange', { detail: this.getState() }))
  }
}

/** The one websocket connection this plugin maintains, for the lifetime of the plugin. */
const connection = new ConnectionManager()

module.exports = { pathToFileUrl, connection }
