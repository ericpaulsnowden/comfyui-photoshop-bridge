"""The ``PhotoshopAnnotate`` ComfyUI node (PROTOCOL.md §6d).

Pairs a typed instruction with a region mask so a downstream model gets both
"what to change" (STRING, never rendered onto pixels) and "where" (MASK),
without the user having to lay text out on the image by hand (the design
brief this node exists to satisfy, ``research/research-annotate-node.md``
§0). Two ways to produce the MASK: plug one in from any ComfyUI-only mask
source (MaskEditor, a segmentation node, ...), or let the user mark up an
auto-created **"Instructions" layer** in Photoshop and derive the mask from
that layer's own painted pixels (product-owner spec, 2026-07-17 --
superseding an earlier whole-image pixel-diff design, see below).

Reuses :mod:`cpsb.nodes`' shared plumbing rather than duplicating it: tensor
<-> PIL conversion (:func:`cpsb.nodes._tensor_to_pil` /
:func:`cpsb.nodes._pil_to_tensor`), the module-level backend state
(:func:`cpsb.nodes._require_state`, wired by the SAME single
``cpsb.nodes.configure()`` call in the top-level ``__init__.py`` -- see that
function's own docstring), and -- for opening Photoshop -- the bridge node's
tier-selecting open seam itself,
:meth:`cpsb.nodes.PhotoshopBridge._open_in_photoshop`, called directly rather
than re-implemented, so this node's Tier 1/Tier 2 behavior (and its bounded,
non-hanging Tier 2 send) is identical to the bridge node's own by
construction, not by a second copy that could drift.

**The "Instructions" layer redesign (product-owner spec, 2026-07-17).** PS
mode used to hand Photoshop a FLAT, layer-less PSD (:func:`cpsb.psd_io.write_psd`)
and derive the MASK by diffing the whole flattened image against whatever
came back -- workable with any tool/color, but unable to tell "the user
painted a mask" apart from "the user edited the picture itself", and unable
to preserve either signal independently. This node now instead writes the
handoff PSD LAYERED (:func:`_write_instructions_psd`): the input image as a
bottom pixel layer, plus a fully-transparent, top-level layer named exactly
:data:`INSTRUCTIONS_LAYER_NAME` on top, ready for the user to draw on. On
save, :func:`_read_ps_saved_psd` reopens that same saved ``source.psd`` with
psd-tools and looks for that layer BY NAME: if it is still there, its own
painted pixels (opaque vs. transparent) become the MASK directly, and the
IMAGE output becomes the composite of every OTHER layer -- so any edit the
user made to the base picture BAKES INTO the image output, while only the
Instructions layer is treated specially. If the user renamed or deleted that
layer, this falls back to treating the saved file as a plain edited image
(full composite, MASK from the ordinary mask-socket/zeros precedence) --
never a crash, just a degraded-but-valid result. The construction API used
to build the layered write -- ``PSDImage.new(mode="RGB")`` ->
``create_pixel_layer`` -- is the SAME one :mod:`cpsb.compose_psd` already
verified empirically against the installed psd-tools 1.17.4 (that module's
own docstring); :func:`_write_instructions_psd` and :func:`_layer_alpha_mask`
document the additional psd-tools claims specific to reading an alpha-
carrying layer back out of an RGB-mode document, verified the same way by
this module's own test suite.

PS mode BLOCKS the workflow until the user saves (PROTOCOL.md §6d, product-
owner update 2026-07-17): when there is no consumable edit yet, this node
writes/reuses a handoff, opens Photoshop through the shared seam above, and
then blocks in :meth:`cpsb.handoff.HandoffManager.wait_for_edit` -- the exact
same blocking primitive :meth:`cpsb.nodes.PhotoshopBridge.execute`'s "Wait
for first save" mode uses -- until the first save lands, the user cancels, or
*timeout_seconds* elapses. Cancel/timeout raise ComfyUI's own
``InterruptProcessingException`` via :func:`cpsb.nodes._raise_interrupt`,
reused rather than reimplemented for the identical reason.

Like :mod:`cpsb.nodes`, this module never imports ``torch``/``numpy`` at
module level: :func:`cpsb.nodes._tensor_to_pil` already proves plain
``numpy`` arrays (no torch) are enough for every pixel operation this node
needs, so importing ``numpy`` locally, inside each function that touches
array data, keeps this module importable in a plain test environment too.
``psd-tools``, by contrast, IS imported at module level here, exactly like
:mod:`cpsb.psd_io` and :mod:`cpsb.compose_psd` already do -- it is this
package's own declared dependency (``requirements.txt``), not a
ComfyUI-provided-but-undeclared one, so there is no test-environment
importability reason to defer it.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PIL import Image, ImageDraw
from psd_tools import PSDImage

from . import nodes
from .handoff import HandoffManager, HandoffMeta, SourceRef, WaitOutcome, compute_source_hash

if TYPE_CHECKING:
    from .nodes import _NodeState

logger = logging.getLogger("cpsb")

#: Exact top-level layer name this node looks for on read, and writes on
#: open (product-owner spec, 2026-07-17). Part of the protocol contract --
#: not just a display label -- so it lives here as a named constant rather
#: than inlined at each use site.
INSTRUCTIONS_LAYER_NAME = "Instructions"

#: Name for the bottom (base-image) layer written alongside
#: :data:`INSTRUCTIONS_LAYER_NAME`. Unlike that name, this one is NOT part of
#: the read-side contract (the base layer is identified by "not being the
#: Instructions layer", never by its own name) -- purely cosmetic, so the
#: layer has a sensible label in Photoshop's own layer panel.
_BASE_LAYER_NAME = "Image"

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


def _write_instructions_psd(psd_path: Path, pil_image: Image.Image) -> None:
    """Write *pil_image* as a LAYERED handoff PSD (product-owner spec, 2026-07-17).

    Two top-level pixel layers, bottom to top: *pil_image* itself (named
    :data:`_BASE_LAYER_NAME`), then a fully-transparent layer named exactly
    :data:`INSTRUCTIONS_LAYER_NAME`, sized to the same canvas -- so Photoshop
    opens with an empty layer already selected and ready to draw on. Uses the
    SAME ``PSDImage.new(mode="RGB")`` -> ``create_pixel_layer`` construction
    API :mod:`cpsb.compose_psd` already verified empirically against the
    installed psd-tools 1.17.4 (that module's own docstring), not
    ``PSDImage.frompil`` (:mod:`cpsb.psd_io`'s old flat write -- always
    layer-less, exactly what this node no longer wants).

    One psd-tools trap, specific to the transparent layer and the reason this
    function does not simply call ``create_pixel_layer`` twice: psd-tools
    decides where a layer's alpha goes purely from the PARENT document's
    ``pil_mode`` at ``create_pixel_layer`` time. For an ``"RGB"`` document it
    converts the RGBA source down to RGB -- compositing a fully transparent
    source onto BLACK -- and re-attaches the discarded alpha as an all-zero
    USER_LAYER_MASK (``psd_tools/api/layers.py``, ``PixelLayer.frompil``).
    Photoshop then opens a black layer behind a black mask that hides every
    brush stroke, so painting on it appears to do nothing at all. Temporarily
    advertising a transparency band (channels 3 -> 4) makes ``pil_mode``
    report ``"RGBA"`` for the duration of that ONE call, so the alpha is
    written where Photoshop expects an ordinary empty layer to carry it: the
    layer's own TRANSPARENCY_MASK (channel -1), with no layer mask at all.
    The count is restored before :meth:`~psd_tools.api.psd_image.PSDImage.save`
    so the FILE stays a plain 3-channel RGB document and never gains a stray
    "Alpha 1" channel in Photoshop's Channels panel.

    Args:
        psd_path: Destination ``source.psd`` path. Parent directories are
            created if needed.
        pil_image: The node's input image, decoded. Always ``"RGB"`` in
            practice (:func:`cpsb.nodes._tensor_to_pil` never produces
            anything else), but converted defensively here too.
    """
    psd_path.parent.mkdir(parents=True, exist_ok=True)
    width, height = pil_image.size
    psd = PSDImage.new(mode="RGB", size=(width, height), depth=8)
    psd.create_pixel_layer(
        pil_image.convert("RGB"), name=_BASE_LAYER_NAME, top=0, left=0, opacity=255
    )
    blank_instructions = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    header = psd._record.header
    header.channels += 1
    try:
        psd.create_pixel_layer(
            blank_instructions, name=INSTRUCTIONS_LAYER_NAME, top=0, left=0, opacity=255
        )
    finally:
        header.channels -= 1
    psd.save(psd_path)


def _find_top_level_layer(psd: PSDImage, name: str) -> Any | None:
    """The first TOP-LEVEL (direct-child) layer of *psd* named exactly *name*, or ``None``.

    Deliberately iterates *psd* itself, never ``psd.descendants()``:
    :class:`~psd_tools.api.psd_image.PSDImage` is itself a psd-tools
    ``GroupMixin`` (verified against the installed 1.17.4 -- ``for layer in
    psd`` yields only its immediate children, exactly like iterating a
    ``Group``), so this only ever matches a genuinely TOP-LEVEL layer, never
    one nested inside some group the user happened to create while marking
    up the image -- matching the product-owner spec's own wording, "a
    top-level layer named exactly Instructions".

    Args:
        psd: The already-open document.
        name: The exact layer name to match (case-sensitive, no trimming).

    Returns:
        The matching layer object, or ``None`` if no top-level layer has
        that exact name.
    """
    for layer in psd:
        if layer.name == name:
            return layer
    return None


def _layer_alpha_mask(psd: PSDImage, layer: Any) -> Any:
    """*layer*'s own opacity as an ``(H, W)`` float32 0..1 array, full-canvas-sized.

    Uses ``layer.composite(viewport=psd.viewbox)`` rather than
    ``layer.topil()``, for two independent reasons, both verified
    empirically against the installed psd-tools 1.17.4 (this module's own
    test suite builds and reopens real PSDs to check both):

    1. ``composite()`` is the only reader that handles BOTH shapes this
       layer can arrive in. :func:`_write_instructions_psd` hands Photoshop
       a layer whose opacity lives in its own TRANSPARENCY_MASK channel,
       but the user is free to add a real layer mask in Photoshop (or open
       a PSD written by an older version of this node, where the alpha WAS
       an all-zero USER_LAYER_MASK). ``layer.topil()`` ignores a layer mask
       entirely and reads back fully opaque; ``layer.composite()`` applies
       both, so it reflects what the user actually sees on screen.
    2. Photoshop commonly trims a saved layer's own pixel bounds down to
       just its non-transparent region -- a mostly-empty "Instructions"
       layer is exactly this case. Passing ``viewport=psd.viewbox``
       (the document's own full canvas box) forces the composited result
       back onto the FULL canvas at the correct offset, instead of
       returning an image cropped to wherever the user happened to paint.

    Args:
        psd: The already-open document (consulted for its canvas
            size/viewbox).
        layer: The layer to read (any composable layer -- pixel layer or
            group).

    Returns:
        A ``(H, W)`` float32 array, values in ``[0.0, 1.0]``. All-zero if
        the layer has no pixels at all within the viewport (nothing was
        ever drawn, or ``composite()`` finds nothing to render).
    """
    import numpy as np

    width, height = psd.size
    composite = layer.composite(viewport=psd.viewbox)
    if composite is None:
        return np.zeros((height, width), dtype=np.float32)
    alpha = np.array(composite.convert("RGBA").split()[-1]).astype(np.float32) / 255.0
    return alpha


def _composite_excluding_layer(psd: PSDImage, excluded_layer: Any) -> Image.Image:
    """RGB composite of every layer in *psd* EXCEPT *excluded_layer* itself.

    Filters by object IDENTITY (``candidate is not excluded_layer``), not by
    name: a differently-nested layer that happens to share the Instructions
    layer's name (inside some group the user created) is never accidentally
    excluded too -- only the exact layer object the caller found is.
    ``layer_filter`` is psd-tools' own documented hook for exactly this
    (:meth:`~psd_tools.api.psd_image.PSDImage.composite`'s ``layer_filter``
    parameter, called during compositing for every layer, nested or not);
    the default filter it replaces is ``PixelLayer.is_visible``, so ordinary
    visibility handling is preserved for every OTHER layer -- this only adds
    one further exclusion on top of it.

    Args:
        psd: The already-open document.
        excluded_layer: The layer to omit from the composite (the found
            Instructions layer).

    Returns:
        An ``"RGB"`` PIL image, sized to the document's canvas -- any edit
        made to another layer (e.g. the base image itself) is included,
        exactly the "bake edits in" behavior the product-owner spec calls
        for.
    """
    composite = psd.composite(
        layer_filter=lambda candidate: candidate.is_visible() and candidate is not excluded_layer
    )
    return composite.convert("RGB")


def _read_ps_saved_psd(psd_path: Path) -> tuple[Image.Image, Any | None, Image.Image]:
    """Resolve ``(image, mask, combined)`` from a saved layered handoff PSD.

    (product-owner spec, 2026-07-17; *combined* added 2026-07-18.)

    Args:
        psd_path: The handoff's ``source.psd`` -- the exact file Photoshop
            opened and, in Tier 1 / local Tier 2, the user's Cmd/Ctrl+S
            overwrote in place.

    Returns:
        ``(image, mask, combined)``:

        * A top-level layer named exactly :data:`INSTRUCTIONS_LAYER_NAME`
          IS found: *image* is the RGB composite of every OTHER layer
          (:func:`_composite_excluding_layer`) -- any edit the user made to
          the base layer bakes straight into this output; only the
          Instructions layer itself is treated specially. *mask* is that
          layer's own opacity (:func:`_layer_alpha_mask`), a ``(H, W)``
          float32 array, never ``None`` in this branch. *combined* is the
          FULL composite -- the base image WITH the user's real painted
          strokes on top, in their real colors -- which is what
          visual-prompt edit models (the "edit what I circled" convention)
          consume, as opposed to the clean *image* + *mask* pair an
          inpainting model wants.
        * NOT found (the user renamed or deleted it): *image* is the FULL
          composite of the whole document (every visible layer, psd-tools'
          own default filter); *mask* is ``None`` -- signalling the caller
          to fall back through the ordinary mask-input-socket/zeros
          precedence tiers, exactly as if this were a plain edited image
          with no Instructions layer involved at all (PROTOCOL.md §6d,
          "if that layer is renamed or deleted then the image is just
          treated like an image"). *combined* is that same full composite:
          with nothing designated as annotation, there is nothing separate
          left to combine, so the two views legitimately coincide.

    Note:
        In REMOTE Tier 2, the connected plugin saves to its own sandbox and
        uploads a flat PNG (:mod:`cpsb.routes`' upload handler) -- it never
        overwrites THIS server-side ``source.psd`` with a layered file, so
        re-opening it here after such an edit finds the ORIGINAL,
        just-written (still-blank) Instructions layer. That degrades
        gracefully through the FOUND branch above (mask ends up all-zero,
        image ends up unchanged) rather than crashing or surfacing the
        remote edit -- an accepted limitation until layered upload lands
        for Tier 2 remote, not a bug in this function.
    """
    psd = PSDImage.open(psd_path)
    instructions_layer = _find_top_level_layer(psd, INSTRUCTIONS_LAYER_NAME)
    if instructions_layer is None:
        full = psd.composite().convert("RGB")
        return full, None, full
    image = _composite_excluding_layer(psd, instructions_layer)
    mask = _layer_alpha_mask(psd, instructions_layer)
    combined = psd.composite().convert("RGB")
    return image, mask, combined


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


def _create_handoff(
    state: _NodeState, node_id: str, pil_image: Image.Image, source_hash: str
) -> HandoffMeta:
    """Write a fresh ``bridge_node`` handoff for *pil_image* (not yet opened).

    Mirrors :meth:`cpsb.nodes.PhotoshopBridge._create_handoff` (a bridge-node
    input is an in-memory tensor, not a file ``/view`` could address, so
    ``source`` is a descriptive placeholder -- PROTOCOL.md §6). Opening
    Photoshop is a separate step (:func:`_open_and_block_for_edit`) so the
    "reuse an existing handoff" and "create a fresh one" branches in
    :func:`_resolve_ps_mode_edit` can share one open/block call site. Writes
    the handoff's ``source.psd`` LAYERED (:func:`_write_instructions_psd`,
    product-owner spec 2026-07-17), not the old flat
    :func:`cpsb.psd_io.write_psd`.

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
    _write_instructions_psd(psd_path, pil_image)
    state.manager.note_source_written(meta.handoff_id)
    return meta


def _open_and_block_for_edit(
    state: _NodeState,
    node_id: str,
    meta: HandoffMeta,
    psd_path: Path,
    timeout_seconds: float,
) -> None:
    """Open Photoshop (tier-selected) and BLOCK until the first save (PROTOCOL.md §6d).

    Identical in shape to :meth:`cpsb.nodes.PhotoshopBridge.execute`'s "Wait
    for first save" tail: open via the shared, tier-selecting seam
    (:meth:`cpsb.nodes.PhotoshopBridge._open_in_photoshop`, reused rather than
    reimplemented -- it already logs tier selection and the launch result
    under its own ``cpsb bridge:`` prefix), then poll
    :meth:`cpsb.handoff.HandoffManager.wait_for_edit` on this thread (which
    does not block ComfyUI's event loop -- see ``cpsb.nodes``'s own module
    docstring). Every step is ALSO logged here under ``cpsb annotate:`` so a
    "didn't open Photoshop" report is diagnosable from this node's own log
    trail alone, without having to cross-reference the bridge node's prefix.

    Args:
        state: The configured backend state.
        node_id: This node instance's ``unique_id``, stringified.
        meta: The handoff being opened (new or reused).
        psd_path: The handoff's ``source.psd`` path.
        timeout_seconds: Bound on the blocking wait.

    Returns:
        Nothing. Returning normally means the wait outcome was
        :data:`~cpsb.handoff.WaitOutcome.EDITED` -- the caller may now
        re-open *psd_path* (Photoshop's own save) to resolve the image/mask.

    Raises:
        Whatever :func:`cpsb.nodes._raise_interrupt` raises (ComfyUI's own
        ``InterruptProcessingException`` when running inside ComfyUI, a
        plain ``RuntimeError`` otherwise): when the open attempt itself
        fails (never reaches the wait -- nothing to wait for), or the wait
        ends in ``CANCELLED``/``TIMEOUT``.
    """
    logger.info("cpsb annotate: node %s handoff %s: opening Photoshop", node_id, meta.handoff_id)
    attempt = nodes.PhotoshopBridge._open_in_photoshop(state, meta, psd_path)
    if attempt.ok:
        logger.info(
            "cpsb annotate: node %s handoff %s: launch result ok (tier %d)",
            node_id,
            meta.handoff_id,
            attempt.tier,
        )
    else:
        logger.warning(
            "cpsb annotate: node %s handoff %s: could not open Photoshop (tier %d): %s, "
            "interrupting",
            node_id,
            meta.handoff_id,
            attempt.tier,
            attempt.error,
        )
        # _open_in_photoshop already called manager.mark_error(...) for us
        # (both tiers' failure branches do); nothing left to do but stop the
        # workflow rather than hang waiting for a save that can never arrive.
        nodes._raise_interrupt()

    logger.info(
        "cpsb annotate: node %s handoff %s: waiting for edit (timeout=%ss)",
        node_id,
        meta.handoff_id,
        timeout_seconds,
    )
    outcome = state.manager.wait_for_edit(meta.handoff_id, float(timeout_seconds))
    logger.info(
        "cpsb annotate: node %s handoff %s: wait outcome '%s'",
        node_id,
        meta.handoff_id,
        outcome,
    )
    if outcome != WaitOutcome.EDITED:
        nodes._raise_interrupt()


def _resolve_ps_mode_edit(
    state: _NodeState,
    node_id: str,
    pil_image: Image.Image,
    source_hash: str,
    timeout_seconds: float,
) -> tuple[Image.Image, Any | None, Image.Image]:
    """The PS-mode ``(image, mask, combined)`` resolution (PROTOCOL.md §6d) -- BLOCKS.

    Node-reuse semantics mirror :meth:`cpsb.nodes.PhotoshopBridge.execute`
    exactly: an active handoff whose recorded ``source_hash`` no longer
    matches the current input belongs to OLD pixels and is superseded before
    anything else happens (a legacy handoff with no recorded ``source_hash``
    is treated as matching, same documented choice as the bridge node). Once
    that's settled, two cases remain: (a) the (possibly just-refreshed)
    active handoff already has an edit -- consume it by re-opening its
    ``source.psd`` with psd-tools (:func:`_read_ps_saved_psd`), WITHOUT
    reopening Photoshop (the existing consume behavior, unchanged); (b) no
    consumable edit exists yet (no active handoff, or one exists but hasn't
    been saved into) -- write a fresh handoff or reuse the existing one
    (:func:`_create_handoff` / :func:`_write_instructions_psd`), open
    Photoshop through the shared tier-selecting seam, and BLOCK this call
    (:func:`_open_and_block_for_edit`) until the user saves, cancels, or
    *timeout_seconds* elapses. This is the same "always (re)open, then wait"
    shape as the bridge node's "Wait for first save" mode -- there is no
    non-blocking mode left to preserve here (PROTOCOL.md §6d, product-owner
    update 2026-07-17): a manual re-queue after a prior timeout reuses and
    reopens the SAME handoff (layers intact), exactly like the bridge node's
    own documented re-queue-after-timeout behavior.

    Args:
        state: The configured backend state.
        node_id: This node instance's ``unique_id``, stringified.
        pil_image: The current input, decoded.
        source_hash: :func:`cpsb.handoff.compute_source_hash` of *pil_image*.
        timeout_seconds: Bound on the blocking wait, case (b) only.

    Returns:
        ``(image, mask, combined)`` -- per :func:`_read_ps_saved_psd`'s own
        contract:
        *image* is the composite excluding the Instructions layer when found,
        else the full document composite; *mask* is that layer's own opacity,
        or ``None`` when no Instructions layer was found (the caller falls
        back to the ordinary mask-socket/zeros precedence tiers for the
        MASK output only -- the IMAGE output stays the full composite either
        way); *combined* is the full composite WITH the painted strokes.

        Case (b) never returns without either a result or raising: a
        normal return from :func:`_open_and_block_for_edit` means the wait
        outcome was EDITED, so *psd_path* is confirmed to hold Photoshop's
        own save.

    Raises:
        See :func:`_open_and_block_for_edit` -- propagates unchanged.
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
        logger.info(
            "cpsb annotate: node %s handoff %s: edit already arrived, consuming without "
            "reopening",
            node_id,
            active.handoff_id,
        )
        psd_path = manager.handoff_dir(active.handoff_id) / "source.psd"
        return _read_ps_saved_psd(psd_path)

    if active is None:
        logger.info("cpsb annotate: node %s: no active handoff, creating", node_id)
        meta = _create_handoff(state, node_id, pil_image, source_hash)
    else:
        logger.info(
            "cpsb annotate: node %s handoff %s: reopening for blocking wait",
            node_id,
            active.handoff_id,
        )
        meta = active
    psd_path = manager.handoff_dir(meta.handoff_id) / "source.psd"

    _open_and_block_for_edit(state, node_id, meta, psd_path, timeout_seconds)
    # A normal return above means WaitOutcome.EDITED -- Photoshop's own save
    # now sits at psd_path, ready to be re-opened with psd-tools.
    return _read_ps_saved_psd(psd_path)


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

    Outputs ``(IMAGE, MASK, STRING, IMAGE)``. The ``STRING`` is *instruction*
    verbatim, never rendered onto any pixels (the whole point -- "not having
    to lay out text on an image", ``research/research-annotate-node.md``
    §0). The second ``IMAGE`` (*annotated*) is a copy of the resolved image
    with a red box drawn at the resolved mask's bounding box when
    *box_composite* is ``True`` (the Kontext/Qwen-Image-Edit box-annotation
    convention), else the resolved image unchanged.

    The first ``IMAGE`` output and the MASK are resolved together
    (product-owner spec, 2026-07-17 -- the "Instructions" layer redesign):

    * **Pass-through mode** (:data:`AnnotateMode.PASS_THROUGH`, the default):
      never even looks up a handoff. IMAGE is the unchanged input; MASK is
      the ``mask`` input socket if connected, else all-zero.
    * **PS mode** (:data:`AnnotateMode.PS_MODE`), once a save is consumable
      (see below): this node re-opens the saved handoff PSD with psd-tools
      and looks for a top-level layer named exactly
      :data:`INSTRUCTIONS_LAYER_NAME` (:func:`_read_ps_saved_psd`):

      - **Found**: MASK = that layer's own painted pixels (its opacity,
        normalized 0..1); IMAGE = the composite of every OTHER layer --
        so any edit the user made to the base picture itself BAKES INTO
        the image output, while only the Instructions layer is treated
        specially.
      - **Not found** (renamed or deleted by the user): IMAGE = the FULL
        composite of the whole saved document, treated as a plain edited
        image; MASK falls back to the ordinary tiers -- the ``mask`` input
        socket if connected, else all-zero.

    PS mode BLOCKS (PROTOCOL.md §6d, product-owner update 2026-07-17): with
    no matching edit yet, it writes (or reuses) a ``bridge_node`` handoff
    whose ``source.psd`` is a LAYERED PSD (input image + a blank
    "Instructions" layer, :func:`_write_instructions_psd`), opens Photoshop
    through the same tier-selecting seam the bridge node uses, and then
    blocks ``execute()`` -- identical to
    :meth:`cpsb.nodes.PhotoshopBridge.execute`'s "Wait for first save" mode
    -- until the user marks up and saves the image, cancels, or
    *timeout_seconds* elapses (see :func:`_open_and_block_for_edit`). A save
    resumes this same call, which then re-reads the saved ``source.psd`` per
    the precedence above; cancel/timeout raise ComfyUI's own
    ``InterruptProcessingException``, stopping the workflow rather than
    silently returning zeros. There is no re-run mode and so no auto-queue
    (PROTOCOL.md §5/§6d) -- once this node returns with a real result, the
    user is done; a re-queue only reopens Photoshop if they explicitly want
    another pass.
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
                "timeout_seconds": ("INT", {"default": 1800, "min": 10, "max": 86400}),
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
        timeout_seconds: int,
        unique_id: str,
        mask: Any = None,
    ) -> str:
        """Hash of every input, folded with the latest-edit hash when consumable.

        Base hash covers *image* (its :func:`cpsb.handoff.compute_source_hash`,
        the same PNG-encoding hash a handoff's own identity is keyed on),
        *instruction*, *annotate_mode*, *box_composite*, and whether *mask*
        is connected at all -- every input that can change this node's
        output on its own, without any Photoshop round trip (PROTOCOL.md
        §6d: "image+instruction+mask-presence+params hash"). *timeout_seconds*
        is accepted (ComfyUI passes every declared input to ``IS_CHANGED``)
        but deliberately NOT folded into the hash -- like the bridge node's
        own ``IS_CHANGED`` (PROTOCOL.md §6), it only bounds how long
        ``execute()`` waits, it never changes what a completed run produces,
        so hashing it would force needless re-execution on a mere timeout
        tweak. When *annotate_mode* is PS mode AND this node's active handoff
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
        timeout_seconds: int,
        unique_id: str,
        mask: Any = None,
    ) -> tuple[Any, Any, str, Any]:
        """``(IMAGE, MASK, STRING, IMAGE)`` per the class docstring's precedence rules.

        In PS mode with no consumable edit yet, this call BLOCKS (see the
        class docstring, :func:`_resolve_ps_mode_edit`,
        :func:`_open_and_block_for_edit`) until the user saves in Photoshop,
        cancels, or *timeout_seconds* elapses -- the latter two raise
        ComfyUI's own ``InterruptProcessingException`` rather than returning.

        Args:
            image: The ``IMAGE`` input tensor.
            instruction: The typed instruction, returned verbatim.
            annotate_mode: One of :class:`AnnotateMode`'s two values.
            box_composite: Whether to draw a red box on the *annotated*
                output.
            timeout_seconds: Bound on the PS-mode blocking wait; unused in
                Pass-through mode.
            unique_id: This node instance's id (ComfyUI's hidden
                ``UNIQUE_ID`` input), used to key its handoff lookup.
            mask: The optional ``MASK`` input socket.

        Returns:
            ``(image, mask, instruction, annotated)``. In Pass-through mode,
            *image* is the exact same tensor object passed in. In PS mode,
            *image* is derived from the saved handoff PSD (see the class
            docstring) -- it is the same object as the input only by
            coincidence, never guaranteed.
        """
        state = nodes._require_state()
        node_id = str(unique_id)
        logger.info(
            "cpsb annotate: node %s: execute() starting (mode=%r)", node_id, annotate_mode
        )

        pil_image = nodes._tensor_to_pil(image)
        source_hash = compute_source_hash(pil_image)

        if annotate_mode == AnnotateMode.PS_MODE:
            result_image, ps_mask_array, combined_image = _resolve_ps_mode_edit(
                state, node_id, pil_image, source_hash, timeout_seconds
            )
            image_tensor = nodes._pil_to_tensor(result_image)
        else:
            result_image = pil_image
            ps_mask_array = None
            # Pass-through (ComfyUI-only tier): there is no Photoshop document,
            # so no painted strokes exist to combine -- `annotated` degrades to
            # the box-or-unchanged behavior below.
            combined_image = None
            image_tensor = image  # same tensor object: pass-through never re-encodes

        mask_array = self._resolve_mask_array(ps_mask_array, mask, result_image)
        mask_tensor = _array_to_mask_tensor(mask_array)
        annotated = self._build_annotated(
            image_tensor, mask_array, bool(box_composite), combined_image
        )

        return image_tensor, mask_tensor, instruction, annotated

    @staticmethod
    def _resolve_mask_array(
        ps_mask_array: Any | None, mask_socket: Any, image_for_sizing: Image.Image
    ):
        """MASK fallback tiers (PROTOCOL.md §6d) -- the PS-mode layer read is resolved
        by the caller.

        Args:
            ps_mask_array: The Instructions layer's own opacity
                (:func:`_read_ps_saved_psd`), or ``None`` when unavailable
                (pass-through mode, or PS mode with no Instructions layer
                found).
            mask_socket: The node's optional ``mask`` input tensor, or
                ``None`` if unconnected.
            image_for_sizing: The resolved image for this execution (for
                zero-mask sizing only) -- PS mode's own resolved image, which
                may differ in size from the original input if the user
                resized the canvas in Photoshop.

        Returns:
            A ``(H, W)`` float32 numpy array.
        """
        if ps_mask_array is not None:
            return ps_mask_array
        if mask_socket is not None:
            return _mask_tensor_to_array(mask_socket)
        import numpy as np

        width, height = image_for_sizing.size
        return np.zeros((height, width), dtype=np.float32)

    @staticmethod
    def _build_annotated(
        image_tensor: Any,
        mask_array: Any,
        box_composite: bool,
        combined_image: Image.Image | None = None,
    ) -> Any:
        """The *annotated* output: the image WITH the annotation on it (PROTOCOL.md §6d).

        This is the "imaging layers and annotations combined" view, and which
        FORM the annotation takes is what ``box_composite`` selects:

        * ``box_composite=True`` -> a synthetic 4px pure-red unfilled rectangle
          at the mask's bounding box, drawn on the CLEAN image. This is the
          tidy box-prompt convention Kontext/Qwen-Image-Edit respond to, and
          deliberately replaces the raw strokes rather than adding to them: a
          rough marking blob plus a box around it is noisier for the model than
          the box alone.
        * ``box_composite=False`` -> *combined_image*: the base image with the
          user's REAL painted strokes on top, in their real colors.

        Before 2026-07-18 the ``False`` branch returned the image completely
        unannotated, which made the output indistinguishable from ``image`` and
        left no way at all to see what had actually been painted -- reported by
        the product owner as "if image is composited what is annotated?".

        *combined_image* is ``None`` in pass-through (ComfyUI-only) mode, where
        no Photoshop document and therefore no painted strokes exist; the output
        then falls back to the unchanged image, preserving the original
        behavior for that tier.

        Args:
            image_tensor: The node's own resolved image tensor (returned
                as-is when there is nothing to draw).
            mask_array: The final, precedence-resolved ``(H, W)`` mask.
            box_composite: The ``box_composite`` widget value.
            combined_image: The full PS composite including the painted
                strokes, or ``None`` when there is none.

        Returns:
            A tensor: *image_tensor* itself when there is nothing to show, or
            a freshly encoded copy carrying the annotation.
        """
        if not box_composite:
            if combined_image is None:
                return image_tensor
            return nodes._pil_to_tensor(combined_image)
        bbox = _bbox_of_nonzero(mask_array)
        if bbox is None:
            return image_tensor

        pil_image = nodes._tensor_to_pil(image_tensor).convert("RGB")
        annotated = pil_image.copy()
        ImageDraw.Draw(annotated).rectangle(bbox, outline=_BOX_COLOR, width=_BOX_STROKE_WIDTH)
        return nodes._pil_to_tensor(annotated)
