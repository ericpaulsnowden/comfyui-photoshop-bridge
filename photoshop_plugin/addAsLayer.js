/**
 * @file "Add as a layer" (owner ask 2026-07-24) + refine-pass layer placement
 * (roadmap R1): pushes an image into the ACTIVE document as a new raster
 * layer, stretched to the document bounds — a render/refine result IS a
 * re-render of that canvas, so fitting it edge-to-edge is the correct
 * placement, and the tiny aspect drift a lossy round trip can introduce is
 * invisible against a match.
 *
 * Mechanism — deliberately ALL typed DOM API, no batchPlay:
 *   1. Write the image bytes to a temp file (exporter.js's own temp-folder
 *      pattern, reversed direction).
 *   2. `app.open(tempFile)` → a throwaway document whose one (Background)
 *      layer IS the image.
 *   3. `layer.duplicate(targetDoc)` — the exact machinery `manualSend.js`'s
 *      `runLayerExport` already proved in production, in the opposite
 *      direction (cross-document duplicate is pixel-exact; a Background
 *      source becomes a normal layer in the target).
 *   4. Close the temp doc, then `Layer.scale()`/`Layer.translate()` (both
 *      documented UXP Layer methods) fit the new layer to the document
 *      bounds. Scaling via layer methods sidesteps the placeEvent
 *      descriptor's DPI-dependent sizing entirely. This is also how the
 *      ≤4096px refine ceiling meets Eric's "PSD size ideally" ruling: pixels
 *      arrive capped, the LAYER still lands full-canvas.
 *
 * REPLACE mode (refine pass; main-panel Stack/Replace control → prefs.js):
 * before placing, delete every existing top-level layer with the SAME name —
 * so iterative refines keep exactly one "ComfyUI refined" layer. Stack mode
 * never deletes anything.
 *
 * Everything mutating runs inside ONE `core.executeAsModal` scope, per this
 * plugin's universal convention. Throws on failure — callers catch, log, and
 * surface it.
 */

const { app, core } = require('photoshop')
const uxp = require('uxp')
const { localFileSystem, formats } = uxp.storage
const { logInfo, describeError } = require('./log.js')
const { base64Decode } = require('./connection.js')

/**
 * Adds one image to the active document as a new top layer scaled to the
 * document bounds.
 * @param {Uint8Array} bytes - The image file bytes (PNG or JPEG).
 * @param {'png' | 'jpg'} extension - Temp-file extension matching *bytes*.
 * @param {{layerName?: string, mode?: 'stack' | 'replace'}} [options]
 * @returns {Promise<void>}
 * @throws {Error} When no document is open, or any Photoshop step fails.
 */
async function addImageAsLayer(bytes, extension, options = {}) {
  const layerName = options.layerName || 'ComfyUI render'
  const mode = options.mode === 'replace' ? 'replace' : 'stack'

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

  const tempFolder = await localFileSystem.getTemporaryFolder()
  const tempFile = await tempFolder.createFile(`cpsb_render_${Date.now()}.${extension}`, {
    overwrite: true
  })
  await tempFile.write(bytes.buffer, { format: formats.binary })

  await core.executeAsModal(
    async () => {
      // REPLACE: retire the previous refine(s) of the same name first, so the
      // document keeps exactly one such layer across iterative refines. Only
      // exact name matches, only top-level layers — never anything inside the
      // user's own groups.
      if (mode === 'replace') {
        const stale = []
        for (let i = 0; i < targetDoc.layers.length; i++) {
          if (targetDoc.layers[i].name === layerName) stale.push(targetDoc.layers[i])
        }
        for (const layer of stale) {
          await layer.delete()
        }
      }

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
      newLayer.name = layerName

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
      logInfo(`image added to "${targetDoc.title}" as layer "${layerName}" (${mode})`)
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

/**
 * Adds one render (base64 JPEG, the wire format of `result_frame`) to the
 * active document as a new top layer named "ComfyUI render" — the preview
 * panel button's LOCAL fallback path when the server's full-quality render
 * can't be fetched.
 * @param {string} jpegB64
 * @returns {Promise<void>}
 */
async function addRenderAsLayer(jpegB64) {
  await addImageAsLayer(base64Decode(jpegB64), 'jpg', { layerName: 'ComfyUI render' })
}

module.exports = { addImageAsLayer, addRenderAsLayer }
