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
 * @property {string} psd_path - Absolute path to `source.psd` (local mode).
 * @property {string} file_url - e.g. `/cpsb/file/<id>` (remote mode).
 */

/**
 * @typedef {Object} CpsbHandoffCancelledMessage
 * @property {'handoff_cancelled'} type
 * @property {string} handoff_id
 */

/**
 * @typedef {CpsbHelloAckMessage | CpsbPingMessage | CpsbOpenHandoffMessage | CpsbHandoffCancelledMessage} CpsbServerMessage
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
    // open_handoff / handoff_cancelled / anything future and unrecognized —
    // not this module's concern. handoffs.js listens for these.
    this.dispatchEvent(new CustomEvent('message', { detail: msg }))
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
