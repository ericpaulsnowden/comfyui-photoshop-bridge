"""PSD read/write for the Tier 1 (file-handoff) round trip (PLAN.md §4).

Three entry points:

* :func:`write_psd` -- turns a PIL image into the flat, layer-less PSD that
  gets handed to Photoshop. A layer-less document is what lets a plain
  Cmd/Ctrl+S save back to the same path with no save-format dialog, whether
  or not the user adds layers.
* :func:`read_edited_psd` -- reads Photoshop's saved-over PSD back into
  8-bit RGB(A) pixels, preferring the embedded Maximize-Compatibility
  composite and falling back to psd-tools' own layer compositor.
* :func:`extract_mask_channel` -- reads the same saved PSD's extra alpha/
  spot channels (PROTOCOL.md §4) and returns whichever one is the mask, if
  any.

Every claim about psd-tools' behavior below (``has_preview()``/``topil()``
semantics, the CMYK channel-inversion quirk, 16-bit downsampling, the extra-
channel/``ALPHA_NAMES_UNICODE`` behavior :func:`extract_mask_channel` relies
on) was verified against the installed ``psd-tools`` 1.17.4 by writing and
reading back real PSD files, not taken from memory or documentation alone.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

from PIL import Image
from psd_tools import PSDImage
from psd_tools.constants import Compression, Resource

try:
    # Not documented as public API (psd_tools.api.utils, no leading
    # underscore but not re-exported from the psd_tools top-level package)
    # -- the only viable way, in the installed 1.17.4, to tell a document's
    # plain composite-transparency channel apart from a genuine extra one
    # (see extract_mask_channel's docstring). Guarded so an older installed
    # psd-tools version that lacks this internal module degrades
    # extract_mask_channel to "always None" instead of breaking this
    # entire module's importability -- write_psd/read_edited_psd (the core
    # Tier 1 round trip) must keep working regardless.
    from psd_tools.api.utils import get_transparency_index, has_transparency
except ImportError:  # pragma: no cover - depends on the installed psd-tools version
    get_transparency_index = None
    has_transparency = None

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


def extract_mask_channel(psd_path: Path, preferred_name: str) -> Image.Image | None:
    """Extract a saved PSD's mask channel, if it has one (PROTOCOL.md §4, the authority).

    Inspects the document's channels beyond the core color channels and the
    built-in composite-transparency channel (an ordinary RGBA image's own
    alpha -- not a mask candidate on its own):

    * No extra channels -> ``None``.
    * Exactly one extra channel -> it *is* the mask, regardless of its name.
    * More than one -> the one named *preferred_name* (case-insensitive)
      wins; no match -> ``None``.

    Every claim below was verified against the installed ``psd-tools``
    1.17.4 by round-tripping hand-built low-level ``psd_tools.psd`` records
    (header + ``ImageData`` + ``ImageResources``) through a real file
    save/open and inspecting ``PSDImage.channels``, ``image_resources``, and
    ``topil(channel=N)`` directly -- not taken from memory or documentation,
    and no higher-level "list channel names" API exists in this version:

    * A document's non-color channels are enumerated, in trailing-channel
      order, by the ``ALPHA_NAMES_UNICODE`` image resource (falling back to
      the legacy ``ALPHA_NAMES_PASCAL`` Pascal-string form for older files
      that only wrote that one) -- e.g. for an ``N``-channel document whose
      ``ALPHA_NAMES_UNICODE`` list has ``k`` entries, those entries name
      channels ``N-k .. N-1`` in order. This holds whether an entry is the
      plain composite-transparency channel or a genuinely extra/named one.
    * When *neither* resource is present at all -- this package's own
      freshly-written, never-yet-Photoshop-touched flat PSD, or a plain
      RGBA image with only baked-in transparency and no document-level
      named channel -- there is no reliable per-channel name/identity to
      key off, so this reports no mask candidate rather than guessing.
      Verified empirically that psd-tools itself cannot tell "plain RGBA
      transparency" and "one genuinely extra, unnamed channel" apart in
      that situation (both read back with ``has_transparency() == True``
      and no recoverable channel index for it) -- so neither can this
      function, and it is exactly the situation that never arises from a
      real Photoshop save, per the two bullets above and the module-level
      note below.
    * ``psd_tools.api.utils.has_transparency`` / ``get_transparency_index``
      -- the same helpers ``PSDImage.topil()``/``numpy()`` themselves use
      internally for this exact purpose -- identify which one (at most) of
      the named entries, if any, is the plain composite-transparency
      channel rather than a genuine extra one; it is excluded from the
      candidate list before applying the one/multiple/none selection above.
    * ``PSDImage.topil(channel=N)`` returns a clean single-channel image
      (mode ``"L"`` at 8-bit depth; 16/32-bit are already downsampled to
      ``"L"`` by psd-tools itself) with none of ``topil()``'s whole-image
      post-processing (ICC, CMYK inversion, white-background removal)
      applied -- verified by reading ``psd_tools.api.pil_io`` directly and
      by round-tripping known pixel values through it. It returns ``None``
      when ``has_preview()`` is ``False`` (the document was saved without
      Maximize Compatibility): exactly like this module's own composite-
      vs-recomposite RGB fallback, there is no merged-channel data to read
      a mask from in that case, and psd-tools has no per-channel
      equivalent of layer recompositing to fall back to.

    Reaches into ``psd_tools.api.utils`` for ``has_transparency``/
    ``get_transparency_index``, which is not documented as public API (no
    leading underscore, but not re-exported from the ``psd_tools`` top-level
    package either) -- the only viable way, in this psd-tools version, to
    tell a plain transparency channel apart from a genuine extra one. A
    future psd-tools release could relocate or rename it; if the installed
    version lacks it entirely (module import failed), this function always
    returns ``None`` -- degraded, but the rest of this module (the core
    Tier 1 write/read round trip) keeps working regardless.

    Not yet confirmed against a real Photoshop-authored file (no Photoshop
    access in this environment, PROTOCOL.md §4 flags this as a follow-up
    spike -- see ``tests/fixtures/README``): specifically, that Photoshop
    always writes ``ALPHA_NAMES_UNICODE``/``ALPHA_IDENTIFIERS`` for every
    channel it manages once *any* extra channel exists in a document, which
    this function's whole approach assumes.

    Args:
        psd_path: Path to the saved PSD to inspect.
        preferred_name: The configured ``mask_channel_name`` setting, used
            to disambiguate when more than one extra channel is present.

    Returns:
        A single-channel, 8-bit ``"L"`` PIL image (white = 1.0, no
        inversion applied -- PROTOCOL.md §4) the same size as the document,
        or ``None`` if there is no extra-channel mask to extract. Does not
        itself catch unexpected exceptions from a corrupt/unreadable file;
        callers treat those as a non-fatal extraction failure (PROTOCOL.md
        §4: "log + null, edit still ingests").
    """
    if has_transparency is None or get_transparency_index is None:
        logger.warning(
            "%s: psd_tools.api.utils.has_transparency/get_transparency_index are not "
            "available in the installed psd-tools version; mask extraction is disabled",
            psd_path,
        )
        return None

    psd = PSDImage.open(psd_path)
    total_channels = psd.channels

    alpha_names = psd.image_resources.get_data(Resource.ALPHA_NAMES_UNICODE)
    if not alpha_names:
        alpha_names = psd.image_resources.get_data(Resource.ALPHA_NAMES_PASCAL)
    if not alpha_names:
        return None

    start = total_channels - len(alpha_names)
    if start < 0:
        # ALPHA_NAMES claims more entries than there are channels at all --
        # an inconsistent/malformed file. Nothing here can be trusted.
        logger.warning("%s: alpha channel names outnumber the document's channels", psd_path)
        return None

    transparency_index = get_transparency_index(psd) if has_transparency(psd) else -1
    candidates = [
        (start + offset, name)
        for offset, name in enumerate(alpha_names)
        if start + offset != transparency_index
    ]
    if not candidates:
        return None
    if len(candidates) == 1:
        winning_index = candidates[0][0]
    else:
        preferred_lower = preferred_name.lower()
        matches = [index for index, name in candidates if name.lower() == preferred_lower]
        if not matches:
            return None
        winning_index = matches[0]

    channel_image = psd.topil(channel=winning_index)
    if channel_image is None:
        return None
    if channel_image.mode != "L":
        channel_image = channel_image.convert("L")
    return channel_image


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
