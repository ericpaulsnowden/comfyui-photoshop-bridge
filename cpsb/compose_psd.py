"""The ``PhotoshopComposePSD`` ComfyUI node (PROTOCOL.md §6c).

Takes N ``IMAGE`` inputs (``image_1``, ``image_2``, ...) and writes them as a
single group of pixel layers into one PSD, using ``psd-tools``' documented
construction API (``PSDImage.new`` -> ``create_pixel_layer`` -> ``create_group``,
research/research-multilayer-compose.md §1.1/§3). Canvas size is the max
width/height across every connected input; each layer keeps its own native
resolution (never rescaled) and is centered on the shared canvas;
``image_1`` becomes the BOTTOM layer, higher indices stack on top, and every
layer lands inside one named, expanded group. ``edit_after=True`` additionally
opens the written file in Photoshop, non-blocking, the same way a right-click
"Open in Photoshop" on a Load PSD node would.

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
from .handoff import HandoffManager, HandoffMeta, SourceRef, compute_source_hash

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
    pil_images: list[Image.Image], filename_prefix: str, group_name: str, edit_after: bool
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
    §1/§6). ``filename_prefix``/``group_name``/``edit_after`` are folded in
    too, since a change to any of them should also force re-execution
    (a different filename or group label is a different desired output, and
    toggling ``edit_after`` changes what execute() does even for pixel-
    identical inputs).

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
        edit_after: The ``edit_after`` widget value.

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
    hasher.update(b"1" if edit_after else b"0")
    return hasher.hexdigest()


def _find_matching_active_handoff(
    manager: HandoffManager, node_id: str, inputs_hash: str
) -> HandoffMeta | None:
    """The active ``load_psd`` handoff for *node_id*, iff it has a consumable edit.

    Mirrors :func:`cpsb.load_psd._find_matching_active_handoff`'s predicate
    exactly (same shape, deliberately re-implemented rather than imported --
    ``cpsb/load_psd.py`` is owned by another concurrent change; importing a
    private helper from it would couple this module to that one's internals
    for no real benefit, matching how ``cpsb/nodes.py`` and
    ``cpsb/load_psd.py`` already keep small, parallel helpers rather than
    sharing every last one). Shared by
    :meth:`PhotoshopComposePSD.IS_CHANGED` and
    :meth:`PhotoshopComposePSD.execute`.

    Args:
        manager: The handoff manager.
        node_id: This node instance's ``unique_id``, stringified.
        inputs_hash: :func:`_compute_inputs_hash` of the CURRENT inputs/params.

    Returns:
        The matching handoff, or ``None`` when there is no active handoff for
        this node, it isn't ``origin_kind == "load_psd"`` (defensive -- see
        :func:`cpsb.load_psd._find_matching_active_handoff`'s identical
        note), its recorded ``source_hash`` doesn't equal *inputs_hash*, or
        it has no edits yet -- each meaning "write a fresh compose instead".
    """
    active = manager.find_active_for_node(node_id)
    if (
        active is not None
        and active.origin_kind == "load_psd"
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

    ``edit_after=True`` additionally creates a ``load_psd`` handoff for the
    just-written file and opens Photoshop non-blocking (PROTOCOL.md §6c;
    see :meth:`_open_after_compose`'s docstring for exactly what "non-
    blocking" and "managed copy, not edit-in-place" mean for this v1).

    Consume semantics mirror :class:`~cpsb.load_psd.PhotoshopLoadPSD`
    exactly, keyed off :func:`_compute_inputs_hash` instead of a single
    file's raw bytes (PROTOCOL.md §6c): while an ACTIVE ``load_psd`` handoff
    for this node has a ``source_hash`` matching the CURRENT inputs' combined
    hash and at least one edit, :meth:`execute` returns that edit's pixels
    instead of composing fresh -- so re-queuing after a Photoshop save
    delivers the user's manual compositing/masking work, the same "consume
    the edit" pattern PROTOCOL.md §6/§6b establish.
    """

    CATEGORY = "image/photoshop"
    RETURN_TYPES = ("IMAGE", "MASK", "STRING")
    FUNCTION = "execute"

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, Any]:
        """``filename_prefix``/``group_name``/``edit_after`` + up to
        :data:`MAX_IMAGE_INPUTS` optional ``image_N`` sockets + hidden
        ``unique_id`` (PROTOCOL.md §6c).

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
                "edit_after": ("BOOLEAN", {"default": False}),
            },
            "optional": optional,
            "hidden": {"unique_id": "UNIQUE_ID"},
        }

    @classmethod
    def IS_CHANGED(
        cls,
        filename_prefix: str,
        group_name: str,
        edit_after: bool,
        unique_id: str,
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
        """
        pil_images = [nodes._tensor_to_pil(t) for t in _collect_connected_images(kwargs)]
        prefix = _sanitize_filename_prefix(filename_prefix)
        inputs_hash = _compute_inputs_hash(pil_images, prefix, group_name, edit_after)

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
        edit_after: bool,
        unique_id: str,
        **kwargs: Any,
    ) -> tuple[Any, Any, str]:
        """Compose (or consume) and return ``(IMAGE, MASK, STRING)`` (PROTOCOL.md §6c).

        Serves a consumable active edit first (see the class docstring's
        "Consume semantics" paragraph); otherwise composes the connected
        inputs fresh, writes the PSD, and -- when ``edit_after`` -- opens
        Photoshop on it (:meth:`_open_after_compose`).

        Args:
            filename_prefix: Base name for the written file (sanitized via
                :func:`_sanitize_filename_prefix` before use).
            group_name: Name of the single group every layer is placed in.
            edit_after: Whether to open the written file in Photoshop.
            unique_id: This node instance's id (ComfyUI's hidden
                ``UNIQUE_ID`` input), used to key its handoff lookup.
            **kwargs: The connected ``image_N`` tensors, whichever subset
                ComfyUI passed for this execution.

        Returns:
            ``(IMAGE, MASK, STRING)`` -- see the class docstring.

        Raises:
            ValueError: No ``image_N`` input is connected -- there is
                nothing to compose.
        """
        state = nodes._require_state()
        manager = state.manager
        node_id = str(unique_id)

        tensors = _collect_connected_images(kwargs)
        if not tensors:
            raise ValueError("PhotoshopComposePSD needs at least one connected image_N input")
        pil_images = [nodes._tensor_to_pil(tensor) for tensor in tensors]

        prefix = _sanitize_filename_prefix(filename_prefix)
        inputs_hash = _compute_inputs_hash(pil_images, prefix, group_name, edit_after)

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
            "cpsb compose_psd: node %s: composing %d layer(s) into group %r",
            node_id,
            len(pil_images),
            group_name,
        )
        psd, canvas_width, canvas_height, placements = _build_group_psd(pil_images, group_name)
        output_path = _allocate_output_path(state.context.input_dir, prefix)
        psd.save(output_path)
        logger.info("cpsb compose_psd: node %s: wrote %s", node_id, output_path)

        flattened = _flatten_placements(placements, canvas_width, canvas_height)
        image_tensor, mask_tensor = nodes._tensors_from_image(flattened)

        if edit_after:
            try:
                self._open_after_compose(state, node_id, output_path, inputs_hash, flattened)
            except Exception:
                # PROTOCOL.md §6c: "Failure to open = log + cpsb.status error
                # event, never a node crash" -- the composed outputs above
                # are already valid and must still be returned regardless of
                # what happens here. _open_in_photoshop (below) already
                # catches and marks its own ordinary failure modes; this is
                # a last-resort guard against a genuinely unexpected one.
                logger.exception(
                    "cpsb compose_psd: node %s: opening Photoshop after compose failed",
                    node_id,
                )

        return image_tensor, mask_tensor, output_path.name

    @staticmethod
    def _open_after_compose(
        state: nodes._NodeState,
        node_id: str,
        psd_path: Path,
        inputs_hash: str,
        composite_image: Image.Image,
    ) -> None:
        """``edit_after=True``: hand *psd_path* off to Photoshop (PROTOCOL.md §6c).

        Creates a ``load_psd``-origin handoff and opens it the same
        tier-selected, non-blocking way "Open in Photoshop" would for any
        other Load-PSD-style source -- reusing
        :meth:`cpsb.nodes.PhotoshopBridge._open_in_photoshop` (the
        restructured, tier-selecting, bounded open path
        :class:`~cpsb.nodes.PhotoshopBridge` itself calls; there is no
        narrower public seam exposed for this today -- see this package's
        build report for the exact call made and why). "Non-blocking" here
        means exactly what PROTOCOL.md §6's "Open only (don't wait)" mode
        means for the bridge node: this call returns as soon as the OS
        launch (Tier 1) or the plugin ``open_handoff`` send (Tier 2)
        completes -- it never waits for a save.

        **v1 uses a MANAGED COPY, not ``edit_in_place``**: the handoff's own
        ``source.psd`` is a byte-for-byte copy of *psd_path* (written here,
        via :func:`~pathlib.Path.write_bytes`, mirroring PROTOCOL.md §2's
        "COPIES that file verbatim -- never write_psd/frompil" rule for
        psd-native sources), not a pointer at *psd_path* itself. This is a
        deliberate scope decision for this build, not an oversight: wiring
        true ``edit_in_place`` here would mean setting
        ``HandoffManager.create(edit_in_place=True, original_path=...)`` and
        registering the path with ``CpsbWatcher.watch_original`` -- both are
        reached through ``cpsb/routes.py`` /
        ``cpsb/watcher.py``-adjacent plumbing this build does not own or
        touch (see the file-ownership note in this package's build report).
        Once that concurrent ``edit_in_place`` work has landed, upgrading
        this method to point the handoff at *psd_path* directly (skipping
        the copy) is the natural follow-up -- the generated file is this
        node's own output, so editing it in place is safe by construction,
        exactly as PROTOCOL.md §6c's own line about it says.

        ``source_hash`` is set to *inputs_hash* (the SAME value
        :func:`_compute_inputs_hash` produces for
        :meth:`PhotoshopComposePSD.IS_CHANGED`/``execute``'s own consume
        check) rather than a hash of *psd_path*'s bytes -- see
        :func:`_compute_inputs_hash`'s docstring for why: the consume check
        in ``execute()`` needs to recompute a comparable value cheaply from
        the CURRENT inputs on every call, which a PSD-bytes hash cannot
        support without re-serializing a file just to test equality.

        Args:
            state: The shared backend state.
            node_id: This node instance's id (for logging).
            psd_path: The just-written compose output (already on disk).
            inputs_hash: :func:`_compute_inputs_hash` of the inputs that
                produced *psd_path* -- recorded as the handoff's
                ``source_hash``.
            composite_image: The flattened composite (for the handoff's
                ``orig_thumb.png``).
        """
        manager = state.manager
        psd_bytes = psd_path.read_bytes()

        meta = manager.create(
            origin_node_id=node_id,
            origin_kind="load_psd",
            workflow_name="",
            source=SourceRef(filename=psd_path.name, subfolder="", type="input"),
            original_image=composite_image,
            source_hash=inputs_hash,
        )
        handoff_psd_path = manager.handoff_dir(meta.handoff_id) / "source.psd"
        handoff_psd_path.parent.mkdir(parents=True, exist_ok=True)
        handoff_psd_path.write_bytes(psd_bytes)
        manager.note_source_written(meta.handoff_id)

        attempt = nodes.PhotoshopBridge._open_in_photoshop(state, meta, handoff_psd_path)
        if attempt.ok:
            logger.info(
                "cpsb compose_psd: node %s handoff %s: opened Photoshop (tier %d)",
                node_id,
                meta.handoff_id,
                attempt.tier,
            )
        else:
            # _open_in_photoshop already called manager.mark_error(...)
            # internally, which emits the cpsb.status error event
            # PROTOCOL.md §6c requires -- this is an additional, module-
            # local log line only.
            logger.warning(
                "cpsb compose_psd: node %s handoff %s: could not open Photoshop (%s)",
                node_id,
                meta.handoff_id,
                attempt.error,
            )
