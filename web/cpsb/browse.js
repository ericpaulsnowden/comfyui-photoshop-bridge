/**
 * @file The "Browse..." dialog for `PhotoshopComposePSD.existing_psd_path`
 * (`cpsb/compose_psd.py`, added v0.5.20): a user report (a test-checklist
 * note, verbatim) that they "can't seem to get an arbitrary PSD to show up"
 * when asked to type/paste a server-side path into that STRING widget.
 * ComfyUI nodes run server-side, so a real OS file-open dialog is impossible
 * from the browser — this is the correct pattern instead: the frontend asks
 * `GET /cpsb/browse` (`cpsb/routes.py`) to list a directory, this module
 * renders that listing as a navigable dialog, the user clicks their way to a
 * folder/file, and the CHOSEN path is written back onto the node's
 * `existing_psd_path` widget. This module talks to exactly that one
 * read-only route — it never writes anything itself (the actual PSD, new or
 * appended-to, is only ever written later by `cpsb/compose_psd.py`'s own
 * `execute()`, when the node runs).
 *
 * Wired from `compose.js`'s `attachAppendTargetWidgets`: the "Browse..."
 * button widget that function creates (directly after `existing_psd_path`,
 * ALWAYS enabled -- v0.5.28 removed the `append_to_existing` BOOLEAN/
 * `existing_psd` COMBO this button used to be gated on, see `compose.js`'s
 * own header) calls {@link openBrowseDialog} with the node and that widget.
 *
 * Remote-machine honesty (this project's established convention — compare
 * `compose.js`'s "Written: ... (on ComfyUI machine)" display and `open.js`'s
 * remote-open confirm dialog): the dialog title is explicitly "Browse
 * ComfyUI machine", never just "Browse", so a user reaching this ComfyUI
 * instance from a DIFFERENT computer is never confused about whose
 * filesystem is actually being listed.
 *
 * Always a built-in overlay, no external libraries — this project's
 * established pattern for anything richer than `ui.chooseDialog`'s fixed
 * two/three-button shape (see `ui.js`'s own file header for why native
 * `app.extensionManager.dialog` APIs can't express a scrollable, navigable
 * file list either).
 */

import * as api from './api.js'
import * as ui from './ui.js'

/** Extensions {@link normalizeNewFilename} treats as already present (case-insensitive). */
const PSD_EXTENSIONS = ['.psd', '.psb']

/** Appended to a "New PSD here" name that doesn't already end in one of {@link PSD_EXTENSIONS}. */
const DEFAULT_NEW_PSD_EXTENSION = '.psd'

/**
 * Mirrors `cpsb/routes.py`'s `_BROWSE_MAX_ENTRIES` for the truncation
 * notice's wording only (hand-synced by value, never imported — the same
 * small-stable-constant hand-mirroring convention `compose.js`'s own
 * `MAX_IMAGE_INPUTS` doc comment already establishes for a frontend/backend
 * pair like this one). Never sent to the server and never used to actually
 * cap anything client-side — the server always enforces the real cap;
 * getting this display constant out of sync would only make the truncation
 * message's number wrong, never let more entries through.
 */
const BROWSE_MAX_ENTRIES_DISPLAY = 500

/**
 * @param {string} raw
 * @returns {string} *raw*, trimmed, with `.psd` appended unless it already
 * ends in `.psd`/`.psb` (case-insensitive) — the "New PSD here" mini-input's
 * own contract (this file's header).
 */
function normalizeNewFilename(raw) {
  const trimmed = raw.trim()
  const lower = trimmed.toLowerCase()
  if (PSD_EXTENSIONS.some((ext) => lower.endsWith(ext))) return trimmed
  return `${trimmed}${DEFAULT_NEW_PSD_EXTENSION}`
}

/**
 * @param {number} bytes
 * @returns {string} A short human-readable size ("512 B", "12 KB", "3.4 MB").
 */
function formatFileSize(bytes) {
  if (bytes < 1024) return `${bytes} B`
  const kilobytes = bytes / 1024
  if (kilobytes < 1024) return `${kilobytes.toFixed(kilobytes < 10 ? 1 : 0)} KB`
  const megabytes = kilobytes / 1024
  return `${megabytes.toFixed(megabytes < 10 ? 1 : 0)} MB`
}

/**
 * Applies *path* to *pathWidget* the same way `loadpsd.js`'s `ingestFile`
 * applies an uploaded filename to its own combo widget: set `.value`, then
 * invoke `.callback` defensively (a throwing callback must not break the
 * dialog or leave the widget unset), then request a redraw so the node's
 * canvas-rendered widget text updates immediately.
 * @param {import('../../../scripts/app.js').LGraphNode} node
 * @param {import('../../../scripts/app.js').IBaseWidget} pathWidget
 * @param {string} path
 */
function applyChosenPath(node, pathWidget, path) {
  pathWidget.value = path
  try {
    pathWidget.callback?.(path)
  } catch (error) {
    api.warn('existing_psd_path widget callback threw after a Browse selection', error)
  }
  node.graph?.setDirtyCanvas(true, false)
}

/**
 * @param {{className: string, name: string, meta?: string, onClick: () => void}} options
 * @returns {HTMLElement}
 */
function browseRow({ className, name, meta, onClick }) {
  return ui.el('div', {
    className: `cpsb-browse-row ${className}`,
    children: [
      ui.el('span', { className: 'cpsb-browse-row-name', text: name }),
      ...(meta ? [ui.el('span', { className: 'cpsb-browse-row-meta', text: meta })] : [])
    ],
    on: { click: onClick }
  })
}

/**
 * Opens the "Browse ComfyUI machine" dialog for *node*, wiring the eventual
 * selection onto *pathWidget* (`existing_psd_path`) — the "Browse..." button
 * widget's click handler (`compose.js`'s `attachAppendTargetWidgets`).
 *
 * Guarded per-node (`node.__cpsbBrowseDialogOpen`), not module-global, so
 * two different Compose nodes can each have their own dialog open at the
 * same time, but clicking "Browse..." twice on the SAME node while its
 * dialog is already open is a no-op rather than stacking a second overlay.
 *
 * Keyboard: Escape closes (document-level capture listener, matching
 * `ui.chooseDialog`'s own convention exactly). Clicking the dimmed backdrop,
 * or the "Close" button, also closes without choosing anything.
 * @param {import('../../../scripts/app.js').LGraphNode} node
 * @param {import('../../../scripts/app.js').IBaseWidget} pathWidget
 */
export function openBrowseDialog(node, pathWidget) {
  if (node.__cpsbBrowseDialogOpen) return
  node.__cpsbBrowseDialogOpen = true
  ui.injectStyles()

  /** @type {import('./api.js').CpsbBrowseResponse | null} */
  let current = null
  let closed = false

  const errorBox = ui.el('div', { className: 'cpsb-browse-error' })
  errorBox.style.display = 'none'

  const pathInput = ui.el('input', {
    className: 'cpsb-browse-path-input',
    attrs: { type: 'text', placeholder: 'Type or paste an absolute path…', spellcheck: 'false' }
  })
  const goButton = ui.el('button', { className: 'cpsb-browse-go-button', text: 'Go' })
  const list = ui.el('div', { className: 'cpsb-browse-list' })
  const newNameInput = ui.el('input', {
    className: 'cpsb-browse-new-input',
    attrs: { type: 'text', placeholder: 'New PSD filename…', spellcheck: 'false' }
  })
  // "Use This Name" -- deliberately NOT "Create": clicking this only sets
  // existing_psd_path to a not-yet-existing dir/name pair (this file's
  // header, and PhotoshopComposePSD.execute's own documented "point at a
  // file that isn't there yet is a first-run convenience" behavior) --
  // the real file is written later, when the node actually runs.
  const newButton = ui.el('button', { className: 'cpsb-browse-new-button', text: 'Use This Name' })
  const closeButton = ui.el('button', { className: 'cpsb-browse-close', text: 'Close (Esc)' })

  const dialog = ui.el('div', {
    className: 'cpsb-browse-dialog',
    children: [
      ui.el('div', {
        className: 'cpsb-browse-header',
        children: [
          ui.el('div', { className: 'cpsb-browse-title', text: 'Browse ComfyUI machine' }),
          closeButton
        ]
      }),
      errorBox,
      ui.el('div', { className: 'cpsb-browse-path-row', children: [pathInput, goButton] }),
      list,
      ui.el('div', { className: 'cpsb-browse-new-row', children: [newNameInput, newButton] })
    ],
    on: { click: (event) => event.stopPropagation() }
  })

  const backdrop = ui.el('div', {
    className: 'cpsb-dialog-backdrop',
    children: [dialog],
    on: { click: () => close() }
  })

  /** @param {KeyboardEvent} event */
  function onKeyDown(event) {
    if (event.key === 'Escape') close()
  }

  function close() {
    if (closed) return
    closed = true
    node.__cpsbBrowseDialogOpen = false
    document.removeEventListener('keydown', onKeyDown, true)
    backdrop.remove()
  }

  function showError(message) {
    errorBox.textContent = message
    errorBox.style.display = ''
  }

  function clearError() {
    errorBox.style.display = 'none'
    errorBox.textContent = ''
  }

  /** @param {string} path */
  function choosePath(path) {
    applyChosenPath(node, pathWidget, path)
    ui.showToast({ severity: 'success', summary: 'existing_psd_path set', detail: path })
    close()
  }

  /** @param {import('./api.js').CpsbBrowseResponse} data */
  function render(data) {
    current = data
    pathInput.value = data.path ?? ''
    // "New PSD here" only makes sense once a real, existing directory is
    // being browsed -- the roots listing (data.path === null) is a virtual
    // view (Home / ComfyUI Input / volumes), not itself a directory a file
    // could be created in.
    newNameInput.disabled = data.path == null
    newButton.disabled = data.path == null
    list.replaceChildren()

    const rows = []
    if (data.parent != null) {
      rows.push(
        browseRow({
          className: 'cpsb-browse-row-parent',
          name: '.. (parent directory)',
          onClick: () => load(data.parent)
        })
      )
    }
    for (const dir of data.dirs) {
      rows.push(
        browseRow({
          className: 'cpsb-browse-row-dir',
          name: `${dir.name}/`,
          onClick: () => load(dir.path)
        })
      )
    }
    for (const file of data.files) {
      rows.push(
        browseRow({
          className: 'cpsb-browse-row-file',
          name: file.name,
          meta: formatFileSize(file.size),
          onClick: () => choosePath(file.path)
        })
      )
    }

    if (rows.length === 0) {
      list.append(ui.el('div', { className: 'cpsb-browse-empty', text: 'Nothing here.' }))
    } else {
      for (const row of rows) list.append(row)
    }

    if (data.truncated) {
      list.append(
        ui.el('div', {
          className: 'cpsb-browse-truncated',
          text: `Showing the first ${BROWSE_MAX_ENTRIES_DISPLAY} entries — this folder has more.`
        })
      )
    }
  }

  /** @param {string} path */
  async function load(path) {
    try {
      const data = await api.browseDirectory(path || '')
      clearError()
      render(data)
    } catch (error) {
      showError(api.errorMessage(error))
    }
  }

  function confirmNewName() {
    if (!current || current.path == null) return
    const raw = newNameInput.value
    if (!raw.trim()) return
    const filename = normalizeNewFilename(raw)
    const sep = current.sep || '/'
    const basePath = current.path.endsWith(sep) ? current.path : `${current.path}${sep}`
    choosePath(`${basePath}${filename}`)
  }

  closeButton.addEventListener('click', () => close())
  goButton.addEventListener('click', () => load(pathInput.value))
  pathInput.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault()
      load(pathInput.value)
    }
  })
  newButton.addEventListener('click', () => confirmNewName())
  newNameInput.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault()
      confirmNewName()
    }
  })

  document.addEventListener('keydown', onKeyDown, true)
  document.body.appendChild(backdrop)
  requestAnimationFrame(() => backdrop.classList.add('cpsb-dialog-visible'))

  load('') // initial paint: the browse roots
}
