"""The realtime-drawing nodes (docs/roadmap/realtime-drawing.md M1/M3).

``PhotoshopLiveCanvas`` is the graph's window onto the canvas the user is
ACTIVELY drawing in Photoshop: the plugin's Live Mode streams a keep-latest
JPEG of the canvas over the plugin websocket after every detected stroke
(``live_frame``, PROTOCOL.md §3 -- no save involved, no handoff, no disk),
and this node serves the newest frame as ``(IMAGE, MASK)``. ``IS_CHANGED``
keys on the server-side frame counter, so ComfyUI's own caching makes a
re-queue with no new frame a near-free no-op -- that is the entire
backpressure story on the graph side, and it is why the live loop (M2's
frontend queue, or a hand-clicked Queue) can fire liberally without wasting
GPU on unchanged frames.

**Tier-2-plugin-required, like** :class:`cpsb.actions.PhotoshopAction` **and
for the same ethos reason** (``bridge-design-ethos.md``): there is no Tier-1
equivalent of save-free capture -- the file watcher only ever sees saves, and
"draw with live feedback" without the plugin is exactly the "ComfyUI-only
version is impossible" exception. The node interrupts with an actionable log
line rather than silently returning stale pixels when no plugin is connected
or Live Mode isn't streaming.

**Frames are ephemeral by design.** The plan's commitment (roadmap "Design
commitments"): a live frame is never a handoff, never written to disk, never
in the gallery. This module therefore has none of the create/supersede/
wait_for_edit machinery every other PS-touching node carries -- its entire
server-side state is one keep-latest slot on the plugin connection
(:func:`cpsb.routes.get_live_frame`), which dies with the connection.

MASK is always all-zeros: the wire format is JPEG (the documented UXP
``imaging.encodeImageData`` output, JPEG-only -- see the roadmap's research
notes), which cannot carry alpha. The output exists for wiring parity with
every other node in this pack; derive real masks downstream.

Reuses :mod:`cpsb.nodes`' shared plumbing through the module object
(``nodes._require_state()``, ``nodes._tensors_from_image()``,
``nodes._raise_interrupt()``) -- the exact cross-module convention
:mod:`cpsb.actions`/:mod:`cpsb.annotate` established, so tests that
monkeypatch ``cpsb.nodes.X`` reach this module's calls too.
"""

from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import io
import logging
from typing import Any

from PIL import Image

from . import nodes, routes

logger = logging.getLogger("cpsb")

#: Bound on waiting for the ``result_frame`` websocket send to be scheduled
#: and sent on ComfyUI's event loop -- the same deadlock-proofing bound (and
#: rationale) as :data:`cpsb.actions._RUN_ACTION_SEND_TIMEOUT_SECONDS`: a
#: wedged plugin connection or a stopped loop fails this ONE send instead of
#: hanging ComfyUI's prompt worker.
_RESULT_SEND_TIMEOUT_SECONDS = 10.0

#: JPEG quality for preview frames pushed to the Photoshop panel. 85 is the
#: classic size/quality sweet spot; a preview panel never needs lossless.
_RESULT_JPEG_QUALITY = 85


class PhotoshopLiveCanvas:
    """Serves the newest live-drawing frame from the Photoshop plugin (module docstring).

    Inputs: ``auto_queue`` (COMBO ``On``/``Off``) -- read CLIENT-SIDE only,
    by ``web/cpsb/live.js`` (M2): ``On`` means an arriving live frame
    auto-queues a re-run (coalesced), ``Off`` means frames still stream and
    the next MANUAL queue picks the newest up. The server/backend never
    reads it -- the same widget-read-by-frontend gating convention as the
    bridge node's own ``mode`` check in ``pasteback.js``'s auto-queue.

    Output: ``(IMAGE, MASK)`` -- MASK always zeros (JPEG wire format, no
    alpha; module docstring).
    """

    CATEGORY = "image/photoshop"
    RETURN_TYPES = ("IMAGE", "MASK")
    FUNCTION = "execute"

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, Any]:
        return {
            "required": {
                "auto_queue": (["On", "Off"], {"default": "On"}),
            },
        }

    @classmethod
    def IS_CHANGED(cls, auto_queue: str) -> str:
        """The server-side live frame counter -- the caching key that makes the loop cheap.

        A new frame changes the returned string, forcing re-execution on the
        next queue; an unchanged frame leaves it stable, so ComfyUI serves
        the whole downstream subgraph from cache (near-free). ``auto_queue``
        is deliberately NOT folded in -- like ``timeout_seconds`` on the
        other nodes, a widget change is already caught by ComfyUI's own
        input diffing.
        """
        state = nodes._require_state()
        frame = routes.get_live_frame(state.app)
        if frame is None:
            return "no-frame"
        _jpeg, seq, _title = frame
        return f"frame-{seq}"

    def execute(self, auto_queue: str) -> tuple[Any, Any]:
        state = nodes._require_state()

        if not routes.tier2_connected(state.app):
            logger.warning(
                "cpsb live: no Tier-2 plugin connected -- live drawing requires the "
                "Photoshop panel plugin (there is no save-free capture without it). "
                "Connect the ComfyUI Bridge panel in Photoshop and toggle Live Mode."
            )
            nodes._raise_interrupt()

        frame = routes.get_live_frame(state.app)
        if frame is None:
            logger.warning(
                "cpsb live: no live frame yet -- toggle Live Mode in the Photoshop "
                "panel (and make a stroke) so the plugin starts streaming the canvas."
            )
            nodes._raise_interrupt()

        jpeg, seq, doc_title = frame
        try:
            image = Image.open(io.BytesIO(jpeg))
            image.load()
        except Exception:
            # A frame that passed the server's cheap SOI sniff but doesn't
            # decode (truncated capture?) -- interrupt with a log rather than
            # crash the prompt worker; the next stroke replaces the frame.
            logger.warning(
                "cpsb live: frame %d (%r) failed to decode; waiting for the next stroke",
                seq,
                doc_title,
                exc_info=True,
            )
            nodes._raise_interrupt()

        logger.info("cpsb live: serving frame %d from %r (%dx%d)", seq, doc_title, *image.size)
        return nodes._tensors_from_image(image)


class PhotoshopLivePreview:
    """Pushes its IMAGE input to the plugin's "ComfyUI Preview" panel (M3).

    The live loop's feedback surface: wire the sampler/decode output here
    and each render appears in a panel DOCKED INSIDE PHOTOSHOP, beside the
    canvas the user is drawing on -- the roadmap's headline UX. An output
    node (``OUTPUT_NODE = True``, no return sockets); encodes the first
    frame of its input batch as JPEG (quality {_RESULT_JPEG_QUALITY}) and
    sends it as a single ``result_frame`` websocket message
    (:func:`cpsb.routes.send_result_frame`) -- fire-and-forget keep-latest,
    mirroring ``live_frame``'s posture in the other direction.

    **Deliberately NOT Tier-2-gated with an interrupt**, unlike
    :class:`PhotoshopLiveCanvas`: this node runs at the very END of a
    render. If the plugin dropped mid-run, killing the workflow here would
    throw away a finished image the user can still see in the ComfyUI tab
    -- so a missing plugin is a logged no-op, never a failure. (The
    CANVAS node already gates the pipeline's start on Tier-2.)

    ``prompt``/``extra_pnginfo`` hidden inputs are deliberately not taken --
    nothing is saved anywhere, so there is no metadata to embed.
    """

    CATEGORY = "image/photoshop"
    RETURN_TYPES = ()
    FUNCTION = "execute"
    OUTPUT_NODE = True

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, Any]:
        return {
            "required": {
                "image": ("IMAGE",),
            },
        }

    def execute(self, image: Any) -> dict[str, Any]:
        state = nodes._require_state()

        pil_image = nodes._tensor_to_pil(image)
        buffer = io.BytesIO()
        pil_image.save(buffer, format="JPEG", quality=_RESULT_JPEG_QUALITY)
        data_b64 = base64.b64encode(buffer.getvalue()).decode("ascii")

        doc_title = ""
        frame = routes.get_live_frame(state.app)
        if frame is not None:
            _jpeg, _seq, doc_title = frame

        sent, why = self._send_result(state, data_b64, doc_title)
        if not sent:
            # No interrupt, by design (class docstring): the render already
            # succeeded; only the in-Photoshop preview is unavailable.
            logger.warning(
                "cpsb live: result frame not delivered to the Photoshop preview panel (%s)",
                why,
            )
        return {}

    @staticmethod
    def _send_result(state: Any, data_b64: str, doc_title: str) -> tuple[bool, str]:
        """Cross-thread, bounded ``result_frame`` send -- mirrors
        :meth:`cpsb.actions.PhotoshopAction._send_run_action`'s shape (see
        that method's docstring for the deadlock-proofing rationale),
        including refusing the cross-thread wait when already ON the loop's
        own thread.
        """
        if nodes._running_on_state_loop(state):
            return False, "running on the event-loop thread; skipping to avoid a deadlock"

        future: concurrent.futures.Future[bool] | None = None
        try:
            future = asyncio.run_coroutine_threadsafe(
                routes.send_result_frame(state.app, data_b64, doc_title),
                state.loop,
            )
            sent = future.result(timeout=_RESULT_SEND_TIMEOUT_SECONDS)
        except (concurrent.futures.TimeoutError, RuntimeError) as exc:
            if future is not None:
                future.cancel()  # Best-effort; harmless if already done.
            return False, f"send timed out/failed: {exc}"

        if not sent:
            return False, "no Tier-2 plugin connected"
        return True, ""
