"""The ``PhotoshopAnnotate`` ComfyUI node (PROTOCOL.md §6d).

Pairs a typed instruction with a region mask so a downstream model gets both
"what to change" (STRING, never rendered onto pixels) and "where" (MASK),
without the user having to lay text out on the image by hand (the design
brief this node exists to satisfy, ``research/research-annotate-node.md``
§0). Two ways to produce the MASK: plug one in from any ComfyUI-only mask
source (MaskEditor, a segmentation node, ...), or let the user mark up the
image in Photoshop itself and derive the mask from the PIXEL DIFFERENCE
between what was sent and what came back -- ANY tool/color works, since nothing
here inspects layers or channels, only the final flattened pixels (PROTOCOL.md
§4 removed channel-based extraction entirely; this node never relied on it).

Reuses :mod:`cpsb.nodes`' shared plumbing rather than duplicating it: tensor
<-> PIL conversion (:func:`cpsb.nodes._tensor_to_pil` /
:func:`cpsb.nodes._pil_to_tensor`), the module-level backend state
(:func:`cpsb.nodes._require_state`, wired by the SAME single
``cpsb.nodes.configure()`` call in the top-level ``__init__.py`` -- see that
function's own docstring), and -- for opening Photoshop -- the bridge node's
tier-selecting open seam itself,
:meth:`cpsb.nodes.PhotoshopBridge._open_in_photoshop`, called directly rather
than re-implemented, so this node's Tier 1/Tier 2 behavior (and its bounded,
non-hanging Tier 2 send) is identical to the bridge node's "Open only" mode
by construction, not by a second copy that could drift.

Like :mod:`cpsb.nodes`, this module never imports ``torch``/``numpy``/
``scipy`` at module level: :func:`cpsb.nodes._tensor_to_pil` already proves
plain ``numpy`` arrays (no torch) are enough for every pixel operation this
node needs, so importing ``numpy`` locally, inside each function that touches
array data, keeps this module importable in a plain test environment too.
``scipy`` gets the same treatment for an additional reason: it is not this
package's own dependency at all (``requirements.txt`` doesn't list it) but
IS guaranteed present at runtime because ComfyUI itself depends on it --
the identical reasoning :mod:`cpsb.nodes`' own docstring already documents
for torch/numpy, applied to a second, ComfyUI-provided-not-declared package.
"""

from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING, Any

from PIL import Image, ImageDraw

from . import nodes
from .handoff import HandoffManager, HandoffMeta, SourceRef, compute_source_hash
from .psd_io import write_psd

if TYPE_CHECKING:
    from .nodes import _NodeState

logger = logging.getLogger("cpsb")

#: Per-channel abs-difference threshold (0-255 scale) above which a pixel
#: counts as "edited" for the Photoshop diff mask (PROTOCOL.md §6d: "a small
#: threshold"). ~4% of full-scale: large enough to ignore the lossless-but-
#: not-bit-identical round trip a pixel takes through this package's own PSD
#: write (``psd_io.write_psd``, psd-tools RLE encoding) and Photoshop's own
#: re-save, small enough to still catch a faint/thin pencil mark. Chosen as a
#: sane constant, not derived -- documented here rather than tuned per image.
_DIFF_THRESHOLD = 10

#: Iterations for both the scipy morphological close and the degraded PIL
#: dilate-only fallback (see :func:`_close_and_fill_mask`). Two passes closes
#: small gaps in a hand-drawn outline (PROTOCOL.md §6d's whole reason for
#: closing at all: "the user may mark with ANY tool/color", including a thin
#: outline stroke that a single-pass close would still leave holed) without
#: letting the mask balloon far past the user's actual marks.
_MORPH_ITERATIONS = 2

#: 4px pure red, no fill (PROTOCOL.md §6d) -- the box-annotation convention
#: Kontext/Qwen-Image-Edit are documented to respond to
#: (``research/research-annotate-node.md`` §1.5).
_BOX_STROKE_WIDTH = 4
_BOX_COLOR = (255, 0, 0)


class AnnotateMode:
    """String constants for the ``annotate_mode`` COMBO input (PROTOCOL.md §6d).

    Unlike :class:`cpsb.nodes.BridgeMode`, the frontend does not string-match
    on these (this node has no per-mode auto-queue policy -- PROTOCOL.md §6d:
    "No auto-queue... this node has no re-run mode"), but the literal text is
    still part of the protocol contract, so it is named here rather than
    inlined.
    """

    PASS_THROUGH = "Pass through"
    PS_MODE = "Open in Photoshop (mask from edits)"


def _import_scipy_ndimage() -> Any | None:
    """``scipy.ndimage``, or ``None`` if scipy is not importable.

    Guarded the same way this package treats torch/numpy (see this module's
    own docstring): scipy is a core ComfyUI dependency, not this package's,
    so it is never declared in ``requirements.txt`` and never imported at
    module level. Tests force the degraded fallback path (see
    :func:`_dilate_only_fallback`) by monkeypatching this function directly
    rather than manipulating ``sys.modules``.
    """
    try:
        from scipy import ndimage
    except ImportError:
        return None
    return ndimage


def _raw_diff_mask(source_image: Image.Image, edit_image: Image.Image) -> Any | None:
    """Boolean ``(H, W)`` array where *edit_image* differs from *source_image*.

    A pixel counts as "edited" when the max abs difference across its RGB
    channels exceeds :data:`_DIFF_THRESHOLD` -- comparing on RGB (not the
    raw mode) so an edit saved with or without an alpha channel diffs
    consistently either way.

    Args:
        source_image: The image handed to Photoshop.
        edit_image: The ingested edit read back from disk.

    Returns:
        A numpy bool array, or ``None`` if the two images differ in pixel
        size (e.g. the user resized the canvas in Photoshop) -- no
        pixel-aligned diff is possible then, and callers fall back to the
        next MASK precedence tier (PROTOCOL.md §6d).
    """
    import numpy as np

    source_rgb = source_image.convert("RGB")
    edit_rgb = edit_image.convert("RGB")
    if source_rgb.size != edit_rgb.size:
        return None
    source_arr = np.asarray(source_rgb, dtype=np.int16)
    edit_arr = np.asarray(edit_rgb, dtype=np.int16)
    diff = np.abs(source_arr - edit_arr).max(axis=-1)
    return diff > _DIFF_THRESHOLD


def _dilate_only_fallback(raw_mask: Any) -> Any:
    """Degraded no-scipy fallback for :func:`_close_and_fill_mask`.

    PIL has no hole-filling primitive, so this only dilates (``MaxFilter``,
    the "grow the white region" half of a true morphological close) -- it
    closes small gaps in a stroke the same way the scipy path's
    ``binary_closing`` does, but a genuinely hollow outline (a closed ring
    with an untouched interior) stays hollow: there is no
    ``binary_fill_holes`` equivalent here. Degraded, not broken: a mask that
    is a thin ring instead of a filled blob is still usably positioned for a
    "roughly mark this area" feature, just less complete than the scipy path.

    Args:
        raw_mask: Boolean ``(H, W)`` array (see :func:`_raw_diff_mask`).

    Returns:
        A boolean ``(H, W)`` array, dilated by :data:`_MORPH_ITERATIONS`
        3x3 passes.
    """
    import numpy as np
    from PIL import ImageFilter

    mask_image = Image.fromarray((raw_mask.astype("uint8") * 255), mode="L")
    for _ in range(_MORPH_ITERATIONS):
        mask_image = mask_image.filter(ImageFilter.MaxFilter(3))
    return np.asarray(mask_image) > 127


def _close_and_fill_mask(raw_mask: Any) -> Any:
    """Morphologically close then hole-fill *raw_mask* (PROTOCOL.md §6d).

    Turns a hand-drawn OUTLINE (a ring of edited pixels, not a filled region
    -- the natural result of "circling" something with a brush or lasso
    stroke) into a filled region, so the mask actually covers the area the
    user meant to mark rather than just its boundary. Idempotent on an
    already-filled scribble: closing/filling a solid blob with no interior
    holes is a safe no-op, so one code path handles both drawing styles
    (``research/research-annotate-node.md`` §1.4).

    Prefers ``scipy.ndimage`` (``binary_closing`` + ``binary_fill_holes``,
    guarded via :func:`_import_scipy_ndimage`); falls back to
    :func:`_dilate_only_fallback` -- dilate-only, no true closing, no hole
    fill -- when scipy is not importable, documented there as degraded.

    Args:
        raw_mask: Boolean ``(H, W)`` array (see :func:`_raw_diff_mask`).

    Returns:
        A boolean ``(H, W)`` array. An all-zero input is returned unchanged
        (nothing to close or fill).
    """
    import numpy as np

    if not raw_mask.any():
        return raw_mask

    ndimage = _import_scipy_ndimage()
    if ndimage is None:
        return _dilate_only_fallback(raw_mask)

    # Full 3x3 (8-connectivity) structure so diagonal gaps in a stroke close
    # too, not just orthogonal ones.
    structure = np.ones((3, 3), dtype=bool)
    closed = ndimage.binary_closing(raw_mask, structure=structure, iterations=_MORPH_ITERATIONS)
    return ndimage.binary_fill_holes(closed)


def _compute_diff_mask(
    manager: HandoffManager, handoff_id: str, source_image: Image.Image
) -> Any | None:
    """The closed-and-filled Photoshop diff mask for *handoff_id*, as float32 0/1.

    Args:
        manager: The handoff manager.
        handoff_id: An active handoff already confirmed to have an edit.
        source_image: The image originally handed to Photoshop for this
            handoff.

    Returns:
        A ``(H, W)`` float32 numpy array (values exactly 0.0 or 1.0), or
        ``None`` if there is no edit file on disk (a filesystem race --
        cheap to guard) or its size doesn't match *source_image* -- both
        cases mean "no diff mask available," and the caller falls back to
        the next MASK precedence tier (PROTOCOL.md §6d).
    """
    edit_path = manager.edit_image_path(handoff_id)
    if edit_path is None or not edit_path.exists():
        return None
    with Image.open(edit_path) as edit_file:
        edit_file.load()
        raw = _raw_diff_mask(source_image, edit_file)
    if raw is None:
        logger.warning(
            "cpsb annotate: handoff %s: edit size differs from source, "
            "skipping the diff mask",
            handoff_id,
        )
        return None
    return _close_and_fill_mask(raw).astype("float32")


def _mask_tensor_to_array(mask_tensor: Any) -> Any:
    """First frame of a ComfyUI ``MASK`` tensor as an ``(H, W)`` float32 numpy array."""
    import numpy as np

    frame = mask_tensor[0]
    array = frame.cpu().numpy() if hasattr(frame, "cpu") else np.asarray(frame)
    return np.clip(array, 0.0, 1.0).astype(np.float32)


def _array_to_mask_tensor(mask_array: Any) -> Any:
    """``(H, W)`` numpy array as a ``MASK`` tensor shaped ``(1, H, W)`` (PROTOCOL.md §6d)."""
    import torch

    return torch.from_numpy(mask_array.astype("float32"))[None, ...]


def _bbox_of_nonzero(mask_array: Any) -> tuple[int, int, int, int] | None:
    """Inclusive ``(x0, y0, x1, y1)`` bounding box of every nonzero pixel in *mask_array*.

    Multi-region masks (several disjoint marked areas) resolve to ONE box
    spanning every nonzero pixel across all of them -- a deliberate v1
    simplification (PROTOCOL.md §6d: "single box, v1 -- document"), not a
    per-region search.

    Args:
        mask_array: A ``(H, W)`` numpy array.

    Returns:
        The bounding box, or ``None`` for an all-zero mask.
    """
    import numpy as np

    nonzero = mask_array > 0
    rows = np.any(nonzero, axis=1)
    if not rows.any():
        return None
    cols = np.any(nonzero, axis=0)
    y_indices = np.where(rows)[0]
    x_indices = np.where(cols)[0]
    return int(x_indices[0]), int(y_indices[0]), int(x_indices[-1]), int(y_indices[-1])


def _create_handoff_and_open(
    state: _NodeState, node_id: str, pil_image: Image.Image, source_hash: str
) -> HandoffMeta:
    """Write a fresh ``bridge_node`` handoff for *pil_image* and open it, non-blocking.

    Mirrors :meth:`cpsb.nodes.PhotoshopBridge._create_handoff` (a bridge-node
    input is an in-memory tensor, not a file ``/view`` could address, so
    ``source`` is a descriptive placeholder -- PROTOCOL.md §6), then hands
    the open itself to the bridge node's own tier-selecting seam,
    :meth:`cpsb.nodes.PhotoshopBridge._open_in_photoshop`, so this is
    identical to that node's "Open only (don't wait)" mode: fire-and-forget,
    bounded on the Tier 2 path, never blocks this call.

    Args:
        state: The configured backend state (``nodes._require_state()``).
        node_id: This node instance's ``unique_id``, stringified.
        pil_image: The decoded source pixels.
        source_hash: :func:`cpsb.handoff.compute_source_hash` of *pil_image*.

    Returns:
        The newly created handoff's metadata.
    """
    meta = state.manager.create(
        origin_node_id=node_id,
        origin_kind="bridge_node",
        workflow_name="",
        source=SourceRef(filename=f"annotate_{node_id}.png", subfolder="", type="temp"),
        original_image=pil_image,
        source_hash=source_hash,
    )
    psd_path = state.manager.handoff_dir(meta.handoff_id) / "source.psd"
    write_psd(psd_path, pil_image)
    state.manager.note_source_written(meta.handoff_id)
    nodes.PhotoshopBridge._open_in_photoshop(state, meta, psd_path)
    return meta


def _resolve_ps_mode_diff_mask(
    state: _NodeState, node_id: str, pil_image: Image.Image, source_hash: str
) -> Any | None:
    """The PS-mode MASK precedence tier (PROTOCOL.md §6d, tier 1).

    Node-reuse semantics mirror :meth:`cpsb.nodes.PhotoshopBridge.execute`
    exactly: an active handoff whose recorded ``source_hash`` no longer
    matches the current input belongs to OLD pixels and is superseded before
    anything else happens (a legacy handoff with no recorded ``source_hash``
    is treated as matching, same documented choice as the bridge node). Once
    that's settled, three cases remain -- (a) the (possibly just-refreshed)
    active handoff already has an edit: derive the diff mask from it; (b) an
    active handoff exists but has no edit yet: it is already open and
    waiting, so this call does nothing further (never reopens Photoshop on a
    passthrough re-execution -- PROTOCOL.md §6d: "No auto-queue... this node
    has no re-run mode", the same reasoning that keeps the bridge node's
    non-blocking modes from reopening on every re-run); (c) no active handoff
    at all: create one and open Photoshop, non-blocking.

    Args:
        state: The configured backend state.
        node_id: This node instance's ``unique_id``, stringified.
        pil_image: The current input, decoded.
        source_hash: :func:`cpsb.handoff.compute_source_hash` of *pil_image*.

    Returns:
        A ``(H, W)`` float32 0/1 numpy array, or ``None`` when there is no
        edit to derive one from yet (cases (b) and (c) above, or a
        same-size-mismatch inside :func:`_compute_diff_mask`) -- the caller
        falls back to the next MASK precedence tier.
    """
    manager = state.manager
    active = manager.find_active_for_node(node_id)

    if active is not None and active.source_hash is not None and active.source_hash != source_hash:
        logger.info(
            "cpsb annotate: node %s: input changed, superseding handoff %s",
            node_id,
            active.handoff_id,
        )
        manager.supersede(active.handoff_id)
        active = None

    if active is not None and active.edits:
        return _compute_diff_mask(manager, active.handoff_id, pil_image)

    if active is None:
        logger.info("cpsb annotate: node %s: no active handoff, creating and opening", node_id)
        _create_handoff_and_open(state, node_id, pil_image, source_hash)
    else:
        logger.info(
            "cpsb annotate: node %s handoff %s: already open, not reopening",
            node_id,
            active.handoff_id,
        )
    return None


def _fold_edit_hash_for_is_changed(
    manager: HandoffManager, node_id: str, source_hash: str
) -> str | None:
    """The latest-edit hash to fold into ``IS_CHANGED``, if any (PROTOCOL.md §6d)."""
    active = manager.find_active_for_node(node_id)
    if (
        active is not None
        and (active.source_hash is None or active.source_hash == source_hash)
        and active.edits
    ):
        return manager.latest_edit_hash(active.handoff_id)
    return None


class PhotoshopAnnotate:
    """Pairs a typed instruction with a region MASK for a downstream model (PROTOCOL.md §6d).

    Outputs ``(IMAGE, MASK, STRING, IMAGE)``: the first ``IMAGE`` is always
    the unchanged input (this node never modifies the pixels it was given,
    only derives a mask and, optionally, a SEPARATE annotated copy); the
    ``STRING`` is *instruction* verbatim, never rendered onto any pixels
    (the whole point -- "not having to lay out text on an image",
    ``research/research-annotate-node.md`` §0); the second ``IMAGE``
    (*annotated*) is a copy with a red box drawn at the resolved mask's
    bounding box when *box_composite* is ``True`` (the Kontext/Qwen-Image-Edit
    box-annotation convention), else the same unchanged input again.

    MASK resolution precedence (PROTOCOL.md §6d), first match wins:

    1. **Photoshop diff mask** -- only when *annotate_mode* is
       :data:`AnnotateMode.PS_MODE` and this node's active handoff (matched
       by ``source_hash``) has at least one edit: the pixel difference
       between what was sent and what came back, closed and hole-filled
       (:func:`_compute_diff_mask`). Works with ANY Photoshop tool/color --
       nothing here inspects layers or channels.
    2. **The `mask` input socket** -- whatever a ComfyUI-only source (a
       MaskEditor, a segmentation node, ...) already provided.
    3. **All-zero**, sized to *image*.

    Pass-through mode (:data:`AnnotateMode.PASS_THROUGH`, the default) never
    even looks up a handoff -- tier 1 is entirely gated on PS mode, so this
    mode never touches Photoshop at all, exactly as PROTOCOL.md §6d
    specifies. PS mode with no matching edit yet writes a ``bridge_node``
    handoff and opens Photoshop non-blocking (fire-and-forget, identical to
    the bridge node's "Open only" mode -- see
    :func:`_create_handoff_and_open`), then returns passthrough outputs; the
    user marks up the image and saves, and the NEXT queue derives the diff
    mask from that save. There is no re-run mode and so no auto-queue
    (PROTOCOL.md §5/§6d) -- the user re-queues manually once they've saved.
    """

    CATEGORY = "image/photoshop"
    RETURN_TYPES = ("IMAGE", "MASK", "STRING", "IMAGE")
    # Purely cosmetic (two IMAGE sockets would otherwise both just say
    # "IMAGE" in the graph) -- ComfyUI does not require RETURN_NAMES to
    # match RETURN_TYPES's length semantics any differently.
    RETURN_NAMES = ("image", "mask", "instruction", "annotated")
    FUNCTION = "execute"

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, Any]:
        return {
            "required": {
                "image": ("IMAGE",),
                "instruction": ("STRING", {"multiline": True, "default": ""}),
                "annotate_mode": (
                    [AnnotateMode.PASS_THROUGH, AnnotateMode.PS_MODE],
                    {"default": AnnotateMode.PASS_THROUGH},
                ),
                "box_composite": ("BOOLEAN", {"default": False}),
            },
            "optional": {
                "mask": ("MASK",),
            },
            "hidden": {
                "unique_id": "UNIQUE_ID",
            },
        }

    @classmethod
    def IS_CHANGED(
        cls,
        image: Any,
        instruction: str,
        annotate_mode: str,
        box_composite: bool,
        unique_id: str,
        mask: Any = None,
    ) -> str:
        """Hash of every input, folded with the latest-edit hash when consumable.

        Base hash covers *image* (its :func:`cpsb.handoff.compute_source_hash`,
        the same PNG-encoding hash a handoff's own identity is keyed on),
        *instruction*, *annotate_mode*, *box_composite*, and whether *mask*
        is connected at all -- every input that can change this node's
        output on its own, without any Photoshop round trip (PROTOCOL.md
        §6d: "image+instruction+mask-presence+params hash"). When
        *annotate_mode* is PS mode AND this node's active handoff
        (source_hash-matched) already has an edit, that edit's own hash is
        folded in too, so an arriving Photoshop save forces re-execution the
        same way an arriving bridge-node edit does (the "standard" pattern,
        PROTOCOL.md §6/§6c) -- gated to PS mode specifically so a stale
        handoff left over from a since-switched-away-from PS-mode run never
        causes a pass-through execution to needlessly re-fire.
        """
        pil_image = nodes._tensor_to_pil(image)
        image_hash = compute_source_hash(pil_image)
        params_blob = f"{instruction}|{annotate_mode}|{bool(box_composite)}|{mask is not None}"
        base = hashlib.sha256(f"{image_hash}:{params_blob}".encode()).hexdigest()

        if annotate_mode != AnnotateMode.PS_MODE:
            return base

        manager = nodes._require_state().manager
        edit_hash = _fold_edit_hash_for_is_changed(manager, str(unique_id), image_hash)
        return f"{base}:{edit_hash}" if edit_hash is not None else base

    def execute(
        self,
        image: Any,
        instruction: str,
        annotate_mode: str,
        box_composite: bool,
        unique_id: str,
        mask: Any = None,
    ) -> tuple[Any, Any, str, Any]:
        """``(IMAGE, MASK, STRING, IMAGE)`` per the class docstring's precedence rules.

        Args:
            image: The ``IMAGE`` input tensor.
            instruction: The typed instruction, returned verbatim.
            annotate_mode: One of :class:`AnnotateMode`'s two values.
            box_composite: Whether to draw a red box on the *annotated*
                output.
            unique_id: This node instance's id (ComfyUI's hidden
                ``UNIQUE_ID`` input), used to key its handoff lookup.
            mask: The optional ``MASK`` input socket.

        Returns:
            ``(image, mask, instruction, annotated)`` -- *image* is always
            the same tensor object passed in.
        """
        state = nodes._require_state()
        node_id = str(unique_id)
        logger.info(
            "cpsb annotate: node %s: execute() starting (mode=%r)", node_id, annotate_mode
        )

        pil_image = nodes._tensor_to_pil(image)
        source_hash = compute_source_hash(pil_image)

        diff_mask_array = None
        if annotate_mode == AnnotateMode.PS_MODE:
            diff_mask_array = _resolve_ps_mode_diff_mask(state, node_id, pil_image, source_hash)

        mask_array = self._resolve_mask_array(diff_mask_array, mask, pil_image)
        mask_tensor = _array_to_mask_tensor(mask_array)
        annotated = self._build_annotated(image, mask_array, bool(box_composite))

        return image, mask_tensor, instruction, annotated

    @staticmethod
    def _resolve_mask_array(diff_mask_array: Any | None, mask_socket: Any, pil_image: Image.Image):
        """MASK precedence tiers 2-3 (PROTOCOL.md §6d) -- tier 1 is resolved by the caller.

        Args:
            diff_mask_array: Tier 1's result, or ``None`` if unavailable/not
                applicable.
            mask_socket: The node's optional ``mask`` input tensor, or
                ``None`` if unconnected.
            pil_image: The current input image, decoded (for zero-mask
                sizing only).

        Returns:
            A ``(H, W)`` float32 numpy array.
        """
        if diff_mask_array is not None:
            return diff_mask_array
        if mask_socket is not None:
            return _mask_tensor_to_array(mask_socket)
        import numpy as np

        width, height = pil_image.size
        return np.zeros((height, width), dtype=np.float32)

    @staticmethod
    def _build_annotated(image_tensor: Any, mask_array: Any, box_composite: bool) -> Any:
        """The *annotated* output (PROTOCOL.md §6d).

        Returns *image_tensor* completely unchanged -- the exact same
        tensor object, not a re-encoded copy -- whenever *box_composite* is
        ``False`` or the final mask has no nonzero pixels ("mask empty ->
        annotated = original unmodified"). Otherwise draws a 4px pure-red
        rectangle, unfilled, at the mask's bounding box on a fresh RGB copy.

        Args:
            image_tensor: The node's own input tensor (returned as-is when
                no box is drawn).
            mask_array: The final, precedence-resolved ``(H, W)`` mask.
            box_composite: The ``box_composite`` widget value.

        Returns:
            A tensor: either *image_tensor* itself, or a freshly encoded
            copy with the box drawn on it.
        """
        if not box_composite:
            return image_tensor
        bbox = _bbox_of_nonzero(mask_array)
        if bbox is None:
            return image_tensor

        pil_image = nodes._tensor_to_pil(image_tensor).convert("RGB")
        annotated = pil_image.copy()
        ImageDraw.Draw(annotated).rectangle(bbox, outline=_BOX_COLOR, width=_BOX_STROKE_WIDTH)
        return nodes._pil_to_tensor(annotated)
