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

CMYK note (bug postmortem, verified against ``psd_tools`` 1.17.4 SOURCE, not
just its behavior -- see :func:`normalize_to_rgb8`): a naive round trip built
from ``PSDImage.frompil()``/``PSDImage.new()`` does *not* reproduce a real
Photoshop CMYK file's on-disk byte convention, because those constructors
write raw PIL channel bytes with no inversion of their own
(``psd_tools.api.psd_image.PSDImage.frompil``: ``channel.tobytes()``, no
CMYK special-casing) while psd-tools' *read* side (``psd_tools.api.pil_io
.post_process``) unconditionally un-inverts any ``"CMYK"``-mode image it
returns. A frompil-built fixture therefore gets "corrected" once by that
unconditional read-side invert starting from data that never needed
correcting -- any *second* invert applied downstream then cancels that
spurious flip and looks right by accident. Building a fixture that actually
matches what Photoshop writes requires manually pre-inverting the CMYK bytes
(``Image.eval(cmyk_image, lambda v: 255 - v)``) *before* handing them to
``frompil()``/``new()``, so the on-disk bytes end up ``255 * (1 - ink)`` the
way real Photoshop saves them; tests in ``tests/test_psd_io.py`` build
fixtures this way for exactly this reason.
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
    * ``CMYK`` converts straight through Pillow's own ``Image.convert("RGB")``
      -- deliberately with NO extra inversion here. PSD stores CMYK channels
      Photoshop-inverted on disk (byte = 255 * (1 - ink)), but psd-tools
      1.17.4 already undoes that itself before handing back a PIL image:
      ``psd_tools.api.pil_io.post_process()`` runs
      ``ImageChops.invert(image)`` on every ``"CMYK"``-mode result,
      unconditionally, for BOTH of :func:`read_edited_psd`'s callers
      (``PSDImage.topil()`` calls it directly; ``PSDImage.composite()``'s
      layer-compositor fallback (``composite_pil``) calls the identical
      ``post_process`` at its own tail end) -- confirmed by reading
      ``psd_tools/api/pil_io.py`` and ``psd_tools/composite/composite.py``
      source directly, not inferred from behavior alone. So by the time an
      image reaches this function, its CMYK bytes are already in Pillow's
      own standard convention (byte = ink amount directly) -- exactly what
      ``Image.convert("RGB")`` expects. An earlier version of this function
      inverted CMYK data AGAIN here on top of psd-tools' own fix, which is
      what actually caused the reported "CMYK PSD loads as solid black" bug:
      double-inverting a red pixel that arrived correctly as
      ``(0, 255, 255, 0)`` turns it into ``(255, 0, 0, 255)`` -- full ink on
      every channel -- which converts to black. Verified empirically against
      a fixture whose on-disk bytes were manually pre-inverted to match
      Photoshop's real convention (see this module's docstring): the old
      double-invert code produced mean brightness 0.0 for a solid-red 64x64
      fixture; removing the extra invert produces the correct ``(255, 0, 0)``.
      This is still a NAIVE, non-color-managed conversion when no ICC
      profile is embedded (or ``ImageCms``/little-cms isn't available) --
      Pillow's built-in CMYK->RGB math, not a proper ICC transform -- so
      results are preview-grade, not colorimetrically accurate; when a CMYK
      document DOES carry an embedded ICC profile, psd-tools applies a real
      ICC transform itself (also inside ``post_process``, via
      ``apply_icc=True`` on both ``read_edited_psd`` call sites) and hands
      back an already-RGB image, bypassing this branch entirely. Also note:
      psd-tools' ``post_process`` only calls ``image.putalpha(alpha)`` for
      ``"RGB"``/``"L"`` mode, never for ``"CMYK"`` -- a CMYK document's own
      alpha/transparency channel is dropped by psd-tools itself before this
      function ever sees the image, a pre-existing psd-tools limitation
      outside this function's control.
    * ``LA`` (grayscale + alpha) becomes ``RGBA`` so alpha survives, per
      PROTOCOL.md §4 ("alpha is preserved when present").
    * Everything else (``L``, ``1``, ``P``, ``LAB``, ...) uses Pillow's
      standard conversion to ``RGB``. Grayscale (``"L"``) needs no special
      handling: PSD/psd-tools has no analogous inversion quirk for
      grayscale, verified empirically against a genuine grayscale PSD
      fixture -- ``.convert("RGB")`` alone round-trips it correctly.
    """
    if image.mode in ("RGB", "RGBA"):
        return image
    if image.mode == "LA":
        return image.convert("RGBA")
    return image.convert("RGB")
