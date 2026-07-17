/**
 * @file Small shared UI helpers used by `menu.js` and `gallery.js`: a toast
 * wrapper, a yes/no confirm wrapper, a multi-choice chooser overlay, a tiny
 * DOM builder, and the one-time stylesheet injector. Kept dependency-free
 * (no framework) per the project's vanilla-JS constraint.
 *
 * `app.extensionManager.toast` and `.dialog.confirm` are used when present
 * (verified shapes: `Comfy-Org/ComfyUI_frontend`
 * `src/types/extensionTypes.ts` `ToastManager`/`ExtensionManager`, and
 * `src/services/dialogService.ts`). Two things are deliberately *never*
 * routed through those native APIs, because the verified shapes cannot
 * express them:
 *  - An action button on a toast (`ToastMessageOptions` has no
 *    action/button field) — `showActionToast` always uses the built-in
 *    overlay.
 *  - A three-way labeled choice (`dialog.confirm`'s `ConfirmOptions` only
 *    ever yields true/false/null, and only supports custom labels on the
 *    fixed `type: "dirtyClose"` preset) — `chooseDialog` always uses the
 *    built-in overlay.
 */

import { app } from '../../../scripts/app.js'
import { warn } from './api.js'

const STYLE_LINK_ID = 'cpsb-styles'
let stylesInjected = false
let toastApiWarned = false
let dialogApiWarned = false

/**
 * Injects `cpsb.css` as a `<link>` once per page load. The href is resolved
 * relative to this module's own URL (`import.meta.url`) rather than a
 * hardcoded `/extensions/...` path, so it keeps working regardless of what
 * folder name the backend mounts this pack under.
 */
export function injectStyles() {
  if (stylesInjected) return
  stylesInjected = true
  if (document.getElementById(STYLE_LINK_ID)) return
  const link = document.createElement('link')
  link.id = STYLE_LINK_ID
  link.rel = 'stylesheet'
  link.href = new URL('./cpsb.css', import.meta.url).href
  document.head.appendChild(link)
}

/**
 * @typedef {Object} ElOptions
 * @property {string} [className]
 * @property {string} [text]
 * @property {Record<string, string>} [attrs]
 * @property {Record<string, EventListener>} [on]
 * @property {(Node | string)[]} [children]
 */

/**
 * Minimal DOM builder — this project is vanilla JS with no templating
 * engine, so every list/card/dialog in `gallery.js` and `menu.js` goes
 * through this one helper for consistency.
 * @param {string} tag
 * @param {ElOptions} [options]
 * @returns {HTMLElement}
 */
export function el(tag, options = {}) {
  const node = document.createElement(tag)
  if (options.className) node.className = options.className
  if (options.text !== undefined) node.textContent = options.text
  if (options.attrs) {
    for (const [key, value] of Object.entries(options.attrs)) {
      node.setAttribute(key, value)
    }
  }
  if (options.on) {
    for (const [type, listener] of Object.entries(options.on)) {
      node.addEventListener(type, listener)
    }
  }
  if (options.children) {
    for (const child of options.children) {
      node.append(child instanceof Node ? child : document.createTextNode(child))
    }
  }
  return node
}

function getToastContainer() {
  let container = document.querySelector('.cpsb-toast-container')
  if (!container) {
    container = el('div', { className: 'cpsb-toast-container' })
    document.body.appendChild(container)
  }
  return container
}

/**
 * @param {HTMLElement} node
 */
function dismissToast(node) {
  if (!node.isConnected) return
  node.classList.remove('cpsb-toast-visible')
  node.addEventListener('transitionend', () => node.remove(), { once: true })
  // Safety net in case the transition never fires (reduced-motion, already
  // hidden tab, etc.) so toasts can never pile up permanently.
  setTimeout(() => node.remove(), 400)
}

/**
 * @param {HTMLElement} node
 * @param {number} life - Milliseconds; 0 or omitted means "no auto-dismiss".
 */
function scheduleDismiss(node, life) {
  if (!life) return
  setTimeout(() => dismissToast(node), life)
}

/**
 * @param {{severity: 'success'|'info'|'warn'|'error', summary: string, detail?: string, life: number}} options
 */
function showFallbackToast({ severity, summary, detail, life }) {
  injectStyles()
  const node = el('div', {
    className: `cpsb-toast cpsb-toast-${severity}`,
    children: [
      el('div', { className: 'cpsb-toast-summary', text: summary }),
      ...(detail ? [el('div', { className: 'cpsb-toast-detail', text: detail })] : [])
    ]
  })
  getToastContainer().appendChild(node)
  requestAnimationFrame(() => node.classList.add('cpsb-toast-visible'))
  scheduleDismiss(node, life)
}

/**
 * Shows a transient, non-blocking notification. Prefers
 * `app.extensionManager.toast.add`; falls back to a built-in toast (styled
 * via `cpsb.css`) on older frontends or if the native call throws.
 * @param {{severity?: 'success'|'info'|'warn'|'error', summary: string, detail?: string, life?: number}} options
 */
export function showToast({ severity = 'info', summary, detail, life = 5000 }) {
  const toastApi = app.extensionManager?.toast
  if (toastApi && typeof toastApi.add === 'function') {
    try {
      toastApi.add({ severity, summary, detail, life })
      return
    } catch (error) {
      if (!toastApiWarned) {
        toastApiWarned = true
        warn(
          'app.extensionManager.toast.add threw; using the built-in ' +
            'fallback toast for the rest of this session',
          error
        )
      }
    }
  } else if (!toastApiWarned) {
    toastApiWarned = true
    warn(
      'app.extensionManager.toast is unavailable on this frontend; using ' +
        'a built-in fallback toast'
    )
  }
  showFallbackToast({ severity, summary, detail, life })
}

/**
 * A transient notification with exactly one clickable action, e.g. "Edit
 * received — [Add as node]". Always uses the built-in overlay (see file
 * header) so the action reliably fires regardless of frontend version.
 * @param {{summary: string, detail?: string, actionLabel: string, onAction: () => void, life?: number}} options
 * @returns {HTMLElement} The toast element, in case the caller wants to
 * dismiss it early (e.g. the node it refers to was removed).
 */
export function showActionToast({ summary, detail, actionLabel, onAction, life = 12000 }) {
  injectStyles()
  const node = el('div', { className: 'cpsb-toast cpsb-toast-info cpsb-toast-action' })
  node.append(
    el('div', {
      className: 'cpsb-toast-body',
      children: [
        el('div', { className: 'cpsb-toast-summary', text: summary }),
        ...(detail ? [el('div', { className: 'cpsb-toast-detail', text: detail })] : [])
      ]
    }),
    el('button', {
      className: 'cpsb-toast-action-button',
      text: actionLabel,
      on: {
        click: () => {
          dismissToast(node)
          onAction()
        }
      }
    })
  )
  getToastContainer().appendChild(node)
  requestAnimationFrame(() => node.classList.add('cpsb-toast-visible'))
  scheduleDismiss(node, life)
  return node
}

/**
 * Simple yes/no confirmation. Prefers `app.extensionManager.dialog.confirm`;
 * falls back to `window.confirm` (always available) on older frontends.
 * @param {{title: string, message: string}} options
 * @returns {Promise<boolean>}
 */
export async function confirmDialog({ title, message }) {
  const dialog = app.extensionManager?.dialog
  if (dialog && typeof dialog.confirm === 'function') {
    try {
      const result = await dialog.confirm({ title, message })
      return result === true
    } catch (error) {
      if (!dialogApiWarned) {
        dialogApiWarned = true
        warn(
          'app.extensionManager.dialog.confirm threw; using window.confirm ' +
            'for the rest of this session',
          error
        )
      }
    }
  } else if (!dialogApiWarned) {
    dialogApiWarned = true
    warn(
      'app.extensionManager.dialog is unavailable on this frontend; using ' +
        'window.confirm'
    )
  }
  return window.confirm(`${title}\n\n${message}`)
}

/**
 * A small modal offering the given labeled choices plus an implicit
 * cancel (Escape key or backdrop click). Always a built-in overlay — see
 * the file header for why native dialog/toast APIs cannot express this.
 * @param {{title: string, message: string, choices: {label: string, value: string, primary?: boolean}[]}} options
 * @returns {Promise<string | null>} The chosen value, or `null` if dismissed
 * without choosing.
 */
export function chooseDialog({ title, message, choices }) {
  injectStyles()
  return new Promise((resolve) => {
    let settled = false
    /** @param {string | null} value */
    const settle = (value) => {
      if (settled) return
      settled = true
      document.removeEventListener('keydown', onKeyDown, true)
      backdrop.remove()
      resolve(value)
    }
    /** @param {KeyboardEvent} event */
    const onKeyDown = (event) => {
      if (event.key === 'Escape') settle(null)
    }
    const actionButtons = choices.map((choice) =>
      el('button', {
        className: `cpsb-dialog-button${choice.primary ? ' cpsb-dialog-button-primary' : ''}`,
        text: choice.label,
        on: { click: () => settle(choice.value) }
      })
    )
    const dialog = el('div', {
      className: 'cpsb-dialog',
      children: [
        el('div', { className: 'cpsb-dialog-title', text: title }),
        el('div', { className: 'cpsb-dialog-message', text: message }),
        el('div', {
          className: 'cpsb-dialog-actions',
          children: [
            ...actionButtons,
            el('button', {
              className: 'cpsb-dialog-button',
              text: 'Cancel',
              on: { click: () => settle(null) }
            })
          ]
        })
      ],
      on: { click: (event) => event.stopPropagation() }
    })
    const backdrop = el('div', {
      className: 'cpsb-dialog-backdrop',
      children: [dialog],
      on: { click: () => settle(null) }
    })
    document.addEventListener('keydown', onKeyDown, true)
    document.body.appendChild(backdrop)
    requestAnimationFrame(() => backdrop.classList.add('cpsb-dialog-visible'))
  })
}

/**
 * Formats a unix-seconds timestamp as a short relative string for the
 * gallery list ("3m ago", "2h ago", "5d ago"), falling back to a locale
 * date beyond 30 days.
 * @param {number} unixSeconds
 * @returns {string}
 */
export function formatRelativeTime(unixSeconds) {
  const deltaS = Math.max(0, Math.round((Date.now() - unixSeconds * 1000) / 1000))
  if (deltaS < 60) return 'just now'
  const deltaM = Math.round(deltaS / 60)
  if (deltaM < 60) return `${deltaM}m ago`
  const deltaH = Math.round(deltaM / 60)
  if (deltaH < 24) return `${deltaH}h ago`
  const deltaD = Math.round(deltaH / 24)
  if (deltaD < 30) return `${deltaD}d ago`
  return new Date(unixSeconds * 1000).toLocaleDateString()
}
