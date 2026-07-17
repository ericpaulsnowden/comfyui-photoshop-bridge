/**
 * @file Wires up the "ComfyUI" panel's DOM (panel.html) to live plugin
 * state: the connection pill, the active-handoffs list with per-document
 * "Send back now" buttons, and the Advanced section's log ring buffer. Pure
 * UI glue — holds no state of its own beyond the DOM it renders into, and
 * every element it needs already exists in panel.html's static markup (this
 * plugin has exactly one panel, which is also the plugin's one and only
 * HTML document, so there is no per-panel `rootNode` content to build up
 * dynamically).
 */

const { connection } = require('./connection.js')
const { getActiveHandoffs, registryEvents, deliverEdit } = require('./handoffs.js')
const { getLogLines, onLogLine, logError, describeError } = require('./log.js')

/** @type {Record<import('./connection.js').CpsbConnectionState['status'], string>} */
const STATUS_LABELS = {
  disconnected: 'Disconnected',
  connecting: 'Connecting…',
  connected: 'Connected'
}

let initialized = false

/**
 * Wires up the panel. Safe to call more than once — only the first call
 * attaches listeners; in practice `index.js` calls this exactly once, from
 * the panel entrypoint's `create()`.
 * @returns {void}
 */
function initPanel() {
  if (initialized) return
  initialized = true

  const statusDot = /** @type {HTMLElement} */ (document.getElementById('cpsb-status-dot'))
  const statusText = /** @type {HTMLElement} */ (document.getElementById('cpsb-status-text'))
  const serverUrlEl = /** @type {HTMLElement} */ (document.getElementById('cpsb-server-url'))
  const handoffList = /** @type {HTMLElement} */ (document.getElementById('cpsb-handoff-list'))
  const logEl = /** @type {HTMLElement} */ (document.getElementById('cpsb-log'))

  /** @returns {void} */
  function renderConnection() {
    const state = connection.getState()
    statusDot.className = `cpsb-dot cpsb-dot-${state.status}`
    statusText.textContent = STATUS_LABELS[state.status] || state.status
    serverUrlEl.textContent = state.url
  }

  /**
   * @param {import('./handoffs.js').CpsbHandoffRecord} record
   * @returns {HTMLLIElement}
   */
  function buildHandoffItem(record) {
    const item = document.createElement('li')
    item.className = 'cpsb-handoff-item'

    const name = document.createElement('div')
    name.className = 'cpsb-handoff-name'
    name.textContent = record.docTitle
    name.title = record.docTitle

    const status = document.createElement('div')
    status.className = 'cpsb-handoff-status'
    status.textContent = record.status

    const button = document.createElement('sp-button')
    button.setAttribute('quiet', '')
    button.textContent = 'Send back now'
    button.addEventListener('click', () => {
      deliverEdit(record.handoffId).catch((error) => {
        logError(`"Send back now" (panel) failed: ${describeError(error)}`)
      })
    })

    item.appendChild(name)
    item.appendChild(status)
    item.appendChild(button)
    return item
  }

  /** @returns {void} */
  function renderHandoffs() {
    const records = getActiveHandoffs()
    handoffList.innerHTML = ''
    if (records.length === 0) {
      const empty = document.createElement('li')
      empty.className = 'cpsb-empty'
      empty.textContent = 'No documents from ComfyUI are currently open.'
      handoffList.appendChild(empty)
      return
    }
    for (const record of records) {
      handoffList.appendChild(buildHandoffItem(record))
    }
  }

  /**
   * @param {import('./log.js').CpsbLogLine} line
   * @returns {void}
   */
  function appendLogLine(line) {
    const time = new Date(line.ts).toLocaleTimeString()
    logEl.textContent += `[${time}] ${line.level.toUpperCase()} ${line.message}\n`
    logEl.scrollTop = logEl.scrollHeight
  }

  connection.addEventListener('statechange', renderConnection)
  registryEvents.addEventListener('change', renderHandoffs)
  onLogLine(appendLogLine)

  renderConnection()
  renderHandoffs()
  for (const line of getLogLines()) appendLogLine(line)
}

module.exports = { initPanel }
