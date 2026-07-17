"""The ``PhotoshopBridge`` ComfyUI node (PROTOCOL.md §6).

``torch`` (and the tensor-conversion use of ``numpy``) are imported only
inside the functions that need them, never at module level, so this module
-- and everything about it that does not touch actual tensors -- stays
importable in a plain test environment without either installed. ComfyUI
always provides both to the node's real runtime; neither is declared in this
package's own ``requirements.txt``.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PIL import Image

from .context import CpsbContext
from .handoff import HandoffManager, HandoffMeta, SourceRef, WaitOutcome, compute_source_hash
from .psd_io import write_psd
from .routes import OpenAttempt, open_in_photoshop

if TYPE_CHECKING:
    from aiohttp import web

logger = logging.getLogger("cpsb")


@dataclass
class _NodeState:
    """Shared backend state, wired in once via :func:`configure`.

    ComfyUI instantiates node classes itself with no constructor arguments,
    so this -- like every other node package's module-level registries --
    is the only place this state can live.
    """

    context: CpsbContext
    manager: HandoffManager
    app: web.Application
    loop: asyncio.AbstractEventLoop


_state: _NodeState | None = None


def configure(
    context: CpsbContext,
    manager: HandoffManager,
    app: web.Application,
    loop: asyncio.AbstractEventLoop,
) -> None:
    """Wire the shared backend state into this module.

    Called once from the top-level ``__init__.py``. *loop* must be
    ComfyUI's own running event loop (``PromptServer.instance.loop``) --
    the Photoshop Bridge node's ``execute()`` runs on its own per-prompt
    worker thread (the same pattern cg-image-picker/ComfyUI-pause use), so
    reaching Tier 2's plugin websocket (bound to *that* loop) from there
    requires :func:`asyncio.run_coroutine_threadsafe`, not a fresh
    ``asyncio.run()`` on the worker thread.
    """
    global _state
    _state = _NodeState(context=context, manager=manager, app=app, loop=loop)


def _require_state() -> _NodeState:
    if _state is None:
        raise RuntimeError(
            "cpsb.nodes.configure() must be called from the package's __init__.py "
            "before PhotoshopBridge can execute"
        )
    return _state


def _tensor_to_pil(image: Any) -> Image.Image:
    """First frame of a ComfyUI ``IMAGE`` tensor (float32, [0, 1], NHWC) as a PIL image."""
    import numpy as np

    frame = image[0]
    array = frame.cpu().numpy() if hasattr(frame, "cpu") else np.asarray(frame)
    array = np.clip(array * 255.0, 0, 255).astype(np.uint8)
    return Image.fromarray(array, mode="RGB")


def _pil_to_tensor(image: Image.Image) -> Any:
    """Inverse of :func:`_tensor_to_pil`: a PIL image as a single-frame ``IMAGE`` tensor."""
    import numpy as np
    import torch

    array = np.array(image.convert("RGB")).astype(np.float32) / 255.0
    return torch.from_numpy(array)[None, ...]


def _raise_interrupt() -> None:
    """Raise ComfyUI's own cancellation exception.

    Verified against the current ComfyUI source: `execution.py` catches
    `comfy.model_management.InterruptProcessingException` specifically to
    stop a running prompt, and `nodes.py`'s own `interrupt_processing()`
    helper raises the identical type -- this is the real, current import
    path, not `comfy_execution.graph.ExecutionBlocker` or similar. Imported
    locally, guarded, for the same testability reason as torch/numpy above.
    """
    try:
        import comfy.model_management as model_management
    except ImportError as exc:
        raise RuntimeError(
            "Photoshop Bridge needs comfy.model_management, which is only "
            "available when running inside ComfyUI"
        ) from exc
    raise model_management.InterruptProcessingException()


class PhotoshopBridge:
    """Round-trips an image through Photoshop from inside a workflow (PROTOCOL.md §6).

    Sends ``image`` to Photoshop the same way "Open in Photoshop" does
    (tier-selected: the UXP plugin if connected, otherwise an OS-level
    launch), then either blocks until an edit is saved back
    (``wait_for_edit=True``, the default) or returns immediately with
    whatever this node last received.

    Node-reuse semantics: this node keeps at most one *active* handoff per
    node instance (keyed by ``unique_id``), and the handoff records a
    ``source_hash`` of the pixels it was created from. On every execution
    the current input is hashed and compared first: a mismatch means the
    upstream graph re-generated the image, so the old handoff (and any edits
    in it, which belong to the OLD pixels) is superseded and a fresh one is
    created -- a stale edit is never served for a new input. When the hash
    matches and an edit has already arrived -- which is exactly what causes
    ComfyUI to call ``execute()`` again at all, since ``IS_CHANGED`` changes
    the moment an edit lands -- that edit is handed over immediately without
    reopening Photoshop. Photoshop is (re)opened only when no matching
    active handoff exists yet, or the existing one has no edit yet (e.g. a
    plugin still mid-edit, or a previous run that never got a chance to
    start waiting because ``wait_for_edit`` was ``False``).
    """

    CATEGORY = "image/photoshop"
    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "execute"

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, Any]:
        return {
            "required": {
                "image": ("IMAGE",),
                "wait_for_edit": ("BOOLEAN", {"default": True}),
                "timeout_seconds": ("INT", {"default": 1800, "min": 10, "max": 86400}),
            },
            "hidden": {
                "unique_id": "UNIQUE_ID",
                "prompt": "PROMPT",
                "extra_pnginfo": "EXTRA_PNGINFO",
            },
        }

    @classmethod
    def IS_CHANGED(
        cls,
        image: Any,
        wait_for_edit: bool,
        timeout_seconds: int,
        unique_id: str,
        prompt: Any = None,
        extra_pnginfo: Any = None,
    ) -> str:
        """SHA256 of the latest edit for this node, or a constant when there is none.

        An arriving edit changes the returned hash, forcing this node (and
        everything downstream) to re-execute on the next queue -- the same
        mechanism `LoadImage.IS_CHANGED` uses (PROTOCOL.md §6).
        """
        manager = _require_state().manager
        active = manager.find_active_for_node(str(unique_id))
        if active is None:
            return "no-handoff"
        edit_hash = manager.latest_edit_hash(active.handoff_id)
        return edit_hash if edit_hash is not None else active.handoff_id

    def execute(
        self,
        image: Any,
        wait_for_edit: bool,
        timeout_seconds: int,
        unique_id: str,
        prompt: Any = None,
        extra_pnginfo: Any = None,
    ) -> tuple[Any]:
        state = _require_state()
        manager = state.manager
        node_id = str(unique_id)

        pil_image = _tensor_to_pil(image)
        incoming_hash = compute_source_hash(pil_image)

        active = manager.find_active_for_node(node_id)
        if (
            active is not None
            and active.source_hash is not None
            and active.source_hash != incoming_hash
        ):
            # The upstream input changed since this handoff was created: any
            # edits it holds belong to the OLD pixels and must not be served
            # for the new input. Retire it and start fresh. (A handoff with
            # no recorded source_hash -- written by a pre-source_hash version
            # -- is treated as matching, so upgrading never mass-supersedes
            # existing handoffs; the trade-off is one potentially stale serve
            # per legacy handoff versus churn on every pre-upgrade round trip.)
            logger.info(
                "Photoshop Bridge %s: input changed, superseding handoff %s",
                node_id,
                active.handoff_id,
            )
            manager.supersede(active.handoff_id)
            active = None

        if active is not None and active.edits:
            logger.info("Photoshop Bridge %s: an edit already arrived, returning it", node_id)
            return (self._load_edit_tensor(manager, active.handoff_id, image),)

        if active is None:
            meta, psd_path = self._create_handoff(state, node_id, pil_image)
        else:
            meta = active
            psd_path = manager.handoff_dir(meta.handoff_id) / "source.psd"

        attempt = self._open_in_photoshop(state, meta, psd_path)

        if not wait_for_edit:
            return (image,)

        if not attempt.ok:
            logger.warning(
                "Photoshop Bridge %s: could not open Photoshop (%s)", node_id, attempt.error
            )
            _raise_interrupt()

        outcome = manager.wait_for_edit(meta.handoff_id, float(timeout_seconds))
        if outcome != WaitOutcome.EDITED:
            logger.info("Photoshop Bridge %s: wait ended with '%s'", node_id, outcome)
            _raise_interrupt()

        return (self._load_edit_tensor(manager, meta.handoff_id, image),)

    @staticmethod
    def _create_handoff(
        state: _NodeState, node_id: str, pil_image: Image.Image
    ) -> tuple[HandoffMeta, Path]:
        # A bridge node's input is an in-memory tensor, not a file ComfyUI's
        # /view could address, so `source` is a descriptive placeholder --
        # it only matters for the gallery display and the sibling-output
        # step, and the latter is scoped to `terminal_output` origins only.
        meta = state.manager.create(
            origin_node_id=node_id,
            origin_kind="bridge_node",
            workflow_name="",
            source=SourceRef(filename=f"bridge_{node_id}.png", subfolder="", type="temp"),
            original_image=pil_image,
        )
        psd_path = state.manager.handoff_dir(meta.handoff_id) / "source.psd"
        write_psd(psd_path, pil_image)
        state.manager.note_source_written(meta.handoff_id)
        return meta, psd_path

    @staticmethod
    def _open_in_photoshop(state: _NodeState, meta: HandoffMeta, psd_path: Path) -> OpenAttempt:
        """Run the (async) tier-select-and-open on ComfyUI's own event loop.

        `execute()` runs on this node's own worker thread, but the Tier 2
        websocket connection belongs to the server's main event loop --
        `run_coroutine_threadsafe` is the correct, safe way to invoke a
        coroutine that touches it from a different thread. No deadlock with
        the coroutine's internal `asyncio.to_thread(launch_photoshop, ...)`:
        `future.result()` blocks only this worker thread, which is not the
        loop thread, so the loop stays free to drive the executor job and
        resolve the future.
        """
        future = asyncio.run_coroutine_threadsafe(
            open_in_photoshop(state.app, state.context, state.manager, meta, psd_path),
            state.loop,
        )
        return future.result()

    @staticmethod
    def _load_edit_tensor(manager: HandoffManager, handoff_id: str, fallback_image: Any) -> Any:
        path = manager.edit_image_path(handoff_id)
        if path is None or not path.exists():
            return fallback_image
        with Image.open(path) as edit_image:
            edit_image.load()
            return _pil_to_tensor(edit_image)
