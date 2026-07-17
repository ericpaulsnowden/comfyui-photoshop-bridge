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
 * Default ComfyUI port (docs/PROTOCOL.md, PLAN.md §5). Tier 2's MVP does not
 * support a non-default port — see PLAN.md §6/§10 — so this is a constant,
 * not a setting.
 */
const WS_URL = 'ws://127.0.0.1:8188/cpsb/ws'

/** HTTP origin for the `/cpsb/file/*` and `/cpsb/upload` routes (PROTOCOL.md §2). */
const HTTP_ORIGIN = 'http://127.0.0.1:8188'

/** Exponential backoff schedule in ms, capping at 10s forever (PLAN.md §5). */
const BACKOFF_STEPS_MS = [1000, 2000, 5000, 10000]

/** `WebSocket.readyState` value meaning "open" (docs/reference-js WebSocket page). */
const WS_READY_STATE_OPEN = 1

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
 * @property {string | null} lastError
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
 * Manages the single websocket connection to ComfyUI. Import the shared
 * `connection` singleton below rather than constructing this directly.
 */
class ConnectionManager extends EventTarget {
  constructor() {
    super()
    /** @type {'disconnected' | 'connecting' | 'connected'} */
    this.status = 'disconnected'
    /** @type {string | null} */
    this.serverVersion = null
    /** @type {boolean | null} */
    this.localMode = null
    /** @type {string | null} */
    this.lastError = null
    /** @type {WebSocket | null} */
    this._socket = null
    this._backoffIndex = 0
    /** @type {ReturnType<typeof setTimeout> | null} */
    this._reconnectTimer = null
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

  /** @returns {void} */
  _open() {
    this._setStatus('connecting')
    /** @type {WebSocket} */
    let socket
    try {
      socket = new WebSocket(WS_URL)
    } catch (error) {
      this.lastError = describeError(error)
      logError(`could not create WebSocket for ${WS_URL}: ${this.lastError}`)
      this._scheduleReconnect()
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
    socket.onerror = () => {
      // The WebSocket spec gives `error` events no useful detail; `close`
      // always follows and carries what detail is available, so reconnect
      // scheduling lives there exclusively to avoid double-scheduling.
      logWarn(`WebSocket error event for ${WS_URL}`)
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
    this.localMode = await this._probeLocalMode(msg.input_cpsb_path)
    /** @type {CpsbReadyMessage} */
    const ready = { type: 'ready', local_mode: this.localMode }
    this.send(ready)
    this._backoffIndex = 0
    this.lastError = null
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
    this._setStatus('disconnected')
    if (wasConnected) {
      const reason = (event && event.reason) || `code ${event && event.code}`
      logWarn(`disconnected from server (${reason}) — reconnecting`)
    }
    this._scheduleReconnect()
  }

  /** @returns {void} */
  _scheduleReconnect() {
    if (this._reconnectTimer) return
    const delay = BACKOFF_STEPS_MS[Math.min(this._backoffIndex, BACKOFF_STEPS_MS.length - 1)]
    this._backoffIndex += 1
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
      url: WS_URL,
      serverVersion: this.serverVersion,
      localMode: this.localMode,
      lastError: this.lastError
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

module.exports = { HTTP_ORIGIN, pathToFileUrl, connection }
