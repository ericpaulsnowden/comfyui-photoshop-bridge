"""The ``PhotoshopLoadPSD`` ComfyUI node (PROTOCOL.md §6b).

Lets a workflow START from a ``.psd``/``.psb`` file living in ComfyUI's
``input/`` directory instead of a flat raster image: the ``psd`` COMBO input
lists those files, mirroring ``LoadImage.INPUT_TYPES`` -- verified against
ComfyUI's real source (``comfyanonymous/ComfyUI``, ``nodes.py``, current
``master``, fetched directly from raw.githubusercontent.com while building
this node, the same verification standard :mod:`cpsb.nodes`'s own module
docstring uses):

.. code-block:: python

    @classmethod
    def INPUT_TYPES(s):
        input_dir = folder_paths.get_input_directory()
        files = [f for f in os.listdir(input_dir) if os.path.isfile(os.path.join(input_dir, f))]
        files = folder_paths.filter_files_content_types(files, ["image"])
        return {"required": {"image": (sorted(files), {"image_upload": True})}}

:func:`_list_psd_files` below reproduces the ``os.listdir`` + ``os.path.isfile``
shape (flat, non-recursive) exactly, but filters by extension instead of
``folder_paths.filter_files_content_types(files, ["image"])``: a PSD's guessed
MIME type (``image/vnd.adobe.photoshop``) WOULD pass that "image" content-type
filter, but Pillow's PSD plugin garbles multi-layer files if handed to the
stock ``LoadImage``-style widget -- multi-layer PSDs are misreported as
animated frames and never update size on ``seek()``, defeating LoadImage's own
mismatch guard (research-psd-loading.md §1, empirically confirmed). That is
the whole reason this is a dedicated node rather than a `LoadImage` reuse, and
also why the combo below carries no ``image_upload`` option: that flag is what
``cpsb.js``' frontend companion (`menu.js::captureImageUploadType`) -- and
ComfyUI's own core ``uploadImage.ts`` extension -- key off to attach the stock
upload widget, which is hardcoded to ``png``/``jpeg``/``webp`` and cannot
accept a ``.psd``/``.psb`` file at all (PROTOCOL.md §6b: the frontend instead
adds its own hand-rolled upload widget, out of this package's scope).

Shares its tensor plumbing with :mod:`cpsb.nodes`
(:class:`~cpsb.nodes.PhotoshopBridge`) rather than duplicating it: both nodes
derive a MASK output from a resolved image the identical way (``1 - alpha``,
else zeros -- PROTOCOL.md §6/§6b; a prior third tier, an extracted document
channel mask, was removed, PROTOCOL.md §4: "owner's call"). This module calls
into ``nodes``'s helpers via the module object (``nodes._tensors_from_edit_file(...)``
etc.), mirroring the existing ``routes.launch_photoshop`` cross-module
convention :mod:`cpsb.nodes` already documents, rather than
``from .nodes import _tensors_from_edit_file`` -- consistent with how the
rest of this codebase keeps a call site monkeypatchable by whichever module
actually owns the implementation. Shared backend state
(:class:`~cpsb.context.CpsbContext`, the :class:`~cpsb.handoff.HandoffManager`)
is likewise read from ``nodes``'s own module-level state via
:func:`cpsb.nodes._require_state` / :func:`cpsb.nodes._state_if_configured` --
both nodes are wired up by the SAME single ``cpsb.nodes.configure()`` call in
the top-level ``__init__.py``, so a second, parallel state container here
would just be a second source of truth for one thing.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any

from . import nodes, routes
from .context import CpsbContext
from .handoff import HandoffManager, HandoffMeta
from .psd_io import read_edited_psd

logger = logging.getLogger("cpsb")

#: Extensions this node's combo lists and ``VALIDATE_INPUTS`` accepts,
#: case-insensitive (PROTOCOL.md §6b). Kept in sync with
#: ``cpsb.routes._PSD_NATIVE_EXTENSIONS`` (the `/cpsb/open` side of the same
#: constraint) by hand -- both are short, stable, two-entry tuples unlikely
#: to drift, and importing one module's "private" constant into the other
#: would couple them for no real benefit.
PSD_EXTENSIONS: tuple[str, ...] = (".psd", ".psb")


def _list_psd_files(input_dir: Path) -> list[str]:
    """Sorted ``.psd``/``.psb`` filenames directly under *input_dir*.

    Non-recursive, matching ``LoadImage.INPUT_TYPES``'s own flat directory
    listing (see this module's docstring) -- a PSD nested in a subfolder is
    not offered, the same as an image would not be.

    Args:
        input_dir: ComfyUI's input directory (``CpsbContext.input_dir``).

    Returns:
        Sorted filenames (not full paths) whose extension, lower-cased, is
        ``.psd`` or ``.psb``. Empty if *input_dir* doesn't exist yet.
    """
    if not input_dir.is_dir():
        return []
    return sorted(
        entry.name
        for entry in input_dir.iterdir()
        if entry.is_file() and entry.suffix.lower() in PSD_EXTENSIONS
    )


def _resolve_psd_path(context: CpsbContext, psd: str) -> Path | None:
    """Resolve the combo's selected filename to an absolute path, safely.

    Reuses :func:`cpsb.routes._resolve_source_path` (``subfolder=""``,
    ``type="input"``) rather than re-deriving its containment check: a
    COMBO value submitted via a raw API prompt is not re-validated against
    the options ``INPUT_TYPES`` advertised, so a crafted ``psd`` value (e.g.
    containing ``..``) must be rejected the same way `POST /cpsb/open`
    already rejects a path-traversing ``filename``.

    Args:
        context: The active backend context.
        psd: The node's ``psd`` input value -- a bare filename, as listed
            by :func:`_list_psd_files`.

    Returns:
        The resolved absolute path, or ``None`` if *psd* would escape
        *context.input_dir*.
    """
    return routes._resolve_source_path(context, psd, "", "input")


def _consume_active_edit(manager: HandoffManager, handoff_id: str) -> tuple[Any, Any] | None:
    """``(IMAGE, MASK)`` tensors for *handoff_id*'s latest edit, if any.

    Thin file-resolution wrapper around
    :func:`cpsb.nodes._tensors_from_edit_file` -- the shared MASK-derivation
    logic itself lives there, alongside
    :class:`~cpsb.nodes.PhotoshopBridge`'s identical consume path.

    Args:
        manager: The handoff manager.
        handoff_id: A handoff already confirmed to have at least one edit.

    Returns:
        The edit's tensors, or ``None`` if the edit file has vanished from
        disk since (a filesystem race -- cheap to guard, shouldn't happen
        in practice). Callers fall back to flattening the source PSD fresh
        in that case, exactly as if no matching handoff existed at all.
    """
    edit_path = manager.edit_image_path(handoff_id)
    if edit_path is None or not edit_path.exists():
        return None
    return nodes._tensors_from_edit_file(edit_path)


def _find_matching_active_handoff(
    manager: HandoffManager, node_id: str, file_hash: str
) -> HandoffMeta | None:
    """The active ``load_psd`` handoff for *node_id*, iff it has a consumable edit.

    Shared by :meth:`PhotoshopLoadPSD.IS_CHANGED` and
    :meth:`PhotoshopLoadPSD.execute` -- both need the identical "is there an
    edit to consume instead of re-flattening" predicate (PROTOCOL.md §6b).

    Args:
        manager: The handoff manager.
        node_id: This node instance's ``unique_id``, stringified.
        file_hash: sha256 of the currently-selected PSD's raw bytes.

    Returns:
        The matching handoff, or ``None`` if any of the following holds --
        each meaning "flatten the source PSD instead": no active handoff
        for this node at all; one whose ``origin_kind`` isn't ``load_psd``
        (defensive -- node ids are unique per graph, so this shouldn't
        happen in practice, but a stray match must never be served); one
        created from different bytes (the file on disk was swapped out
        under the same filename); or one with no edits yet.
    """
    active = manager.find_active_for_node(node_id)
    if (
        active is not None
        and active.origin_kind == "load_psd"
        and active.source_hash == file_hash
        and active.edits
    ):
        return active
    return None


class PhotoshopLoadPSD:
    """Loads and flattens a ``.psd``/``.psb`` from ComfyUI's input dir (PROTOCOL.md §6b).

    The ``psd`` COMBO input lists ``.psd``/``.psb`` files in ComfyUI's input
    directory (:func:`_list_psd_files`); the frontend additionally offers a
    hand-rolled upload widget for those extensions (out of this package's
    scope -- see this module's own docstring). Outputs are ``(IMAGE, MASK)``:
    the selected file is flattened via :func:`cpsb.psd_io.read_edited_psd`
    (embedded Maximize-Compatibility composite, falling back to psd-tools'
    own recompositing) and its MASK is ``1 - alpha`` of that flattened image
    when it carries transparency, else zeros -- the identical derivation
    :class:`~cpsb.nodes.PhotoshopBridge` uses (PROTOCOL.md §6).

    Round trip (PROTOCOL.md §6b): a right-click "Open in Photoshop" on this
    node creates a ``load_psd`` handoff whose ``source.psd`` is a byte-for-
    byte copy of the selected file (PROTOCOL.md §2 -- never a re-encoded
    flatten, so the user's own layers survive the round trip). While an
    ACTIVE handoff for this node has a ``source_hash`` matching the
    currently-selected file's raw bytes AND at least one edit,
    :meth:`execute` returns that edit's tensors instead of re-flattening the
    original -- the identical "consume the edit" pattern
    :class:`~cpsb.nodes.PhotoshopBridge` uses (PROTOCOL.md §6), which is
    what lets re-queuing this node after a Photoshop save actually deliver
    the edited pixels instead of looping back to the unedited source. If the
    selected file's bytes no longer match the handoff's recorded
    ``source_hash`` (the user picked a different file, or the same filename
    was overwritten with different content), the stale handoff is simply
    ignored here -- unlike the bridge node, this node never creates or
    supersedes handoffs itself, so there is nothing to reconcile beyond not
    serving pixels that don't belong to the current selection.

    Edit-original option (PROTOCOL.md §6b): the ``edit_original`` BOOLEAN
    widget (default ``False``) is read by the frontend at open time
    (``web/cpsb/menu.js``/``loadpsd.js``), not by this class -- when
    ``True``, the resulting handoff's edit target is the user's OWN
    selected file rather than a managed copy (``cpsb.routes``' load_psd
    branch, watched by ``cpsb.watcher``). The round-trip mechanics above are
    unaffected either way: an edit always lands as an ``edit_%03d.png`` in
    the handoff's managed folder regardless of where it was read from.
    """

    CATEGORY = "image/photoshop"
    RETURN_TYPES = ("IMAGE", "MASK")
    FUNCTION = "execute"

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, Any]:
        """The ``psd`` COMBO (PROTOCOL.md §6b) + hidden ``unique_id``.

        Returns an empty combo (rather than raising) when
        :func:`cpsb.nodes.configure` hasn't run yet -- ComfyUI-adjacent
        tooling can introspect node types without a live backend context
        (see :func:`cpsb.nodes._state_if_configured`'s own docstring), and
        this classmethod must tolerate that the same way ``LoadImage``'s
        own ``INPUT_TYPES`` tolerates an empty/missing input directory.
        """
        state = nodes._state_if_configured()
        files = _list_psd_files(state.context.input_dir) if state is not None else []
        return {
            "required": {
                "psd": (files,),
                # PROTOCOL.md §6b "Edit-original option": default False (the
                # safe, non-destructive copy-to-handoff behavior this node
                # has always had). menu.js reads this widget's live value at
                # right-click time (loadpsd.js's getEditOriginal) and threads
                # it into `/cpsb/open` as `edit_in_place` -- this method's
                # own execute()/IS_CHANGED never consult it (see their
                # docstrings): it governs how a handoff gets OPENED, not
                # what pixels this node returns once one exists.
                "edit_original": ("BOOLEAN", {"default": False}),
            },
            "hidden": {
                "unique_id": "UNIQUE_ID",
            },
        }

    @classmethod
    def VALIDATE_INPUTS(cls, psd: str) -> bool | str:
        """Friendly upfront check, mirroring ``LoadImage.VALIDATE_INPUTS``.

        Confirms the selected file still exists and is a ``.psd``/``.psb``
        before the prompt is queued, rather than surfacing a raw
        ``FileNotFoundError`` mid-run. A no-op (``True``) when unconfigured,
        for the same tooling-without-a-live-backend reason as
        :meth:`INPUT_TYPES`.
        """
        state = nodes._state_if_configured()
        if state is None:
            return True
        if Path(psd).suffix.lower() not in PSD_EXTENSIONS:
            return f"Not a .psd/.psb file: {psd!r}"
        resolved = _resolve_psd_path(state.context, psd)
        if resolved is None or not resolved.is_file():
            return f"PSD file not found: {psd!r}"
        return True

    @classmethod
    def IS_CHANGED(cls, psd: str, unique_id: str, edit_original: bool = False) -> str:
        """sha256 of *psd*'s raw bytes, folding in the latest edit hash when consumable.

        An arriving edit must force this node (and everything downstream)
        to re-execute on the next queue -- the same mechanism
        ``LoadImage.IS_CHANGED`` and
        :meth:`cpsb.nodes.PhotoshopBridge.IS_CHANGED` both use. Returning
        just the file hash would NOT change once an edit lands (the
        selected file on disk is the ORIGINAL; edits live in the handoff
        folder), so :func:`_find_matching_active_handoff`'s match folds the
        latest edit's own hash in whenever one is consumable (PROTOCOL.md
        §6b), changing the return value again on every subsequent save.

        *edit_original* is accepted (mirroring
        :meth:`cpsb.nodes.PhotoshopBridge.IS_CHANGED`'s identical convention
        of declaring every currently-required ``INPUT_TYPES`` field, even
        ones its own cache key doesn't depend on) but never folds into the
        returned value: which pixels this node's own :meth:`execute` would
        produce for a given *psd* selection never depends on it -- it only
        governs how a handoff gets opened (menu.js's ``/cpsb/open`` request,
        PROTOCOL.md §6b), a decision already made and recorded on the
        handoff by the time any edit reaches this node's consume path.
        Defaults ``False`` so every pre-existing caller (this node's own
        tests included) that predates this input keeps working unchanged.
        """
        state = nodes._require_state()
        psd_path = _resolve_psd_path(state.context, psd)
        if psd_path is None or not psd_path.is_file():
            # Missing file: return the bare selector so a value that WAS a
            # 64-hex-char hash changes (forcing re-execution, which then
            # raises a clear FileNotFoundError from execute() itself)
            # rather than silently keeping a stale cached result forever.
            return psd
        file_hash = hashlib.sha256(psd_path.read_bytes()).hexdigest()

        active = _find_matching_active_handoff(state.manager, str(unique_id), file_hash)
        if active is not None:
            edit_hash = state.manager.latest_edit_hash(active.handoff_id)
            if edit_hash is not None:
                return f"{file_hash}:{edit_hash}"
        return file_hash

    def execute(self, psd: str, unique_id: str, edit_original: bool = False) -> tuple[Any, Any]:
        """``(IMAGE, MASK)`` for the selected PSD (PROTOCOL.md §6b).

        Serves a consumable active edit first (see the class docstring's
        "Round trip" paragraph); otherwise flattens *psd* fresh via
        :func:`cpsb.psd_io.read_edited_psd`.

        Args:
            psd: The selected combo filename.
            unique_id: This node instance's id (ComfyUI's hidden
                ``UNIQUE_ID`` input), used to key its handoff lookup.
            edit_original: The node's current ``edit_original`` widget
                value (PROTOCOL.md §6b). Accepted for parity with every
                declared ``INPUT_TYPES`` field -- ComfyUI passes it here as
                a real keyword argument -- but not read: whichever way a
                handoff was opened (copy vs. in-place), an arrived edit
                always lands in the SAME place (an ``edit_%03d.png`` in the
                handoff's managed folder), so the consume path above is
                identical either way. Defaults ``False`` so every
                pre-existing caller keeps working unchanged.

        Returns:
            ``(IMAGE, MASK)`` tensors.

        Raises:
            FileNotFoundError: *psd* no longer resolves to a file (deleted
                or moved since selection). Deliberately not caught: there
                is no sensible pixel output to substitute, so this
                surfaces as the node's own execution error, exactly like
                ``LoadImage`` raising on a missing file.
        """
        state = nodes._require_state()
        manager = state.manager
        node_id = str(unique_id)

        psd_path = _resolve_psd_path(state.context, psd)
        if psd_path is None or not psd_path.is_file():
            raise FileNotFoundError(f"PSD file not found: {psd}")
        source_hash = hashlib.sha256(psd_path.read_bytes()).hexdigest()

        active = _find_matching_active_handoff(manager, node_id, source_hash)
        if active is not None:
            consumed = _consume_active_edit(manager, active.handoff_id)
            if consumed is not None:
                logger.info(
                    "cpsb load_psd: node %s handoff %s: consuming latest edit",
                    node_id,
                    active.handoff_id,
                )
                return consumed

        logger.info("cpsb load_psd: node %s: flattening %s", node_id, psd_path)
        image, _fidelity = read_edited_psd(psd_path)
        return nodes._tensors_from_image(image)
