/**
 * @file "Add as a layer" (owner ask 2026-07-24): pushes the preview panel's
 * current render into the ACTIVE document as a new raster layer, stretched to
 * the document bounds — the render IS a (downscaled) re-render of that
 * canvas, so fitting it edge-to-edge is the correct placement, and the tiny
 * aspect drift a JPEG round trip can introduce is invisible against a match.
 *
 * Mechanism — deliberately ALL typed DOM API, no batchPlay:
 *   1. Decode the render's base64 JPEG and write it to a temp file
 *      (exporter.js's own temp-folder pattern, reversed direction).
 *   2. `app.open(tempFile)` → a throwaway document whose one (Background)
 *      layer IS the render.
 *   3. `layer.duplicate(targetDoc)` — the exact machinery `manualSend.js`'s
 *      `runLayerExport` already proved in production, in the opposite
 *      direction (cross-document duplicate is pixel-exact; a Background
 *      source becomes a normal layer in the target).
 *   4. Close the temp doc, then `Layer.scale()`/`Layer.translate()` (both
 *      documented UXP Layer methods) fit the new layer to the document
 *      bounds. Scaling via layer methods sidesteps the placeEvent
 *      descriptor's DPI-dependent sizing entirely.
 *
 * Everything mutating runs inside ONE `core.executeAsModal` scope, per this
 * plugin's universal convention. Throws on failure — the preview panel's
 * button handler catches, logs, and surfaces it.
 */

const { app, core } = require('photoshop')
const uxp = require('uxp')
const { localFileSystem, formats } = uxp.storage
const { logInfo, describeError } = require('./log.js')
const { base64Decode } = require('./connection.js')

/**
 * Adds one render (base64 JPEG, the wire format of `result_frame`) to the
 * active document as a new top layer named "ComfyUI render", scaled to the
 * document bounds.
 * @param {string} jpegB64
 * @returns {Promise<void>}
 * @throws {Error} When no document is open, or any Photoshop step fails.
 */
async function addRenderAsLayer(jpegB64) {
  /** @type {import('photoshop').Document | null} */
  let targetDoc
  try {
    targetDoc = app.activeDocument
  } catch (_error) {
    targetDoc = null
  }
  if (!targetDoc) {
    throw new Error('No open document to add the layer to')
  }

  const bytes = base64Decode(jpegB64)
  const tempFolder = await localFileSystem.getTemporaryFolder()
  const tempFile = await tempFolder.createFile(`cpsb_render_${Date.now()}.jpg`, {
    overwrite: true
  })
  await tempFile.write(bytes.buffer, { format: formats.binary })

  await core.executeAsModal(
    async () => {
      const tempDoc = await app.open(tempFile)
      /** @type {any} */
      let newLayer
      try {
        newLayer = await tempDoc.layers[0].duplicate(targetDoc)
      } finally {
        await tempDoc.closeWithoutSaving()
      }
      app.activeDocument = targetDoc
      // duplicate() is documented to return the new Layer; belt-and-braces
      // for host variance: a cross-doc duplicate lands at the top of the
      // target's stack, so layers[0] is the same layer.
      if (!newLayer) newLayer = targetDoc.layers[0]
      newLayer.name = 'ComfyUI render'

      const bounds = newLayer.bounds
      const layerWidth = Number(bounds.right) - Number(bounds.left)
      const layerHeight = Number(bounds.bottom) - Number(bounds.top)
      if (layerWidth > 0 && layerHeight > 0) {
        await newLayer.scale(
          (Number(targetDoc.width) / layerWidth) * 100,
          (Number(targetDoc.height) / layerHeight) * 100
        )
        // Re-read: scale() anchors per its default, so align top-left to the
        // canvas origin explicitly.
        const scaled = newLayer.bounds
        await newLayer.translate(-Number(scaled.left), -Number(scaled.top))
      }
      logInfo(`render added to "${targetDoc.title}" as a layer`)
    },
    { commandName: 'ComfyUI: add render as layer' }
  )

  // Best-effort temp hygiene — the OS temp folder would reap it anyway.
  try {
    await tempFile.delete()
  } catch (error) {
    logInfo(`temp render file not deleted (${describeError(error)}) — OS temp cleanup will`)
  }
}

module.exports = { addRenderAsLayer }
