"""PSD read/write for the Tier 1 (file-handoff) round trip (PLAN.md §4).

Two entry points:

* :func:`write_psd` -- turns a PIL image into the flat, layer-less PSD that
  gets handed to Photoshop. A layer-less document is what lets a plain
  Cmd/Ctrl+S save back to the same path with no save-format dialog, whether
  or not the user adds layers.
* :func:`read_edited_psd` -- reads Photoshop's saved-over PSD back into
  8-bit RGB(A) pixels, preferring the embedded Maximize-Compatibility
  composite and falling back to psd-tools' own layer compositor.

A prior third entry point, ``extract_mask_channel`` (channel-based MASK
extraction from a document's extra alpha/spot channels), was removed
(PROTOCOL.md §4: "owner's call" -- field testing showed plain alpha-based
masking already covers the need). MASK outputs now derive solely from
``1 - alpha`` of the resolved image (:mod:`cpsb.nodes`); see git history and
research/research-annotate-node.md if this is ever revisited.

Every claim about psd-tools' behavior below (``has_preview()``/``topil()``
semantics, the CMYK channel-inversion quirk, 16-bit downsampling) was
verified against the installed ``psd-tools`` 1.17.4 by writing and reading
back real PSD files, not taken from memory or documentation alone.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

from PIL import Image
from psd_tools import PSDImage
from psd_tools.constants import Compression

logger = logging.getLogger("cpsb")

#: Fidelity values `psd_io` itself can produce. ("plugin" is assigned by
#: `cpsb.routes`' upload handler for Tier 2 and never touches this module.)
PsdFidelity = Literal["composite", "recomposite"]


def write_psd(path: Path, image: Image.Image, compression: Compression = Compression.RLE) -> None:
    """Write *image* as a flat PSD at *path* for Photoshop to open and re-save.

    Args:
        path: Destination ``source.psd`` path. Parent directories are
            created if needed.
        image: The source pixels (RGB or RGBA), typically converted
            straight from a ComfyUI ``IMAGE`` tensor.
        compression: PSD channel compression. RLE (the psd-tools default)
            is lossless and universally readable.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    psd = PSDImage.frompil(image, compression=compression)
    psd.save(path)


def read_edited_psd(path: Path) -> tuple[Image.Image, PsdFidelity]:
    """Read a Photoshop-saved PSD back into normalized 8-bit RGB(A) pixels.

    Tries the embedded Maximize-Compatibility composite first -- Photoshop's
    own pixel-accurate flattened render, read directly via ``topil()``
    without psd-tools re-interpreting any layer/effect data (fidelity
    ``"composite"``). If the document has no such composite (the user
    declined Maximize Compatibility on that save, or the file was never
    layered so no version-info resource was written), falls back to
    psd-tools' own layer compositor (fidelity ``"recomposite"`` -- PLAN.md
    §4 flags this path as lower fidelity: partial adjustment-layer and
    effect support).

    Args:
        path: Path to the saved ``source.psd``.

    Returns:
        A ``(image, fidelity)`` tuple. *image* is always 8-bit ``"RGB"`` or
        ``"RGBA"``.
    """
    psd = PSDImage.open(path)
    if psd.has_preview():
        image = psd.topil(apply_icc=True)
        if image is not None:
            return normalize_to_rgb8(image), "composite"
        logger.warning(
            "%s: has_preview() was true but topil() returned nothing; "
            "falling back to layer recompositing",
            path,
        )
    image = psd.composite(apply_icc=True)
    return normalize_to_rgb8(image), "recomposite"


def normalize_to_rgb8(image: Image.Image) -> Image.Image:
    """Normalize any PSD-derived PIL image to 8-bit ``RGB``/``RGBA`` (PLAN.md §4).

    psd-tools already downsamples 16-bit-per-channel data to 8 bits while
    building the composite image (verified: a 16-bit-depth RGB document
    round-trips through ``topil()`` as mode ``"RGB"`` with correctly scaled
    values), so only the color *mode* needs translating here:

    * ``RGB``/``RGBA`` pass through unchanged.
    * ``CMYK`` is inverted band-by-band before conversion. PSD stores CMYK
      channels Photoshop-inverted (byte = 255 * (1 - ink)); psd-tools passes
      that convention straight into a PIL ``"CMYK"`` image, but Pillow's own
      ``Image.convert("RGB")`` expects the opposite. Verified empirically:
      converting such an image directly turns a solid orange swatch
      near-black. ``Image.eval`` is used for the inversion because
      ``ImageOps.invert`` does not support ``"CMYK"``.
    * ``LA`` (grayscale + alpha) becomes ``RGBA`` so alpha survives, per
      PROTOCOL.md §4 ("alpha is preserved when present").
    * Everything else (``L``, ``1``, ``P``, ``LAB``, ...) uses Pillow's
      standard conversion to ``RGB``.
    """
    if image.mode in ("RGB", "RGBA"):
        return image
    if image.mode == "CMYK":
        return Image.eval(image, lambda value: 255 - value).convert("RGB")
    if image.mode == "LA":
        return image.convert("RGBA")
    return image.convert("RGB")
