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
from pathlib import Path
from typing import Any

from PIL import Image
from psd_tools import PSDImage

from . import nodes
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


def _tensor_frames_to_pils(image: Any) -> list[Image.Image]:
    """Every frame of a ComfyUI ``IMAGE`` tensor (NHWC float32 [0,1]) as PIL images.

    A ComfyUI ``IMAGE`` is a BATCH: a VAE Decode (or any node) can emit several
    images on a single socket. Unlike :func:`cpsb.nodes._tensor_to_pil` (which
    keeps only the first frame), this expands the whole batch so each image
    becomes its own PSD layer (PROTOCOL.md §6c: multi-image batches -> layers).
    """
    import numpy as np

    frames: list[Image.Image] = []
    for frame in image:  # iterate the leading batch dimension
        array = frame.cpu().numpy() if hasattr(frame, "cpu") else np.asarray(frame)
        array = np.clip(array * 255.0, 0, 255).astype(np.uint8)
        frames.append(Image.fromarray(array, mode="RGB"))
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


def _compute_inputs_hash(
    pil_images: list[Image.Image], filename_prefix: str, group_name: str, mode: str
) -> str:
    """A deterministic sha256 identity for "these inputs, these params".

    Shared by :meth:`PhotoshopComposePSD.IS_CHANGED` and
    :meth:`PhotoshopComposePSD.execute` -- both need the IDENTICAL value so
    that a handoff created inside ``execute()`` (its ``source_hash``, see
    :meth:`PhotoshopComposePSD._open_after_compose`) can later be recognized
    as "still matching the current inputs" by a subsequent call to either
    method (PROTOCOL.md §6c: "execute() returns the latest edit ... when the
    active handoff's source_hash matches the current inputs' hash").

    Deliberately hashes the ORDERED CONCATENATION of each image's own
    :func:`~cpsb.handoff.compute_source_hash` (a "hash of hashes"), per
    research-multilayer-compose.md §6's recommendation for a multi-input
    ``source_hash``: any addition, removal, reorder, or pixel change of an
    input changes the combined result, matching what a single-image node's
    plain ``compute_source_hash`` comparison already guarantees (PROTOCOL.md
    §1/§6). ``filename_prefix``/``group_name``/``mode`` are folded in too,
    since a change to any of them should also force re-execution (a different
    filename or group label is a different desired output, and switching
    ``mode`` changes what execute() does even for pixel-identical inputs).
    Folding ``mode`` in also keeps a handoff created under one mode from ever
    being consumed by a run under a different mode: the recorded
    ``source_hash`` bakes the mode in, so a stale handoff from a
    since-switched-away-from mode simply fails the ``source_hash`` match in
    :func:`_find_matching_active_handoff` rather than needing a separate guard.

    Note this is NOT literally "sha256 of the PSD bytes that would be
    written" -- computing that would require re-serializing a full PSD on
    every ``IS_CHANGED`` call (expensive, and psd-tools' write path is not
    guaranteed byte-for-byte deterministic run to run) just to test
    equality. Hashing the inputs directly is cheap, deterministic by
    construction, and is what actually makes the "does this handoff still
    match" check in :meth:`PhotoshopComposePSD.execute` work without
    rewriting a file merely to compare it.

    Args:
        pil_images: The connected inputs, already decoded to PIL, in
            ``image_1..image_N`` order.
        filename_prefix: The (already-:func:`_sanitize_filename_prefix`'d)
            filename prefix.
        group_name: The ``group_name`` widget value.
        mode: The ``mode`` widget value (one of :data:`MODE_DONT_OPEN`,
            :attr:`cpsb.nodes.BridgeMode.WAIT_FIRST_SAVE`, or
            :attr:`cpsb.nodes.BridgeMode.RERUN_EVERY_SAVE`).

    Returns:
        A 64-char lowercase hex sha256 digest.
    """
    hasher = hashlib.sha256()
    for image in pil_images:
        hasher.update(compute_source_hash(image).encode("ascii"))
    hasher.update(filename_prefix.encode("utf-8"))
    hasher.update(b"\x00")
    hasher.update(group_name.encode("utf-8"))
    hasher.update(b"\x00")
    hasher.update(mode.encode("utf-8"))
    return hasher.hexdigest()


def _find_matching_active_handoff(
    manager: HandoffManager, node_id: str, inputs_hash: str
) -> HandoffMeta | None:
    """The active ``bridge_node`` handoff for *node_id*, iff it has a consumable edit.

    Mirrors :meth:`cpsb.nodes.PhotoshopBridge.execute`'s consume predicate
    (same shape, deliberately re-implemented rather than imported --
    ``cpsb/nodes.py`` is owned elsewhere and keeps its check inline; a small
    parallel helper here couples nothing to that module's internals). Shared
    by :meth:`PhotoshopComposePSD.IS_CHANGED` and
    :meth:`PhotoshopComposePSD.execute`.

    The ``origin_kind == "bridge_node"`` filter matches what this node's own
    open paths now write (:meth:`PhotoshopComposePSD._create_bridge_handoff`,
    PROTOCOL.md §6c: "The handoff uses origin_kind ``bridge_node``"), so the
    just-opened handoff -- and any edit saved into it -- is recognized on the
    next queue and its edit consumed. It replaces the earlier ``load_psd``
    filter the fire-and-forget ``edit_after`` build used.

    Args:
        manager: The handoff manager.
        node_id: This node instance's ``unique_id``, stringified.
        inputs_hash: :func:`_compute_inputs_hash` of the CURRENT inputs/params.

    Returns:
        The matching handoff, or ``None`` when there is no active handoff for
        this node, it isn't ``origin_kind == "bridge_node"`` (defensive --
        e.g. a leftover handoff of another kind for the same node id), its
        recorded ``source_hash`` doesn't equal *inputs_hash* (different
        inputs/params/mode), or it has no edits yet -- each meaning "write a
        fresh compose instead".
    """
    active = manager.find_active_for_node(node_id)
    if (
        active is not None
        and active.origin_kind == "bridge_node"
        and active.source_hash == inputs_hash
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


def _build_group_psd(
    pil_images: list[Image.Image], group_name: str
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
        ``(rgb_image, left, top)`` per layer, bottom-to-top, reused by
        :func:`_flatten_placements` so the IMAGE/MASK outputs are derived
        from the exact same positions just written to disk rather than by
        re-reading the file back.
    """
    canvas_width = max(image.width for image in pil_images)
    canvas_height = max(image.height for image in pil_images)
    psd = PSDImage.new(mode="RGB", size=(canvas_width, canvas_height), depth=8)

    layers = []
    placements: list[tuple[Image.Image, int, int]] = []
    for index, image in enumerate(pil_images, start=1):
        rgb_image = image.convert("RGB")
        left = _centered_offset(rgb_image.width, canvas_width)
        top = _centered_offset(rgb_image.height, canvas_height)
        layer = psd.create_pixel_layer(
            rgb_image, name=f"Layer {index}", top=top, left=left, opacity=_LAYER_OPACITY
        )
        layers.append(layer)
        placements.append((rgb_image, left, top))

    psd.create_group(layer_list=layers, name=group_name)
    return psd, canvas_width, canvas_height, placements


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

    Every layer is fully opaque (no per-layer alpha exists: a ComfyUI
    ``IMAGE`` tensor never carries one), so compositing bottom-to-top is a
    plain, in-order overwrite of each layer's own bounding box; the
    resulting alpha channel is 255 wherever ANY layer's bbox covered that
    pixel, 0 elsewhere (canvas regions no input reaches -- possible when the
    max WIDTH and max HEIGHT come from different inputs, so no single image,
    nor necessarily their union, covers every corner). RGB under an alpha-0
    pixel is black (the canvas's own fill color) -- never accessed by a
    downstream consumer that respects alpha, but still a fixed, deterministic
    value rather than undefined.

    Args:
        placements: ``(rgb_image, left, top)`` bottom-to-top, from
            :func:`_build_group_psd`.
        canvas_width: Document width.
        canvas_height: Document height.

    Returns:
        An ``"RGBA"`` image, ready for
        :func:`cpsb.nodes._tensors_from_image` (its ``"A" in mode`` check is
        exactly what turns this alpha channel into the MASK output).
    """
    canvas = Image.new("RGB", (canvas_width, canvas_height), (0, 0, 0))
    coverage = Image.new("L", (canvas_width, canvas_height), 0)
    for image, left, top in placements:
        canvas.paste(image, (left, top))
        coverage.paste(255, (left, top, left + image.width, top + image.height))
    result = canvas.convert("RGBA")
    result.putalpha(coverage)
    return result


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
    own ``/view``.

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

    Consume semantics mirror :meth:`cpsb.nodes.PhotoshopBridge.execute`,
    keyed off :func:`_compute_inputs_hash` instead of a single file's raw
    bytes (PROTOCOL.md §6c): while an ACTIVE ``bridge_node`` handoff for this
    node has a ``source_hash`` matching the CURRENT inputs' combined hash and
    at least one edit, :meth:`execute` returns that edit's pixels (flattened)
    instead of composing fresh -- so re-queuing after a Photoshop save
    delivers the user's manual compositing/masking work, the same "consume
    the edit" pattern PROTOCOL.md §6/§6b establish. This consume check runs
    first for EVERY mode, so an already-saved edit is served without
    re-opening Photoshop regardless of which mode is selected.
    """

    CATEGORY = "image/photoshop"
    RETURN_TYPES = ("IMAGE", "MASK", "STRING")
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
        """
        optional = {f"image_{i}": ("IMAGE",) for i in range(1, MAX_IMAGE_INPUTS + 1)}
        return {
            "required": {
                "filename_prefix": ("STRING", {"default": DEFAULT_FILENAME_PREFIX}),
                "group_name": ("STRING", {"default": DEFAULT_GROUP_NAME}),
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
            },
            "optional": optional,
            "hidden": {"unique_id": "UNIQUE_ID"},
        }

    @classmethod
    def IS_CHANGED(
        cls,
        filename_prefix: str,
        group_name: str,
        mode: str,
        timeout_seconds: int,
        unique_id: str,
        max_layers: int = DEFAULT_MAX_LAYERS,
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
        genuinely changes the output.
        """
        pil_images, _ = _collect_layer_images(kwargs, max_layers)
        prefix = _sanitize_filename_prefix(filename_prefix)
        inputs_hash = _compute_inputs_hash(pil_images, prefix, group_name, mode)

        state = nodes._state_if_configured()
        if state is None:
            return inputs_hash
        active = _find_matching_active_handoff(state.manager, str(unique_id), inputs_hash)
        if active is not None:
            edit_hash = state.manager.latest_edit_hash(active.handoff_id)
            if edit_hash is not None:
                return f"{inputs_hash}:{edit_hash}"
        return inputs_hash

    def execute(
        self,
        filename_prefix: str,
        group_name: str,
        mode: str,
        timeout_seconds: int,
        unique_id: str,
        max_layers: int = DEFAULT_MAX_LAYERS,
        **kwargs: Any,
    ) -> tuple[Any, Any, str]:
        """Compose (or consume) and return ``(IMAGE, MASK, STRING)`` (PROTOCOL.md §6c).

        Serves a consumable active edit first (the class docstring's "Consume
        semantics" paragraph -- this runs for EVERY mode, so an already-saved
        edit is returned without re-opening Photoshop). Otherwise composes
        the connected inputs fresh, writes the LAYERED PSD, and dispatches on
        *mode*:

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
        file that was composed and handed off.

        Args:
            filename_prefix: Base name for the written file (sanitized via
                :func:`_sanitize_filename_prefix` before use).
            group_name: Name of the single group every layer is placed in.
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
            **kwargs: The connected ``image_N`` tensors (each possibly a
                multi-image batch), whichever subset ComfyUI passed.

        Returns:
            ``(IMAGE, MASK, STRING)`` -- see the class docstring.

        Raises:
            ValueError: No ``image_N`` input is connected -- there is
                nothing to compose.
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
        inputs_hash = _compute_inputs_hash(pil_images, prefix, group_name, mode)

        active = _find_matching_active_handoff(manager, node_id, inputs_hash)
        if active is not None:
            consumed = _consume_active_edit(manager, active.handoff_id)
            if consumed is not None:
                logger.info(
                    "cpsb compose_psd: node %s handoff %s: consuming latest edit",
                    node_id,
                    active.handoff_id,
                )
                image_tensor, mask_tensor = consumed
                return image_tensor, mask_tensor, active.source.filename

        logger.info(
            "cpsb compose_psd: node %s: composing %d layer(s) into group %r (mode=%r)",
            node_id,
            len(pil_images),
            group_name,
            mode,
        )
        psd, canvas_width, canvas_height, placements = _build_group_psd(pil_images, group_name)
        output_path = _allocate_output_path(state.context.input_dir, prefix)
        psd.save(output_path)
        logger.info("cpsb compose_psd: node %s: wrote %s", node_id, output_path)

        flattened = _flatten_placements(placements, canvas_width, canvas_height)
        image_tensor, mask_tensor = nodes._tensors_from_image(flattened)

        if mode == MODE_DONT_OPEN:
            # Old always-flat behavior: no Photoshop, no handoff.
            return image_tensor, mask_tensor, output_path.name

        if mode == nodes.BridgeMode.WAIT_FIRST_SAVE:
            # BLOCKS until the first save; returns the SAVED edit (flattened).
            # Deliberately NOT wrapped in try/except: an open failure or a
            # cancel/timeout must propagate as InterruptProcessingException
            # (the bridge/annotate contract), never be swallowed here.
            image_tensor, mask_tensor = self._open_and_wait_for_edit(
                state, node_id, output_path, inputs_hash, flattened, timeout_seconds
            )
            return image_tensor, mask_tensor, output_path.name

        # mode == BridgeMode.RERUN_EVERY_SAVE: open non-blocking, pass the flat
        # composite through. PROTOCOL.md §6c: "Failure to open = log + cpsb.status
        # error event, never a node crash" -- the composed outputs above are
        # already valid and must still be returned. _open_in_photoshop already
        # catches and marks its own ordinary failure modes; this try/except is a
        # last-resort guard against a genuinely unexpected one.
        try:
            self._open_passthrough(state, node_id, output_path, inputs_hash, flattened)
        except Exception:
            logger.exception(
                "cpsb compose_psd: node %s: opening Photoshop after compose failed",
                node_id,
            )

        return image_tensor, mask_tensor, output_path.name

    @staticmethod
    def _create_bridge_handoff(
        state: nodes._NodeState,
        node_id: str,
        psd_path: Path,
        inputs_hash: str,
        composite_image: Image.Image,
    ) -> HandoffMeta:
        """Create the ``bridge_node`` handoff whose ``source.psd`` is *psd_path* copied.

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
        handoff's ``source.psd`` is a copy of *psd_path* rather than a pointer
        at it. PROTOCOL.md §6c's own line says the generated file is this
        node's own output, so editing it in place would be safe by
        construction -- but wiring true ``edit_in_place`` means
        ``HandoffManager.create(edit_in_place=True, original_path=...)`` PLUS
        registering that out-of-managed-folder path with the watcher
        (``CpsbWatcher.watch_original``), reached through
        ``cpsb/routes.py``/``cpsb/watcher.py`` plumbing this change does not
        own. The managed copy makes the blocking round trip work end-to-end
        all the same (the watcher already covers the managed folder, so a
        Photoshop save into ``source.psd`` is ingested and unblocks the wait);
        pointing the handoff directly at *psd_path* is the natural follow-up
        once that ``edit_in_place`` plumbing lands.

        ``source_hash`` is set to *inputs_hash* (the SAME value the consume
        check recomputes from the current inputs) rather than a hash of
        *psd_path*'s bytes -- see :func:`_compute_inputs_hash`'s docstring for
        why a PSD-bytes hash cannot support the cheap per-call equality test
        the consume path needs.

        Args:
            state: The shared backend state.
            node_id: This node instance's id (the handoff's ``origin_node_id``).
            psd_path: The just-written compose output (already on disk).
            inputs_hash: :func:`_compute_inputs_hash` of the inputs that
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
            source_hash=inputs_hash,
        )
        handoff_psd_path = manager.handoff_dir(meta.handoff_id) / "source.psd"
        handoff_psd_path.parent.mkdir(parents=True, exist_ok=True)
        handoff_psd_path.write_bytes(psd_bytes)
        manager.note_source_written(meta.handoff_id)
        return meta

    @staticmethod
    def _open_and_wait_for_edit(
        state: nodes._NodeState,
        node_id: str,
        psd_path: Path,
        inputs_hash: str,
        composite_image: Image.Image,
        timeout_seconds: int,
    ) -> tuple[Any, Any]:
        """"Wait for first save": open Photoshop and BLOCK until saved (PROTOCOL.md §6c).

        Identical in shape to :meth:`cpsb.nodes.PhotoshopBridge.execute`'s
        "Wait for first save" tail and :func:`cpsb.annotate._open_and_block_for_edit`:
        create the handoff (:meth:`_create_bridge_handoff`), open via the
        shared tier-selecting seam
        (:meth:`cpsb.nodes.PhotoshopBridge._open_in_photoshop`, reused rather
        than reimplemented -- it already logs tier selection and the launch
        result), then poll :meth:`cpsb.handoff.HandoffManager.wait_for_edit`
        on this (worker) thread until the first save. Every step is also
        logged under ``cpsb compose_psd:`` so a "didn't open Photoshop" report
        is diagnosable from this node's own log trail.

        Args:
            state: The shared backend state.
            node_id: This node instance's id.
            psd_path: The just-written compose output.
            inputs_hash: Recorded as the handoff's ``source_hash``.
            composite_image: The flattened composite (thumbnail, and the
                fallback returned if the edit file races away after the wait).
            timeout_seconds: Bound on the blocking wait.

        Returns:
            ``(IMAGE, MASK)`` tensors of the SAVED edit (flattened) -- a
            normal return means the wait outcome was
            :data:`~cpsb.handoff.WaitOutcome.EDITED`.

        Raises:
            Whatever :func:`cpsb.nodes._raise_interrupt` raises (ComfyUI's own
            ``InterruptProcessingException`` inside ComfyUI): when the open
            attempt fails (never reaches the wait), or the wait ends in
            ``CANCELLED``/``TIMEOUT``.
        """
        manager = state.manager
        meta = PhotoshopComposePSD._create_bridge_handoff(
            state, node_id, psd_path, inputs_hash, composite_image
        )
        handoff_psd_path = manager.handoff_dir(meta.handoff_id) / "source.psd"

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
            return consumed
        # Filesystem race: the edit file vanished between ingest and read.
        # Fall back to the flat composite so the outputs stay valid.
        return nodes._tensors_from_image(composite_image)

    @staticmethod
    def _open_passthrough(
        state: nodes._NodeState,
        node_id: str,
        psd_path: Path,
        inputs_hash: str,
        composite_image: Image.Image,
    ) -> None:
        """"Re-run on every save": open Photoshop non-blocking (PROTOCOL.md §6c).

        Creates the ``bridge_node`` handoff (:meth:`_create_bridge_handoff`)
        and opens it through the shared tier-selecting seam, returning as soon
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
            inputs_hash: Recorded as the handoff's ``source_hash``.
            composite_image: The flattened composite (for the thumbnail).
        """
        manager = state.manager
        meta = PhotoshopComposePSD._create_bridge_handoff(
            state, node_id, psd_path, inputs_hash, composite_image
        )
        handoff_psd_path = manager.handoff_dir(meta.handoff_id) / "source.psd"

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
