/**
 * @file "Send a layer/document to ComfyUI" (2026-07-23) -- the reverse of
 * every other round trip in this pack: the USER initiates from a Photoshop
 * command (manifest.json's `sendToComfyUI`, top-level Plugins menu --
 * Photoshop's UXP host has no documented way to add a native layer/document
 * right-click entry, confirmed by Adobe staff on the Creative Cloud
 * Developer Forums, "Submenu items on a context menu": "It does not appear
 * this is wired up in Ps" -- so the Plugins menu is the correct, and only,
 * native trigger surface), with NO existing ComfyUI node or handoff behind
 * it at all.
 *
 * Two sends: the ACTIVE LAYER, isolated into its own same-size document,
 * trimmed to its own non-transparent bounds, then exported ({@link
 * runLayerExport}, the one genuinely new export path this feature adds); or
 * the WHOLE DOCUMENT, reusing `exporter.js`'s existing `runExport()`
 * verbatim (duplicate + flatten + export + close, already spike-6-proven in
 * production -- no new code for this branch at all). The user picks per
 * send via a native `<dialog>`/`uxpShowModal()` picker ({@link
 * showSendPicker}) -- confirmed on the Creative Cloud Developer Forums
 * ("Show dialog without having a panel") that a command-only trigger's
 * execution context already has a usable `document`/`document.body` with no
 * panel or HTML `main` file required; the poster's own working fix is the
 * pattern this follows (`document.createElement('dialog')` + `.
 * uxpShowModal()`), adapted to this codebase's existing convention of
 * building elements via `document.createElement`/`setAttribute`/
 * `addEventListener` (panel.js's own `sp-button` usage) rather than inline
 * `onclick` HTML attributes.
 *
 * **Known, honest, UNVERIFIED risk (needs its own live-Photoshop check, the
 * same spike-gated posture this pack's other genuinely new UXP surfaces
 * already take -- e.g. exporter.js's own PNG-save descriptor, resolved by
 * SPIKES.md spike 6): `Layer.duplicate(destDoc)`'s pixel-offset/position
 * behavior when `destDoc` is a FRESH same-size document is undocumented --
 * Adobe's own reference example only covers duplicating into an ALREADY-
 * OPEN, pre-existing document. Because the destination is created with
 * `fill: TRANSPARENT` and exported WITHOUT flattening (both load-bearing --
 * see {@link runLayerExport}'s own comments), a position shift cannot
 * corrupt the result: the Trim-to-transparent-bounds step still crops
 * correctly around wherever the pixels actually landed, so the worst case
 * is the crop being taken from a shifted spot, never a wrong-sized or
 * white-matted image. A batchPlay "duplicate to new document" descriptor
 * was considered and deliberately NOT used instead: it is unverified for
 * UXP specifically (only the whole-DOCUMENT-duplicate variant is forum-
 * confirmed, and that variant is itself reported broken on Photoshop 2026),
 * so building on it would trade one undocumented risk for a worse one.**
 *
 * **Also known: only the TOPMOST of a multi-layer selection is exported --
 * no auto-merge.** `Layer.merge()`'s documented "merges just the selected
 * layers" behavior is reported broken for a multi-selection on the Creative
 * Cloud Developer Forums (a real bug: only the bottommost pair actually
 * merges, the rest of the selection is silently ignored), with no confirmed
 * batchPlay-merge alternative to fall back to -- so this deliberately never
 * attempts to merge a multi-selection, the same "known, honest limitation"
 * posture `cpsb/actions.py`/`runAction.js` already take for the Action-
 * dialog-freeze risk, rather than silently doing something a real bug
 * report shows doesn't work as advertised. Select a single layer (or merge
 * manually first) before sending if this matters.
 */

const { app, core, constants } = require('photoshop')
const { logInfo, logError, describeError } = require('./log.js')
const { runExport, exportFlattenedDocument } = require('./exporter.js')
const { pushManualSend } = require('./uploader.js')

/**
 * Shows a two-button native dialog asking the user what to send. Works from
 * a bare command-entrypoint context with no panel open -- see this file's
 * own doc comment for the forum source confirming this.
 * @returns {Promise<'layer' | 'document' | null>} `null` if the dialog was
 * dismissed (e.g. Escape) rather than a button clicked.
 */
async function showSendPicker() {
  const dialog = document.createElement('dialog')

  const heading = document.createElement('h3')
  heading.textContent = 'Send to ComfyUI'
  dialog.appendChild(heading)

  const body = document.createElement('p')
  body.textContent = 'Send just the active layer, or the whole document?'
  dialog.appendChild(body)

  const actions = document.createElement('div')
  actions.style.display = 'flex'
  actions.style.gap = '8px'
  actions.style.justifyContent = 'flex-end'

  const layerButton = document.createElement('sp-button')
  layerButton.setAttribute('variant', 'secondary')
  layerButton.textContent = 'Active Layer'
  layerButton.addEventListener('click', () => dialog.close('layer'))

  const documentButton = document.createElement('sp-button')
  documentButton.setAttribute('variant', 'cta')
  documentButton.textContent = 'Whole Document'
  documentButton.addEventListener('click', () => dialog.close('document'))

  actions.appendChild(layerButton)
  actions.appendChild(documentButton)
  dialog.appendChild(actions)
  document.body.appendChild(dialog)

  try {
    const result = await dialog.uxpShowModal({ title: 'ComfyUI Bridge', resize: 'none' })
    return result === 'layer' || result === 'document' ? result : null
  } finally {
    dialog.remove()
  }
}

/**
 * Isolates *doc*'s topmost active/selected layer into its OWN same-size
 * document, trims it to its own non-transparent bounds, and exports it via
 * the SAME batchPlay/Imaging export pair `exporter.js` already uses for the
 * whole document ({@link exportFlattenedDocument}) -- no new export logic,
 * only a new source document to feed it. See this file's own doc comment
 * for the two known, honest limitations (fresh-document position, no
 * multi-select merge) this leans on.
 * @param {import('photoshop').Document} doc
 * @returns {Promise<{bytes: Uint8Array, title: string}>}
 * @throws {Error} If there is no active layer, or both export paths fail.
 */
async function runLayerExport(doc) {
  return core.executeAsModal(
    async () => {
      const layer = doc.activeLayers && doc.activeLayers[0]
      if (!layer) {
        throw new Error('No active layer to export')
      }
      const layerName = layer.name
      // fill: TRANSPARENT is load-bearing, not cosmetic. createDocument's
      // default fill is an opaque white Background layer, under which
      // trim(TRANSPARENT) below finds no transparent pixels and silently
      // no-ops — the export would come back full-canvas-sized and matted
      // on white, indistinguishable from a whole-document send. mode: RGB
      // pins the export pipeline's expectation regardless of the source
      // document's own color mode (the duplicate converts on the way in).
      const destDoc = await app.createDocument({
        width: doc.width,
        height: doc.height,
        resolution: doc.resolution,
        mode: constants.NewDocumentMode.RGB,
        fill: constants.DocumentFill.TRANSPARENT
      })
      try {
        await layer.duplicate(destDoc)
        app.activeDocument = destDoc
        await destDoc.trim(constants.TrimType.TRANSPARENT, true, true, true, true)
        // flatten: false — Document.flatten() composites onto an opaque
        // Background, matting this lone layer's transparency onto white,
        // which defeats the point of sending "just that layer". Both export
        // paths preserve alpha on an un-flattened document (see
        // exportFlattenedDocument's own doc), so the PNG that reaches
        // ComfyUI keeps it — and Add-as-node's LoadImage derives its MASK
        // from exactly that alpha.
        const bytes = await exportFlattenedDocument(destDoc, { flatten: false })
        return { bytes, title: `${layerName} (layer)` }
      } finally {
        destDoc.closeWithoutSaving()
      }
    },
    { commandName: 'ComfyUI: export active layer' }
  )
}

/**
 * The "Send to ComfyUI" Plugins-menu command's handler (manifest.json's
 * `sendToComfyUI` entrypoint, wired in index.js). Never throws -- every
 * failure is logged, mirroring this plugin's existing "Send"/`sendBackNow`
 * command's identical silent-on-success, log-on-failure posture: the new
 * card appearing in the ComfyUI gallery IS the success confirmation, no
 * separate toast (this command context has no guaranteed panel to show one
 * in anyway).
 * @returns {Promise<void>}
 */
async function sendToComfyUI() {
  /** @type {import('photoshop').Document | null} */
  let doc
  try {
    doc = app.activeDocument
  } catch (_error) {
    doc = null
  }
  if (!doc) {
    logError('"Send to ComfyUI": no active document')
    return
  }

  let choice
  try {
    choice = await showSendPicker()
  } catch (error) {
    logError(`"Send to ComfyUI" picker failed: ${describeError(error)}`)
    return
  }
  if (!choice) {
    return // Dismissed -- not an error, the user just changed their mind.
  }

  let bytes
  let title
  try {
    if (choice === 'layer') {
      ;({ bytes, title } = await runLayerExport(doc))
    } else {
      bytes = await runExport(doc)
      title = `${doc.title} (whole document)`
    }
  } catch (error) {
    logError(`"Send to ComfyUI" (${choice}) export failed: ${describeError(error)}`)
    return
  }

  const handoffId = await pushManualSend(title, bytes)
  if (handoffId) {
    logInfo(`"Send to ComfyUI" (${choice}) delivered as handoff ${handoffId}`)
  }
  // pushManualSend already logs its own failure after exhausting retries --
  // nothing more to do here on a null result.
}

module.exports = { sendToComfyUI }
