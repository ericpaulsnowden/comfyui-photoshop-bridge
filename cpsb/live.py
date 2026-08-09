"""The realtime-drawing nodes (docs/roadmap/realtime-drawing.md M1/M2/M3).

Six nodes: ``PhotoshopLiveCanvas`` (the canvas in, Tier-2-required),
``PhotoshopLivePrompt`` (the preview panel's prompt in) and
``PhotoshopLiveCreativity`` (the preview panel's creativity control -> a
denoise FLOAT) -- both NOT Tier-2-gated, falling back to their own widgets so
ComfyUI-only works -- ``PhotoshopLivePreview`` (the render back out to a
docked Photoshop panel, which also hosts the prompt + creativity controls),
and the refine pass pair (roadmap R1): ``PhotoshopRefineSource`` (the last
full-quality render and/or a full-res canvas capture back INTO any refine
workflow) and ``PhotoshopAddLayer`` (a result pushed straight into the
current document as a layer).

``PhotoshopLiveCanvas`` is the graph's window onto the canvas the user is
ACTIVELY drawing in Photoshop: the plugin's Live Mode streams a keep-latest
JPEG of the canvas over the plugin websocket after every detected stroke
(``live_frame``, PROTOCOL.md §3 -- no save involved, no handoff, no disk),
and this node serves the newest frame as ``(IMAGE, MASK)``. ``IS_CHANGED``
keys on a CONTENT HASH of the newest frame (see its docstring for why not
the frame counter), so ComfyUI's own caching makes a
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
import hashlib
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

#: Long-side cap for preview frames (review-caught, 2026-07-24): nothing else
#: bounds the IMAGE input's size, and a natural "preview the upscaled result"
#: wiring (a 4x upscaler before this node) would otherwise serialize a
#: 10-20MB JPEG into ONE websocket frame per render, stalling the event loop
#: and risking a silent plugin-side drop. The panel is a preview surface --
#: 1024px is plenty, and mirrors the capture side's own 768px discipline.
_RESULT_MAX_SIDE = 1024


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

    CATEGORY = "Photoshop Bridge/Live Rendering (requires Photoshop)"
    RETURN_TYPES = ("IMAGE", "MASK")
    OUTPUT_TOOLTIPS = (
        "The newest frame captured from the Photoshop canvas.",
        "Always all zero -- the live stream has no alpha channel to derive a mask from. "
        "Present for wiring parity with the other nodes in this pack.",
    )
    FUNCTION = "execute"
    DESCRIPTION = (
        "Feeds the live drawing loop with the newest frame of whatever you're currently "
        "drawing on the canvas in Photoshop, streamed automatically after every stroke "
        "-- no saving required. Requires the Photoshop panel plugin, with Live Mode "
        "turned on in its panel; there's no file-only fallback, since this needs a live "
        "stream rather than a saved file. Use auto_queue to control whether an arriving "
        "stroke re-queues the workflow for you or just waits for your next manual "
        "queue. Pair with Photoshop Live Prompt / Photoshop Live Creativity and a "
        "preview node for a real-time sketch-to-render loop."
    )

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, Any]:
        return {
            "required": {
                "auto_queue": (
                    ["On", "Off"],
                    {
                        "default": "On",
                        "tooltip": (
                            "Whether a new stroke in Photoshop automatically re-queues "
                            "the workflow. 'On' re-queues (coalesced, so rapid strokes "
                            "don't pile up runs). 'Off' still streams every stroke -- it "
                            "just waits for you to queue manually to pick up the newest "
                            "one."
                        ),
                    },
                ),
            },
        }

    @classmethod
    def IS_CHANGED(cls, auto_queue: str) -> str:
        """A content hash of the newest frame -- the caching key that makes the loop cheap.

        Keyed on the FRAME BYTES, deliberately not the frame counter
        (review-caught bug, 2026-07-24): the counter lives on the
        per-websocket ``PluginConnection`` and restarts at 0 on every plugin
        reconnect, so a counter key like ``frame-1`` could ALIAS a
        previous session's already-executed key and make ComfyUI serve the
        OLD canvas's cached render for a brand-new drawing -- silently
        swallowing the first stroke after every reconnect. A content hash
        cannot alias across sessions, and its one "collision" is the
        correct one by definition: identical canvas bytes -> identical
        render -> serving the cache is exactly right. (~0.2ms to hash a
        typical frame, only on IS_CHANGED calls -- noise next to a
        generation.) ``auto_queue`` is deliberately NOT folded in -- like
        ``timeout_seconds`` on the other nodes, a widget change is already
        caught by ComfyUI's own input diffing.
        """
        state = nodes._require_state()
        frame = routes.get_live_frame(state.app)
        if frame is None:
            return "no-frame"
        jpeg, _seq, _title = frame
        return hashlib.sha256(jpeg).hexdigest()[:16]

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


class PhotoshopLivePrompt:
    """Serves the prompt the user typed in the plugin panel's Live Mode field.

    The realtime companion to :class:`PhotoshopLiveCanvas`: wire its ``STRING``
    output into a ``CLIPTextEncode`` ``text`` input (drag onto the socket; since
    frontend 1.16 every widget-backed input already has one, so there is no
    "convert to input" step) so the user can drive the prompt from INSIDE
    Photoshop -- change the
    words in the panel, the live loop re-renders with them, no tabbing to the
    ComfyUI graph. The panel streams each edit as a ``live_prompt`` message
    (PROTOCOL.md §3); the server keeps the newest in one slot
    (:func:`cpsb.routes.get_live_prompt`).

    **NOT Tier-2-gated**, unlike the canvas node: a prompt is not save-free
    capture, it is just text, and the ComfyUI-only path must keep working
    (``bridge-design-ethos.md``: "the ComfyUI-only version must work"). So the
    node ALWAYS has a usable value -- it falls back to its own ``prompt``
    widget whenever the panel field is empty or no plugin is connected. The
    plugin makes it *better* (edit live, without leaving Photoshop), never
    *required*.
    """

    CATEGORY = "Photoshop Bridge/Live Rendering (requires Photoshop)"
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("prompt",)
    OUTPUT_TOOLTIPS = (
        "The effective prompt -- from the Photoshop panel if it's connected and "
        "non-empty, otherwise this node's own prompt widget.",
    )
    FUNCTION = "execute"
    DESCRIPTION = (
        "Supplies a text prompt you can type and edit live from the Photoshop panel, "
        "without switching back to the ComfyUI tab -- wire its output into a CLIP Text "
        "Encode node's text input. Works with ComfyUI alone using this node's own "
        "prompt widget as a fallback; connecting the Photoshop panel plugin lets the "
        "panel's own prompt field take over instead. Meant for the live drawing loop, "
        "alongside Photoshop Live Canvas."
    )

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, Any]:
        return {
            "required": {
                "prompt": (
                    "STRING",
                    {
                        "multiline": True,
                        "default": "a detailed digital painting, vivid colors",
                        "tooltip": (
                            "Fallback prompt used when the Photoshop panel isn't "
                            "connected or its own prompt field is empty. Once the panel "
                            "is connected, typing there overrides this widget live."
                        ),
                    },
                ),
            },
        }

    @classmethod
    def IS_CHANGED(cls, prompt: str) -> str:
        """Bust the cache when the PANEL prompt changes.

        The node widget's own value is already diffed by ComfyUI (it is a
        graph input), so this only needs to surface the plugin-streamed
        prompt, which the graph cannot otherwise see. Returns that streamed
        text (namespaced ``"live:"``) or the ``"no-live-prompt"`` sentinel
        when the field is empty / no plugin, so typing in the panel re-runs
        the graph while an unchanged field is served from cache -- the same
        backpressure story as the canvas node.

        The ``"live:"`` prefix keeps the empty-state sentinel in a value-space
        no panel text can occupy (review-caught, 2026-07-24): without it, a
        user who literally typed ``no-live-prompt`` then CLEARED the field
        would hit the identical cache signature -- ComfyUI folds this return
        into the node's cache key alongside the widget value -- and the
        node would keep serving the stale ``no-live-prompt`` instead of
        falling back to the widget. A streamed prompt is always a non-empty
        stripped string, so ``live:<text>`` can never equal the sentinel.
        """
        state = nodes._require_state()
        live = routes.get_live_prompt(state.app)
        return f"live:{live}" if live is not None else "no-live-prompt"

    def execute(self, prompt: str) -> tuple[str]:
        state = nodes._require_state()
        live = routes.get_live_prompt(state.app)
        effective = live if live is not None else prompt
        source = "plugin panel" if live is not None else "node widget"
        logger.info("cpsb live: serving prompt from %s (%d chars)", source, len(effective))
        return (effective,)


class PhotoshopLiveCreativity:
    """A "creativity" knob for the live loop, driven from the preview panel.

    Outputs a ``FLOAT`` to wire into the KSampler's ``denoise`` input (drag
    onto the socket -- no "convert to input" step since frontend 1.16). The
    preview panel's Creativity control is a **Low/Medium/High** segmented
    button row, NOT a continuous slider (``photoshop_plugin/previewPanel.js``
    ``CREATIVITY_LEVELS``: the 0-100 slider was too granular to read); each
    level streams ``0.0``/``0.5``/``1.0`` (``live_creativity``, PROTOCOL.md
    §3), which this node maps onto a denoise band ``[min_denoise,
    max_denoise]`` -- so the three buttons land on the band's min, midpoint
    and max -- letting the user tune how much the AI re-imagines their drawing
    WITHOUT opening ComfyUI. Low creativity = hug the sketch (low denoise);
    high = reinterpret it (high denoise).

    Denoise is the single most effective adherence control for img2img (and,
    at a fixed few-step count, the fix for "the render looks 99% like my
    drawing"). It is the one setting exposed here; the Creativity control
    could later drive more (CFG, steps) -- see the roadmap.

    **NOT Tier-2-gated** (like :class:`PhotoshopLivePrompt`): falls back to its
    own ``creativity`` widget when the panel isn't driving it, so the
    ComfyUI-only path still works.
    """

    CATEGORY = "Photoshop Bridge/Live Rendering (requires Photoshop)"
    RETURN_TYPES = ("FLOAT",)
    RETURN_NAMES = ("denoise",)
    OUTPUT_TOOLTIPS = (
        "The creativity level mapped onto a denoise range (the Photoshop panel's "
        "CREATIVITY RANGE when it is connected, else min_denoise..max_denoise) -- "
        "wire this into a KSampler's denoise input.",
    )
    FUNCTION = "execute"
    DESCRIPTION = (
        "Turns a single 'creativity' level into a denoise value for a sampler, so the "
        "Low / Medium / High buttons in the Photoshop panel control how closely a live "
        "render sticks to your sketch versus reinterpreting it. Works with ComfyUI "
        "alone using this node's own creativity widget as a fallback; connecting the "
        "Photoshop panel plugin lets its Creativity buttons drive the value live "
        "instead. Wire the denoise output into a KSampler's denoise input. "
        "min_denoise/max_denoise are the fallback range creativity maps onto (Low "
        "lands on min_denoise, Medium halfway between, High on max_denoise), used "
        "only when the panel isn't supplying its own -- with the panel connected, "
        "its per-capture-size CREATIVITY RANGE setting overrides these widgets."
    )

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, Any]:
        return {
            "required": {
                "creativity": (
                    "FLOAT",
                    {
                        "default": 0.6,
                        "min": 0.0,
                        "max": 1.0,
                        "step": 0.01,
                        "tooltip": (
                            "Fallback creativity level (0 = hug the drawing, 1 = "
                            "reinterpret it freely), used when the Photoshop panel's "
                            "Low / Medium / High Creativity buttons aren't driving "
                            "this node."
                        ),
                    },
                ),
                "min_denoise": (
                    "FLOAT",
                    {
                        "default": 0.40,
                        "min": 0.0,
                        "max": 1.0,
                        "step": 0.01,
                        "tooltip": (
                            "Denoise that creativity 0 maps to when the Photoshop "
                            "panel isn't supplying its own range -- how little the "
                            "sampler changes your drawing at the lowest creativity "
                            "setting. With the panel connected, its per-capture-size "
                            "CREATIVITY RANGE setting overrides this."
                        ),
                    },
                ),
                "max_denoise": (
                    "FLOAT",
                    {
                        "default": 0.85,
                        "min": 0.0,
                        "max": 1.0,
                        "step": 0.01,
                        "tooltip": (
                            "Denoise that creativity 1 maps to when the Photoshop "
                            "panel isn't supplying its own range -- how much the "
                            "sampler can reinterpret your drawing at the highest "
                            "creativity setting. With the panel connected, its "
                            "per-capture-size CREATIVITY RANGE setting overrides this."
                        ),
                    },
                ),
            },
        }

    @staticmethod
    def _denoise(creativity: float, min_denoise: float, max_denoise: float) -> float:
        # sorted() so a min>max misconfiguration can't invert the band.
        lo, hi = sorted((float(min_denoise), float(max_denoise)))
        c = max(0.0, min(1.0, float(creativity)))
        return round(lo + c * (hi - lo), 4)

    @classmethod
    def IS_CHANGED(cls, creativity: float, min_denoise: float, max_denoise: float) -> str:
        """Bust the cache when the PANEL level OR its band changes.

        The widget values are diffed natively by ComfyUI, so this only
        surfaces the panel-streamed state (namespaced ``live:``, like
        :meth:`PhotoshopLivePrompt.IS_CHANGED`, so it can never alias the
        no-panel sentinel), letting a level change -- or a capture-size
        switch that brings a different band with it -- re-run the graph while
        an untouched panel is served from cache.
        """
        state = nodes._require_state()
        live = routes.get_live_creativity(state.app)
        if live is None:
            return "no-live-creativity"
        band = routes.get_live_creativity_band(state.app)
        return f"live:{live}:{band[0]}-{band[1]}" if band else f"live:{live}"

    def execute(
        self, creativity: float, min_denoise: float, max_denoise: float
    ) -> tuple[float]:
        state = nodes._require_state()
        live = routes.get_live_creativity(state.app)
        effective = live if live is not None else creativity
        # The panel's band (a per-capture-size preference) wins when present:
        # the right denoise range depends on the model+resolution combo, which
        # the panel tracks per capture size (owner report 2026-07-24). Without
        # one -- ComfyUI-only, or an older plugin -- the node's own widgets
        # define the band exactly as before.
        band = routes.get_live_creativity_band(state.app)
        lo, hi = band if band is not None else (min_denoise, max_denoise)
        denoise = self._denoise(effective, lo, hi)
        source = "panel" if live is not None else "node widget"
        band_source = "panel band" if band is not None else "node band"
        logger.info(
            "cpsb live: creativity %.2f from %s over %s [%.2f, %.2f] -> denoise %.3f",
            effective,
            source,
            band_source,
            lo,
            hi,
            denoise,
        )
        return (denoise,)


class PhotoshopLivePreview:
    """Pushes its IMAGE input to the plugin's "ComfyUI Preview" panel (M3).

    The live loop's feedback surface: wire the sampler/decode output here
    and each render appears in a panel DOCKED INSIDE PHOTOSHOP, beside the
    canvas the user is drawing on -- the roadmap's headline UX. An output
    node (``OUTPUT_NODE = True``, no return sockets); encodes the first
    frame of its input batch as JPEG (quality 85, ``_RESULT_JPEG_QUALITY``) and
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

    CATEGORY = "Photoshop Bridge/Live Rendering (requires Photoshop)"
    RETURN_TYPES = ()
    FUNCTION = "execute"
    OUTPUT_NODE = True
    DESCRIPTION = (
        "Sends the input image to a preview panel docked inside Photoshop, right next "
        "to the canvas you're drawing on -- the display surface for the live drawing "
        "loop. Requires the Photoshop panel plugin to actually show anything; without "
        "it connected, this node quietly does nothing rather than stopping the "
        "workflow, since the render itself already succeeded. Use at the end of a "
        "live-loop render, alongside Photoshop Refine Source and Photoshop Add Layer "
        "for the optional refine pass."
    )

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, Any]:
        return {
            "required": {
                "image": (
                    "IMAGE",
                    {"tooltip": "The rendered image to display in the Photoshop preview panel."},
                ),
            },
        }

    def execute(self, image: Any) -> dict[str, Any]:
        state = nodes._require_state()

        pil_image = nodes._tensor_to_pil(image)
        # Refine-pass cornerstone (roadmap R1): keep the FULL-QUALITY render
        # in the app-level keep-latest slot BEFORE the display copy is
        # thumbnailed below -- `PhotoshopRefineSource` refines from it and
        # the plugin's "Add as a layer" fetches it (`request_render`). The
        # panel keeps getting the small JPEG; this slot is the real pixels.
        routes.set_last_render(state.app, pil_image.copy())
        # thumbnail() only ever shrinks (aspect preserved) -- a sampler-size
        # render passes through untouched; see _RESULT_MAX_SIDE for why the
        # cap exists at all.
        pil_image.thumbnail((_RESULT_MAX_SIDE, _RESULT_MAX_SIDE))
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
        coro = routes.send_result_frame(state.app, data_b64, doc_title)
        try:
            future = asyncio.run_coroutine_threadsafe(coro, state.loop)
            sent = future.result(timeout=_RESULT_SEND_TIMEOUT_SECONDS)
        except Exception as exc:
            # Broad on purpose (review-caught, 2026-07-24): the future
            # re-raises whatever the send coroutine raised, and a plugin
            # transport dying mid-write raises aiohttp's
            # ClientConnectionResetError -- an OSError subclass the old
            # (TimeoutError, RuntimeError) tuple missed, which made the
            # EXACT case this node's contract promises to absorb (plugin
            # dropped mid-render) fail the finished render instead.
            if future is not None:
                future.cancel()  # Best-effort; harmless if already done.
            else:
                coro.close()  # Never scheduled (no usable loop) -- avoid the warning.
            return False, f"send timed out/failed: {exc}"

        if not sent:
            return False, "no Tier-2 plugin connected"
        return True, ""


#: Long-side cap for images pushed as Photoshop layers (refine pass R1;
#: Eric's ceiling decision 2026-07-24: target the PSD's size, proportionally
#: capped below 4096). The server caps PIXELS at 4096; the plugin scales the
#: placed layer to the document bounds, so a document larger than 4096 still
#: gets a full-canvas layer -- just from <=4096 source pixels.
_ADD_LAYER_MAX_SIDE = 4096

#: Bound on the cross-thread `add_layer` chunked send. Larger than the
#: result-frame bound: a full-quality PNG can be tens of MB and the transfer
#: is chunked, so give a remote (LAN) plugin real time before declaring it
#: undeliverable.
_ADD_LAYER_SEND_TIMEOUT_SECONDS = 60.0


class PhotoshopRefineSource:
    """Serves the refine pass its source images (roadmap R1, decided 2026-07-24).

    Two ``IMAGE`` outputs, and the refine workflow wires whichever it wants
    (Eric's "both, workflow picks" decision):

    - ``render`` -- the last live render at FULL quality (the app-level slot
      :func:`cpsb.routes.get_last_render`, stored by ``PhotoshopLivePreview``
      before its display copy is downscaled). "Refine exactly what you saw."
    - ``canvas`` -- the full-resolution canvas capture the plugin uploaded
      with the Refine click (`refine_push`, PROTOCOL.md §3; proportionally
      capped plugin-side per the <=4096 ceiling). The higher-quality-ceiling
      source; composition can drift from the shown render.

    FALLBACKS keep every wiring shape usable: a missing canvas serves the
    render on both outputs (e.g. a manual ComfyUI-side Queue with no Refine
    click -- the R1 path), a missing render serves the canvas on both, and
    only BOTH missing interrupts with an actionable log. NOT Tier-2-gated:
    the render slot fills without any plugin.

    **Mute this node to disarm the refine branch.** ComfyUI prunes the whole
    dependent subtree of a muted node from execution (verified on the test
    rig, 2026-07-24) -- the frontend live loop un-mutes it for exactly one
    queue per Refine click (`cpsb.refine`, web/cpsb/live.js) and re-mutes
    after, so refine chains never run per-stroke.
    """

    CATEGORY = "Photoshop Bridge/Live Rendering (requires Photoshop)"
    RETURN_TYPES = ("IMAGE", "IMAGE")
    RETURN_NAMES = ("render", "canvas")
    OUTPUT_TOOLTIPS = (
        "The last full-quality render from Photoshop Live Preview. Falls back to the "
        "canvas capture if no render exists yet.",
        "A full-resolution capture of the Photoshop canvas, taken when you clicked "
        "Refine. Falls back to the last render if no capture exists yet.",
    )
    FUNCTION = "execute"
    DESCRIPTION = (
        "Feeds a refine pass its starting image: the last full-quality render from "
        "Photoshop Live Preview, a full-resolution capture of the Photoshop canvas, or "
        "both -- wire whichever output your refine workflow wants. Works with ComfyUI "
        "alone; the render output fills in without any plugin connected, and the "
        "canvas output fills in when the Photoshop panel plugin sends one. Falls back "
        "to whichever of the two it has when the other is missing, and only stops the "
        "workflow if neither is available yet. Mute this node to disarm the whole "
        "refine branch so it only runs on an explicit Refine click, not on every "
        "drawing stroke."
    )

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, Any]:
        return {"required": {}}

    @classmethod
    def IS_CHANGED(cls) -> str:
        """Key on (refine request, render, canvas) so each Refine click --
        and each new render, for manual re-queues -- re-runs the branch once,
        while an unchanged state is served from cache (the same backpressure
        story as every other live node)."""
        state = nodes._require_state()
        app = state.app
        render = routes.get_last_render(app)
        canvas = routes.get_refine_canvas(app)
        return (
            f"req-{routes.get_refine_request_id(app)}"
            f":render-{render[1] if render else 0}"
            f":canvas-{canvas[1] if canvas else 0}"
        )

    def execute(self) -> tuple[Any, Any]:
        state = nodes._require_state()

        render = routes.get_last_render(state.app)
        render_image = render[0] if render else None

        canvas_image = None
        canvas = routes.get_refine_canvas(state.app)
        if canvas is not None:
            png_bytes, canvas_seq = canvas
            try:
                canvas_image = Image.open(io.BytesIO(png_bytes))
                canvas_image.load()
            except Exception:
                logger.warning(
                    "cpsb refine: canvas capture %d failed to decode; falling back to the render",
                    canvas_seq,
                    exc_info=True,
                )

        if render_image is None and canvas_image is None:
            logger.warning(
                "cpsb refine: nothing to refine yet -- run the live loop (or click Refine "
                "in the Photoshop preview panel) so a render or canvas capture exists."
            )
            nodes._raise_interrupt()

        effective_render = render_image if render_image is not None else canvas_image
        effective_canvas = canvas_image if canvas_image is not None else render_image
        logger.info(
            "cpsb refine: serving render=%s canvas=%s",
            "live" if render_image is not None else "canvas-fallback",
            "capture" if canvas_image is not None else "render-fallback",
        )
        render_tensor, _mask = nodes._tensors_from_image(effective_render)
        canvas_tensor, _mask = nodes._tensors_from_image(effective_canvas)
        return (render_tensor, canvas_tensor)


class PhotoshopAddLayer:
    """Pushes its IMAGE input into the CURRENT Photoshop document as a layer.

    Refine pass R1 (roadmap; Eric's routing decision: "the workflow decides"
    -- ending a chain in THIS node routes the result straight into the
    document, ending it in ``PhotoshopLivePreview`` routes it to the preview
    pane; wire both for both). An output node: encodes the first frame of
    its input batch as full-quality PNG, proportionally capped at
    {_ADD_LAYER_MAX_SIDE}px long side (the <=4096 ceiling -- the plugin
    scales the placed layer to the document bounds, so pixels arrive capped
    but the LAYER lands full-canvas), and sends it as chunked
    ``add_layer_chunk`` messages (:func:`cpsb.routes.send_add_layer`). The
    plugin places it honoring the main panel's Stack-vs-Replace preference.

    **NOT Tier-2-gated with an interrupt** -- same no-fail contract and
    rationale as :class:`PhotoshopLivePreview`: this runs at the END of a
    (possibly expensive) refine; a dropped plugin must not throw the result
    away, so a missing plugin is a logged no-op.
    """

    CATEGORY = "Photoshop Bridge/Live Rendering (requires Photoshop)"
    RETURN_TYPES = ()
    FUNCTION = "execute"
    OUTPUT_NODE = True
    DESCRIPTION = (
        "Pushes the input image into the current Photoshop document as a new layer -- "
        "the way to route a refine pass's result straight back into your document "
        "instead of (or alongside) the preview panel. Requires the Photoshop panel "
        "plugin to actually deliver the layer; without it connected, this node quietly "
        "does nothing rather than stopping the workflow, since the image is already "
        "visible in ComfyUI either way. Set layer_name to control the name Photoshop "
        "gives the new layer."
    )

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, Any]:
        return {
            "required": {
                "image": (
                    "IMAGE",
                    {
                        "tooltip": (
                            "The image to add as a new layer in the current Photoshop "
                            "document."
                        )
                    },
                ),
                "layer_name": (
                    "STRING",
                    {
                        "default": "ComfyUI refined",
                        "tooltip": "Name for the new layer in Photoshop.",
                    },
                ),
            },
        }

    def execute(self, image: Any, layer_name: str) -> dict[str, Any]:
        state = nodes._require_state()

        pil_image = nodes._tensor_to_pil(image)
        # thumbnail() only ever shrinks (aspect preserved); a render already
        # under the ceiling passes through at full size.
        pil_image.thumbnail((_ADD_LAYER_MAX_SIDE, _ADD_LAYER_MAX_SIDE))
        buffer = io.BytesIO()
        pil_image.save(buffer, format="PNG")
        png_b64 = base64.b64encode(buffer.getvalue()).decode("ascii")

        name = (layer_name or "").strip() or "ComfyUI refined"
        sent, why = self._send_add_layer(state, png_b64, name)
        if not sent:
            logger.warning(
                "cpsb refine: layer not delivered to Photoshop (%s) -- the image is still "
                "visible in ComfyUI",
                why,
            )
        else:
            logger.info(
                "cpsb refine: pushed %dx%d layer %r to Photoshop",
                *pil_image.size,
                name,
            )
        return {}

    @staticmethod
    def _send_add_layer(state: Any, png_b64: str, layer_name: str) -> tuple[bool, str]:
        """Cross-thread, bounded chunked send -- the exact
        :meth:`PhotoshopLivePreview._send_result` shape with a larger bound
        (see :data:`_ADD_LAYER_SEND_TIMEOUT_SECONDS`)."""
        if nodes._running_on_state_loop(state):
            return False, "running on the event-loop thread; skipping to avoid a deadlock"

        future: concurrent.futures.Future[bool] | None = None
        coro = routes.send_add_layer(state.app, png_b64, layer_name)
        try:
            future = asyncio.run_coroutine_threadsafe(coro, state.loop)
            sent = future.result(timeout=_ADD_LAYER_SEND_TIMEOUT_SECONDS)
        except Exception as exc:
            if future is not None:
                future.cancel()  # Best-effort; harmless if already done.
            else:
                coro.close()  # Never scheduled (no usable loop) -- avoid the warning.
            return False, f"send timed out/failed: {exc}"

        if not sent:
            return False, "no Tier-2 plugin connected"
        return True, ""
