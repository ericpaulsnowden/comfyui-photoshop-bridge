"""Non-PSD raster decoding for the Load PSD node's broadened file support.

Today this means **TIFF only** -- the one non-PSD format this pack decodes
in-process, because Pillow (already a hard dependency) reads it with no extra
library. :func:`decode_to_rgb8` is the single dispatch point so both
:class:`cpsb.load_psd.PhotoshopLoadPSD`'s IMAGE/MASK output and a future
server-side preview can share one decode path, mirroring how both already
share :func:`cpsb.psd_io.read_edited_psd` for ``.psd``/``.psb``.

**Illustrator ``.ai`` and camera-raw/``.dng`` are deliberately NOT decoded
here.** They used to be, via the optional ``pypdfium2``/``rawpy`` packages, but
that pulled third-party decoders into the pack -- against this project's ethos
(``bridge-design-ethos.md``: "no reliance on third-party libraries to do
this"). Photoshop itself opens both natively (``.ai`` through its PDF engine,
``.dng`` through Camera Raw), so those formats move to a dedicated Tier-2
"Open via Photoshop" node (see ``docs/roadmap/ps-external-decode.md``) that
routes the file through the connected plugin instead of decoding it in-process.
Load PSD stays a ComfyUI-only loader: ``.psd``/``.psb`` plus TIFF, all readable
with no plugin and no optional dependency.

TIFF feasibility (assessed empirically, not from docs alone -- the same
verification standard :mod:`cpsb.psd_io` holds itself to):

* Pillow reads TIFF natively; no new dependency. Verified against real fixtures
  built with ``tifffile`` (mirroring genuine multi-sample/bit-depth TIFF
  layouts a real image editor would write, not just ``numpy.asarray``-round-
  tripped ones): an 8-bit RGB TIFF and even a **16-bit-per-channel RGB** TIFF
  both open directly as Pillow mode ``"RGB"`` with correctly pre-scaled 8-bit
  values (libtiff does this downsampling itself on read) -- but a **16-bit
  grayscale** TIFF opens as mode ``"I;16"`` with the raw 0-65535 sample
  untouched, and Pillow's own ``.convert("RGB")`` on that mode does NOT scale
  correctly (empirically: a mid-gray 0.3*65535 sample converts to near-white
  ``(255, 255, 255)`` instead of the correct ~76 -- the identical *shape* of
  bug the PSD CMYK postmortem describes, just a different transform).
  :func:`_decode_tiff` below scales that case through ``numpy`` itself
  (:func:`_scale_16bit_grayscale_to_l8`) before ever reaching
  :func:`cpsb.psd_io.normalize_to_rgb8`. CMYK TIFF (``PhotometricInterpretation
  = Separated``) was verified against a fixture built to the TIFF baseline
  spec's plain ink-direct convention (0 = no ink) -- Pillow opens it as mode
  ``"CMYK"`` with THOSE exact byte values (no read-side inversion the way
  ``psd-tools`` applies for PSD), and ``normalize_to_rgb8``'s existing,
  no-extra-inversion CMYK branch (built for the v0.5.28 PSD bug) produces the
  mathematically correct RGB on it unmodified. This is **not** verified against
  a genuine Photoshop-exported CMYK TIFF (no Photoshop available in this
  environment) -- if a report ever comes in that a real Photoshop CMYK TIFF
  loads with inverted colors the way the original PSD bug did, that is the
  first place to look; nothing here special-cases TIFF CMYK the way PSD needed
  to. Multi-page TIFF: Pillow already opens positioned at frame 0 by default
  (verified); :func:`_decode_tiff` also calls ``.seek(0)`` explicitly so that
  stays true regardless of Pillow-version behavior.

:func:`decode_to_rgb8` returns a single normalized ``PIL.Image.Image`` (mode
``"RGB"`` or ``"RGBA"``) rather than a ``(rgb_image, alpha_or_none)`` pair:
every existing consumer of a decoded image in this codebase --
:func:`cpsb.psd_io.normalize_to_rgb8`'s own contract,
:func:`cpsb.nodes._tensors_from_image`'s MASK derivation (``"A" in
image.mode``), :func:`cpsb.psd_io.read_edited_psd`'s return shape -- already
encodes "does this carry alpha" as the PIL mode itself, not as a parallel
value, so this hands off cleanly to those existing ones.
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
from PIL import Image

from .psd_io import normalize_to_rgb8

logger = logging.getLogger("cpsb")

#: The only non-PSD extensions :func:`decode_to_rgb8` decodes -- Pillow (a
#: pre-existing hard dependency) reads TIFF natively, no optional library
#: involved.
TIFF_EXTENSIONS: tuple[str, ...] = (".tif", ".tiff")

#: Formats that can plausibly round-trip through PROTOCOL.md §6b's
#: ``edit_original`` option ("edit the user's own selected file in place"):
#: Photoshop must be able to both open AND save back to the format. TIFF
#: qualifies (a plain Cmd/Ctrl+S on a ``.tif`` re-writes the same ``.tif``,
#: exactly like the existing PSD case). NOT consumed anywhere in this package
#: yet: ``edit_original`` is read purely by the frontend and ``POST
#: /cpsb/open``'s handoff creation (``cpsb.routes``), which only recognizes
#: ``.psd``/``.psb`` for ``origin_kind: "load_psd"`` today. Exposed here as the
#: single place this policy is decided, so a future change extending
#: ``cpsb.routes``/``web/cpsb`` to open TIFF in Photoshop doesn't have to
#: re-derive it.
EDIT_IN_PLACE_CAPABLE_EXTENSIONS: tuple[str, ...] = TIFF_EXTENSIONS


def available_extensions() -> tuple[str, ...]:
    """Extensions :func:`decode_to_rgb8` can decode.

    Just :data:`TIFF_EXTENSIONS` -- there is no optional dependency to gate on
    anymore (``.ai``/raw moved to the Tier-2 "Open via Photoshop" node, see the
    module docstring). Kept as a function rather than inlining the constant so
    :mod:`cpsb.load_psd`'s combo/``VALIDATE_INPUTS`` call site stays stable if a
    future no-dependency format is ever added the same way TIFF was.
    """
    return TIFF_EXTENSIONS


def decode_to_rgb8(path: Path) -> Image.Image:
    """Decode *path* to an 8-bit ``RGB``/``RGBA`` PIL image, by extension.

    Dispatches on ``path.suffix.lower()`` -- the same "trust the extension,
    don't sniff content" convention :mod:`cpsb.load_psd`/:mod:`cpsb.routes`
    already use for ``.psd``/``.psb``. The one branch ends by handing its
    decoded image through :func:`cpsb.psd_io.normalize_to_rgb8` so it heals
    through the identical, already-battle-tested mode normalization (including
    its CMYK fix) rather than duplicating it.

    Args:
        path: The source file. Must have one of :data:`TIFF_EXTENSIONS` as its
            suffix -- callers are expected to have already checked
            :func:`available_extensions`.

    Returns:
        An 8-bit ``"RGB"`` or ``"RGBA"`` PIL image.

    Raises:
        ValueError: *path*'s extension isn't one this module decodes (a caller
            bug -- should have been filtered out already).
    """
    suffix = path.suffix.lower()
    if suffix in TIFF_EXTENSIONS:
        return _decode_tiff(path)
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
