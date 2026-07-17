"""The ``PhotoshopBridge`` ComfyUI node (PROTOCOL.md §6).

``torch`` (and the tensor-conversion use of ``numpy``) are imported only
inside the functions that need them, never at module level, so this module
-- and everything about it that does not touch actual tensors -- stays
importable in a plain test environment without either installed. ComfyUI
always provides both to the node's real runtime; neither is declared in this
package's own ``requirements.txt``.

Threading model, verified against ComfyUI (comfyanonymous/ComfyUI, ``master``,
fetched directly from raw.githubusercontent.com while diagnosing a real
"node doesn't open Photoshop, but the right-click route does" bug report --
line numbers below are as of that fetch and may drift upstream):

* ``main.py``'s ``start_comfyui()`` builds ONE event loop (``asyncio_loop =
  asyncio.new_event_loop()``, ``asyncio.set_event_loop(asyncio_loop)``,
  ``main.py:500-501``) and hands it to ``server.PromptServer(asyncio_loop)``
  (``main.py:502``), which stores it verbatim as ``self.loop`` in its own
  ``__init__`` (``server.py:227``; ``PromptServer.instance = self`` is set
  two lines above it, ``server.py:217``) -- this is
  ``PromptServer.instance.loop``, the value this package's :func:`configure`
  below receives as *loop*. That same loop object is what the main thread
  parks in for the rest of the process's life (``event_loop.
  run_until_complete(x)``, ``main.py:575``, serving HTTP/websocket traffic).
  Custom-node loading -- where this package's top-level ``__init__.py``
  calls :func:`configure` -- happens earlier in that same call, via
  ``asyncio_loop.run_until_complete(nodes.init_extra_nodes(...))``
  (``main.py:508-511``), so *loop* is already the real, live server loop at
  the moment this package captures it, and it is never reassigned afterward
  (only one ``self.loop = loop`` write site in all of ``server.py``).
* Prompt execution runs on a SEPARATE daemon thread: ``threading.Thread(
  target=prompt_worker, daemon=True, args=(prompt_server.prompt_queue,
  prompt_server,)).start()`` (``main.py:525``, started once, after custom
  nodes finish loading). ``prompt_worker``'s ``while True:`` loop dequeues
  one prompt at a time and calls ``e.execute(item[2], prompt_id, extra_data,
  item[4])`` synchronously (``main.py:359``) -- ``PromptExecutor.execute()``
  (``execution.py:724-725``) is literally ``asyncio.run(self.execute_async(
  ...))``: a BRAND NEW, throwaway event loop, scoped to that one call, on
  whichever OS thread called it (``prompt_worker``'s). It is never the same
  loop object as ``server.loop``.
* A synchronous node ``FUNCTION`` -- what ``PhotoshopBridge.execute`` is --
  is invoked directly and inline from that ad-hoc loop's coroutine chain,
  with no ``asyncio.to_thread``/executor/thread hop of any kind:
  ``execution.py:289,302-305`` resolves ``f = getattr(obj, func)`` and,
  since ``inspect.iscoroutinefunction(f)`` is ``False`` for a plain ``def``,
  takes the ``else`` branch: ``with CurrentNodeContext(...): result =
  f(**inputs)``. (Only a truly ``async def FUNCTION`` gets
  ``asyncio.create_task`` treatment, ``execution.py:290-301`` -- irrelevant
  here, and this dispatch is identical for V1 and V3-style node classes.)

Conclusion: ``PhotoshopBridge.execute()`` genuinely runs on ``prompt_worker``'s
OS thread, never on ``server.loop``'s own thread -- the assumption the
original cross-thread-wait design relied on DOES hold in current ComfyUI.
But it runs inside *a* running loop the whole time (the ad-hoc per-prompt
one), so ``asyncio.get_running_loop()`` never raises from here -- a naive
"is there a running loop" check would be wrong; :func:`_running_on_state_loop`
below checks identity against *state.loop* specifically, not mere presence.
And "today's assumption is true" is not the same as "safe to bet the whole
prompt queue on it forever": nothing below requires it to be true at all for
Tier 1 (the common case, see :meth:`PhotoshopBridge._launch_tier1_direct`),
and the one remaining loop-dependent step -- the Tier 2 plugin send -- is
bounded and guarded rather than trusted unconditionally (see
:meth:`PhotoshopBridge._send_tier2_open`).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PIL import Image

from . import routes
from .context import CpsbContext
from .handoff import HandoffManager, HandoffMeta, SourceRef, WaitOutcome, compute_source_hash
from .psd_io import write_psd

if TYPE_CHECKING:
    from aiohttp import web

logger = logging.getLogger("cpsb")

#: Bound on waiting for the Tier 2 (plugin websocket) open request to be
#: scheduled and sent on ComfyUI's event loop. Only this one, genuinely
#: loop-bound step is time-boxed this way -- Tier 1 never touches the loop
#: at all (see module docstring). A wedged plugin connection or a loop that
#: stops pumping now fails this ONE open attempt instead of hanging
#: `prompt_worker` (and therefore ComfyUI's entire prompt queue, which
#: processes one item at a time) forever.
_TIER2_SEND_TIMEOUT_SECONDS = 10.0


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
    ComfyUI's own running event loop (``PromptServer.instance.loop``).
    ``PhotoshopBridge.execute()`` runs on ComfyUI's per-prompt worker
    thread, never on *loop*'s own thread -- verified against ComfyUI's
    current source; see the module docstring's "Threading model" section
    for the full citation trail. Tier 2 (the plugin websocket, bound to
    *loop*) is therefore reached via :func:`asyncio.run_coroutine_threadsafe`
    from the worker thread, but bounded and guarded (see
    :meth:`PhotoshopBridge._send_tier2_open`) rather than trusted
    unconditionally.
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


def _image_tensor_size(image: Any) -> tuple[int, int]:
    """``(width, height)`` of a ComfyUI ``IMAGE`` tensor (NHWC, single frame)."""
    _, height, width, _ = image.shape
    return width, height


def _mask_tensor_from_l(image: Image.Image) -> Any:
    """A single-channel PIL image (white = 1.0) as a ComfyUI ``MASK`` tensor.

    Used as-is, no inversion (PROTOCOL.md §6/§4) -- this is the extracted-
    channel-mask preference, the first tier of the MASK output's preference
    order.
    """
    import numpy as np
    import torch

    array = np.array(image.convert("L")).astype(np.float32) / 255.0
    return torch.from_numpy(array)[None, ...]


def _alpha_complement_mask_tensor(image: Image.Image) -> Any:
    """``1 - alpha`` of *image* as a ``MASK`` tensor (LoadImage parity, PROTOCOL.md §6).

    The second tier of the MASK output's preference order. Callers only
    invoke this once they've confirmed *image*'s mode carries an alpha band.
    """
    import numpy as np
    import torch

    alpha = np.array(image.convert("RGBA").split()[-1]).astype(np.float32) / 255.0
    return torch.from_numpy(1.0 - alpha)[None, ...]


def _zeros_mask_tensor(width: int, height: int) -> Any:
    """An all-zero ``MASK`` tensor sized ``(1, height, width)`` (PROTOCOL.md §6).

    The last tier of the MASK output's preference order -- also what a
    passthrough run (no edit yet) always pairs with the input image.
    """
    import torch

    return torch.zeros((1, height, width), dtype=torch.float32)


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


def _running_on_state_loop(state: _NodeState) -> bool:
    """Whether THIS thread is already inside *state.loop*'s own run loop.

    Not the same question as "is any loop running on this thread": the
    per-prompt ``asyncio.run()`` loop (module docstring, "Threading model")
    means that is ALWAYS true while a node executes, so treating "a loop is
    running" as the danger signal would wrongly disable Tier 2 on every
    single run. The actual hazard is identity: ``run_coroutine_threadsafe(
    ..., state.loop).result()`` only deadlocks if this thread is the one
    already driving *state.loop* itself -- checked directly here rather
    than assumed either way. Current ComfyUI never returns ``True`` from
    this (module docstring conclusion), but this package cannot make that a
    guarantee about a third-party host's execution model.
    """
    try:
        return asyncio.get_running_loop() is state.loop
    except RuntimeError:
        return False


class BridgeMode:
    """String constants for the ``mode`` COMBO input (PROTOCOL.md §6).

    The frontend string-matches on these exact values -- do not vary them.
    Replaces the earlier `wait_for_edit` BOOLEAN input as a pre-release
    breaking change with no migration shim: a saved workflow that still has
    the old boolean-input version of this node must have the node re-added.
    """

    WAIT_FIRST_SAVE = "Wait for first save"
    RERUN_EVERY_SAVE = "Re-run on every save"
    OPEN_ONLY = "Open only (don't wait)"


class PhotoshopBridge:
    """Round-trips an image through Photoshop from inside a workflow (PROTOCOL.md §6).

    Sends ``image`` to Photoshop the same way "Open in Photoshop" does
    (tier-selected: the UXP plugin if connected, otherwise an OS-level
    launch). The ``mode`` input (:class:`BridgeMode`, PROTOCOL.md §6)
    selects one of three behaviors: "Wait for first save" blocks until the
    first edit is saved back and delivers it in this same run; "Re-run on
    every save" never blocks -- it opens Photoshop once, passes the input
    through unchanged, and relies on the frontend auto-queueing a re-run per
    save (each of which then consumes the latest edit via the node-reuse
    semantics below) for a live-iterate workflow; "Open only (don't wait)"
    is backend-identical to "Re-run on every save" -- the only difference is
    the frontend's auto-queue policy for this mode (PROTOCOL.md §5), not
    anything this node does differently.

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
    reopening Photoshop. Otherwise, "Wait for first save" always (re)opens,
    exactly like the original ``wait_for_edit=True`` behavior (e.g. a manual
    re-queue after a previous wait timed out resumes/refocuses the same
    document). The two non-blocking modes only (re)open for a genuinely NEW
    handoff (none existed yet, or the old one was just superseded above):
    since they never wait, a passthrough re-execution against a handoff that
    is simply still open and unsaved must not relaunch/refocus Photoshop on
    every single re-run (PROTOCOL.md §5/§6).

    Outputs: ``(IMAGE, MASK)``. MASK preference order (PROTOCOL.md §6): the
    edit's extracted channel mask (PROTOCOL.md §4) -> else ``1 - alpha`` of
    the edit image (LoadImage parity) -> else an all-zero mask sized to the
    image. White = 1.0; inversion is the consumer's job. A passthrough run
    (no edit yet, either mode) returns an all-zero mask alongside the
    unchanged input image.
    """

    CATEGORY = "image/photoshop"
    RETURN_TYPES = ("IMAGE", "MASK")
    FUNCTION = "execute"

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, Any]:
        return {
            "required": {
                "image": ("IMAGE",),
                "mode": (
                    [BridgeMode.WAIT_FIRST_SAVE, BridgeMode.RERUN_EVERY_SAVE, BridgeMode.OPEN_ONLY],
                    {"default": BridgeMode.WAIT_FIRST_SAVE},
                ),
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
        mode: str,
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
        mode: str,
        timeout_seconds: int,
        unique_id: str,
        prompt: Any = None,
        extra_pnginfo: Any = None,
    ) -> tuple[Any, Any]:
        state = _require_state()
        manager = state.manager
        node_id = str(unique_id)
        logger.info("cpsb bridge: node %s: execute() starting (mode=%r)", node_id, mode)

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
                "cpsb bridge: node %s: input changed, superseding handoff %s",
                node_id,
                active.handoff_id,
            )
            manager.supersede(active.handoff_id)
            active = None

        if active is not None and active.edits:
            logger.info("cpsb bridge: node %s: an edit already arrived, returning it", node_id)
            return self._load_edit_tensors(manager, active.handoff_id, image)

        is_new_handoff = active is None
        if is_new_handoff:
            meta, psd_path = self._create_handoff(state, node_id, pil_image, incoming_hash)
        else:
            meta = active
            psd_path = manager.handoff_dir(meta.handoff_id) / "source.psd"

        # "Wait for first save" always (re)opens, matching the original
        # wait_for_edit=True behavior exactly. The two non-blocking modes
        # only open a genuinely new handoff -- see the class docstring's
        # "Node-reuse semantics" for why reusing an existing, unsaved one
        # must not relaunch Photoshop for those modes.
        if mode == BridgeMode.WAIT_FIRST_SAVE or is_new_handoff:
            logger.info(
                "cpsb bridge: node %s handoff %s: opening Photoshop", node_id, meta.handoff_id
            )
            attempt = self._open_in_photoshop(state, meta, psd_path)
        else:
            logger.info(
                "cpsb bridge: node %s handoff %s: mode=%r, handoff already open, "
                "not reopening",
                node_id,
                meta.handoff_id,
                mode,
            )
            attempt = None

        if mode != BridgeMode.WAIT_FIRST_SAVE:
            logger.info(
                "cpsb bridge: node %s handoff %s: mode=%r, returning immediately",
                node_id,
                meta.handoff_id,
                mode,
            )
            # Passthrough: no edit exists yet for this (possibly brand-new)
            # handoff, so there is nothing to derive a mask from -- an
            # all-zero mask sized to the input image (PROTOCOL.md §6).
            width, height = _image_tensor_size(image)
            return image, _zeros_mask_tensor(width, height)

        # mode == BridgeMode.WAIT_FIRST_SAVE, so `attempt` was always assigned above.
        if not attempt.ok:
            logger.warning(
                "cpsb bridge: node %s handoff %s: could not open Photoshop (%s), interrupting",
                node_id,
                meta.handoff_id,
                attempt.error,
            )
            _raise_interrupt()

        logger.info(
            "cpsb bridge: node %s handoff %s: waiting for edit (timeout=%ss)",
            node_id,
            meta.handoff_id,
            timeout_seconds,
        )
        outcome = manager.wait_for_edit(meta.handoff_id, float(timeout_seconds))
        logger.info(
            "cpsb bridge: node %s handoff %s: wait outcome '%s'", node_id, meta.handoff_id, outcome
        )
        if outcome != WaitOutcome.EDITED:
            _raise_interrupt()

        return self._load_edit_tensors(manager, meta.handoff_id, image)

    @staticmethod
    def _create_handoff(
        state: _NodeState, node_id: str, pil_image: Image.Image, source_hash: str
    ) -> tuple[HandoffMeta, Path]:
        # A bridge node's input is an in-memory tensor, not a file ComfyUI's
        # /view could address, so `source` is a descriptive placeholder --
        # it only matters for the gallery display and the sibling-output
        # step, and the latter is scoped to `terminal_output` origins only.
        # source_hash is passed through rather than recomputed: execute()
        # already hashed pil_image once to decide whether to supersede.
        meta = state.manager.create(
            origin_node_id=node_id,
            origin_kind="bridge_node",
            workflow_name="",
            source=SourceRef(filename=f"bridge_{node_id}.png", subfolder="", type="temp"),
            original_image=pil_image,
            source_hash=source_hash,
        )
        psd_path = state.manager.handoff_dir(meta.handoff_id) / "source.psd"
        write_psd(psd_path, pil_image)
        state.manager.note_source_written(meta.handoff_id)
        return meta, psd_path

    @staticmethod
    def _open_in_photoshop(
        state: _NodeState, meta: HandoffMeta, psd_path: Path
    ) -> routes.OpenAttempt:
        """Tier-select and open *psd_path*, bounded and observable either way.

        Tier selection itself never touches the event loop:
        :func:`routes.tier2_connected` is a plain, synchronous, loop-free
        check (PROTOCOL.md §2/§3), so this decides Tier 1 vs. Tier 2 purely
        on the calling (worker) thread. Tier 1
        (:meth:`_launch_tier1_direct`) then launches Photoshop IN-LINE on
        that same thread -- no loop, no cross-thread wait, nothing that
        depends on the module docstring's threading finding being true.
        Only Tier 2 (:meth:`_send_tier2_open`) needs the loop at all, since
        the plugin websocket is bound to it; that path is bounded to
        ``_TIER2_SEND_TIMEOUT_SECONDS`` and guarded against ever blocking
        the loop's own thread on itself. Every step logs at INFO under the
        ``cpsb bridge:`` prefix so a failed open is diagnosable from the
        ComfyUI console alone.
        """
        node_id = meta.origin_node_id
        handoff_id = meta.handoff_id

        if routes.tier2_connected(state.app):
            logger.info(
                "cpsb bridge: node %s handoff %s: tier 2 (plugin) selected", node_id, handoff_id
            )
            attempt = PhotoshopBridge._send_tier2_open(state, meta, psd_path)
        else:
            logger.info(
                "cpsb bridge: node %s handoff %s: tier 1 (direct launch) selected",
                node_id,
                handoff_id,
            )
            attempt = PhotoshopBridge._launch_tier1_direct(state, meta, psd_path)

        if attempt.ok:
            logger.info(
                "cpsb bridge: node %s handoff %s: launch result ok (tier %d)",
                node_id,
                handoff_id,
                attempt.tier,
            )
        else:
            logger.warning(
                "cpsb bridge: node %s handoff %s: launch result error (tier %d): %s",
                node_id,
                handoff_id,
                attempt.tier,
                attempt.error,
            )
        return attempt

    @staticmethod
    def _launch_tier1_direct(
        state: _NodeState, meta: HandoffMeta, psd_path: Path
    ) -> routes.OpenAttempt:
        """Launch Photoshop synchronously, in-line, on the calling thread.

        Deliberately duplicates ~6 lines of ``cpsb/routes.py``'s
        ``open_in_photoshop`` Tier 1 branch (the ``launch_photoshop`` call
        plus its ``mark_editing``/``mark_error`` handling) instead of
        importing and reusing it: that branch is written as ``await
        asyncio.to_thread(launch_photoshop, ...)`` specifically because ITS
        caller is an aiohttp request handler running ON the server's event
        loop, which must not block. This caller is already on a plain
        worker thread with nothing else depending on it -- reaching for the
        loop here would only add a failure mode, not remove one. If either
        copy's launch/mark handling changes, update both (cross-reference:
        ``cpsb/routes.py``, ``open_in_photoshop``, Tier 1 branch).

        ``routes.launch_photoshop`` is called through the ``routes`` module
        object (not a direct ``from .launcher import launch_photoshop``) so
        tests that monkeypatch ``cpsb.routes.launch_photoshop`` -- the
        existing convention in ``tests/test_nodes.py`` -- intercept this
        path too.
        """
        result = routes.launch_photoshop(
            psd_path, state.context.settings.get("photoshop_path", "")
        )
        if result.ok:
            state.manager.mark_editing(meta.handoff_id)
        else:
            state.manager.mark_error(meta.handoff_id, result.error or "Failed to launch Photoshop")
        return routes.OpenAttempt(tier=1, ok=result.ok, error=result.error)

    @staticmethod
    def _send_tier2_open(
        state: _NodeState, meta: HandoffMeta, psd_path: Path
    ) -> routes.OpenAttempt:
        """Ask the connected Tier 2 plugin to open *psd_path*, bounded to
        ``_TIER2_SEND_TIMEOUT_SECONDS``.

        This is the ONLY part of the open path that still crosses threads:
        the plugin websocket lives on *state.loop*, so reaching it from
        this (worker) thread needs ``asyncio.run_coroutine_threadsafe``.
        Unlike the original implementation, the returned future's
        ``result()`` is bounded -- a wedged plugin connection or a loop
        that stops pumping now fails this ONE open attempt instead of
        hanging ``prompt_worker`` (and therefore ComfyUI's entire prompt
        queue, which processes one item at a time) forever.

        Defensively refuses to even attempt the cross-thread wait when this
        thread turns out to already BE *state.loop*'s own thread (see
        :func:`_running_on_state_loop`) -- calling ``future.result()``
        there would deadlock for real (scheduling work onto a loop, then
        blocking the very thread that must run it). Current ComfyUI never
        takes this branch (module docstring), but this package cannot make
        that a guarantee about a third-party host, so it falls back to the
        loop-free Tier 1 path instead of trusting it unconditionally.
        """
        node_id = meta.origin_node_id
        handoff_id = meta.handoff_id

        if _running_on_state_loop(state):
            logger.warning(
                "cpsb bridge: node %s handoff %s: execute() is running on ComfyUI's "
                "own event-loop thread -- skipping the Tier 2 websocket round trip "
                "(it would deadlock) and launching directly instead",
                node_id,
                handoff_id,
            )
            return PhotoshopBridge._launch_tier1_direct(state, meta, psd_path)

        future: concurrent.futures.Future[routes.OpenAttempt] | None = None
        try:
            future = asyncio.run_coroutine_threadsafe(
                routes.open_in_photoshop(
                    state.app, state.context, state.manager, meta, psd_path
                ),
                state.loop,
            )
            return future.result(timeout=_TIER2_SEND_TIMEOUT_SECONDS)
        except (concurrent.futures.TimeoutError, RuntimeError) as exc:
            if future is not None:
                future.cancel()  # Best-effort; harmless if already done/failed.
            error = (
                f"Timed out after {_TIER2_SEND_TIMEOUT_SECONDS:.0f}s waiting for "
                f"ComfyUI's event loop to send the Tier 2 open request ({exc})"
            )
            logger.error("cpsb bridge: node %s handoff %s: %s", node_id, handoff_id, error)
            state.manager.mark_error(handoff_id, error)
            return routes.OpenAttempt(tier=2, ok=False, error=error)

    @staticmethod
    def _load_edit_tensors(
        manager: HandoffManager, handoff_id: str, fallback_image: Any
    ) -> tuple[Any, Any]:
        """The active handoff's ``(IMAGE, MASK)`` tensors (PROTOCOL.md §6).

        Falls back to *fallback_image* (the node's own input, unchanged)
        paired with an all-zero mask when there is no edit on disk yet
        (shouldn't normally happen here -- every caller already confirmed
        ``active.edits`` or a successful wait first -- but a filesystem
        race is cheap to guard against). MASK preference order once the
        edit image itself is loaded: the edit's extracted channel mask
        (PROTOCOL.md §4, via ``HandoffManager.mask_image_path``) -> else
        ``1 - alpha`` of the edit image (LoadImage parity) -> else an
        all-zero mask sized to the edit image.
        """
        path = manager.edit_image_path(handoff_id)
        if path is None or not path.exists():
            width, height = _image_tensor_size(fallback_image)
            return fallback_image, _zeros_mask_tensor(width, height)

        with Image.open(path) as edit_image:
            edit_image.load()
            image_tensor = _pil_to_tensor(edit_image)

            mask_path = manager.mask_image_path(handoff_id)
            if mask_path is not None and mask_path.exists():
                with Image.open(mask_path) as mask_image:
                    mask_image.load()
                    return image_tensor, _mask_tensor_from_l(mask_image)

            if "A" in edit_image.mode:
                return image_tensor, _alpha_complement_mask_tensor(edit_image)

            width, height = edit_image.size
            return image_tensor, _zeros_mask_tensor(width, height)
