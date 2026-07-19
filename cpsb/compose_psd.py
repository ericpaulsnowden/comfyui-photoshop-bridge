"""The ``PhotoshopComposePSD`` ComfyUI node (PROTOCOL.md §6c).

Takes N ``IMAGE`` inputs (``image_1``, ``image_2``, ...) and writes them as a
single group of pixel layers into one PSD, using ``psd-tools``' documented
construction API (``PSDImage.new`` -> ``create_pixel_layer`` -> ``create_group``,
research/research-multilayer-compose.md §1.1/§3). Canvas size is the max
width/height across every connected input; each layer keeps its own native
resolution (never rescaled) and is centered on the shared canvas;
``image_1`` becomes the BOTTOM layer, higher indices stack on top, and every
layer lands inside one named, expanded group. The ``mode`` COMBO
(PROTOCOL.md §6c) then mirrors the "Edit in Photoshop" bridge node's three
behaviors, applied to the freshly-written LAYERED file so the user
composites/adjusts LAYERS in Photoshop and the node outputs the SAVED result
flattened: "Wait for first save" (the default) BLOCKS ``execute()`` until the
first save then continues with that edit; "Re-run on every save" opens
Photoshop, passes the flat composite through, and relies on the frontend
auto-queueing a re-run per save (each consuming the latest edit); "Don't open
(composite only)" is the old always-flat behavior that never opens Photoshop.
This replaces the earlier fire-and-forget ``edit_after`` BOOLEAN as a
pre-release breaking change (the product owner's "doesn't make sense" call:
the useful flow is the blocking stop-open-edit-continue one, now the default).

**psd-tools group-write API, verified empirically against the installed
1.17.4** (not taken from docs/tests alone -- research-multilayer-compose.md
§1.1 flags this as UNCONFIRMED-until-spiked; this module's build closes that
gap the same way :mod:`cpsb.psd_io` closes its own psd-tools claims, per that
module's own docstring precedent): a throwaway script built a 3-layer group
(one input carrying a genuine alpha channel), saved it, reopened it with a
FRESH ``PSDImage.open()`` (not the same in-memory object), and asserted: the
top-level document holds exactly one child, of ``kind == "group"``, with the
requested ``name``; the group holds exactly the 3 layers, in insertion
order (bottom-to-top, matching ``layer_list`` order to
:func:`~psd_tools.api.psd_image.PSDImage.create_group`); each layer's
reopened ``.bbox`` matches the centered offset it was created with, including
under ODD canvas/image dimensions (floor-division centering, e.g. a 7x9
canvas against a 4x4 layer reopens at ``bbox=(1, 2, 5, 6)`` exactly);
``.composite()`` at both a corner covered by only the bottom layer and the
center covered by the top layer returned the expected colors (compositing
correctness, not just structural presence); a single-layer (N=1) group and
an eight-layer (N=8) group both round-tripped with correct order and count;
``PSDImage.save()`` accepts a ``pathlib.Path`` directly (not just ``str``).
Group masks (``group.create_mask()``, research §1.2) were deliberately NOT
exercised or used here -- research flags that specific call as untested by
psd-tools' own suite on ``Group`` and recommends v1 skip it; this module's
MASK output is derived from the overall composite's alpha instead (see
:func:`_flatten_placements`), never a PSD-level group mask.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from PIL import Image
from psd_tools import PSDImage
from psd_tools.constants import ColorMode

from . import nodes, routes
from .context import CpsbContext
from .handoff import HandoffManager, HandoffMeta, SourceRef, WaitOutcome, compute_source_hash

logger = logging.getLogger("cpsb")

#: Upper bound on the ``image_N`` optional inputs :meth:`PhotoshopComposePSD.
#: INPUT_TYPES` declares (PROTOCOL.md §6c: "the frontend only shows
#: connected+1"). A generous static ceiling, not a real limit on how many
#: layers a document can hold -- the frontend (``web/cpsb/compose.js``)
#: reveals sockets one at a time as each is connected, so a user only ever
#: sees this many at once if they connect all of them.
MAX_IMAGE_INPUTS = 20

DEFAULT_FILENAME_PREFIX = "compose"
DEFAULT_GROUP_NAME = "ComfyUI Layers"

#: Base name for the pixel layers; each layer is ``"<layer_name> <index>"`` with
#: index counting 1..N bottom-to-top. Replaces the removed ``filename_prefix``
#: widget (which only named an intermediate file the user never saw — Photoshop
#: opens a managed PSD copy, not that file).
DEFAULT_LAYER_NAME = "Layer"

#: The Compose-node-specific third ``mode`` string (PROTOCOL.md §6c). The
#: other two options reuse :class:`cpsb.nodes.BridgeMode`'s constants verbatim
#: (``WAIT_FIRST_SAVE`` / ``RERUN_EVERY_SAVE``), but this one is deliberately
#: NOT ``BridgeMode.OPEN_ONLY``: that bridge string is "Open only (don't wait)"
#: and its meaning is "fire-and-forget open, then pass through", whereas this
#: node's third mode is "never open Photoshop at all" (the old always-flat
#: ``edit_after=False`` behavior). Different text, different behavior -- so it
#: is its own constant rather than an alias of the bridge one.
MODE_DONT_OPEN = "Don't open (composite only)"

#: Per-layer opacity written into the PSD (PROTOCOL.md §6c says nothing
#: about partial opacity, so every layer is fully opaque -- the visible
#: overlap between layers is purely a function of stacking order and each
#: image's own bounding box, not blending).
_LAYER_OPACITY = 255

#: Extensions the ``existing_psd`` combo lists / ``existing_psd_path``
#: accepts for the "append to existing document" feature. Identical to
#: :data:`cpsb.load_psd.PSD_EXTENSIONS`, deliberately duplicated by hand
#: rather than imported -- ``cpsb/load_psd.py`` is owned by another change
#: in flight, and this is the same short, stable, two-entry-tuple
#: hand-mirroring convention that module's own docstring already
#: establishes for ``cpsb.routes._PSD_NATIVE_EXTENSIONS``.
_PSD_EXTENSIONS: tuple[str, ...] = (".psd", ".psb")


def _collect_connected_images(kwargs: dict[str, Any]) -> list[Any]:
    """The connected ``image_N`` tensors from *kwargs*, in index order.

    Args:
        kwargs: The node call's keyword arguments -- everything beyond the
            declared required/hidden inputs, i.e. whichever ``image_N``
            optional sockets ComfyUI actually connected for this execution.

    Returns:
        Tensors in ascending ``N`` order (``image_1`` first). Only indices
        ``1..MAX_IMAGE_INPUTS`` are ever considered, matching what
        :meth:`PhotoshopComposePSD.INPUT_TYPES` declares.
    """
    images = []
    for index in range(1, MAX_IMAGE_INPUTS + 1):
        image = kwargs.get(f"image_{index}")
        if image is not None:
            images.append(image)
    return images


#: Default cap on how many images become PSD layers (the `max_layers` widget).
#: Generous enough that ordinary batches (a handful of VAE-decoded images, plus
#: a few separate sockets) never truncate, while still bounding a runaway batch
#: (e.g. a long video decode) so it can't silently produce a thousand-layer PSD.
DEFAULT_MAX_LAYERS = 64


def _array_to_pil(array: Any) -> Image.Image:
    """A single uint8 HWC (or HW) array as a PIL image, mode matched to channels.

    The channel count -- not a hardcoded ``"RGB"`` -- decides the PIL mode, so a
    4-channel (RGBA) frame is never reinterpreted as a 3-byte-per-pixel RGB
    buffer (that byte-misalignment tiled/shifted every pixel past (0,0) into
    noise -- the exact symptom layer-decomposition models like "Qwen Image
    Layered Control", which emit RGBA, produced). ALPHA IS PRESERVED: a
    4-channel frame becomes an ``"RGBA"`` PIL so the downstream PSD layer carries
    real per-pixel transparency (:func:`_build_group_psd`).

    Args:
        array: A uint8 numpy array, either 2-D ``(H, W)`` (grayscale) or 3-D
            ``(H, W, C)`` with ``C`` channels.

    Returns:
        * ``ndim == 2`` or ``C == 1`` -> grayscale ``"L"`` -> ``.convert("RGB")``.
        * ``C == 2`` -> first channel as grayscale -> ``.convert("RGB")`` (a
          rare layout; treat band 0 as luminance, drop the odd second band).
        * ``C == 3`` -> ``"RGB"`` (the normal-VAE path, unchanged).
        * ``C >= 4`` -> ``"RGBA"`` from the first four channels (extras dropped).
    """
    import numpy as np

    if array.ndim == 2:
        return Image.fromarray(array, mode="L").convert("RGB")

    channels = array.shape[-1]
    if channels == 1:
        return Image.fromarray(array[..., 0], mode="L").convert("RGB")
    if channels == 2:
        return Image.fromarray(array[..., 0], mode="L").convert("RGB")
    if channels == 3:
        return Image.fromarray(array, mode="RGB")
    # 4+ channels: keep RGBA (a mismatched mode is what corrupted the buffer).
    # ascontiguousarray because slicing off any extra channels yields a
    # non-contiguous view Image.fromarray cannot read directly.
    return Image.fromarray(np.ascontiguousarray(array[..., :4]), mode="RGBA")


def _tensor_frames_to_pils(image: Any) -> list[Image.Image]:
    """Every frame of a ComfyUI ``IMAGE`` tensor (NHWC float32 [0,1]) as PIL images.

    A ComfyUI ``IMAGE`` is a BATCH: a VAE Decode (or any node) can emit several
    images on a single socket. Unlike :func:`cpsb.nodes._tensor_to_pil` (which
    keeps only the first frame), this expands the whole batch so each image
    becomes its own PSD layer (PROTOCOL.md §6c: multi-image batches -> layers).

    Each frame's PIL mode is matched to its channel count by
    :func:`_array_to_pil` rather than forced to ``"RGB"`` -- so a 4-channel
    (RGBA) frame from a layer-decomposition model is expanded correctly (and
    keeps its alpha) instead of being garbled by an RGB reinterpretation of a
    4-byte-per-pixel buffer.
    """
    import numpy as np

    frames: list[Image.Image] = []
    for frame in image:  # iterate the leading batch dimension
        array = frame.cpu().numpy() if hasattr(frame, "cpu") else np.asarray(frame)
        # Round-to-nearest (not truncate) so a channel's float value maps to the
        # nearest 8-bit level; solid colors are unaffected (255.0/0.0 are exact).
        array = np.clip(array * 255.0, 0, 255).round().astype(np.uint8)
        frames.append(_array_to_pil(array))
    return frames


def _collect_layer_images(
    kwargs: dict[str, Any], max_layers: int
) -> tuple[list[Image.Image], int]:
    """Expand every connected ``image_N`` socket's batch into layer images.

    Order is ``image_1``..``image_N`` (bottom-to-top), and within each socket
    the batch's own frame order. The total is capped at *max_layers* -- only the
    first *max_layers* images become layers, and frames past the cap are counted
    but never decoded (a huge batch is sliced before conversion, so it can't
    blow up memory just to be dropped).

    Returns ``(pil_images, total_available)`` where *total_available* is how many
    images existed across all sockets before the cap, so the caller can tell the
    user when it truncated.
    """
    pil_images: list[Image.Image] = []
    total_available = 0
    for tensor in _collect_connected_images(kwargs):
        total_available += len(tensor)
        remaining = max_layers - len(pil_images)
        if remaining > 0:
            pil_images.extend(_tensor_frames_to_pils(tensor[:remaining]))
    return pil_images, total_available


def _sanitize_filename_prefix(raw: str) -> str:
    """Reduce ``filename_prefix`` to a single safe filename component.

    A user-controlled ``STRING`` widget feeds directly into a path under
    :attr:`~cpsb.context.CpsbContext.input_dir`
    (:func:`_allocate_output_path`), so a path separator or parent
    reference must never be allowed through -- the same defense-in-depth
    concern :func:`cpsb.context.sanitize_managed_name` addresses for the
    managed-folder setting, reimplemented locally here rather than imported:
    that helper sanitizes a whole path SEGMENT (folder name), this one a
    filename PREFIX that is always followed by ``_%05d.psd``, a narrower and
    slightly different contract not worth coupling the two modules over.

    Args:
        raw: The node's ``filename_prefix`` widget value, as received.

    Returns:
        A prefix safe to interpolate into ``f"{prefix}_{index:05d}.psd"``:
        never empty, never containing ``/`` or ``\\``, never ``.``/``..``.
    """
    name = (raw or "").strip()
    if not name or name in (".", ".."):
        return DEFAULT_FILENAME_PREFIX
    name = name.replace("/", "_").replace("\\", "_")
    return name or DEFAULT_FILENAME_PREFIX


def _compute_identity_hash(
    pil_images: list[Image.Image],
    group_name: str,
    layer_name: str = DEFAULT_LAYER_NAME,
    append_to_existing: bool = False,
    append_target: str = "",
) -> str:
    """The handoff's source IDENTITY -- deliberately mode/prefix-FREE.

    This is what a ``bridge_node`` handoff's ``source_hash`` is now recorded
    as (:meth:`PhotoshopComposePSD._create_bridge_handoff`) and what
    :meth:`PhotoshopComposePSD.execute`'s reuse/supersede check compares
    against, matching :func:`cpsb.handoff.compute_source_hash`'s own
    pixels-only contract (the same one :class:`cpsb.nodes.PhotoshopBridge`
    and :mod:`cpsb.annotate` key their own supersede-on-changed-input checks
    on). It intentionally does NOT include ``mode`` or ``filename_prefix`` --
    :func:`_compute_inputs_hash` used to fold ``mode`` into the very value
    recorded as ``source_hash``, which meant merely flipping the ``mode``
    widget (e.g. "Re-run on every save" -> "Wait for first save") with
    otherwise-identical pixels changed the handoff's identity: the already-
    open handoff could never match again, so a second handoff -- and a
    second live Photoshop document -- got created and the first was left
    dangling (the confirmed "spins forever" / "slew of new documents" bug).
    Hashing only the pixels + the structural params that actually change the
    written PSD's own content (``group_name``, ``layer_name``,
    ``append_to_existing``/``append_target``) keeps a mode flip, or a
    filename-prefix edit, from stranding an in-progress edit.

    ``append_to_existing``/``append_target`` ARE part of this identity
    (unlike ``mode``/``filename_prefix``): appending is on or off, and (when
    on) which document it writes into, both genuinely change what this run
    WOULD produce on disk -- toggling either must supersede any handoff
    whose recorded identity predates the change, exactly like a genuinely
    different connected image would, rather than being silently ignored by
    the reuse check the way a mere mode flip is. See
    :meth:`PhotoshopComposePSD.execute`'s "duplicate-append" docstring
    section for the full reasoning this feeds.

    Args:
        pil_images: The connected inputs, already decoded to PIL, in
            ``image_1..image_N`` order.
        group_name: The ``group_name`` widget value (changes the written
            PSD's group name, so it is part of the document's identity).
        layer_name: The ``layer_name`` widget value (changes every layer's
            name for the same reason).
        append_to_existing: The ``append_to_existing`` widget value.
        append_target: :func:`_append_target_key` of the current
            ``existing_psd``/``existing_psd_path`` widget values -- empty
            when *append_to_existing* is ``False`` (the target is
            irrelevant then, so it must not perturb the identity).

    Returns:
        A 64-char lowercase hex sha256 digest.
    """
    hasher = hashlib.sha256()
    for image in pil_images:
        hasher.update(compute_source_hash(image).encode("ascii"))
    hasher.update(group_name.encode("utf-8"))
    hasher.update(b"\x00")
    hasher.update(layer_name.encode("utf-8"))
    hasher.update(b"\x00")
    hasher.update(b"\x01" if append_to_existing else b"\x00")
    hasher.update(b"\x00")
    hasher.update(append_target.encode("utf-8"))
    return hasher.hexdigest()


def _compute_inputs_hash(
    pil_images: list[Image.Image],
    filename_prefix: str,
    group_name: str,
    mode: str,
    layer_name: str = DEFAULT_LAYER_NAME,
    append_to_existing: bool = False,
    append_target: str = "",
) -> str:
    """A deterministic sha256 identity for "these inputs, these params, this mode".

    Used ONLY by :meth:`PhotoshopComposePSD.IS_CHANGED` as the base value
    that forces ComfyUI to re-execute this node when ANYTHING relevant
    changes -- pixels, ``filename_prefix``, ``group_name``, ``layer_name``,
    ``mode``, ``append_to_existing``, or the append target
    (switching any of these genuinely changes what ``execute()`` does even
    for pixel-identical inputs, so each must be folded in HERE). This is
    deliberately a DIFFERENT value from :func:`_compute_identity_hash` --
    see that function's docstring for why a handoff's recorded
    ``source_hash`` must NOT include ``mode``/``filename_prefix`` while this
    ``IS_CHANGED`` value still needs to.

    Built on top of :func:`_compute_identity_hash` (same pixel/group/layer/
    append hashing) rather than duplicating it, with ``filename_prefix`` and
    ``mode`` folded in afterward.

    Note this is NOT literally "sha256 of the PSD bytes that would be
    written" -- computing that would require re-serializing a full PSD on
    every ``IS_CHANGED`` call (expensive, and psd-tools' write path is not
    guaranteed byte-for-byte deterministic run to run) just to test
    equality. Hashing the inputs directly is cheap and deterministic by
    construction.

    Args:
        pil_images: The connected inputs, already decoded to PIL, in
            ``image_1..image_N`` order.
        filename_prefix: The (already-:func:`_sanitize_filename_prefix`'d)
            filename prefix.
        group_name: The ``group_name`` widget value.
        mode: The ``mode`` widget value (one of :data:`MODE_DONT_OPEN`,
            :attr:`cpsb.nodes.BridgeMode.WAIT_FIRST_SAVE`, or
            :attr:`cpsb.nodes.BridgeMode.RERUN_EVERY_SAVE`).
        append_to_existing: The ``append_to_existing`` widget value.
        append_target: :func:`_append_target_key` of the current
            ``existing_psd``/``existing_psd_path`` widget values.

    Returns:
        A 64-char lowercase hex sha256 digest.
    """
    identity_hash = _compute_identity_hash(
        pil_images, group_name, layer_name, append_to_existing, append_target
    )
    hasher = hashlib.sha256()
    hasher.update(identity_hash.encode("ascii"))
    hasher.update(b"\x00")
    hasher.update(filename_prefix.encode("utf-8"))
    hasher.update(b"\x00")
    hasher.update(mode.encode("utf-8"))
    return hasher.hexdigest()


def _find_matching_active_handoff(
    manager: HandoffManager, node_id: str, identity_hash: str
) -> HandoffMeta | None:
    """The active ``bridge_node`` handoff for *node_id*, iff it has a consumable edit.

    Mirrors :meth:`cpsb.nodes.PhotoshopBridge.execute`'s consume predicate
    (same shape, deliberately re-implemented rather than imported --
    ``cpsb/nodes.py`` is owned elsewhere and keeps its check inline; a small
    parallel helper here couples nothing to that module's internals). Used by
    :meth:`PhotoshopComposePSD.IS_CHANGED` (:meth:`PhotoshopComposePSD.execute`
    has its own inline reuse/supersede logic, since unlike this read-only
    helper it must be able to mutate state via ``manager.supersede``).

    The ``origin_kind == "bridge_node"`` filter matches what this node's own
    open paths now write (:meth:`PhotoshopComposePSD._create_bridge_handoff`,
    PROTOCOL.md §6c: "The handoff uses origin_kind ``bridge_node``"), so the
    just-opened handoff -- and any edit saved into it -- is recognized on the
    next queue and its edit consumed. It replaces the earlier ``load_psd``
    filter the fire-and-forget ``edit_after`` build used.

    Args:
        manager: The handoff manager.
        node_id: This node instance's ``unique_id``, stringified.
        identity_hash: :func:`_compute_identity_hash` of the CURRENT inputs --
            deliberately mode/prefix-FREE (see that function's docstring),
            since a handoff's recorded ``source_hash`` is now identity-only
            too.

    Returns:
        The matching handoff, or ``None`` when there is no active handoff for
        this node, it isn't ``origin_kind == "bridge_node"`` (defensive --
        e.g. a leftover handoff of another kind for the same node id), its
        recorded ``source_hash`` doesn't equal *identity_hash* (the actual
        inputs changed), or it has no edits yet -- each meaning "write a
        fresh compose instead".
    """
    active = manager.find_active_for_node(node_id)
    if (
        active is not None
        and active.origin_kind == "bridge_node"
        and active.source_hash == identity_hash
        and active.edits
    ):
        return active
    return None


def _consume_active_edit(manager: HandoffManager, handoff_id: str) -> tuple[Any, Any] | None:
    """``(IMAGE, MASK)`` tensors for *handoff_id*'s latest edit, if any.

    Thin file-resolution wrapper around
    :func:`cpsb.nodes._tensors_from_edit_file` -- the shared MASK-derivation
    logic itself lives there, identical to
    :class:`~cpsb.nodes.PhotoshopBridge`'s and
    :class:`~cpsb.load_psd.PhotoshopLoadPSD`'s own consume paths.

    Args:
        manager: The handoff manager.
        handoff_id: A handoff already confirmed to have at least one edit.

    Returns:
        The edit's tensors, or ``None`` if the edit file has vanished from
        disk since (a filesystem race). Callers fall back to composing
        fresh, exactly as if no matching handoff existed at all.
    """
    edit_path = manager.edit_image_path(handoff_id)
    if edit_path is None or not edit_path.exists():
        return None
    return nodes._tensors_from_edit_file(edit_path)


def _centered_offset(item_size: int, canvas_size: int) -> int:
    """Floor-division centering offset, matching ComfyUI-core's own convention.

    Identical to the offset math ``comfy_extras/nodes_images.py``'s
    ``ImageStitch``/``ResizeAndPadImage`` use for their own no-rescale
    centering (research-multilayer-compose.md §3), cited here rather than
    re-derived: ``(canvas_size - item_size) // 2``. For an item exactly as
    large as the canvas (the N=1 case, or the single input that itself
    determined the canvas's max dimension), this returns 0.
    """
    return (canvas_size - item_size) // 2


def _compute_placements(
    pil_images: list[Image.Image], canvas_width: int, canvas_height: int
) -> list[tuple[Image.Image, int, int]]:
    """``(layer_image, left, top)`` per image, centered against a canvas.

    The pure layout math (alpha-preserving mode handling + centering),
    independent of psd-tools/any ``PSDImage`` -- shared by
    :func:`_create_layers_in_psd` (which ALSO creates the real pixel
    layers) and :meth:`PhotoshopComposePSD.execute`'s duplicate-append
    guard (which needs this run's own flatten for the IMAGE/MASK outputs
    but must NOT write anything to disk -- see the class docstring's
    "duplicate-append avoidance" section).

    Args:
        pil_images: Decoded inputs, in ``image_1..image_N`` order.
        canvas_width: Canvas width to center against.
        canvas_height: Canvas height to center against.

    Returns:
        ``(layer_image, left, top)`` per image, in the same order --
        *layer_image* keeps RGB/RGBA as-is (alpha preserved) and converts
        anything else (the old unconditional ``.convert("RGB")`` dropped
        every alpha).
    """
    placements: list[tuple[Image.Image, int, int]] = []
    for image in pil_images:
        layer_image = image if image.mode in ("RGB", "RGBA") else image.convert("RGB")
        left = _centered_offset(layer_image.width, canvas_width)
        top = _centered_offset(layer_image.height, canvas_height)
        placements.append((layer_image, left, top))
    return placements


def _layers_batch_tensor(
    placements: list[tuple[Image.Image, int, int]], canvas_width: int, canvas_height: int
) -> Any:
    """The ``layers`` output: an IMAGE batch, one frame per placed layer.

    Product-owner request 2026-07-18: "when I connect this node to a preview
    node so I can see all of the layers it only shows one image" -- the IMAGE
    output is (correctly) the single flattened composite, so a Preview node
    can only ever show that one frame. This batch is the per-layer view: frame
    *i* is layer *i* alone, placed at its real position on the shared canvas
    (same :func:`_compute_placements` result the PSD itself was written from),
    so a Preview/Save node fans out to one image per layer.

    Every frame must share one size to be batchable, which the shared canvas
    already guarantees. A layer's own alpha is composited onto opaque black --
    ComfyUI's IMAGE tensors are RGB, and black is the conventional flatten
    (matching how a transparent region reads elsewhere in this pack's flat
    outputs).

    Args:
        placements: ``(layer_image, left, top)`` per layer, bottom-to-top --
            the exact list the write path produced.
        canvas_width: The run's canvas width (fresh-build max-of-inputs, or
            the append target's own fixed canvas).
        canvas_height: The run's canvas height.

    Returns:
        A ``(N, H, W, 3)`` float tensor, frame order = layer order
        (``image_1`` first).
    """
    import torch

    frames = []
    for layer_image, left, top in placements:
        canvas = Image.new("RGB", (canvas_width, canvas_height), (0, 0, 0))
        if layer_image.mode == "RGBA":
            canvas.paste(layer_image, (left, top), layer_image)
        else:
            canvas.paste(layer_image, (left, top))
        frames.append(nodes._pil_to_tensor(canvas))
    return torch.cat(frames, dim=0)


def _create_layers_in_psd(
    psd: PSDImage,
    pil_images: list[Image.Image],
    layer_name: str,
    canvas_width: int,
    canvas_height: int,
) -> tuple[list[Any], list[tuple[Image.Image, int, int]]]:
    """Create one pixel layer per *pil_images* entry directly on *psd*,
    centered against ``(canvas_width, canvas_height)`` (:func:`_compute_placements`).

    Factored out of :func:`_build_group_psd` so the identical per-layer
    creation logic (alpha-preserving mode handling, centering, naming,
    opacity) is shared with :func:`_append_run_into_psd` (the "append to
    existing document" feature) rather than duplicated -- the two differ
    only in WHERE ``canvas_width``/``canvas_height`` come from (a fresh
    document sized to the inputs themselves, vs. an existing target's own
    fixed, un-resizable canvas), not in how a layer gets placed once a
    canvas is chosen.

    Args:
        psd: The (already-created or already-opened) document to add layers
            to. Not saved by this function.
        pil_images: Decoded inputs, in ``image_1..image_N`` order.
        layer_name: Base name; layers are named ``"<layer_name> <index>"``,
            1-indexed.
        canvas_width: Canvas width to center against.
        canvas_height: Canvas height to center against.

    Returns:
        ``(layers, placements)`` -- *layers* bottom-to-top, ready for
        ``psd.create_group``; *placements* is ``(layer_image, left, top)``
        per layer, reused by :func:`_flatten_placements` so the IMAGE/MASK
        outputs are derived from the exact same positions just written to
        disk rather than by re-reading the file back. Each *layer_image*
        keeps its own mode (``"RGBA"`` when the source carried alpha) so the
        flatten sees the transparency the PSD layers were written with.
    """
    placements = _compute_placements(pil_images, canvas_width, canvas_height)
    layers = []
    for index, (layer_image, left, top) in enumerate(placements, start=1):
        layer = psd.create_pixel_layer(
            layer_image, name=f"{layer_name} {index}", top=top, left=left, opacity=_LAYER_OPACITY
        )
        layers.append(layer)
    return layers, placements


def _build_group_psd(
    pil_images: list[Image.Image], group_name: str, layer_name: str = DEFAULT_LAYER_NAME
) -> tuple[PSDImage, int, int, list[tuple[Image.Image, int, int]]]:
    """Build the in-memory grouped PSD document (PROTOCOL.md §6c).

    Canvas is ``(max width, max height)`` across every input; each image is
    centered at its own native resolution (never rescaled) via
    :func:`_centered_offset`; ``pil_images[0]`` (``image_1``) becomes the
    bottom layer, later indices stack on top -- the exact order
    ``create_pixel_layer``/``create_group`` preserve, verified empirically
    against psd-tools 1.17.4 (this module's own docstring).

    Args:
        pil_images: Decoded inputs, in ``image_1..image_N`` order. Must be
            non-empty.
        group_name: Name for the single group every layer is placed inside.

    Returns:
        ``(psd, canvas_width, canvas_height, placements)`` -- *psd* is the
        unsaved :class:`~psd_tools.api.psd_image.PSDImage`; *placements* is
        ``(layer_image, left, top)`` per layer, bottom-to-top, reused by
        :func:`_flatten_placements` so the IMAGE/MASK outputs are derived
        from the exact same positions just written to disk rather than by
        re-reading the file back. Each *layer_image* keeps its own mode
        (``"RGBA"`` when the source carried alpha) so the flatten sees the
        transparency the PSD layers were written with.
    """
    canvas_width = max(image.width for image in pil_images)
    canvas_height = max(image.height for image in pil_images)
    # The document mode stays "RGB": an RGB-mode PSD holds RGB *layers* that each
    # carry their own per-pixel transparency (create_pixel_layer from an RGBA PIL
    # yields a transparent layer, verified empirically against psd-tools 1.17.4),
    # so this is NOT a place alpha would be flattened away.
    psd = PSDImage.new(mode="RGB", size=(canvas_width, canvas_height), depth=8)
    layers, placements = _create_layers_in_psd(
        psd, pil_images, layer_name, canvas_width, canvas_height
    )
    psd.create_group(layer_list=layers, name=group_name)
    return psd, canvas_width, canvas_height, placements


def _append_run_into_psd(
    psd: PSDImage, pil_images: list[Image.Image], run_group_name: str, layer_name: str
) -> tuple[list[tuple[Image.Image, int, int]], int, int]:
    """Add *pil_images* as a NEW top-level group onto *psd* -- the "append to
    existing document" feature's own write step (build brief items 1/6).
    Mutates *psd* in place; does NOT save (see :func:`_atomic_save` for why
    saving is a separate, deliberately atomic step).

    Verified empirically (this feature's own pre-build spike): a group
    added via ``psd.create_group(...)`` on an already-populated, REOPENED
    ``PSDImage`` lands AFTER every pre-existing top-level child in
    iteration order -- which is ABOVE them in the stack, exactly matching
    this module's own "later index stacks on top" convention
    (:func:`_build_group_psd`) -- and every pre-existing top-level group,
    its own layers, names, and bboxes are left completely untouched.

    Canvas is *psd*'s OWN existing ``(width, height)`` -- CONSTRAINT 1:
    psd-tools has no canvas-resize API, so appended layers are centered
    against the EXISTING canvas and simply clipped if they don't fit,
    never grow the document (callers warn when this clips --
    :func:`_log_canvas_mismatch_if_needed`).

    Args:
        psd: The already-opened (or freshly-created) target document.
        pil_images: This run's decoded inputs, in ``image_1..image_N`` order.
        run_group_name: This run's own group name (already run-numbered by
            :func:`_next_run_group_name`).
        layer_name: Base layer name for this run's own layers.

    Returns:
        ``(placements, canvas_width, canvas_height)`` -- *placements* covers
        ONLY this run's just-added layers (never any pre-existing document
        content), matching :func:`_build_group_psd`'s own return contract so
        :func:`_flatten_placements` can be reused unchanged; the canvas
        dims are *psd*'s existing, unchanged size.
    """
    canvas_width, canvas_height = psd.width, psd.height
    layers, placements = _create_layers_in_psd(
        psd, pil_images, layer_name, canvas_width, canvas_height
    )
    psd.create_group(layer_list=layers, name=run_group_name)
    return placements, canvas_width, canvas_height


def _flatten_placements(
    placements: list[tuple[Image.Image, int, int]], canvas_width: int, canvas_height: int
) -> Image.Image:
    """The deterministic flattened composite of exactly what was just written.

    Built directly from the same ``(image, left, top)`` placements
    :func:`_build_group_psd` used -- not by reopening and recompositing the
    saved file -- so this is a pure function of the node's own inputs and
    placement math (PROTOCOL.md §6c: "flatten via compositing the layers you
    just placed, deterministic"), with no dependency on psd-tools' own
    compositor or file I/O succeeding.

    Compositing is ALPHA-AWARE ("over" blending), bottom-to-top: each layer is
    placed on a transparent full-canvas frame and
    :func:`PIL.Image.alpha_composite`'d onto the accumulator, so a semi-
    transparent upper layer blends with what is below it rather than fully
    replacing it, and a fully-transparent layer region shows the layers beneath.
    A fully-opaque layer (an ``"RGB"`` source, the normal-VAE case) still
    overwrites its bbox exactly as before -- ``alpha_composite`` of an
    all-255-alpha layer is a plain overwrite -- so that path is unchanged.

    The resulting alpha channel is the composite's own accumulated coverage:
    255 wherever an opaque layer landed, partial where only semi-transparent
    pixels landed, 0 where no layer reached (possible when the max WIDTH and max
    HEIGHT come from different inputs, so no single image, nor necessarily their
    union, covers every corner). RGB under an alpha-0 pixel is black -- never
    accessed by a consumer that respects alpha, but a fixed, deterministic
    value rather than undefined.

    Args:
        placements: ``(layer_image, left, top)`` bottom-to-top, from
            :func:`_build_group_psd` (``layer_image`` may be ``"RGBA"``).
        canvas_width: Document width.
        canvas_height: Document height.

    Returns:
        An ``"RGBA"`` image, ready for
        :func:`cpsb.nodes._tensors_from_image` (its ``"A" in mode`` check is
        exactly what turns this alpha channel into the MASK output -- a fully-
        transparent region yields MASK 1 there, an opaque region MASK 0).
    """
    canvas = Image.new("RGBA", (canvas_width, canvas_height), (0, 0, 0, 0))
    for image, left, top in placements:
        layer = image if image.mode == "RGBA" else image.convert("RGBA")
        positioned = Image.new("RGBA", (canvas_width, canvas_height), (0, 0, 0, 0))
        positioned.paste(layer, (left, top))
        canvas = Image.alpha_composite(canvas, positioned)
    return canvas


def _allocate_output_path(input_dir: Path, filename_prefix: str) -> Path:
    """The next free ``<filename_prefix>_%05d.psd`` path under *input_dir*.

    Args:
        input_dir: ComfyUI's input directory (``CpsbContext.input_dir``).
            Created if missing.
        filename_prefix: Already-:func:`_sanitize_filename_prefix`'d prefix.

    Returns:
        A path that does not currently exist. A plain existence-check loop
        (not an atomic exclusive-create) is deliberately sufficient here,
        not a race condition worth defending against further: ``cpsb.nodes``'
        own module docstring establishes that ComfyUI serializes ALL node
        `execute()` calls, across the whole server, onto a single
        ``prompt_worker`` thread -- there is never genuine concurrent
        `execute()` traffic for this or any other node to race against.
    """
    input_dir.mkdir(parents=True, exist_ok=True)
    index = 1
    while True:
        candidate = input_dir / f"{filename_prefix}_{index:05d}.psd"
        if not candidate.exists():
            return candidate
        index += 1


# -----------------------------------------------------------------------
# "Append to existing document" (product owner brief verbatim: "a switch
# for existing document... writes into an existing psd as new layers... for
# generating multiple runs of images and storing the results into a single
# psd for review vs a slew of separate files"). Three new widgets on
# PhotoshopComposePSD.INPUT_TYPES (appended at the END of "required", so a
# saved workflow's existing widget VALUES -- matched by ComfyUI purely by
# POSITION -- are never silently reassigned to the wrong widget):
# `append_to_existing` (BOOLEAN, default False), `existing_psd` (a COMBO of
# .psd/.psb files already in ComfyUI's input dir, mirroring
# cpsb.load_psd.PhotoshopLoadPSD's own combo -- the ONLY file-picker
# mechanism available to a server-side ComfyUI node, and the one mechanism
# that still works when ComfyUI and Photoshop are on different machines, as
# in this project's own two-machine setup), and `existing_psd_path` (a
# STRING power-user override, used VERBATIM -- never resolved against the
# input dir -- whenever it is non-empty).
# -----------------------------------------------------------------------


def _list_psd_files(input_dir: Path) -> list[str]:
    """Sorted ``.psd``/``.psb`` filenames directly under *input_dir*.

    A byte-for-byte port of :func:`cpsb.load_psd._list_psd_files` (that
    module is owned by another change in flight -- this copies its logic
    rather than importing it, per this feature's own build brief). Flat,
    non-recursive, matching ``LoadImage.INPUT_TYPES``'s own directory
    listing (see ``cpsb.load_psd``'s module docstring for the verified
    upstream reference). Empty if *input_dir* doesn't exist yet -- never
    raises, so :meth:`PhotoshopComposePSD.INPUT_TYPES` stays introspectable
    without a live backend, the same tolerance
    :meth:`cpsb.load_psd.PhotoshopLoadPSD.INPUT_TYPES` already has.
    """
    if not input_dir.is_dir():
        return []
    return sorted(
        entry.name
        for entry in input_dir.iterdir()
        if entry.is_file() and entry.suffix.lower() in _PSD_EXTENSIONS
    )


def _append_target_key(existing_psd: str, existing_psd_path: str) -> str:
    """A cheap, non-validating identity string for the append target.

    Used ONLY as a cache-key ingredient
    (:meth:`PhotoshopComposePSD.IS_CHANGED` and
    :meth:`PhotoshopComposePSD.execute`'s own ``identity_hash`` -- the two
    MUST agree, or a handoff recorded under one value would never be found
    by the other's lookup) -- never for actual path resolution, which is
    :func:`_resolve_append_target`'s job and can raise on an invalid
    selection. This must never raise: ``IS_CHANGED`` runs on every graph
    submission, including with a currently-invalid
    ``existing_psd``/``existing_psd_path`` combination the user hasn't
    fixed yet.

    ``existing_psd_path`` wins when non-empty, mirroring
    :func:`_resolve_append_target`'s own precedence, so the cache key and
    the real resolution agree about WHICH input is "the target" even
    though only the real resolution actually validates it.
    """
    override = (existing_psd_path or "").strip()
    return override if override else (existing_psd or "").strip()


def _resolve_append_target(
    context: CpsbContext, existing_psd: str, existing_psd_path: str
) -> Path:
    """Resolve the real append-target path for ``append_to_existing`` mode.

    Mirrors :func:`cpsb.load_psd._resolve_psd_path`'s shape (validate
    suffix, resolve input-dir-relative, reject path traversal) but ADDS the
    ``existing_psd_path`` power-user override this feature's own brief
    calls for: a non-empty ``existing_psd_path`` is used VERBATIM
    (interpreted as a plain filesystem path, not resolved relative to the
    input directory, and not traversal-checked -- it is an explicit
    path the user typed, the same trust boundary every other STRING widget
    in this package already sits behind), taking priority over the
    ``existing_psd`` combo when non-empty.

    Args:
        context: The active backend context.
        existing_psd: The ``existing_psd`` COMBO's selected filename (bare,
            input-dir-relative, as listed by :func:`_list_psd_files`).
        existing_psd_path: The ``existing_psd_path`` STRING widget's raw
            value.

    Returns:
        The resolved path. This may point at a file that does NOT exist
        yet -- callers create it fresh in that case (build brief item 3,
        "point at a file that isn't there yet must be a first-run
        convenience, not an error"); this function's own job ends at "a
        safe, well-formed path", not "a path that exists".

    Raises:
        ValueError: *existing_psd_path* is non-empty but not a ``.psd``/
            ``.psb`` path; or *existing_psd* is empty (nothing selected and
            no override given); or *existing_psd* is not a ``.psd``/``.psb``
            filename; or *existing_psd* would resolve outside
            *context.input_dir* (only reachable via a crafted raw-API
            value -- a COMBO selection made through the real frontend
            widget is always one of :func:`_list_psd_files`'s own outputs).
    """
    override = (existing_psd_path or "").strip()
    if override:
        candidate = Path(override)
        if candidate.suffix.lower() not in _PSD_EXTENSIONS:
            raise ValueError(
                f"existing_psd_path must be a .psd/.psb file, got: {existing_psd_path!r}"
            )
        return candidate

    combo_value = (existing_psd or "").strip()
    if not combo_value:
        raise ValueError(
            "append_to_existing is enabled but no existing_psd file is selected "
            "and existing_psd_path is empty -- pick a file from existing_psd, or "
            "type a path into existing_psd_path"
        )
    if Path(combo_value).suffix.lower() not in _PSD_EXTENSIONS:
        raise ValueError(f"existing_psd must be a .psd/.psb file, got: {combo_value!r}")
    resolved = routes._resolve_source_path(context, combo_value, "", "input")
    if resolved is None:
        raise ValueError(f"existing_psd escapes the input directory: {combo_value!r}")
    return resolved


def _next_run_group_name(psd: PSDImage | None, group_name: str) -> str:
    """*group_name*, suffixed with the next run number (build brief item 6:
    "run-over-run grouping" -- each execution's layers get their own group
    so an accumulated review document stays navigable).

    ``"<group_name> 1"`` for the first run ever written into a target
    (including a brand-new one, ``psd=None``), ``"<group_name> 2"`` for the
    second, etc. -- one past the highest existing top-level GROUP in *psd*
    already named ``"<group_name> <N>"``.

    Deliberately matches ONLY the numbered form, not a bare ``group_name``
    with no trailing number: this scheme only ever WRITES numbered names
    itself, so a bare match must be something else (a user's own manually
    named group, or a document predating this feature) and is left alone
    rather than being treated as "run 0" and colliding with a future
    "run 1".

    Args:
        psd: The already-opened target document to scan, or ``None`` when
            the target doesn't exist yet (nothing to scan -- always run 1).
        group_name: The ``group_name`` widget's current value.

    Returns:
        The run-numbered group name for THIS execution's own new group.
    """
    pattern = re.compile(rf"^{re.escape(group_name)} (\d+)$")
    highest = 0
    if psd is not None:
        for child in psd:
            if child.kind != "group":
                continue
            match = pattern.match(child.name)
            if match:
                highest = max(highest, int(match.group(1)))
    return f"{group_name} {highest + 1}"


def _ensure_rgb_target(psd: PSDImage, target_path: Path) -> None:
    """Refuse to append into a non-RGB document (build brief guard 5).

    Empirically confirmed (this feature's own pre-build spike, not just
    docs): appending an RGB pixel layer into a non-RGB (e.g. CMYK/
    Grayscale) ``PSDImage`` does NOT raise on its own -- it silently
    converts/desaturates (an RGB (255, 0, 0) layer became a Grayscale L=76
    layer in that spike). Left unguarded, a user appending a colorful
    generation into an accidentally-CMYK/Grayscale review document would
    get silently wrong colors with no error at all -- so this checks
    *psd*'s own recorded :class:`~psd_tools.constants.ColorMode` up front
    and raises a clear, mode-naming error instead.

    Raises:
        ValueError: *psd*'s ``color_mode`` is not
            :attr:`~psd_tools.constants.ColorMode.RGB`, naming the actual
            mode and *target_path*.
    """
    if psd.color_mode != ColorMode.RGB:
        raise ValueError(
            f"Cannot append to {target_path}: its color mode is "
            f"{psd.color_mode.name}, not RGB. Convert it to RGB in Photoshop "
            "first (Image > Mode > RGB Color), or point append_to_existing "
            "at a different/new target."
        )


def _peek_target_canvas(target_path: Path, pil_images: list[Image.Image]) -> tuple[int, int]:
    """The canvas this run's layers will actually be placed against.

    An EXISTING target's own ``(width, height)`` if *target_path* is a file
    (a read-only open -- never mutates or saves it); otherwise the same
    max-across-inputs canvas :func:`_build_group_psd` would compute for a
    brand-new document, since a missing target is created fresh (build
    brief item 3) with that same sizing.
    """
    if target_path.is_file():
        existing = PSDImage.open(target_path)
        return existing.width, existing.height
    return (
        max(image.width for image in pil_images),
        max(image.height for image in pil_images),
    )


def _log_canvas_mismatch_if_needed(
    node_id: str,
    target_path: Path,
    existing_width: int,
    existing_height: int,
    pil_images: list[Image.Image],
) -> None:
    """Warn (build brief guard 5) when this run's own layers won't fully fit
    *target_path*'s EXISTING canvas.

    CONSTRAINT 1: psd-tools has no canvas-resize API (a literal TODO
    comment in its own ``psd_image.py``, confirmed empirically) --
    appending an image LARGER than the existing canvas does not error and
    does not lose data, it is simply clipped visually to the existing
    canvas. That is silent and easy to miss, so this logs a WARNING naming
    both sizes whenever any connected image's own width/height exceeds the
    target's, so the clipping is at least diagnosable from this node's log
    trail.
    """
    max_width = max(image.width for image in pil_images)
    max_height = max(image.height for image in pil_images)
    if max_width > existing_width or max_height > existing_height:
        logger.warning(
            "cpsb compose_psd: node %s: appending into %s (canvas %dx%d) with new "
            "layer(s) up to %dx%d -- psd-tools cannot resize an existing PSD's "
            "canvas, so content outside %dx%d will be clipped",
            node_id,
            target_path,
            existing_width,
            existing_height,
            max_width,
            max_height,
            existing_width,
            existing_height,
        )


def _atomic_save(psd: PSDImage, target_path: Path) -> None:
    """Save *psd* to *target_path* WITHOUT ever truncating a pre-existing file.

    CONSTRAINT 3 (the single most dangerous part of this feature):
    :meth:`~psd_tools.api.psd_image.PSDImage.save` opens its destination
    ``"wb"`` IMMEDIATELY, truncating any existing file at that path before
    writing a single byte of the new content. If serialization then raised
    partway through (a genuinely possible failure -- a corrupt embedded
    resource, an out-of-memory composite, ...), the user's previously-good
    PSD would be left truncated and unopenable, with no way back.

    This instead writes to a fresh, uniquely-named temp file in the SAME
    directory as *target_path* (:func:`tempfile.mkstemp`, guaranteeing the
    same filesystem so the final step is a true atomic rename rather than a
    cross-filesystem copy), and only ``os.replace()``s it onto *target_path*
    once :meth:`PSDImage.save` has fully returned. Any exception during the
    save leaves *target_path* byte-for-byte untouched, and the temp file is
    removed rather than left behind.

    Args:
        psd: The (already-mutated, unsaved) document to write.
        target_path: The final destination -- pre-existing or not.

    Raises:
        Whatever :meth:`PSDImage.save` itself raises -- propagated after
        the temp file is cleaned up, never swallowed (this node's other
        error paths -- the non-RGB guard, the missing-input check -- are
        likewise real exceptions, not silently-logged best-efforts).
    """
    target_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(target_path.parent), prefix=f".{target_path.name}.", suffix=".tmp"
    )
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        psd.save(tmp_path)
        os.replace(tmp_path, target_path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


#: Websocket event name for :func:`_emit_compose_written` (product owner gap:
#: "for 'don't open' how do I later find and open the file?"). A NEW, minimal
#: event, deliberately distinct from ``cpsb.updated``/``cpsb.status``
#: (:mod:`cpsb.handoff`) -- see that function's docstring for why.
COMPOSE_WRITTEN_EVENT = "cpsb.compose_written"


def _emit_compose_written(context: CpsbContext, node_id: str, output_path: Path) -> None:
    """Notify the frontend that *output_path* was just written to disk.

    Closes the product owner's reported gap verbatim: "And for 'don't open'
    how do I later find and open the file?" -- :data:`MODE_DONT_OPEN` builds
    and writes a real, complete PSD but (by design) never opens Photoshop and
    never creates a handoff, so none of this pack's handoff-driven
    discoverability surface (gallery cards, badges, reveal/re-open buttons,
    the right-click node menu's active-handoff submenu) ever learns the file
    exists. This event is the fix: called unconditionally after every REAL
    write this node performs, for all three ``mode`` values alike (including
    :data:`MODE_DONT_OPEN`), so the frontend (``web/cpsb/compose.js``) can
    show "Written: <filename>" on the node regardless of which mode was
    selected -- knowing what a run wrote is useful even when Photoshop DID
    open, not just when it didn't.

    **Why this cannot regress the "Don't open" contract of zero Photoshop
    entanglement**: this function calls :meth:`CpsbContext.send_event`
    DIRECTLY -- the exact same direct-context call
    :func:`cpsb.routes._emit_tier2` already makes for its own non-handoff
    event -- and nothing else. It never touches
    :class:`~cpsb.handoff.HandoffManager` (no ``manager.create``, no
    ``manager.note_source_written``, no status transition of any kind), so
    it cannot create a handoff, cannot write a ``meta.json``, and cannot
    produce a thumbnail (``orig_thumb.png``) -- the three things a handoff
    would otherwise imply. ``send_event`` itself
    (:attr:`cpsb.context.CpsbContext.send_event`) is a thin, side-effect-free
    wrapper over ``PromptServer.send_sync`` (a websocket broadcast to
    already-connected browser tabs); it touches no disk and blocks on
    nothing. Skipping this call entirely (e.g. if a caller is never reached)
    leaves every other behavior of this node completely unchanged -- it is
    purely informational, exactly like :func:`cpsb.routes._emit_tier2`.

    Args:
        context: The active backend context (``state.context``), whose
            ``send_event`` reaches every connected frontend.
        node_id: This node instance's id (the same stringified
            ``unique_id`` every other event/log line in this module keys
            on).
        output_path: The just-written file's full path. Its bare name is
            sent as ``filename``, with ``subfolder=""``/``type="input"``
            alongside it -- the exact same convention (and the exact same
            accepted limitation for an out-of-``input/`` ``existing_psd_path``
            override) already documented on this class's own STRING output;
            see :class:`PhotoshopComposePSD`'s docstring "Outputs" section.
            The FULL path is ALSO sent, as ``path``, resolved via
            :meth:`Path.resolve` here (not assumed of the caller) so it is
            always absolute -- including for an ``existing_psd_path``
            power-user override the user typed as a relative string
            (:func:`_resolve_append_target` returns that override verbatim,
            unresolved; see its own docstring). This is purely additive and
            purely informational, exactly like the rest of this event: it is
            never read back by any identity/cache-key/handoff logic in this
            module or :mod:`cpsb.handoff`, existing only so the frontend's
            "Copy Path" button (``web/cpsb/compose.js``) has the real,
            absolute, server-side location to copy -- the pre-existing
            ``filename`` field alone is not enough for that (it is
            deliberately bare, per the STRING-output convention referenced
            above).
    """
    context.send_event(
        COMPOSE_WRITTEN_EVENT,
        {
            "node_id": node_id,
            "filename": output_path.name,
            "path": str(output_path.resolve()),
            "subfolder": "",
            "type": "input",
        },
    )


class PhotoshopComposePSD:
    """Composes N images into one grouped, multi-layer PSD (PROTOCOL.md §6c).

    ``image_1..image_N`` (``N`` up to :data:`MAX_IMAGE_INPUTS`, all optional
    -- the frontend companion ``web/cpsb/compose.js`` only ever shows
    connected sockets plus one trailing empty one) become pixel layers on a
    shared canvas sized to the max width/height across every connected
    input; each layer keeps its native resolution (never rescaled) and is
    centered; ``image_1`` is the bottom layer, higher indices stack on top;
    every layer lands inside one group named by the ``group_name`` widget.
    Written via ``psd-tools``' ``PSDImage.new`` -> ``create_pixel_layer`` ->
    ``create_group`` (verified against the installed 1.17.4, this module's
    own docstring) to ``input/<filename_prefix>_%05d.psd``
    (:func:`_allocate_output_path`).

    Outputs: ``(IMAGE, MASK, STRING)``. IMAGE is the deterministic flattened
    composite of exactly what was written (:func:`_flatten_placements`, not
    a re-read of the saved file); MASK is ``1 - alpha`` of that composite
    (canvas regions no input covers), else zeros, via the same
    :func:`cpsb.nodes._tensors_from_image` helper every other node in this
    package uses; STRING is the written PSD's filename, relative to
    ``input/`` (``subfolder=""``) -- usable directly by
    :class:`~cpsb.load_psd.PhotoshopLoadPSD`'s ``psd`` combo or ComfyUI's
    own ``/view``. EXCEPTION: in ``append_to_existing`` mode with an
    ``existing_psd_path`` override that points OUTSIDE ``input/`` (a
    power-user escape hatch, used verbatim -- see below), STRING is still
    just that target's bare filename, which is no longer guaranteed unique
    or resolvable via ``input/``-relative tooling; this is an accepted
    limitation of that override, not a bug.

    The ``mode`` COMBO (PROTOCOL.md §6c) mirrors the "Edit in Photoshop"
    bridge node's three behaviors exactly, applied to the just-written
    LAYERED file:

    * :attr:`cpsb.nodes.BridgeMode.WAIT_FIRST_SAVE` ("Wait for first save",
      the DEFAULT) creates a ``bridge_node`` handoff for the file, opens
      Photoshop, and BLOCKS :meth:`execute` in
      :meth:`cpsb.handoff.HandoffManager.wait_for_edit` until the first save
      -- then returns that SAVED edit (flattened) as the IMAGE/MASK outputs.
      Cancel/timeout raise ComfyUI's own ``InterruptProcessingException`` via
      :func:`cpsb.nodes._raise_interrupt`, exactly like the bridge node.
    * :attr:`cpsb.nodes.BridgeMode.RERUN_EVERY_SAVE` ("Re-run on every save")
      never blocks: it opens Photoshop once, passes the flat composite
      through, and relies on the frontend auto-queueing a re-run per save
      (PROTOCOL.md §5), each of which consumes the latest edit via the
      consume path below.
    * :data:`MODE_DONT_OPEN` ("Don't open (composite only)") is the old
      always-flat behavior: build and return the flat composite, never touch
      Photoshop, create no handoff.

    The blocking-wait, open, and consume machinery is imported from
    :mod:`cpsb.nodes` (:meth:`~cpsb.nodes.PhotoshopBridge._open_in_photoshop`,
    :func:`~cpsb.nodes._raise_interrupt`) rather than duplicated, so this
    node's Tier 1/Tier 2 behavior is identical to the bridge node's by
    construction. **v1 uses a MANAGED COPY of the generated file, not
    ``edit_in_place``** (see :meth:`_create_bridge_handoff` for why -- true
    ``edit_in_place`` would mean touching ``cpsb/routes.py``/``cpsb/watcher.py``
    plumbing this build does not own); the blocking round trip works
    end-to-end all the same.

    Consume/reuse/supersede semantics mirror
    :meth:`cpsb.nodes.PhotoshopBridge.execute` and :mod:`cpsb.annotate`,
    keyed off :func:`_compute_identity_hash` -- a mode/prefix-FREE hash of
    the combined inputs (PROTOCOL.md §6c) -- instead of a single file's raw
    bytes: while an ACTIVE ``bridge_node`` handoff for this node has a
    ``source_hash`` matching the CURRENT inputs' identity, :meth:`execute`
    either (a) returns its latest edit's pixels (flattened) instead of
    composing fresh, when one has arrived -- so re-queuing after a Photoshop
    save delivers the user's manual compositing/masking work, the same
    "consume the edit" pattern PROTOCOL.md §6/§6b establish -- or (b), when
    no edit has arrived yet, REUSES that same handoff (reopening its
    managed PSD copy) rather than minting a second one, so a re-queue against a
    handoff that is open in Photoshop but not yet saved never orphans it with
    a duplicate. Only when the identity no longer matches (the actual
    connected images, ``group_name``, or ``layer_name`` changed) is the old
    handoff superseded and a genuinely new one created. The consume check
    runs first for EVERY mode, so an already-saved edit is served without
    re-opening Photoshop regardless of which mode is selected; the reuse
    check likewise applies regardless of mode, so switching *only* the
    ``mode`` widget (with unchanged inputs) can never strand an open
    handoff/document (see :func:`_compute_identity_hash`'s docstring for the
    bug this fixes).

    **Append to an existing document** (``append_to_existing``, product
    owner request: "generating multiple runs of images and storing the
    results into a single psd for review vs a slew of separate files"):
    when the ``append_to_existing`` BOOLEAN is ``True``, this run's layers
    are written into an EXISTING ``.psd``/``.psb`` -- selected via the
    ``existing_psd`` COMBO (files already in ComfyUI's input directory,
    mirroring :class:`~cpsb.load_psd.PhotoshopLoadPSD`'s own combo -- the
    only file-picker mechanism available to a server-side ComfyUI node, and
    the only one that still works when ComfyUI and Photoshop run on
    different machines) or the ``existing_psd_path`` STRING power-user
    override (used VERBATIM whenever non-empty, taking priority over the
    combo) -- as a NEW top-level group, instead of a brand-new auto-numbered
    file. A target that doesn't exist yet is created fresh (a first-run
    convenience, not an error). Each run's group is named
    ``"<group_name> <N>"``, *N* one past the highest existing same-prefixed
    run group already in the target (:func:`_next_run_group_name`) -- so
    successive runs accumulate as distinguishable, separately-named groups
    rather than merging or colliding. Appending into a non-RGB (CMYK/
    Grayscale/...) target is refused with a clear mode-naming error rather
    than silently desaturating the new layers (psd-tools does the latter on
    its own -- :func:`_ensure_rgb_target`); a target whose canvas doesn't
    match the new layers' own size is still WRITTEN (psd-tools cannot
    resize a canvas), but a WARNING names both sizes
    (:func:`_log_canvas_mismatch_if_needed`) so the resulting clipping is
    diagnosable. The write itself is ATOMIC (:func:`_atomic_save`): it never
    truncates the pre-existing target in place, so a failure anywhere in
    the append/serialize path leaves the user's previously-good document
    byte-for-byte untouched.

    Appending is otherwise ORTHOGONAL to the ``mode``/handoff machinery
    above: ``append_to_existing`` only changes WHERE/HOW this run's own
    layers get written to disk (a fixed, persistent target instead of a
    fresh auto-numbered file); the IMAGE/MASK outputs remain this run's OWN
    flattened composite (never the whole accumulated document), and the
    ``mode`` dispatch, handoff creation, and Photoshop-open behavior run
    completely unchanged against whatever ``output_path`` the append step
    resolved to -- opening Photoshop opens a MANAGED COPY of the target
    file exactly as it does for the non-append auto-numbered file, so
    "Wait for first save"/"Re-run on every save" let the user review/edit
    the real, growing, multi-run document, which is the whole point of this
    feature.

    **Duplicate-append avoidance** (the subtle caching interaction): both
    ``append_to_existing`` and the resolved append target are folded into
    :func:`_compute_identity_hash` (so toggling the flag or switching
    targets, even with pixel-identical images, is treated as a genuinely
    new identity -- supersedes any stale handoff and performs a real new
    append) and into :func:`_compute_inputs_hash` (so :meth:`IS_CHANGED`
    forces ComfyUI to re-execute when either changes). Within a single
    identity, however, :meth:`execute` performs the REAL append (a
    persisted, run-numbered group written to disk) at most once per
    distinct identity per node: if an ACTIVE handoff for this node already
    matches the current identity (the reuse case above -- e.g. a re-queue
    while "Wait for first save" is still unsaved), the append step is
    SKIPPED entirely -- this run's own IMAGE/MASK outputs are still computed
    (from the SAME centering math, against the target's already-current
    canvas), but nothing more is written to the target, so re-queuing an
    unedited, still-pending run can never duplicate its group. Only a
    genuinely new identity (different pixels, ``group_name``,
    ``layer_name``, ``append_to_existing``, or target) ever performs
    another real append.
    """

    CATEGORY = "image/photoshop"
    RETURN_TYPES = ("IMAGE", "MASK", "STRING", "IMAGE")
    RETURN_NAMES = ("image", "mask", "filename", "layers")
    FUNCTION = "execute"

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, Any]:
        """``filename_prefix``/``group_name``/``mode``/``timeout_seconds`` + up
        to :data:`MAX_IMAGE_INPUTS` optional ``image_N`` sockets + hidden
        ``unique_id`` (PROTOCOL.md §6c).

        ``mode`` is a COMBO of the SAME three strings the "Edit in Photoshop"
        node uses -- the first two reuse :class:`cpsb.nodes.BridgeMode`'s
        constants verbatim (the frontend string-matches on them for its
        auto-queue policy, PROTOCOL.md §5), the third is this node's own
        :data:`MODE_DONT_OPEN`. ``timeout_seconds`` matches the bridge/annotate
        nodes' bounds (default 1800, min 10, max 86400) and applies only to
        "Wait for first save".

        Every ``image_N`` is declared optional so ComfyUI accepts any
        connected subset ``>= 1`` -- the frontend
        (``web/cpsb/compose.js``) is what actually limits what a user SEES
        to "connected, plus one trailing empty socket"; the backend's own
        declared range here only needs to be generous enough to never run
        out.

        The final three ``required`` entries are the "append to existing
        document" feature (the class docstring's own section): the
        ``append_to_existing`` BOOLEAN (default ``False``, so every existing
        saved workflow keeps its old auto-numbered-file behavior unchanged),
        the ``existing_psd`` COMBO (:func:`_list_psd_files` over ComfyUI's
        input directory -- tolerates an unconfigured backend by listing
        nothing, exactly like :meth:`cpsb.load_psd.PhotoshopLoadPSD.
        INPUT_TYPES`'s own combo), and the ``existing_psd_path`` STRING
        power-user override (default ``""``). These are appended at the
        very END of ``required`` deliberately: ComfyUI matches a saved
        workflow's widget VALUES to widgets purely by POSITION, so inserting
        anywhere else would silently reassign every existing saved
        workflow's ``mode``/``timeout_seconds``/``max_layers`` values onto
        the wrong widgets.
        """
        optional = {f"image_{i}": ("IMAGE",) for i in range(1, MAX_IMAGE_INPUTS + 1)}
        state = nodes._state_if_configured()
        existing_psd_files = _list_psd_files(state.context.input_dir) if state is not None else []
        return {
            "required": {
                "group_name": ("STRING", {"default": DEFAULT_GROUP_NAME}),
                "layer_name": ("STRING", {"default": DEFAULT_LAYER_NAME}),
                "mode": (
                    [
                        nodes.BridgeMode.WAIT_FIRST_SAVE,
                        nodes.BridgeMode.RERUN_EVERY_SAVE,
                        MODE_DONT_OPEN,
                    ],
                    {"default": nodes.BridgeMode.WAIT_FIRST_SAVE},
                ),
                "timeout_seconds": ("INT", {"default": 1800, "min": 10, "max": 86400}),
                "max_layers": (
                    "INT",
                    {"default": DEFAULT_MAX_LAYERS, "min": 1, "max": 512},
                ),
                # -- "append to existing document" (class docstring) --
                # appended LAST: widget order is positional, so anything
                # else here would corrupt every saved workflow's existing
                # widget values.
                "append_to_existing": ("BOOLEAN", {"default": False}),
                "existing_psd": (existing_psd_files,),
                "existing_psd_path": ("STRING", {"default": ""}),
            },
            "optional": optional,
            "hidden": {"unique_id": "UNIQUE_ID"},
        }

    @classmethod
    def IS_CHANGED(
        cls,
        group_name: str,
        mode: str,
        timeout_seconds: int,
        unique_id: str,
        max_layers: int = DEFAULT_MAX_LAYERS,
        layer_name: str = DEFAULT_LAYER_NAME,
        filename_prefix: str = DEFAULT_FILENAME_PREFIX,
        append_to_existing: bool = False,
        existing_psd: str = "",
        existing_psd_path: str = "",
        **kwargs: Any,
    ) -> str:
        """:func:`_compute_inputs_hash`, folded with the latest-edit hash when consumable.

        Mirrors :meth:`cpsb.load_psd.PhotoshopLoadPSD.IS_CHANGED`'s shape
        exactly: the bare inputs hash on its own would NOT change once an
        edit lands (the connected images and widget values are unchanged;
        the edit lives in the handoff folder), so an arriving edit must be
        folded in explicitly to force re-execution on the next queue --
        the same mechanism ``LoadImage.IS_CHANGED`` and every other node in
        this package use. Tolerates an unconfigured backend (module-import-
        time introspection, mirroring
        :meth:`~cpsb.load_psd.PhotoshopLoadPSD.IS_CHANGED`'s own
        ``_state_if_configured`` use) by returning just the bare inputs
        hash in that case, since there is no handoff manager to consult yet.

        *timeout_seconds* is accepted (ComfyUI passes every declared input to
        ``IS_CHANGED``) but deliberately NOT folded into the hash -- exactly
        like the bridge and annotate nodes' own ``IS_CHANGED`` (PROTOCOL.md
        §6/§6d): it only bounds how long a "Wait for first save" run waits,
        never what a completed run produces, so hashing it would force
        needless re-execution on a mere timeout tweak. *mode*, by contrast,
        IS folded (through :func:`_compute_inputs_hash`) because switching it
        genuinely changes the output. *append_to_existing*/*existing_psd*/
        *existing_psd_path* are likewise folded in (via
        :func:`_append_target_key`, the cheap non-validating form -- this
        method must never raise on a currently-invalid selection): toggling
        the flag or switching targets must re-execute this node even with
        pixel-identical upstream images (the class docstring's "append to an
        existing document" section).
        """
        pil_images, _ = _collect_layer_images(kwargs, max_layers)
        prefix = _sanitize_filename_prefix(filename_prefix)
        append_target = (
            _append_target_key(existing_psd, existing_psd_path) if append_to_existing else ""
        )
        inputs_hash = _compute_inputs_hash(
            pil_images, prefix, group_name, mode, layer_name, append_to_existing, append_target
        )

        state = nodes._state_if_configured()
        if state is None:
            return inputs_hash
        # Matching uses the mode/prefix-FREE identity hash -- a handoff's
        # source_hash is recorded as _compute_identity_hash's value now (see
        # that function's docstring), not the mode-sensitive inputs_hash
        # above (which stays mode-sensitive here only so a mode/prefix change
        # alone still forces THIS node to re-execute).
        identity_hash = _compute_identity_hash(
            pil_images, group_name, layer_name, append_to_existing, append_target
        )
        active = _find_matching_active_handoff(state.manager, str(unique_id), identity_hash)
        if active is not None:
            edit_hash = state.manager.latest_edit_hash(active.handoff_id)
            if edit_hash is not None:
                return f"{inputs_hash}:{edit_hash}"
        return inputs_hash

    def execute(
        self,
        group_name: str,
        mode: str,
        timeout_seconds: int,
        unique_id: str,
        max_layers: int = DEFAULT_MAX_LAYERS,
        layer_name: str = DEFAULT_LAYER_NAME,
        filename_prefix: str = DEFAULT_FILENAME_PREFIX,
        append_to_existing: bool = False,
        existing_psd: str = "",
        existing_psd_path: str = "",
        **kwargs: Any,
    ) -> tuple[Any, Any, str]:
        """Compose (or consume) and return ``(IMAGE, MASK, STRING)`` (PROTOCOL.md §6c).

        Serves a consumable active edit first (the class docstring's "Consume
        semantics" paragraph -- this runs for EVERY mode, so an already-saved
        edit is returned without re-opening Photoshop). Otherwise composes
        the connected inputs fresh, writes the LAYERED PSD -- either a fresh
        auto-numbered file (``append_to_existing=False``, unchanged from
        before) or into an existing/new target document
        (``append_to_existing=True``, the class docstring's "append to an
        existing document" section) -- and dispatches on *mode*:

        * :attr:`~cpsb.nodes.BridgeMode.WAIT_FIRST_SAVE`: open Photoshop and
          BLOCK (:meth:`_open_and_wait_for_edit`) until the first save, then
          return that SAVED edit (flattened) as the IMAGE/MASK outputs.
          Cancel/timeout/open-failure raise ``InterruptProcessingException``.
        * :attr:`~cpsb.nodes.BridgeMode.RERUN_EVERY_SAVE`: open Photoshop
          non-blocking (:meth:`_open_passthrough`) and return the flat
          composite; the frontend auto-queues a re-run per save (PROTOCOL.md
          §5) which then takes the consume path above.
        * :data:`MODE_DONT_OPEN`: return the flat composite, never open
          Photoshop, create no handoff.

        The STRING output is always the written PSD's filename, unchanged from
        the old build -- including on the "Wait for first save" path, whose
        IMAGE/MASK come from the saved edit but whose STRING still names the
        file that was composed and handed off. In ``append_to_existing`` mode
        this is the TARGET's own filename (:func:`_resolve_append_target`),
        not a freshly auto-numbered one.

        Args:
            filename_prefix: Base name for the written file (sanitized via
                :func:`_sanitize_filename_prefix` before use). Unused when
                *append_to_existing* is ``True``.
            group_name: Name of the group every layer is placed in (in
                ``append_to_existing`` mode, run-numbered first via
                :func:`_next_run_group_name`).
            mode: One of the three ``mode`` COMBO strings (see the class
                docstring).
            timeout_seconds: Bound on the "Wait for first save" blocking wait;
                unused by the other two modes.
            max_layers: Cap on how many images become layers. Each connected
                socket's IMAGE batch is expanded frame-by-frame into layers
                (so a VAE Decode emitting N images yields N layers); the total
                across all sockets is capped here, oldest-first, with a warning
                logged when it truncates.
            unique_id: This node instance's id (ComfyUI's hidden
                ``UNIQUE_ID`` input), used to key its handoff lookup.
            append_to_existing: When ``True``, this run's layers are written
                into the resolved ``existing_psd``/``existing_psd_path``
                target instead of a new auto-numbered file (class docstring).
            existing_psd: The ``existing_psd`` COMBO's current selection.
            existing_psd_path: The ``existing_psd_path`` power-user override;
                used verbatim, taking priority over *existing_psd*, whenever
                non-empty.
            **kwargs: The connected ``image_N`` tensors (each possibly a
                multi-image batch), whichever subset ComfyUI passed.

        Returns:
            ``(IMAGE, MASK, STRING)`` -- see the class docstring.

        Raises:
            ValueError: No ``image_N`` input is connected -- there is
                nothing to compose. Also raised (``append_to_existing=True``
                only) when neither *existing_psd* nor *existing_psd_path*
                resolves to a usable ``.psd``/``.psb`` path
                (:func:`_resolve_append_target`), or the resolved target
                already exists but is not an RGB document
                (:func:`_ensure_rgb_target`).
            comfy.model_management.InterruptProcessingException: "Wait for
                first save" mode, when the open attempt fails or the wait ends
                in cancel/timeout (via :func:`cpsb.nodes._raise_interrupt`) --
                identical to the bridge node's own blocking behavior.
        """
        state = nodes._require_state()
        manager = state.manager
        node_id = str(unique_id)

        pil_images, total_available = _collect_layer_images(kwargs, max_layers)
        if not pil_images:
            raise ValueError("PhotoshopComposePSD needs at least one connected image_N input")
        if total_available > len(pil_images):
            # No silent truncation: a batch bigger than the cap loses layers, so
            # say so in the log (the user's lever is the max_layers widget).
            logger.warning(
                "cpsb compose_psd: node %s: %d input image(s) exceed max_layers=%d; "
                "using the first %d as layers (raise max_layers to include more)",
                node_id,
                total_available,
                max_layers,
                len(pil_images),
            )

        prefix = _sanitize_filename_prefix(filename_prefix)
        # Append target key for the cache-key-consistent identity hash below
        # (see _append_target_key's docstring: IS_CHANGED computes this same
        # value from the same two widgets, so a handoff recorded here is
        # always findable from there). Empty when append_to_existing is
        # False -- the target is irrelevant then, and must not perturb the
        # identity of a plain (non-append) run.
        append_target = (
            _append_target_key(existing_psd, existing_psd_path) if append_to_existing else ""
        )
        # Mode/prefix-FREE identity (see _compute_identity_hash's docstring):
        # this is what a bridge_node handoff's source_hash is now recorded
        # as, and what reuse/supersede below is keyed on -- deliberately NOT
        # the mode-sensitive _compute_inputs_hash (that value is IS_CHANGED's
        # job only). Folding mode in here was the confirmed bug: a mere mode
        # flip changed the recorded identity, so the already-open handoff
        # could never match again, stranding it as a live, unreachable
        # Photoshop document while a second one got created underneath it.
        # append_to_existing/append_target ARE folded in here, unlike mode --
        # see _compute_identity_hash's own docstring for why.
        identity_hash = _compute_identity_hash(
            pil_images, group_name, layer_name, append_to_existing, append_target
        )

        # -- reuse / supersede (mirrors cpsb.nodes.PhotoshopBridge.execute,
        # cpsb/nodes.py:402-432, and cpsb.annotate's analogous block,
        # cpsb/annotate.py:539-549) -----------------------------------------
        active = manager.find_active_for_node(node_id)
        if active is not None and active.origin_kind != "bridge_node":
            # Defensive: a leftover handoff of another kind for the same node
            # id is not one this node ever created or can consume.
            active = None
        if (
            active is not None
            and active.source_hash is not None
            and active.source_hash != identity_hash
        ):
            # The connected inputs (or group_name/layer_name/append settings)
            # genuinely changed since this handoff was opened: any edits it
            # holds belong to the OLD identity and must not be served for the
            # new one. Retire it and start fresh -- this is the one case a
            # new handoff (and new Photoshop document) is actually warranted.
            logger.info(
                "cpsb compose_psd: node %s: inputs changed, superseding handoff %s",
                node_id,
                active.handoff_id,
            )
            manager.supersede(active.handoff_id)
            active = None

        if active is not None and active.edits:
            consumed = _consume_active_edit(manager, active.handoff_id)
            if consumed is not None:
                logger.info(
                    "cpsb compose_psd: node %s handoff %s: consuming latest edit",
                    node_id,
                    active.handoff_id,
                )
                image_tensor, mask_tensor = consumed
                # The `layers` output stays this run's WRITTEN layers (the
                # inputs as placed), not a decomposition of the saved edit --
                # the consume path only ever fires on an identity match, so
                # the inputs ARE what was written. Canvas resolution mirrors
                # the write path (append target's fixed canvas when
                # appending, max-of-inputs otherwise); the defensive fallback
                # covers an append target deleted since the write, where the
                # write path would fail loudly but consuming a
                # already-delivered edit still should not.
                try:
                    if append_to_existing:
                        consume_target = _resolve_append_target(
                            state.context, existing_psd, existing_psd_path
                        )
                        canvas_width, canvas_height = _peek_target_canvas(
                            consume_target, pil_images
                        )
                    else:
                        canvas_width = max(image.width for image in pil_images)
                        canvas_height = max(image.height for image in pil_images)
                except Exception:
                    canvas_width = max(image.width for image in pil_images)
                    canvas_height = max(image.height for image in pil_images)
                placements = _compute_placements(pil_images, canvas_width, canvas_height)
                layers_tensor = _layers_batch_tensor(placements, canvas_width, canvas_height)
                return image_tensor, mask_tensor, active.source.filename, layers_tensor
            # Filesystem race: the edit file vanished. `active` stays set so
            # the open paths below REUSE it rather than minting a new one.

        if append_to_existing:
            # -- "append to existing document" (class docstring) ----------
            target_path = _resolve_append_target(state.context, existing_psd, existing_psd_path)
            if active is not None:
                # DUPLICATE-APPEND GUARD (class docstring's "duplicate-append
                # avoidance" section): `active` here means an already-open,
                # not-yet-edited handoff for THIS node already matches the
                # CURRENT identity (pixels/group_name/layer_name/append
                # settings all unchanged) -- e.g. a re-queue of "Wait for
                # first save" before anyone has saved yet. That prior call
                # already performed the real append into `target_path`; doing
                # it again here would append a SECOND, redundant group of the
                # exact same content. So: do NOT touch the target file at
                # all. This run's own IMAGE/MASK outputs are still computed,
                # from the SAME centering math against the target's current
                # (already-updated) canvas, purely so the node's outputs stay
                # correct -- but nothing more is written to disk.
                logger.info(
                    "cpsb compose_psd: node %s: append_to_existing reusing pending "
                    "handoff %s for unchanged identity -- skipping re-append into %s",
                    node_id,
                    active.handoff_id,
                    target_path,
                )
                canvas_width, canvas_height = _peek_target_canvas(target_path, pil_images)
                placements = _compute_placements(pil_images, canvas_width, canvas_height)
            elif target_path.is_file():
                psd = PSDImage.open(target_path)
                _ensure_rgb_target(psd, target_path)
                _log_canvas_mismatch_if_needed(
                    node_id, target_path, psd.width, psd.height, pil_images
                )
                run_group_name = _next_run_group_name(psd, group_name)
                logger.info(
                    "cpsb compose_psd: node %s: appending %d layer(s) into %s as group %r "
                    "(mode=%r)",
                    node_id,
                    len(pil_images),
                    target_path,
                    run_group_name,
                    mode,
                )
                placements, canvas_width, canvas_height = _append_run_into_psd(
                    psd, pil_images, run_group_name, layer_name
                )
                _atomic_save(psd, target_path)
                logger.info("cpsb compose_psd: node %s: wrote %s", node_id, target_path)
                _emit_compose_written(state.context, node_id, target_path)
            else:
                # Missing target: first-run convenience, not an error (build
                # brief item 3) -- create it fresh via the same build path
                # _build_group_psd already uses for a brand-new auto-numbered
                # file, just written to the resolved target path instead.
                run_group_name = _next_run_group_name(None, group_name)
                logger.info(
                    "cpsb compose_psd: node %s: existing_psd target %s does not exist yet, "
                    "creating it fresh with %d layer(s) as group %r (mode=%r)",
                    node_id,
                    target_path,
                    len(pil_images),
                    run_group_name,
                    mode,
                )
                psd, canvas_width, canvas_height, placements = _build_group_psd(
                    pil_images, run_group_name, layer_name
                )
                _atomic_save(psd, target_path)
                logger.info("cpsb compose_psd: node %s: wrote %s", node_id, target_path)
                _emit_compose_written(state.context, node_id, target_path)
            output_path = target_path
        else:
            logger.info(
                "cpsb compose_psd: node %s: composing %d layer(s) into group %r (mode=%r)",
                node_id,
                len(pil_images),
                group_name,
                mode,
            )
            psd, canvas_width, canvas_height, placements = _build_group_psd(
                pil_images, group_name, layer_name
            )
            output_path = _allocate_output_path(state.context.input_dir, prefix)
            psd.save(output_path)
            logger.info("cpsb compose_psd: node %s: wrote %s", node_id, output_path)
            _emit_compose_written(state.context, node_id, output_path)

        flattened = _flatten_placements(placements, canvas_width, canvas_height)
        image_tensor, mask_tensor = nodes._tensors_from_image(flattened)
        layers_tensor = _layers_batch_tensor(placements, canvas_width, canvas_height)

        if mode == MODE_DONT_OPEN:
            # Old always-flat behavior: no Photoshop, no handoff. If a
            # handoff was still open for this node (e.g. the user switched
            # from an open-Photoshop mode to this one without saving first),
            # retire it -- nobody will ever consume it otherwise, and it
            # would strand a live Photoshop document.
            if active is not None:
                logger.info(
                    "cpsb compose_psd: node %s: mode=%r, superseding handoff %s (won't be opened)",
                    node_id,
                    mode,
                    active.handoff_id,
                )
                manager.supersede(active.handoff_id)
            return image_tensor, mask_tensor, output_path.name, layers_tensor

        if mode == nodes.BridgeMode.WAIT_FIRST_SAVE:
            # BLOCKS until the first save; returns the SAVED edit (flattened).
            # Deliberately NOT wrapped in try/except: an open failure or a
            # cancel/timeout must propagate as InterruptProcessingException
            # (the bridge/annotate contract), never be swallowed here.
            image_tensor, mask_tensor, result_name = self._open_and_wait_for_edit(
                state, node_id, output_path, identity_hash, flattened, timeout_seconds, active
            )
            # `layers` stays this run's WRITTEN layers even though
            # image/mask are the saved edit: the batch documents what
            # went INTO the document, which is the review view the
            # output exists for.
            return image_tensor, mask_tensor, result_name, layers_tensor

        # mode == BridgeMode.RERUN_EVERY_SAVE: open non-blocking, pass the flat
        # composite through. PROTOCOL.md §6c: "Failure to open = log + cpsb.status
        # error event, never a node crash" -- the composed outputs above are
        # already valid and must still be returned. _open_in_photoshop already
        # catches and marks its own ordinary failure modes; this try/except is a
        # last-resort guard against a genuinely unexpected one.
        result_name = output_path.name
        try:
            result_name = self._open_passthrough(
                state, node_id, output_path, identity_hash, flattened, active
            )
        except Exception:
            logger.exception(
                "cpsb compose_psd: node %s: opening Photoshop after compose failed",
                node_id,
            )

        return image_tensor, mask_tensor, result_name, layers_tensor

    @staticmethod
    def _create_bridge_handoff(
        state: nodes._NodeState,
        node_id: str,
        psd_path: Path,
        identity_hash: str,
        composite_image: Image.Image,
    ) -> HandoffMeta:
        """Create the ``bridge_node`` handoff whose managed PSD copy is *psd_path* copied.

        Shared by both open paths (:meth:`_open_and_wait_for_edit`,
        :meth:`_open_passthrough`). Mirrors
        :meth:`cpsb.nodes.PhotoshopBridge._create_handoff` and
        :func:`cpsb.annotate._create_handoff` -- the same ``origin_kind =
        "bridge_node"`` this node's consume path now looks for (PROTOCOL.md
        §6c) -- but with one deliberate difference: instead of a FLATTENED
        ``write_psd(pil_image)`` (which is all a bridge/annotate input is,
        an in-memory tensor), it copies the just-written LAYERED file
        *psd_path* byte-for-byte, so the user opens the actual layer stack in
        Photoshop and composites/adjusts LAYERS there -- the whole point of
        this node. This is the same copy rule PROTOCOL.md §2 states for
        psd-native sources ("COPIES that file verbatim -- never
        write_psd/frompil").

        **v1 uses a MANAGED COPY, not ``edit_in_place``** (annotate-style
        handoff creation, the fallback the build brief calls for): the
        handoff's managed PSD copy is a copy of *psd_path* rather than a
        pointer at it. PROTOCOL.md §6c's own line says the generated file is
        this node's own output, so editing it in place would be safe by
        construction -- but wiring true ``edit_in_place`` means
        ``HandoffManager.create(edit_in_place=True, original_path=...)`` PLUS
        registering that out-of-managed-folder path with the watcher
        (``CpsbWatcher.watch_original``), reached through
        ``cpsb/routes.py``/``cpsb/watcher.py`` plumbing this change does not
        own. The managed copy makes the blocking round trip work end-to-end
        all the same (the watcher already covers the managed folder, so a
        Photoshop save into it is ingested and unblocks the wait); pointing
        the handoff directly at *psd_path* is the natural follow-up once
        that ``edit_in_place`` plumbing lands.

        ``source_hash`` is set to *identity_hash* (the SAME mode/prefix-FREE
        value :func:`_compute_identity_hash` recomputes from the current
        inputs, and what the reuse/supersede check in :meth:`execute`
        compares against) rather than a hash of *psd_path*'s bytes -- see
        :func:`_compute_identity_hash`'s docstring for why a PSD-bytes hash
        cannot support the cheap per-call equality test the consume path
        needs, and why this must be the mode-FREE identity rather than the
        mode-sensitive :func:`_compute_inputs_hash` value (folding mode in
        here was the confirmed "spins forever" / "slew of documents" bug).

        Args:
            state: The shared backend state.
            node_id: This node instance's id (the handoff's ``origin_node_id``).
            psd_path: The just-written compose output (already on disk).
            identity_hash: :func:`_compute_identity_hash` of the inputs that
                produced *psd_path* -- recorded as the handoff's
                ``source_hash``.
            composite_image: The flattened composite (for the handoff's
                ``orig_thumb.png``).

        Returns:
            The newly created handoff's metadata (status ``pending`` until the
            open attempt marks it ``editing``/``error``).
        """
        manager = state.manager
        psd_bytes = psd_path.read_bytes()

        meta = manager.create(
            origin_node_id=node_id,
            origin_kind="bridge_node",
            workflow_name="",
            source=SourceRef(filename=psd_path.name, subfolder="", type="input"),
            original_image=composite_image,
            source_hash=identity_hash,
        )
        handoff_psd_path = manager.psd_path(meta)
        handoff_psd_path.parent.mkdir(parents=True, exist_ok=True)
        handoff_psd_path.write_bytes(psd_bytes)
        manager.note_source_written(meta.handoff_id)
        return meta

    @staticmethod
    def _open_and_wait_for_edit(
        state: nodes._NodeState,
        node_id: str,
        psd_path: Path,
        identity_hash: str,
        composite_image: Image.Image,
        timeout_seconds: int,
        existing: HandoffMeta | None = None,
    ) -> tuple[Any, Any, str]:
        """"Wait for first save": open Photoshop and BLOCK until saved (PROTOCOL.md §6c).

        Identical in shape to :meth:`cpsb.nodes.PhotoshopBridge.execute`'s
        "Wait for first save" tail and :func:`cpsb.annotate._open_and_block_for_edit`:
        create (or REUSE, see *existing*) the handoff, open via the shared
        tier-selecting seam (:meth:`cpsb.nodes.PhotoshopBridge._open_in_photoshop`,
        reused rather than reimplemented -- it already logs tier selection and
        the launch result), then poll
        :meth:`cpsb.handoff.HandoffManager.wait_for_edit` on this (worker)
        thread until the first save. Every step is also logged under ``cpsb
        compose_psd:`` so a "didn't open Photoshop" report is diagnosable
        from this node's own log trail.

        Args:
            state: The shared backend state.
            node_id: This node instance's id.
            psd_path: The just-written compose output.
            identity_hash: :func:`_compute_identity_hash` of the current
                inputs -- recorded as a freshly-created handoff's
                ``source_hash``. Unused when *existing* is provided (its own
                ``source_hash`` already matched, or the caller wouldn't have
                passed it).
            composite_image: The flattened composite (thumbnail for a fresh
                handoff, and the fallback returned if the edit file races
                away after the wait).
            timeout_seconds: Bound on the blocking wait.
            existing: An already-open, still-unsaved ``bridge_node`` handoff
                for this SAME node whose identity already matches (PROTOCOL.md
                §6c reuse semantics, mirroring
                :meth:`cpsb.nodes.PhotoshopBridge.execute` and
                :func:`cpsb.annotate._resolve_ps_mode`). When given, this
                REUSES it -- reopening the SAME managed PSD copy the user may
                already be working in -- instead of minting a brand-new
                handoff (and a second, orphaned Photoshop document). ``None``
                (the default) creates a fresh one via
                :meth:`_create_bridge_handoff`, as before.

        Returns:
            ``(IMAGE, MASK, filename)`` -- the tensors are the SAVED edit's
            pixels (flattened); a normal return means the wait outcome was
            :data:`~cpsb.handoff.WaitOutcome.EDITED`. *filename* is the
            handoff's own ``source.filename`` -- the ORIGINAL generated PSD
            on reuse, matching what the consume path already reports, not
            necessarily *psd_path*'s name.

        Raises:
            Whatever :func:`cpsb.nodes._raise_interrupt` raises (ComfyUI's own
            ``InterruptProcessingException`` inside ComfyUI): when the open
            attempt fails (never reaches the wait), or the wait ends in
            ``CANCELLED``/``TIMEOUT``.
        """
        manager = state.manager
        if existing is None:
            meta = PhotoshopComposePSD._create_bridge_handoff(
                state, node_id, psd_path, identity_hash, composite_image
            )
            result_name = psd_path.name
        else:
            # Reuse: do NOT rewrite the managed PSD copy -- the user's
            # in-progress layers live in it. Same rule as
            # cpsb/annotate.py:550-557.
            meta = existing
            result_name = existing.source.filename
        handoff_psd_path = manager.psd_path(meta)

        logger.info(
            "cpsb compose_psd: node %s handoff %s: opening Photoshop", node_id, meta.handoff_id
        )
        attempt = nodes.PhotoshopBridge._open_in_photoshop(state, meta, handoff_psd_path)
        if attempt.ok:
            logger.info(
                "cpsb compose_psd: node %s handoff %s: launch result ok (tier %d)",
                node_id,
                meta.handoff_id,
                attempt.tier,
            )
        else:
            logger.warning(
                "cpsb compose_psd: node %s handoff %s: could not open Photoshop (tier %d): "
                "%s, interrupting",
                node_id,
                meta.handoff_id,
                attempt.tier,
                attempt.error,
            )
            # _open_in_photoshop already called manager.mark_error(...); nothing
            # left but to stop the workflow rather than hang waiting for a save
            # that can never arrive.
            nodes._raise_interrupt()

        logger.info(
            "cpsb compose_psd: node %s handoff %s: waiting for edit (timeout=%ss)",
            node_id,
            meta.handoff_id,
            timeout_seconds,
        )
        outcome = manager.wait_for_edit(meta.handoff_id, float(timeout_seconds))
        logger.info(
            "cpsb compose_psd: node %s handoff %s: wait outcome '%s'",
            node_id,
            meta.handoff_id,
            outcome,
        )
        if outcome != WaitOutcome.EDITED:
            nodes._raise_interrupt()

        # A normal return above means WaitOutcome.EDITED -- an edit is on disk.
        consumed = _consume_active_edit(manager, meta.handoff_id)
        if consumed is not None:
            image_tensor, mask_tensor = consumed
            return image_tensor, mask_tensor, result_name
        # Filesystem race: the edit file vanished between ingest and read.
        # Fall back to the flat composite so the outputs stay valid.
        image_tensor, mask_tensor = nodes._tensors_from_image(composite_image)
        return image_tensor, mask_tensor, result_name

    @staticmethod
    def _open_passthrough(
        state: nodes._NodeState,
        node_id: str,
        psd_path: Path,
        identity_hash: str,
        composite_image: Image.Image,
        existing: HandoffMeta | None = None,
    ) -> str:
        """"Re-run on every save": open Photoshop non-blocking (PROTOCOL.md §6c).

        Creates (or REUSES, see *existing*) the ``bridge_node`` handoff and
        opens it through the shared tier-selecting seam, returning as soon
        as the OS launch (Tier 1) or the plugin ``open_handoff`` send (Tier 2)
        completes -- it never waits for a save. The caller returns the flat
        composite; each later save is auto-queued by the frontend (PROTOCOL.md
        §5, keyed on this node's ``mode`` widget being "Re-run on every save")
        and consumed on the resulting re-run via :meth:`execute`'s consume
        path. A failed open is logged and left marked ``error`` by
        :meth:`~cpsb.nodes.PhotoshopBridge._open_in_photoshop` (which emits the
        ``cpsb.status`` event PROTOCOL.md §6c requires) -- never raised, so the
        already-valid composite outputs are still returned.

        Args:
            state: The shared backend state.
            node_id: This node instance's id.
            psd_path: The just-written compose output.
            identity_hash: :func:`_compute_identity_hash` of the current
                inputs -- recorded as a freshly-created handoff's
                ``source_hash``. Unused when *existing* is provided.
            composite_image: The flattened composite (for the thumbnail).
            existing: An already-open ``bridge_node`` handoff for this node
                whose identity already matches -- reuse it (same rationale as
                :meth:`_open_and_wait_for_edit`) instead of creating a new
                one. ``None`` (the default) creates a fresh handoff.

        Returns:
            The filename to report as this call's STRING output: the
            handoff's own ``source.filename`` (the ORIGINAL generated PSD on
            reuse, *psd_path*'s name for a fresh handoff).
        """
        manager = state.manager
        if existing is None:
            meta = PhotoshopComposePSD._create_bridge_handoff(
                state, node_id, psd_path, identity_hash, composite_image
            )
            result_name = psd_path.name
        else:
            # Reuse: do NOT rewrite the managed PSD copy -- the user's
            # in-progress layers live in it. Same rule as
            # cpsb/annotate.py:550-557.
            meta = existing
            result_name = existing.source.filename
        handoff_psd_path = manager.psd_path(meta)

        if existing is not None:
            # Reusing an already-open handoff in a NON-BLOCKING mode: do not
            # relaunch Photoshop. This is the same rule (and the same reason)
            # as cpsb/nodes.py:434-452 -- "Re-run on every save" re-executes on
            # every single save, so relaunching here would yank focus back to
            # Photoshop (and, on Tier 1, re-issue an OS open) on each one,
            # which is precisely the "fires off a bunch of quick commands"
            # disruption this node was reported for. The document is already
            # open in front of the user; there is nothing to open.
            logger.info(
                "cpsb compose_psd: node %s handoff %s: handoff already open, not reopening",
                node_id,
                meta.handoff_id,
            )
            return result_name

        attempt = nodes.PhotoshopBridge._open_in_photoshop(state, meta, handoff_psd_path)
        if attempt.ok:
            logger.info(
                "cpsb compose_psd: node %s handoff %s: opened Photoshop (tier %d)",
                node_id,
                meta.handoff_id,
                attempt.tier,
            )
        else:
            logger.warning(
                "cpsb compose_psd: node %s handoff %s: could not open Photoshop (%s)",
                node_id,
                meta.handoff_id,
                attempt.error,
            )
        return result_name
