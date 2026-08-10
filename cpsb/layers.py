"""PSD → ``LAYERS`` document mapping (docs/roadmap/layered-images.md, L1).

ComfyUI v0.31.0 shipped a first-class layered-image socket type, ``LAYERS``
(``io.Layers``, backend PR #15317): a plain dict any custom node can construct
or consume -- ``{"version": 1, "canvas": (w, h), "layers": [<LayerItem>, ...]}``.
This module is this pack's ONE place that knows the LayerItem key set, so
upstream schema drift (all three core nodes are experimental) means editing
here, not hunting call sites. The key set below is pinned against the REAL
v0.31.1 source (``comfy_extras/nodes_compositor.py``'s ``document_items`` /
``expand_item_frames``, read directly from the test rig 2026-08-09, the same
verification standard :mod:`cpsb.nodes`' docstring cites):

- ``document_items`` accepts ``version`` ``None``/``1`` only, requires
  ``type == "raster"`` and a ``torch.Tensor`` ``image``, validates
  ``blend_mode`` against ``_LAYER_MODES`` (26 names, verified verbatim in
  :data:`LAYERS_BLEND_NAMES`), and stable-sorts by ``z_index``.
- ``expand_item_frames`` defaults: ``x``/``y`` 0, ``opacity`` 1.0,
  ``blend_mode`` "normal", ``visible`` True, ``rotation`` 0.0 (RADIANS,
  about the layer's CENTER -- ``compositor_blend.placed_bounds``),
  ``w``/``h`` <= 0 means native size, ``flip_h``/``flip_v`` False -- so
  items this module builds simply OMIT keys whose value would be the
  default, staying byte-light and forward-tolerant.
- The 50-layer cap (``MAX_LAYERS``) is enforced by CORE at consume time
  (``expand_item_frames`` raises), not by producers -- so
  :func:`document_from_psd` warns past :data:`CORE_MAX_LAYERS` instead of
  raising (Eric's decision 1, 2026-08-09: "don't break older builds or
  current use cases" -- a 60-layer PSD flattens fine today and must keep
  loading).

Masks are BAKED into each item's alpha channel rather than emitted as
separate ``mask`` tensors, for a fidelity reason discovered empirically
(2026-08-09, psd-tools 1.17.4): ``create_pixel_layer`` from an RGBA source --
this pack's own Compose writer included -- stores transparency as a LAYER
MASK, with ``topil()`` returning fully-opaque alpha. A projection that
ignored masks would silently drop the transparency of every PSD this pack
itself writes. Baking (full-layer mask canvas filled with the mask's
``background_color``, mask pixels pasted at their own offset, multiplied
into alpha) is visually identical for compositing purposes and sidesteps
the mask-bbox/offset/background subtleties a faithful separate-tensor
projection would have to re-encode. Vector ("real") masks are not applied
-- pixel masks only, matching psd-tools' own compositing scope.

Blend-mode names: 24 of Photoshop's 27 map 1:1 (:data:`PSD_TO_LAYERS_BLEND`);
``dissolve``/``darker-color``/``lighter-color`` fall back to their nearest
neighbor with a log line (:data:`LOSSY_PSD_BLEND_FALLBACKS`); anything
unknown falls back to ``"normal"`` with a warning -- an unmapped mode must
degrade to a loadable document, never a ``ValueError`` into the user's queue
(core raises on unknown names, so emitting them is not an option).

Compositing-semantics honesty (docs/roadmap/layered-images.md, evidence #4):
core composites in linear/per-mode-perceptual space (GIMP semantics),
Photoshop in sRGB -- a stack this module emits ARRANGES identically in
core's compositor but partial-opacity/non-normal blends render similarly,
not identically, to Photoshop. Nothing here may promise pixel parity.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from PIL import Image
from psd_tools.constants import BlendMode

if TYPE_CHECKING:
    from psd_tools import PSDImage

logger = logging.getLogger("cpsb")

#: The one supported LAYERS document schema version (core validates
#: ``version in (None, 1)``; this pack always writes it explicitly).
LAYERS_VERSION = 1

#: Core's consume-time flattened-layer cap (``nodes_compositor.MAX_LAYERS``,
#: verified on the rig at v0.31.1). Producers are NOT capped -- see the
#: module docstring -- so :func:`document_from_psd` warns past this, only.
CORE_MAX_LAYERS = 50

#: The 26 blend-mode names core's ``_LAYER_MODES`` accepts, verbatim
#: (``comfy_extras/compositor_blend.py``, dumped from the rig at v0.31.1
#: 2026-08-09). Kept as a frozenset for the emit-side sanity check in
#: :func:`_blend_name` -- an unknown name raises ``ValueError`` inside core's
#: ``document_items``, so this module must never emit one.
LAYERS_BLEND_NAMES = frozenset(
    {
        "normal", "multiply", "screen", "overlay", "darken", "lighten",
        "color-dodge", "color-burn", "hard-light", "soft-light", "difference",
        "exclusion", "linear-dodge", "linear-burn", "vivid-light", "pin-light",
        "linear-light", "hard-mix", "subtract", "divide", "grain-extract",
        "grain-merge", "hue", "saturation", "color", "luminosity",
    }
)

#: Photoshop blend modes with an exact-name LAYERS counterpart (24 of 27).
PSD_TO_LAYERS_BLEND: dict[BlendMode, str] = {
    BlendMode.NORMAL: "normal",
    BlendMode.DARKEN: "darken",
    BlendMode.MULTIPLY: "multiply",
    BlendMode.COLOR_BURN: "color-burn",
    BlendMode.LINEAR_BURN: "linear-burn",
    BlendMode.LIGHTEN: "lighten",
    BlendMode.SCREEN: "screen",
    BlendMode.COLOR_DODGE: "color-dodge",
    BlendMode.LINEAR_DODGE: "linear-dodge",
    BlendMode.OVERLAY: "overlay",
    BlendMode.SOFT_LIGHT: "soft-light",
    BlendMode.HARD_LIGHT: "hard-light",
    BlendMode.VIVID_LIGHT: "vivid-light",
    BlendMode.LINEAR_LIGHT: "linear-light",
    BlendMode.PIN_LIGHT: "pin-light",
    BlendMode.HARD_MIX: "hard-mix",
    BlendMode.DIFFERENCE: "difference",
    BlendMode.EXCLUSION: "exclusion",
    BlendMode.SUBTRACT: "subtract",
    BlendMode.DIVIDE: "divide",
    BlendMode.HUE: "hue",
    BlendMode.SATURATION: "saturation",
    BlendMode.COLOR: "color",
    BlendMode.LUMINOSITY: "luminosity",
}

#: The three Photoshop modes with NO LAYERS counterpart, each mapped to its
#: nearest visual neighbor (docs/roadmap/layered-images.md: "each with a
#: sensible neighbor") -- logged per layer when applied.
LOSSY_PSD_BLEND_FALLBACKS: dict[BlendMode, str] = {
    BlendMode.DISSOLVE: "normal",
    BlendMode.DARKER_COLOR: "darken",
    BlendMode.LIGHTER_COLOR: "lighten",
}


def _rgba_tensor(rgba: Image.Image) -> Any:
    """A ``(1, H, W, 4)`` float32 IMAGE tensor from an ``"RGBA"`` PIL image.

    Deliberately NOT :func:`cpsb.nodes._pil_to_tensor` -- that helper
    converts to ``"RGB"`` (the flat-output convention), and a LayerItem's
    whole point is carrying per-layer transparency (core accepts 3- or
    4-channel item images; 4 keeps the alpha).
    """
    import numpy as np
    import torch

    array = np.array(rgba, dtype=np.float32) / 255.0
    return torch.from_numpy(array)[None, ...]


def _blend_name(mode: Any, layer_name: str, source: str) -> str:
    """The LAYERS blend name for a psd-tools *mode*, degrading loudly.

    ``PASS_THROUGH`` (a group's default) is not a leaf blend at all --
    callers passing a group's mode get ``"normal"`` with a debug line (the
    group's children were already composited among themselves, which is most
    of what pass-through means). Lossy trios log at info; anything unknown
    logs a warning. Never raises, and only ever returns a name in
    :data:`LAYERS_BLEND_NAMES` -- core raises ``ValueError`` on unknown
    names, so a bad map entry here would poison every document we emit.
    """
    direct = PSD_TO_LAYERS_BLEND.get(mode)
    if direct is not None:
        return direct
    lossy = LOSSY_PSD_BLEND_FALLBACKS.get(mode)
    if lossy is not None:
        logger.info(
            "cpsb layers: %s: layer %r blend mode %s has no LAYERS equivalent, using %r",
            source, layer_name, mode, lossy,
        )
        return lossy
    if mode == BlendMode.PASS_THROUGH:
        logger.debug(
            "cpsb layers: %s: group %r is pass-through; emitting 'normal'", source, layer_name
        )
        return "normal"
    logger.warning(
        "cpsb layers: %s: layer %r has unmapped blend mode %r, using 'normal'",
        source, layer_name, mode,
    )
    return "normal"


def _bake_mask_into_alpha(rgba: Image.Image, layer: Any, origin: tuple[int, int]) -> Image.Image:
    """*rgba* with *layer*'s pixel mask multiplied into its alpha channel.

    Why baking (not a separate ``mask`` tensor): see the module docstring --
    psd-tools stores an RGBA-created layer's transparency as a mask with
    fully-opaque ``topil()`` alpha, so skipping masks would drop the
    transparency of every PSD this pack itself writes.

    The mask has its OWN bbox and offset, independent of the layer's, plus a
    ``background_color`` (0..255) covering everywhere its bbox doesn't: the
    bake paints a full-*rgba*-sized "L" canvas with that background, pastes
    the mask's pixels at ``(mask.left - origin_x, mask.top - origin_y)``,
    and multiplies the result into alpha. A disabled mask, or one with no
    decodable pixels, leaves *rgba* untouched.

    Args:
        rgba: The layer's pixels, already ``"RGBA"``.
        layer: The psd-tools layer (or group) carrying the mask.
        origin: *rgba*'s own absolute canvas position ``(left, top)`` --
            the layer's ``left``/``top`` for a leaf, the composite bbox's
            corner for a flattened group.

    Returns:
        *rgba* itself when there is nothing to apply, else a new image.
    """
    mask = getattr(layer, "mask", None)
    if mask is None or getattr(mask, "disabled", False):
        return rgba
    mask_pil = mask.topil()
    if mask_pil is None:
        return rgba
    import numpy as np

    background = mask.background_color
    if not isinstance(background, int):
        background = 255
    canvas = Image.new("L", rgba.size, background)
    canvas.paste(mask_pil, (mask.left - origin[0], mask.top - origin[1]))
    array = np.array(rgba, dtype=np.float32)
    array[..., 3] *= np.array(canvas, dtype=np.float32) / 255.0
    return Image.fromarray(array.astype(np.uint8), mode="RGBA")


def _raster_item(
    layer: Any, source: str, *, opacity_scale: float = 1.0, visible_with_parents: bool = True
) -> dict[str, Any] | None:
    """The LayerItem for one psd-tools PIXEL layer, or ``None`` to skip it.

    ``None`` (logged) for non-raster content -- adjustment/fill layers,
    where ``topil()`` returns nothing. Text layers and smart objects DO
    yield pixels (psd-tools rasterizes), arriving as plain raster layers --
    stated honestly in the node tooltip (L1). ``z_index`` is deliberately
    absent: :func:`document_from_psd` numbers the surviving items at the
    end, so skipped layers never leave holes.

    Args:
        layer: The psd-tools layer.
        source: Filename, for log lines.
        opacity_scale: Product of every ancestor group's own opacity
            (flat mode composes group opacity onto leaves; LAYERS has no
            groups to hang it on).
        visible_with_parents: AND of every ancestor group's own visible
            flag, folded with the layer's own below (same projection).
    """
    pil = layer.topil()
    if pil is None:
        logger.info(
            "cpsb layers: %s: skipping non-raster layer %r (no pixel data)", source, layer.name
        )
        return None
    rgba = pil if pil.mode == "RGBA" else pil.convert("RGBA")
    rgba = _bake_mask_into_alpha(rgba, layer, (int(layer.left), int(layer.top)))
    return {
        "type": "raster",
        "image": _rgba_tensor(rgba),
        "name": layer.name,
        "x": int(layer.left),
        "y": int(layer.top),
        "opacity": (layer.opacity / 255.0) * opacity_scale,
        "blend_mode": _blend_name(layer.blend_mode, layer.name, source),
        "visible": bool(layer.visible) and visible_with_parents,
    }


def _walk_leaves(
    container: Any, opacity_scale: float = 1.0, visible_with_parents: bool = True
):
    """Yields ``(leaf, opacity_scale, visible_with_parents)`` bottom-to-top.

    Descends into groups depth-first in psd-tools iteration order (index 0
    is the BOTTOM layer -- verified empirically 2026-08-09), multiplying
    each group's own opacity and ANDing its own visible flag onto its
    descendants: LAYERS has no group concept, so the group's contribution
    must ride on the leaves (Eric's decision 2: flat leaf list is the
    default projection). A group's own non-normal blend mode cannot ride
    along -- logged once per group, leaf blends kept.
    """
    for layer in container:
        if layer.is_group():
            if layer.blend_mode not in (BlendMode.PASS_THROUGH, BlendMode.NORMAL):
                logger.info(
                    "cpsb layers: group %r blend mode %s cannot be represented in a flat "
                    "layer list; its layers keep their own blend modes",
                    layer.name, layer.blend_mode,
                )
            yield from _walk_leaves(
                layer,
                opacity_scale * (layer.opacity / 255.0),
                visible_with_parents and bool(layer.visible),
            )
        else:
            yield layer, opacity_scale, visible_with_parents


def _flattened_group_item(group: Any, source: str) -> dict[str, Any] | None:
    """One LayerItem for a whole top-level *group* (``flatten_groups`` on).

    Composites the group's subtree via psd-tools (sRGB, masks and child
    opacity/visibility/blends applied -- the authoritative flatten this
    pack's honesty notes defer to), carrying the group's OWN
    opacity/blend/visible onto the item so re-blending against other
    top-level content survives the projection.

    The group's OWN properties must not leak into the composited PIXELS,
    because they ride on the item where they stay live and editable --
    psd-tools' ``composite()`` would otherwise apply two of them right into
    the pixels (both verified empirically 2026-08-09: a hidden group
    composites to nothing no matter the ``layer_filter``, and the group's
    own opacity multiplies the composite's alpha; its own MASK is applied
    by ``composite()`` too, which is why -- unlike :func:`_raster_item`'s
    ``topil()`` path -- there is deliberately NO ``_bake_mask_into_alpha``
    call here: baking it again would double-apply). So ``visible`` is
    temporarily forced on and ``opacity`` temporarily reset to 255, both
    restored immediately -- the opened ``PSDImage`` is this projection's
    own read-only copy, never saved. Children keep honoring their OWN
    flags via the ``layer_filter`` (a hidden layer INSIDE the group stays
    hidden in the composite).
    """
    was_visible = bool(group.visible)
    was_opacity = group.opacity
    group.visible = True
    group.opacity = 255
    try:
        bbox = group.bbox
        if bbox is None or bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
            logger.info("cpsb layers: %s: skipping empty group %r", source, group.name)
            return None
        pil = group.composite(layer_filter=lambda candidate: bool(candidate.visible))
    finally:
        group.visible = was_visible
        group.opacity = was_opacity
    if pil is None:
        logger.info("cpsb layers: %s: skipping empty group %r", source, group.name)
        return None
    rgba = pil if pil.mode == "RGBA" else pil.convert("RGBA")
    return {
        "type": "raster",
        "image": _rgba_tensor(rgba),
        "name": group.name,
        "x": int(bbox[0]),
        "y": int(bbox[1]),
        "opacity": group.opacity / 255.0,
        "blend_mode": _blend_name(group.blend_mode, group.name, source),
        "visible": was_visible,
    }


def document_from_psd(psd: PSDImage, *, flatten_groups: bool, source: str) -> dict[str, Any]:
    """The LAYERS document for an opened *psd* (L1's whole product).

    Args:
        psd: An opened psd-tools document. Treated as read-only apart from
            :func:`_flattened_group_item`'s restore-guaranteed visibility
            toggle; never saved.
        flatten_groups: Eric's decision 2 (2026-08-09) verbatim -- ``False``
            (the default widget value): "showing all layers in a flat list",
            every leaf raster layer its own item, group opacity/visibility
            composed on; ``True``: "a checkbox to flatten each group", one
            item per TOP-LEVEL entry, groups composited to single layers.
        source: Filename for log lines.

    Returns:
        ``{"version": 1, "canvas": (w, h), "layers": [...]}`` -- items
        bottom-to-top with ``z_index`` numbered over the SURVIVING items
        (skips leave no holes). Possibly empty ``layers`` (an
        all-adjustment PSD); past :data:`CORE_MAX_LAYERS` a warning names
        the file and core's consume-time cap, but the document is still
        returned in full (module docstring: producers must not break
        loading).
    """
    items: list[dict[str, Any]] = []
    if flatten_groups:
        for layer in psd:
            if layer.is_group():
                item = _flattened_group_item(layer, source)
            else:
                item = _raster_item(layer, source)
            if item is not None:
                items.append(item)
    else:
        for leaf, opacity_scale, visible_with_parents in _walk_leaves(psd):
            item = _raster_item(
                leaf, source, opacity_scale=opacity_scale, visible_with_parents=visible_with_parents
            )
            if item is not None:
                items.append(item)
    for z_index, item in enumerate(items):
        item["z_index"] = z_index
    if len(items) > CORE_MAX_LAYERS:
        logger.warning(
            "cpsb layers: %s: %d layers exceed ComfyUI's %d-layer compositor cap -- the "
            "layers output is complete, but core's Create Layered Image node will reject "
            "it (this pack's own Compose node has no such cap)",
            source, len(items), CORE_MAX_LAYERS,
        )
    return {"version": LAYERS_VERSION, "canvas": (psd.width, psd.height), "layers": items}


def document_from_flat_image(image: Image.Image, name: str) -> dict[str, Any]:
    """A single-layer LAYERS document for a flat raster (the TIFF path).

    Load PSD accepts flat formats too (:func:`cpsb.load_psd._accepted_extensions`);
    their ``layers`` output is the honest one-layer stack: full image at
    ``(0, 0)``, opaque, normal, visible -- so the output is ALWAYS a valid
    document regardless of the selected file's format.
    """
    rgba = image if image.mode == "RGBA" else image.convert("RGBA")
    return {
        "version": LAYERS_VERSION,
        "canvas": (image.width, image.height),
        "layers": [
            {
                "type": "raster",
                "image": _rgba_tensor(rgba),
                "name": name,
                "x": 0,
                "y": 0,
                "z_index": 0,
                "opacity": 1.0,
                "blend_mode": "normal",
                "visible": True,
            }
        ],
    }


def empty_document() -> dict[str, Any]:
    """The degrade-guard document: valid, loadable, zero layers.

    Returned by :mod:`cpsb.load_psd` when layer extraction fails on a file
    whose FLAT decode succeeded -- the new ``layers`` output must never
    make a file that loaded yesterday stop loading today (Eric's decision
    1). Deliberately version-stamped so core still validates it cleanly.
    """
    return {"version": LAYERS_VERSION, "layers": []}
