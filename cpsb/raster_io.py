"""Non-PSD raster decoders for the Load PSD node's broadened file support.

Eric's ask ("I asked for file support beyond psd... especially dng, tiff and
ai") is answered here as a single dispatch point,
:func:`decode_to_rgb8`, so both :class:`cpsb.load_psd.PhotoshopLoadPSD`'s
IMAGE/MASK output and (in the future, once ``cpsb.routes`` -- out of this
module's scope -- is extended to recognize these extensions for its own
``GET /cpsb/psd_preview``) a server-side preview can share one decode path,
mirroring how both of those already share :func:`cpsb.psd_io.read_edited_psd`
for ``.psd``/``.psb``.

Per-format feasibility (assessed empirically, not from memory/docs alone --
same verification standard :mod:`cpsb.psd_io` holds itself to):

* **TIFF** (``.tif``/``.tiff``) -- Pillow already reads TIFF; no new
  dependency. Verified against real fixtures built with ``tifffile``
  (mirroring genuine multi-sample/bit-depth TIFF layouts a real image editor
  would write, not just ``numpy.asarray``-round-tripped ones): an 8-bit RGB
  TIFF and even a **16-bit-per-channel RGB** TIFF both open directly as
  Pillow mode ``"RGB"`` with correctly pre-scaled 8-bit values (libtiff does
  this downsampling itself on read) -- but a **16-bit grayscale** TIFF opens
  as mode ``"I;16"`` with the raw 0-65535 sample untouched, and Pillow's own
  ``.convert("RGB")`` on that mode does NOT scale correctly (empirically: a
  mid-gray 0.3*65535 sample converts to near-white ``(255, 255, 255)``
  instead of the correct ~76 -- the identical *shape* of bug the PSD CMYK
  postmortem describes, just a different transform). :func:`_decode_tiff`
  below scales that case through ``numpy`` itself
  (:func:`_scale_16bit_grayscale_to_l8`) before ever reaching
  :func:`cpsb.psd_io.normalize_to_rgb8`. CMYK TIFF (``PhotometricInterpretation
  = Separated``) was verified against a fixture built to the TIFF baseline
  spec's plain ink-direct convention (0 = no ink) -- Pillow opens it as mode
  ``"CMYK"`` with THOSE exact byte values (no read-side inversion the way
  ``psd-tools`` applies for PSD), and ``normalize_to_rgb8``'s existing,
  no-extra-inversion CMYK branch (built for the v0.5.28 PSD bug) produces the
  mathematically correct RGB on it unmodified. This is **not** verified
  against a genuine Photoshop-exported CMYK TIFF (no Photoshop available in
  this environment, and downloading a real-world sample file was out of
  bounds) -- if a report ever comes in that a real Photoshop CMYK TIFF loads
  with inverted colors the way the original PSD bug did, that is the first
  place to look; nothing here special-cases TIFF CMYK the way PSD needed to.
  Multi-page TIFF: Pillow already opens positioned at frame 0 by default
  (verified); :func:`_decode_tiff` also calls ``.seek(0)`` explicitly so
  that stays true regardless of Pillow-version behavior.

* **Illustrator** (``.ai``) -- modern ``.ai`` files embed a PDF stream.
  ``pypdfium2`` (a self-contained prebuilt wheel, verified installing cleanly
  with no system dependency on both the 3.10 and 3.14 test interpreters, and
  verified actually rendering a real PDF page to correct RGBA pixels) renders
  page 0. Implemented as an OPTIONAL, soft-imported dependency
  (:func:`pypdfium2_available`): the pack still imports and every existing
  behavior still works with it absent, and :func:`_decode_ai` raises a clear,
  actionable ``RuntimeError`` (never a raw ``ImportError``/stack trace) if a
  user points this node at an ``.ai`` file without it installed. Rendered
  with a transparent ``fill_color`` (verified: produces mode ``"RGBA"``) so
  Illustrator art with no background layer still yields real alpha for this
  package's ``1 - alpha`` MASK convention, rather than flattening onto an
  opaque white page.

* **DNG / camera raw** (``.dng`` plus other common camera raw extensions --
  ``rawpy``/LibRaw dispatch by file *content*, not extension, so the same
  decode path covers them for free) -- needs demosaicing. ``rawpy`` (LibRaw
  wheels) was verified installing cleanly with no system dependency on both
  test interpreters, AND verified actually demosaicing a real (synthesized --
  see ``tests/test_psd_io.py``) Bayer-pattern DNG into sane RGB pixels, not
  just importing. Implemented as an OPTIONAL, soft-imported dependency the
  identical way ``.ai`` is (:func:`rawpy_available`, :func:`_decode_raw`).

Both optional libraries are declared in ``requirements.txt`` as commented-out
"install by hand if you need this format" lines rather than unconditional
installs -- see that file's own comment block -- since this repo's
``pyproject.toml`` (an extras-group would normally live there) is out of this
change's scope.

:func:`decode_to_rgb8` returns a single normalized ``PIL.Image.Image`` (mode
``"RGB"`` or ``"RGBA"``) rather than a ``(rgb_image, alpha_or_none)`` pair:
every existing consumer of a decoded image in this codebase --
:func:`cpsb.psd_io.normalize_to_rgb8`'s own contract,
:func:`cpsb.nodes._tensors_from_image`'s MASK derivation (``"A" in
image.mode``), :func:`cpsb.psd_io.read_edited_psd`'s return shape -- already
encodes "does this carry alpha" as the PIL mode itself, not as a parallel
value. Splitting alpha out here would invent a second convention at the
exact seam where this module hands off to those existing ones, for no
benefit to either caller (:meth:`cpsb.load_psd.PhotoshopLoadPSD.execute` can
pass this function's return straight into
:func:`cpsb.nodes._tensors_from_image`, identically to how it already passes
:func:`cpsb.psd_io.read_edited_psd`'s image through today).
"""

from __future__ import annotations

import importlib
import logging
from pathlib import Path

import numpy as np
from PIL import Image

from .psd_io import normalize_to_rgb8

logger = logging.getLogger("cpsb")

#: Always available -- Pillow (a pre-existing hard dependency) reads TIFF
#: natively, no optional library involved.
TIFF_EXTENSIONS: tuple[str, ...] = (".tif", ".tiff")

#: Requires the optional ``pypdfium2`` package (:func:`pypdfium2_available`).
AI_EXTENSIONS: tuple[str, ...] = (".ai",)

#: Requires the optional ``rawpy`` package (:func:`rawpy_available`). ``.dng``
#: is the format Eric specifically asked about; the others are the handful
#: of other camera-raw extensions LibRaw recognizes by content (Canon,
#: Nikon, Sony, Olympus, Panasonic, Fujifilm) -- included because the
#: decode path is identical (:func:`_decode_raw` never branches on which of
#: these it was called for), not because each was individually fixture-
#: verified: only ``.dng`` was (see :mod:`tests.test_psd_io`'s synthesized
#: fixture; a genuine ``.cr2``/``.nef``/etc. file was not available to test
#: against in this environment).
RAW_EXTENSIONS: tuple[str, ...] = (
    ".dng",
    ".cr2",
    ".cr3",
    ".nef",
    ".arw",
    ".orf",
    ".rw2",
    ".raf",
)

#: Formats that can plausibly round-trip through PROTOCOL.md §6b's
#: ``edit_original`` option ("edit the user's own selected file in place"):
#: Photoshop must be able to both open AND save back to the format.
#: TIFF qualifies (a plain Cmd/Ctrl+S on a ``.tif`` re-writes the same
#: ``.tif``, exactly like the existing PSD case). AI/raw formats do not --
#: Photoshop cannot save a flattened raster back into a vector ``.ai``
#: container, and no camera-raw format is a valid Photoshop save target at
#: all, so "edit the original .dng in place" is not a meaningful operation.
#: NOT consumed anywhere in this package yet: ``edit_original`` is read
#: purely by the frontend (see ``cpsb.load_psd``'s own module docstring) and
#: ``POST /cpsb/open``'s handoff creation (``cpsb.routes``) only recognizes
#: ``.psd``/``.psb`` for ``origin_kind: "load_psd"`` at all today -- both
#: files are out of this change's scope. Exposed here as the single place
#: this policy is decided, so a future change extending
#: ``cpsb.routes``/``web/cpsb`` to open these formats in Photoshop doesn't
#: have to re-derive it: that change should gate ``edit_in_place`` on
#: membership in this tuple (today just TIFF), and otherwise always fall
#: back to copying the flattened image into a managed ``.psd``.
EDIT_IN_PLACE_CAPABLE_EXTENSIONS: tuple[str, ...] = TIFF_EXTENSIONS


def _optional_module(name: str):
    """Import *name* if installed, else ``None`` -- never raises.

    The soft-import primitive both optional decoders share (mirrors this
    repo's existing local-import-in-a-function convention for optional/
    heavy dependencies, e.g. :mod:`cpsb.nodes`' own ``torch``/``numpy``
    imports) so the pack itself always imports cleanly regardless of
    whether ``pypdfium2``/``rawpy`` are present.
    """
    try:
        return importlib.import_module(name)
    except ImportError:
        return None


def pypdfium2_available() -> bool:
    """Whether ``.ai`` decoding is currently possible on this interpreter."""
    return _optional_module("pypdfium2") is not None


def rawpy_available() -> bool:
    """Whether camera-raw (``.dng`` etc.) decoding is currently possible."""
    return _optional_module("rawpy") is not None


def available_extensions() -> tuple[str, ...]:
    """Extensions :func:`decode_to_rgb8` can decode RIGHT NOW.

    Always includes :data:`TIFF_EXTENSIONS` (no dependency to gate on); adds
    :data:`AI_EXTENSIONS`/:data:`RAW_EXTENSIONS` only when their optional
    library actually imports. :mod:`cpsb.load_psd` uses this to build its
    combo/``VALIDATE_INPUTS`` accept-list so a user is never offered a file
    type this interpreter can't actually decode.
    """
    extensions = list(TIFF_EXTENSIONS)
    if pypdfium2_available():
        extensions += AI_EXTENSIONS
    if rawpy_available():
        extensions += RAW_EXTENSIONS
    return tuple(extensions)


def decode_to_rgb8(path: Path) -> Image.Image:
    """Decode *path* to an 8-bit ``RGB``/``RGBA`` PIL image, by extension.

    Dispatches on ``path.suffix.lower()`` -- the same "trust the extension,
    don't sniff content" convention :mod:`cpsb.load_psd`/:mod:`cpsb.routes`
    already use for ``.psd``/``.psb``. Every branch ends by handing its
    decoded image through :func:`cpsb.psd_io.normalize_to_rgb8` so all
    formats heal through the identical, already-battle-tested mode
    normalization (including its CMYK fix) rather than duplicating it.

    Args:
        path: The source file. Must have one of :data:`TIFF_EXTENSIONS`,
            :data:`AI_EXTENSIONS`, or :data:`RAW_EXTENSIONS` as its suffix --
            callers are expected to have already checked
            :func:`available_extensions` (this function itself doesn't
            re-check availability beyond raising a clear error, see below).

    Returns:
        An 8-bit ``"RGB"`` or ``"RGBA"`` PIL image.

    Raises:
        ValueError: *path*'s extension isn't one this module recognizes at
            all (a caller bug -- should have been filtered out already).
        RuntimeError: the extension is recognized but its optional
            dependency isn't installed, naming the exact package to
            ``pip install`` (never a raw ``ImportError``/stack trace).
    """
    suffix = path.suffix.lower()
    if suffix in TIFF_EXTENSIONS:
        return _decode_tiff(path)
    if suffix in AI_EXTENSIONS:
        return _decode_ai(path)
    if suffix in RAW_EXTENSIONS:
        return _decode_raw(path)
    raise ValueError(f"{path}: unsupported file extension {suffix!r}")


def _decode_tiff(path: Path) -> Image.Image:
    """TIFF via Pillow (no optional dependency) -- see module docstring."""
    with Image.open(path) as source:
        source.seek(0)  # multi-page TIFF: always the first/flattened page
        source.load()
        image = source
        if image.mode.startswith("I;16"):
            image = _scale_16bit_grayscale_to_l8(image)
        return normalize_to_rgb8(image)


def _scale_16bit_grayscale_to_l8(image: Image.Image) -> Image.Image:
    """Correctly downsample a Pillow ``"I;16"``-mode image to 8-bit ``"L"``.

    Pillow's TIFF reader already downsamples multi-channel (RGB) >8-bit data
    to 8-bit on open (verified: a 16-bit RGB TIFF opens directly as mode
    ``"RGB"`` with correct values, the same as ``psd-tools`` does for a
    16-bit PSD -- see :func:`cpsb.psd_io.normalize_to_rgb8`'s own docstring
    for that precedent), but leaves single-channel 16-bit grayscale data as
    raw ``"I;16"`` samples (0-65535, verified empirically). A plain
    ``.convert("RGB")`` on that mode does NOT scale correctly -- see this
    module's own docstring for the measured near-white-instead-of-gray
    symptom -- so this scales through ``numpy`` (dividing by 257, the exact
    ratio ``65535 / 255``) before any mode conversion happens.
    """
    array = np.asarray(image).astype(np.float64)
    scaled = np.clip(np.round(array / 257.0), 0, 255).astype(np.uint8)
    return Image.fromarray(scaled, mode="L")


#: Render resolution for `.ai`'s embedded PDF page: the scale factor
#: pypdfium2's `PdfPage.render` multiplies PDF canvas units (1/72 inch) by,
#: so 2.0 == 144 DPI -- a reasonable fixed default given this node has no
#: user-facing DPI control (mirroring TIFF/DNG, neither of which expose one
#: either; the source pixels dictate resolution for those instead).
_AI_RENDER_SCALE = 2.0


def _decode_ai(path: Path) -> Image.Image:
    """``.ai`` via its embedded PDF stream, using the optional ``pypdfium2``.

    Renders page 0 with a transparent ``fill_color`` (verified: produces
    mode ``"RGBA"`` output) rather than pypdfium2's opaque-white default, so
    Illustrator artwork with no background layer keeps real transparency for
    this package's ``1 - alpha`` MASK convention instead of flattening onto
    white.
    """
    pdfium = _optional_module("pypdfium2")
    if pdfium is None:
        raise RuntimeError("Reading .ai needs the 'pypdfium2' package: pip install pypdfium2")
    document = pdfium.PdfDocument(str(path))
    try:
        if len(document) == 0:
            raise ValueError(f"{path}: no pages found in this .ai file's embedded PDF stream")
        page = document[0]
        bitmap = page.render(scale=_AI_RENDER_SCALE, fill_color=(255, 255, 255, 0))
        image = bitmap.to_pil()
    finally:
        document.close()
    return normalize_to_rgb8(image)


def _decode_raw(path: Path) -> Image.Image:
    """Camera raw (``.dng`` etc.) via the optional ``rawpy`` (LibRaw).

    Uses ``rawpy``'s own default demosaic/white-balance/brightness pipeline
    (:meth:`rawpy.RawPy.postprocess` with no overrides) -- the same
    reasonable, no-manual-tuning default a "just open the raw file" user
    would expect, verified against a synthesized Bayer-pattern DNG fixture
    (:mod:`tests.test_psd_io`) producing sane, non-degenerate RGB output.
    """
    rawpy = _optional_module("rawpy")
    if rawpy is None:
        raise RuntimeError(
            f"Reading {path.suffix.lower()} needs the 'rawpy' package: pip install rawpy"
        )
    with rawpy.imread(str(path)) as raw:
        rgb_array = raw.postprocess()
    image = Image.fromarray(rgb_array)
    return normalize_to_rgb8(image)
