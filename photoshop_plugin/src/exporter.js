/**
 * @file The Tier 2 export pipeline (PLAN.md §5; docs/PROTOCOL.md §1 —
 * `fidelity: "plugin"`): given an open, saved Photoshop `Document`, produces
 * flattened PNG bytes with no dialogs and no visible mutation of the user's
 * document. Every Photoshop DOM/batchPlay call in this file runs inside
 * `core.executeAsModal` — Adobe's own reference is explicit that this is
 * mandatory for anything that creates/modifies documents, and this pipeline
 * duplicates, flattens, exports, and closes a document on every call.
 *
 * Two paths, tried in order:
 * 1. Primary — duplicate + flatten + a `batchPlay` "save as PNG" descriptor
 *    with `dialogOptions: "dontDisplay"`, writing to a temp file this
 *    plugin then reads and deletes. Research flagged this as historically
 *    fiddly (undocumented descriptor shape) — see the `VERIFY(spike-6)`
 *    comment below for exactly how the descriptor here was sourced.
 * 2. Fallback — `imaging.getPixels()` on the same flattened duplicate,
 *    hand-encoded to PNG (see the encoder section below for why encoding is
 *    hand-rolled at all). Used whenever the primary path fails for any
 *    reason.
 */

const { logWarn, describeError } = require('./log.js')

const { core, action, imaging } = require('photoshop')
const uxp = require('uxp')
const { localFileSystem, formats } = uxp.storage

/**
 * Exports a flattened PNG of `doc`'s current saved state without mutating
 * the visible document: all work happens on a throwaway duplicate, which is
 * always closed (without saving) before this function returns, whether the
 * export succeeded or not.
 * @param {import('photoshop').Document} doc
 * @returns {Promise<Uint8Array>} PNG-encoded bytes.
 * @throws {Error} If both the primary and fallback export paths fail.
 */
async function runExport(doc) {
  return core.executeAsModal(
    async () => {
      const duplicate = await doc.duplicate()
      try {
        await duplicate.flatten()
        try {
          return await exportViaBatchPlay(duplicate)
        } catch (batchPlayError) {
          logWarn(
            `batchPlay PNG export failed, falling back to the Imaging API: ` +
              describeError(batchPlayError)
          )
          return await exportViaImaging(duplicate)
        }
      } finally {
        // closeWithoutSaving() is documented as returning void (not a
        // Promise) — nothing to await.
        duplicate.closeWithoutSaving()
      }
    },
    { commandName: 'ComfyUI: export edit' }
  )
}

/**
 * Primary export path: a `batchPlay` "save as PNG" descriptor targeting a
 * temp file in this plugin's own sandbox, with no dialog.
 *
 * `// VERIFY(spike-6):` the descriptor below is assembled from two
 * independently-confirmed but non-identical sources, not one authoritative
 * example — Adobe's own reference docs describe `batchPlay`'s general shape
 * and `_options.dialogOptions` but publish no save/export descriptor at all.
 * Source (a): an Adobe Community thread ("Export content credentials with
 * UXP") stating the DOM-API-equivalent of `activeDocument.saveAs.png()` is
 * `{_obj: "save", as: {_obj: "PNGFormat"}}`. Source (b): a Creative Cloud
 * Developer Forums thread ("Using BatchPlay to save a PNG in API 2")
 * demonstrating a full working descriptor with `in: {_path, _kind: "local"}`
 * built from `localFileSystem.createSessionToken()`, plus `documentID`,
 * `copy`, `lowerCase`, and `saveStage` fields — but built around the
 * `"fnord SuperPNG"` third-party plugin format, which cannot be used here
 * since it depends on an optional Photoshop plugin most users won't have
 * installed. This function keeps (b)'s `in`/`documentID`/`copy`/`lowerCase`
 * fields but swaps in the native `"PNGFormat"` class from (a), and drops
 * (b)'s `saveStage` field — `saveStage` is a field Photoshop emits in save
 * EVENT notification descriptors, not a documented parameter of the `save`
 * command, so it was almost certainly inert cargo in the forum example. If
 * this lean descriptor fails in spike 6, re-adding
 * `saveStage: {_enum: "saveStageType", _value: "saveBegin"}` is the first
 * variant to test. docs/SPIKES.md spike 6 is the one that runs this against
 * a real Photoshop install across a size/mode matrix and either confirms
 * this shape or replaces it — resolve this comment there (using the Actions
 * panel's "Copy As JavaScript" against a real "Save As PNG", per
 * batchplay.md's own recommended discovery method), not by guessing again
 * from more forum threads.
 *
 * @param {import('photoshop').Document} duplicate - The flattened duplicate.
 * @returns {Promise<Uint8Array>}
 */
async function exportViaBatchPlay(duplicate) {
  const tempFolder = await localFileSystem.getTemporaryFolder()
  const tempFile = await tempFolder.createFile(`cpsb_export_${duplicate.id}_${Date.now()}.png`, {
    overwrite: true
  })
  try {
    const token = localFileSystem.createSessionToken(tempFile)
    const result = await action.batchPlay(
      [
        {
          _obj: 'save',
          as: { _obj: 'PNGFormat' },
          in: { _path: token, _kind: 'local' },
          documentID: duplicate.id,
          copy: true,
          lowerCase: true,
          _options: { dialogOptions: 'dontDisplay' }
        }
      ],
      {}
    )
    // batchPlay resolves successfully even for a failed command — a failure
    // shows up as an `{_obj: "error", ...}` entry in the result list rather
    // than a rejection (per Adobe's batchPlay reference, "Result value").
    const firstResult = result && result[0]
    if (firstResult && firstResult._obj === 'error') {
      throw new Error(firstResult.message || 'batchPlay save-as-PNG returned an error result')
    }
    const contents = await tempFile.read({ format: formats.binary })
    return new Uint8Array(/** @type {ArrayBuffer} */ (contents))
  } finally {
    await tempFile.delete()
  }
}

/**
 * Fallback export path: reads raw pixels via the Imaging API and encodes
 * them to PNG by hand. Only reached if {@link exportViaBatchPlay} throws.
 *
 * Deliberately requests 8-bit components (ignoring the duplicate's native
 * bit depth) to keep the encoder below simple — acceptable for a fallback
 * path whose whole job is "produce *a* correct PNG," not preserve HDR
 * precision. `applyAlpha` is left unset, so an alpha channel (if present)
 * is preserved rather than matted onto white.
 *
 * Known, accepted limitation (research-photoshop.md; Adobe issue #501): the
 * Imaging API's alpha-channel handling is documented as broken specifically
 * for Grayscale/LAB pixel sources. This plugin only ever calls this path
 * against a duplicate that originated from a ComfyUI-authored document,
 * which PLAN.md §4 guarantees is RGB by the time it reaches Photoshop
 * (non-RGB sources are converted to RGB8 server-side beforehand) — so this
 * bug's precondition should not occur here in practice. Documented as a
 * known edge case rather than worked around, per PLAN.md §5.
 *
 * @param {import('photoshop').Document} duplicate - The flattened duplicate.
 * @returns {Promise<Uint8Array>}
 */
async function exportViaImaging(duplicate) {
  const { imageData } = await imaging.getPixels({ documentID: duplicate.id, componentSize: 8 })
  try {
    const pixels = await imageData.getData()
    return encodePNG(/** @type {Uint8Array} */ (pixels), imageData.width, imageData.height, imageData.components)
  } finally {
    imageData.dispose()
  }
}

// --- Minimal PNG encoder -------------------------------------------------
//
// `imaging.encodeImageData()` — the Imaging API's own encoder — only
// produces JPEG: Adobe's doc for it says plainly "you must use jpeg/base64
// encoding" and its only example builds a `data:image/jpeg;base64,` URL.
// Since docs/PROTOCOL.md requires PNG bytes for every edit, the fallback
// path encodes PNG itself below. It writes uncompressed ("stored") DEFLATE
// blocks rather than performing real compression — this produces
// larger-than-optimal but fully valid, spec-compliant PNG files, an
// acceptable trade-off for a rarely-used fallback path that avoids pulling
// in a compression library for one edge case.

const PNG_SIGNATURE = new Uint8Array([137, 80, 78, 71, 13, 10, 26, 10])

/** @type {Uint32Array | null} */
let crcTable = null

/** @returns {Uint32Array} */
function getCrcTable() {
  if (crcTable) return crcTable
  crcTable = new Uint32Array(256)
  for (let n = 0; n < 256; n++) {
    let c = n
    for (let k = 0; k < 8; k++) {
      c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1
    }
    crcTable[n] = c >>> 0
  }
  return crcTable
}

/**
 * Standard PNG/zlib CRC-32 (polynomial 0xEDB88320), as required for every
 * PNG chunk's trailing checksum.
 * @param {Uint8Array} bytes
 * @returns {number}
 */
function crc32(bytes) {
  const table = getCrcTable()
  let crc = 0xffffffff
  for (let i = 0; i < bytes.length; i++) {
    crc = table[(crc ^ bytes[i]) & 0xff] ^ (crc >>> 8)
  }
  return (crc ^ 0xffffffff) >>> 0
}

/**
 * Adler-32, as required for the zlib stream trailer wrapping PNG `IDAT` data.
 * @param {Uint8Array} bytes
 * @returns {number}
 */
function adler32(bytes) {
  const MOD_ADLER = 65521
  let a = 1
  let b = 0
  for (let i = 0; i < bytes.length; i++) {
    a = (a + bytes[i]) % MOD_ADLER
    b = (b + a) % MOD_ADLER
  }
  return ((b << 16) | a) >>> 0
}

/**
 * Wraps `data` in a minimal zlib stream: a 2-byte header, one or more
 * "stored" (uncompressed) DEFLATE blocks capped at 65535 bytes each, and a
 * 4-byte Adler-32 trailer. A valid zlib stream is exactly what a PNG `IDAT`
 * chunk must contain.
 * @param {Uint8Array} data
 * @returns {Uint8Array}
 */
function zlibStore(data) {
  const MAX_STORED_BLOCK = 65535
  const blockCount = Math.max(1, Math.ceil(data.length / MAX_STORED_BLOCK))
  const out = new Uint8Array(2 + blockCount * 5 + data.length + 4)
  let pos = 0
  // zlib header CMF=0x78 (deflate, 32K window), FLG=0x01 — chosen so that
  // (CMF * 256 + FLG) is a multiple of 31 as the zlib spec requires, with no
  // preset dictionary.
  out[pos++] = 0x78
  out[pos++] = 0x01
  let offset = 0
  for (let i = 0; i < blockCount; i++) {
    const remaining = data.length - offset
    const blockLen = Math.min(MAX_STORED_BLOCK, remaining)
    const isFinal = i === blockCount - 1
    out[pos++] = isFinal ? 1 : 0
    out[pos++] = blockLen & 0xff
    out[pos++] = (blockLen >>> 8) & 0xff
    const nlen = ~blockLen & 0xffff
    out[pos++] = nlen & 0xff
    out[pos++] = (nlen >>> 8) & 0xff
    out.set(data.subarray(offset, offset + blockLen), pos)
    pos += blockLen
    offset += blockLen
  }
  const adler = adler32(data)
  out[pos++] = (adler >>> 24) & 0xff
  out[pos++] = (adler >>> 16) & 0xff
  out[pos++] = (adler >>> 8) & 0xff
  out[pos++] = adler & 0xff
  return out
}

/**
 * @param {string} type - Exactly 4 ASCII characters (e.g. `"IHDR"`).
 * @param {Uint8Array} data
 * @returns {Uint8Array} The complete chunk: length + type + data + CRC32.
 */
function pngChunk(type, data) {
  const out = new Uint8Array(12 + data.length)
  const view = new DataView(out.buffer)
  view.setUint32(0, data.length, false)
  for (let i = 0; i < 4; i++) out[4 + i] = type.charCodeAt(i)
  out.set(data, 8)
  const crcInput = out.subarray(4, 8 + data.length)
  view.setUint32(8 + data.length, crc32(crcInput), false)
  return out
}

/**
 * @param {Uint8Array} pixels - Chunky pixel data (the Imaging API's
 * default), one byte per component, `width * height * components` bytes.
 * @param {number} width
 * @param {number} height
 * @param {number} components - 1 (gray), 2 (gray+alpha), 3 (RGB), or 4 (RGBA).
 * @returns {Uint8Array} A complete, valid PNG file.
 */
function encodePNG(pixels, width, height, components) {
  /** @type {Record<number, number>} */
  const colorTypeByComponents = { 1: 0, 2: 4, 3: 2, 4: 6 }
  const colorType = colorTypeByComponents[components]
  if (colorType === undefined) {
    throw new Error(`encodePNG: unsupported component count ${components}`)
  }

  const rowBytes = width * components
  const raw = new Uint8Array((rowBytes + 1) * height)
  for (let y = 0; y < height; y++) {
    const srcOffset = y * rowBytes
    const dstOffset = y * (rowBytes + 1)
    raw[dstOffset] = 0 // filter type 0 ("None") for every scanline
    raw.set(pixels.subarray(srcOffset, srcOffset + rowBytes), dstOffset + 1)
  }

  const ihdr = new Uint8Array(13)
  const ihdrView = new DataView(ihdr.buffer)
  ihdrView.setUint32(0, width, false)
  ihdrView.setUint32(4, height, false)
  ihdr[8] = 8 // bit depth
  ihdr[9] = colorType
  ihdr[10] = 0 // compression method — only value the PNG spec defines
  ihdr[11] = 0 // filter method — only value the PNG spec defines
  ihdr[12] = 0 // interlace method: none

  const chunks = [
    PNG_SIGNATURE,
    pngChunk('IHDR', ihdr),
    pngChunk('IDAT', zlibStore(raw)),
    pngChunk('IEND', new Uint8Array(0))
  ]
  const total = chunks.reduce((sum, chunk) => sum + chunk.length, 0)
  const out = new Uint8Array(total)
  let pos = 0
  for (const chunk of chunks) {
    out.set(chunk, pos)
    pos += chunk.length
  }
  return out
}

module.exports = { runExport }
