"""PSD ⇄ ``LAYERS`` document mapping (docs/roadmap/layered-images.md, L1+L2).

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


# ---------------------------------------------------------------------------
# LAYERS → PSD (L2): preparing an incoming stack for the Compose writer.
# ---------------------------------------------------------------------------

#: LAYERS blend names → psd-tools blend modes, the exact reverse of
#: :data:`PSD_TO_LAYERS_BLEND` (24 modes both ways, asserted by test) plus
#: the two GIMP-only modes core has and Photoshop doesn't
#: (``grain-extract``/``grain-merge``) -- those degrade to NORMAL with a log
#: line in :func:`_psd_blend_mode`, the write-side mirror of the read side's
#: dissolve/darker-color/lighter-color loss.
LAYERS_TO_PSD_BLEND: dict[str, BlendMode] = {
    name: mode for mode, name in PSD_TO_LAYERS_BLEND.items()
}

#: The GIMP-only LAYERS modes with no Photoshop counterpart.
GIMP_ONLY_BLEND_MODES = frozenset({"grain-extract", "grain-merge"})


class PreparedLayer:
    """One write-ready layer from an incoming LAYERS stack.

    Everything PSD cannot express as a live property is already BAKED into
    ``image`` (flips, display-size resize, rotation -- PSD has no
    non-destructive transform this pack could emit; docs/roadmap/
    layered-images.md L2), while everything PSD CAN express stays a
    property: ``left``/``top``/``name``/``opacity``/``blend_mode``/
    ``visible``. A deliberately plain attribute holder (the
    ``OnSaveMode``-style class convention, not a dataclass import for one
    struct).
    """

    __slots__ = ("blend_mode", "image", "left", "name", "opacity", "top", "visible")

    def __init__(
        self,
        image: Image.Image,
        left: int,
        top: int,
        name: str | None,
        opacity: float,
        blend_mode: BlendMode,
        visible: bool,
    ) -> None:
        self.image = image
        self.left = left
        self.top = top
        self.name = name
        self.opacity = opacity
        self.blend_mode = blend_mode
        self.visible = visible


def _psd_blend_mode(name: Any, layer_label: str, source: str) -> BlendMode:
    """The psd-tools blend mode for a LAYERS blend *name*, degrading loudly.

    Mirrors :func:`_blend_name`'s never-raise contract in the write
    direction: an absent name means core's own default (``"normal"``), the
    GIMP-only pair logs at info, anything unrecognized logs a warning --
    a stack that composites in core must always still WRITE, just with the
    closest blend Photoshop has.
    """
    if name is None:
        return BlendMode.NORMAL
    direct = LAYERS_TO_PSD_BLEND.get(name)
    if direct is not None:
        return direct
    if name in GIMP_ONLY_BLEND_MODES:
        logger.info(
            "cpsb layers: %s: layer %r blend mode %r has no Photoshop equivalent, "
            "writing 'normal'",
            source, layer_label, name,
        )
        return BlendMode.NORMAL
    logger.warning(
        "cpsb layers: %s: layer %r has unrecognized blend mode %r, writing 'normal'",
        source, layer_label, name,
    )
    return BlendMode.NORMAL


def _int_or(value: Any, default: int) -> int:
    """Core's own ``_int`` coercion, mirrored: int/float pass (bool is NOT a
    number here), anything else takes *default*."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    return default


def _frame_rgba(tensor_frame: Any) -> Image.Image:
    """One ``(H, W, 3|4)`` float tensor frame as an ``"RGBA"`` PIL image."""
    import numpy as np

    array = tensor_frame.detach().cpu().numpy()
    array = (np.clip(array, 0.0, 1.0) * 255.0).round().astype(np.uint8)
    if array.shape[-1] == 3:
        alpha = np.full((*array.shape[:2], 1), 255, dtype=np.uint8)
        array = np.concatenate([array, alpha], axis=-1)
    return Image.fromarray(array, mode="RGBA")


def _mask_frame_for(mask: Any, index: int) -> Any | None:
    """Core's ``_item_mask_frame`` batch semantics, mirrored verbatim: a
    single-frame mask covers every image frame; a batched mask pairs
    per-index and runs out silently."""
    import torch

    if not isinstance(mask, torch.Tensor):
        return None
    if mask.shape[0] == 1:
        return mask[0]
    if index < mask.shape[0]:
        return mask[index]
    return None


def _apply_stack_mask(rgba: Image.Image, mask_frame: Any, layer_label: str, source: str):
    """*rgba* with a LAYERS ``mask`` frame (1 = transparent, the LoadImage
    convention) multiplied into alpha. A size-mismatched mask is resized
    with a log line rather than rejected -- this is a WRITER, and a
    best-effort mask beats a failed queue."""
    import numpy as np

    mask_array = mask_frame.detach().cpu().numpy()
    if mask_array.shape[:2] != (rgba.height, rgba.width):
        logger.info(
            "cpsb layers: %s: layer %r mask is %sx%s but the image is %sx%s; resizing "
            "the mask to fit",
            source, layer_label,
            mask_array.shape[1], mask_array.shape[0], rgba.width, rgba.height,
        )
        mask_pil = Image.fromarray(
            (np.clip(mask_array, 0.0, 1.0) * 255.0).round().astype(np.uint8), mode="L"
        ).resize(rgba.size, Image.BILINEAR)
        mask_array = np.array(mask_pil, dtype=np.float32) / 255.0
    array = np.array(rgba, dtype=np.float32)
    array[..., 3] *= 1.0 - np.clip(mask_array, 0.0, 1.0)
    return Image.fromarray(array.astype(np.uint8), mode="RGBA")


def _bake_transforms(
    rgba: Image.Image,
    item: dict[str, Any],
    left: int,
    top: int,
    layer_label: str,
    source: str,
) -> tuple[Image.Image, int, int]:
    """Flips, display-size resize, and rotation baked into pixels.

    PSD has no non-destructive transform this pack could emit
    (docs/roadmap/layered-images.md L2), so the write path renders them --
    in core's own order (resize to display size, flips, then rotation),
    with the rotated layer's new top-left recomputed by the exact
    ``placed_bounds`` math core renders with (``compositor_blend.py``, read
    verbatim from the rig at v0.31.1: rotation is RADIANS CLOCKWISE about
    the layer's center; the axis-aligned bounds floor the rotated
    corners). Every applied bake logs at info, naming the layer.
    """
    import math

    width = _int_or(item.get("w"), 0)
    height = _int_or(item.get("h"), 0)
    if width > 0 and height > 0 and (width, height) != rgba.size:
        logger.info(
            "cpsb layers: %s: baking display size %dx%d into layer %r (native %dx%d)",
            source, width, height, layer_label, rgba.width, rgba.height,
        )
        rgba = rgba.resize((width, height), Image.LANCZOS)
    if bool(item.get("flip_h", False)):
        logger.info("cpsb layers: %s: baking horizontal flip into layer %r", source, layer_label)
        rgba = rgba.transpose(Image.FLIP_LEFT_RIGHT)
    if bool(item.get("flip_v", False)):
        logger.info("cpsb layers: %s: baking vertical flip into layer %r", source, layer_label)
        rgba = rgba.transpose(Image.FLIP_TOP_BOTTOM)

    rotation = item.get("rotation")
    rotation = float(rotation) if isinstance(rotation, (int, float)) and not isinstance(
        rotation, bool
    ) else 0.0
    if rotation:
        logger.info(
            "cpsb layers: %s: baking %.4f rad rotation into layer %r", source, rotation, layer_label
        )
        # placed_bounds mirror: center of the pre-rotation box, corners
        # rotated clockwise, floored min = the baked layer's new top-left.
        display_width, display_height = rgba.size
        center_x = left + display_width / 2
        center_y = top + display_height / 2
        cos, sin = math.cos(rotation), math.sin(rotation)
        half_width, half_height = display_width / 2, display_height / 2
        corners = (
            (-half_width, -half_height), (half_width, -half_height),
            (half_width, half_height), (-half_width, half_height),
        )
        xs = [center_x + dx * cos - dy * sin for dx, dy in corners]
        ys = [center_y + dx * sin + dy * cos for dx, dy in corners]
        left = math.floor(min(xs))
        top = math.floor(min(ys))
        # PIL rotates counter-clockwise for positive angles; LAYERS rotation
        # is clockwise, so negate. expand=True grows the frame to the same
        # axis-aligned bounds placed_bounds describes.
        rgba = rgba.rotate(-math.degrees(rotation), expand=True, resample=Image.BICUBIC)
    return rgba, left, top


def prepare_stack(doc: Any, source: str) -> list[PreparedLayer]:
    """Write-ready layers from an incoming LAYERS *doc*, bottom-to-top.

    The consume-side twin of :func:`document_from_psd`, mirroring core's
    own ``document_items``/``expand_item_frames`` semantics (read verbatim
    from the rig at v0.31.1): stable-sort by ``z_index`` (default 0),
    ``type`` must be ``"raster"``, batched images expand one layer per
    frame (each sharing the item's name/properties), masks pair per-frame,
    defaults per core (position 0,0 / opacity 1 / normal / visible).

    Raises ``ValueError`` -- mirroring core's wording -- for a wrong
    document version, a non-raster item type, or an item image that isn't
    a ``(B, H, W, 3|4)`` tensor: an incompatible stack wired into the
    node's socket should fail the queue loudly, exactly as it would wired
    into core's compositor. Per-item DEGRADABLE trouble (blend names,
    mask sizes) degrades loudly instead -- see :func:`_psd_blend_mode` /
    :func:`_apply_stack_mask`.
    """
    import torch

    if not isinstance(doc, dict):
        raise ValueError("LAYERS input is not a layer document (expected a dict)")
    version = doc.get("version")
    if version is not None and version != LAYERS_VERSION:
        raise ValueError(f"LAYERS document version {version!r} is not supported")
    items = [item for item in doc.get("layers") or [] if isinstance(item, dict)]
    items.sort(key=lambda item: _int_or(item.get("z_index"), 0))

    prepared: list[PreparedLayer] = []
    for item in items:
        item_type = item.get("type", "raster")
        if item_type != "raster":
            raise ValueError(f"LAYERS item type {item_type!r} is not supported yet")
        image = item.get("image")
        if not isinstance(image, torch.Tensor) or image.dim() != 4 or image.shape[-1] not in (3, 4):
            raise ValueError(
                "LAYERS item image must be a (batch, height, width, 3|4) tensor"
            )
        name = item.get("name") if isinstance(item.get("name"), str) else None
        label = name if name is not None else f"layer {len(prepared) + 1}"
        left = _int_or(item.get("x"), 0)
        top = _int_or(item.get("y"), 0)
        opacity = item.get("opacity", 1.0)
        opacity = float(opacity) if isinstance(opacity, (int, float)) and not isinstance(
            opacity, bool
        ) else 1.0
        opacity = min(1.0, max(0.0, opacity))
        blend = _psd_blend_mode(item.get("blend_mode"), label, source)
        visible = bool(item.get("visible", True))
        for index in range(image.shape[0]):
            rgba = _frame_rgba(image[index])
            mask_frame = _mask_frame_for(item.get("mask"), index)
            if mask_frame is not None:
                rgba = _apply_stack_mask(rgba, mask_frame, label, source)
            baked, baked_left, baked_top = _bake_transforms(rgba, item, left, top, label, source)
            prepared.append(
                PreparedLayer(baked, baked_left, baked_top, name, opacity, blend, visible)
            )
    return prepared


def stack_extent(prepared: list[PreparedLayer], doc: Any) -> tuple[int, int]:
    """The canvas a *prepared* stack needs: the document's own ``canvas``
    when it carries a valid one (core's ``document_canvas`` coercion,
    mirrored), else the max ``(left + width, top + height)`` over the
    prepared layers -- floored at 1x1 so an empty or fully-negative stack
    still yields a writable document."""
    if isinstance(doc, dict):
        canvas = doc.get("canvas")
        if isinstance(canvas, (tuple, list)) and len(canvas) == 2:
            width, height = _int_or(canvas[0], 0), _int_or(canvas[1], 0)
            if width > 0 and height > 0:
                return width, height
    width = max((layer.left + layer.image.width for layer in prepared), default=1)
    height = max((layer.top + layer.image.height for layer in prepared), default=1)
    return max(1, width), max(1, height)


def stack_frame_count(doc: Any) -> int:
    """How many layers :func:`prepare_stack` would produce for *doc*, with
    NO pixel work (each valid raster item contributes its batch size).

    Exists for :meth:`cpsb.compose_psd.PhotoshopComposePSD.IS_CHANGED`,
    which must mirror ``execute``'s post-cap layer accounting exactly
    (identity hashes computed in both places have to match, or an arriving
    edit stops re-triggering the graph) without re-running the full
    prepare -- both for cost and because prepare LOGS its bakes, which
    would double every message on every queue. Never raises: a malformed
    item counts 0 here and ``execute``'s own :func:`prepare_stack` raises
    the real error before any identity is recorded.
    """
    import torch

    if not isinstance(doc, dict):
        return 0
    count = 0
    for item in doc.get("layers") or []:
        image = item.get("image") if isinstance(item, dict) else None
        if isinstance(image, torch.Tensor) and image.dim() == 4 and image.shape[-1] in (3, 4):
            count += int(image.shape[0])
    return count


def stack_digest(doc: Any) -> str:
    """A deterministic sha256 fingerprint of a LAYERS *doc*, for cache keys.

    Folds every consumed field -- scalar properties verbatim plus the raw
    image/mask tensor bytes -- so any change that would alter the written
    PSD changes the digest (the ``IS_CHANGED``/identity-hash contract
    :mod:`cpsb.compose_psd` hangs on it). Digests the RAW document, before
    :func:`prepare_stack`'s baking: cheaper, and bake output is a pure
    function of these bytes anyway. Never raises on a malformed doc --
    validation is :func:`prepare_stack`'s job; a digest of garbage is
    still a stable digest of that garbage.
    """
    import hashlib

    import torch

    hasher = hashlib.sha256()
    if not isinstance(doc, dict):
        hasher.update(repr(doc).encode("utf-8", "replace"))
        return hasher.hexdigest()
    hasher.update(repr(doc.get("version")).encode("ascii", "replace"))
    hasher.update(repr(doc.get("canvas")).encode("ascii", "replace"))
    layers = doc.get("layers")
    for item in layers if isinstance(layers, list) else []:
        if not isinstance(item, dict):
            hasher.update(b"<non-dict>")
            continue
        for key in ("type", "name", "x", "y", "z_index", "opacity", "blend_mode",
                    "visible", "rotation", "w", "h", "flip_h", "flip_v"):
            hasher.update(f"{key}={item.get(key)!r};".encode("utf-8", "replace"))
        for key in ("image", "mask"):
            tensor = item.get(key)
            if isinstance(tensor, torch.Tensor):
                hasher.update(f"{key}{tuple(tensor.shape)}".encode("ascii"))
                hasher.update(tensor.detach().cpu().contiguous().numpy().tobytes())
        hasher.update(b"\x00")
    return hasher.hexdigest()
