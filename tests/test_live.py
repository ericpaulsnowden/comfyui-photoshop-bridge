"""``PhotoshopLiveCanvas`` (realtime drawing M1, docs/roadmap/realtime-drawing.md).

Node-level behavior against a fake connected plugin whose live-frame slot is
driven directly through :func:`cpsb.routes._handle_live_frame` (a synchronous
handler -- no websocket needed at this level; the wire path has its own tests
in ``tests/test_routes.py``'s ``TestLiveFrame``). Fixture shapes mirror
``tests/test_actions.py`` exactly (the other Tier-2-required node).
"""

from __future__ import annotations

import base64
import io
import logging
import subprocess
import sys
from pathlib import Path
from typing import cast

import numpy as np
import pytest
from aiohttp import web
from PIL import Image

import cpsb.live as live_module
import cpsb.nodes as nodes_module
import cpsb.routes as routes_module
from cpsb.context import CpsbContext
from cpsb.handoff import HandoffManager


def jpeg_bytes(color: tuple[int, int, int], size: tuple[int, int] = (24, 16)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="JPEG", quality=95)
    return buffer.getvalue()


def raises_interrupt():
    """Outside ComfyUI, ``nodes._raise_interrupt`` raises RuntimeError naming
    ``comfy.model_management`` -- same helper as ``tests/test_actions.py``."""
    return pytest.raises(RuntimeError, match=r"comfy\.model_management")


@pytest.fixture
def manager(context: CpsbContext) -> HandoffManager:
    return HandoffManager(context)


@pytest.fixture
def live_app(context: CpsbContext, manager: HandoffManager):
    """An installed app with a READY fake plugin connection -- the node reads
    the live slot through ``routes.get_live_frame(state.app)``."""
    app = web.Application()
    routes_module.install(app, context, manager)
    connection = routes_module.PluginConnection(ws=cast("object", None), ready=True)
    app[routes_module._APP_KEY_PLUGIN].connection = connection
    nodes_module.configure(context, manager, app, cast("object", None))
    yield app, connection
    nodes_module._state = None


@pytest.fixture
def no_plugin_node(context: CpsbContext, manager: HandoffManager):
    app = web.Application()
    routes_module.install(app, context, manager)
    nodes_module.configure(context, manager, app, cast("object", None))
    yield live_module.PhotoshopLiveCanvas()
    nodes_module._state = None


@pytest.fixture
def no_plugin_app(context: CpsbContext, manager: HandoffManager):
    """App installed + nodes configured, but NO plugin connection -- the
    ComfyUI-only path (`PhotoshopLivePrompt` falling back to its widget)."""
    app = web.Application()
    routes_module.install(app, context, manager)
    nodes_module.configure(context, manager, app, cast("object", None))
    yield app
    nodes_module._state = None


def push_prompt(
    context: CpsbContext,
    connection: routes_module.PluginConnection,
    text: str,
) -> None:
    routes_module._handle_live_prompt(
        context, connection, {"type": "live_prompt", "text": text}
    )


def push_creativity(
    context: CpsbContext,
    connection: routes_module.PluginConnection,
    value: float,
    band: tuple[float, float] | None = None,
) -> None:
    msg = {"type": "live_creativity", "value": value}
    if band is not None:
        msg["min_denoise"], msg["max_denoise"] = band
    routes_module._handle_live_creativity(context, connection, msg)


def push_frame(
    context: CpsbContext,
    connection: routes_module.PluginConnection,
    color: tuple[int, int, int],
    title: str = "sketch.psd",
) -> None:
    routes_module._handle_live_frame(
        context,
        connection,
        {
            "type": "live_frame",
            "seq": 1,
            "data_b64": base64.b64encode(jpeg_bytes(color)).decode("ascii"),
            "doc_title": title,
        },
    )


class TestImportability:
    def test_module_imports_without_torch(self):
        """Same isolated-subprocess check as every other node module's own."""
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import cpsb.live as m, sys\n"
                "assert m.PhotoshopLiveCanvas is not None\n"
                "print('torch' in sys.modules)",
            ],
            cwd=str(Path(__file__).resolve().parent.parent),
            capture_output=True,
            text=True,
            check=True,
        )
        assert result.stdout.strip() == "False", result.stderr


class TestIsChanged:
    """The cache key is a CONTENT HASH of the frame bytes, not the frame
    counter -- the counter restarts per plugin connection, so a counter key
    would alias across reconnects and serve a stale cached render for a new
    drawing (PROTOCOL.md 6f)."""

    def test_no_frame_is_stable(self, live_app):
        assert live_module.PhotoshopLiveCanvas.IS_CHANGED(auto_queue="On") == "no-frame"
        assert live_module.PhotoshopLiveCanvas.IS_CHANGED(auto_queue="On") == "no-frame"

    def test_each_new_canvas_changes_the_key(self, context, live_app):
        _app, connection = live_app
        push_frame(context, connection, (1, 1, 1))
        first = live_module.PhotoshopLiveCanvas.IS_CHANGED(auto_queue="On")
        assert first != "no-frame"
        # No new frame -> stable key -> ComfyUI serves the run from cache.
        assert live_module.PhotoshopLiveCanvas.IS_CHANGED(auto_queue="On") == first
        push_frame(context, connection, (2, 2, 2))
        assert live_module.PhotoshopLiveCanvas.IS_CHANGED(auto_queue="On") != first

    def test_identical_bytes_hit_the_cache(self, context, live_app):
        """Same canvas re-sent (e.g. an undo back to a rendered state) is the
        CORRECT cache hit: same pixels in, same render out."""
        _app, connection = live_app
        push_frame(context, connection, (5, 5, 5))
        first = live_module.PhotoshopLiveCanvas.IS_CHANGED(auto_queue="On")
        push_frame(context, connection, (9, 9, 9))
        push_frame(context, connection, (5, 5, 5))
        assert live_module.PhotoshopLiveCanvas.IS_CHANGED(auto_queue="On") == first

    def test_key_survives_reconnect(self, context, live_app):
        """The regression the hash exists to prevent: a NEW connection's first
        frame restarts the seq counter at 1, so a counter-keyed cache would
        collide with the old session's first frame and serve its stale render.
        Different canvases must produce different keys across a reconnect."""
        app, connection = live_app
        push_frame(context, connection, (1, 1, 1))
        old_session = live_module.PhotoshopLiveCanvas.IS_CHANGED(auto_queue="On")

        reconnected = routes_module.PluginConnection(ws=cast("object", None), ready=True)
        app[routes_module._APP_KEY_PLUGIN].connection = reconnected
        push_frame(context, reconnected, (200, 100, 50))
        assert live_module.PhotoshopLiveCanvas.IS_CHANGED(auto_queue="On") != old_session

    def test_auto_queue_not_folded_in(self, context, live_app):
        _app, connection = live_app
        push_frame(context, connection, (3, 3, 3))
        on = live_module.PhotoshopLiveCanvas.IS_CHANGED(auto_queue="On")
        off = live_module.PhotoshopLiveCanvas.IS_CHANGED(auto_queue="Off")
        assert on == off


class TestExecute:
    def test_requires_tier2(self, no_plugin_node):
        with raises_interrupt():
            no_plugin_node.execute(auto_queue="On")

    def test_interrupts_without_a_frame(self, live_app):
        node = live_module.PhotoshopLiveCanvas()
        with raises_interrupt():
            node.execute(auto_queue="On")

    def test_serves_latest_frame_as_tensors(self, context, live_app):
        _app, connection = live_app
        push_frame(context, connection, (255, 0, 0))
        node = live_module.PhotoshopLiveCanvas()

        image_tensor, mask_tensor = node.execute(auto_queue="On")

        assert tuple(image_tensor.shape) == (1, 16, 24, 3)
        pixels = (image_tensor[0].numpy() * 255.0).round().astype(np.uint8)
        red, green, blue = pixels[0, 0]
        assert red > 230 and green < 30 and blue < 30  # JPEG-lossy red
        # MASK is always zeros: JPEG carries no alpha (module docstring).
        assert tuple(mask_tensor.shape) == (1, 16, 24)
        assert float(mask_tensor.max()) == 0.0

    def test_new_frame_replaces_old_pixels(self, context, live_app):
        _app, connection = live_app
        node = live_module.PhotoshopLiveCanvas()
        push_frame(context, connection, (255, 0, 0))
        node.execute(auto_queue="On")

        push_frame(context, connection, (0, 0, 255))
        image_tensor, _mask = node.execute(auto_queue="On")

        pixels = (image_tensor[0].numpy() * 255.0).round().astype(np.uint8)
        red, _green, blue = pixels[0, 0]
        assert blue > 230 and red < 30  # keep-latest: the newest frame wins

    def test_undecodable_frame_interrupts_not_crashes(self, context, live_app):
        _app, connection = live_app
        # Slip a JPEG-SOI-prefixed-but-truncated payload straight into the
        # slot (the server's cheap sniff would admit it).
        connection.live_jpeg = b"\xff\xd8truncated-nonsense"
        connection.live_seq = 7
        node = live_module.PhotoshopLiveCanvas()
        with raises_interrupt():
            node.execute(auto_queue="On")


class _RecordingSocket:
    """Same recording fake as ``tests/test_actions.py``'s."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)


@pytest.fixture
def loop_thread():
    import asyncio
    import threading

    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=5)


@pytest.fixture
def preview_rig(context: CpsbContext, manager: HandoffManager, loop_thread):
    """A configured ``PhotoshopLivePreview`` with a ready fake plugin whose
    socket records every send -- mirrors ``tests/test_actions.py``'s
    ``tier2_action`` fixture."""
    socket = _RecordingSocket()
    app = web.Application()
    routes_module.install(app, context, manager)
    connection = routes_module.PluginConnection(ws=cast("object", socket), ready=True)
    app[routes_module._APP_KEY_PLUGIN].connection = connection
    nodes_module.configure(context, manager, app, loop_thread)
    yield live_module.PhotoshopLivePreview(), socket, connection
    nodes_module._state = None


@pytest.fixture
def addlayer_rig(context: CpsbContext, manager: HandoffManager, loop_thread):
    """A configured ``PhotoshopAddLayer`` with a ready fake plugin whose
    socket records every send -- the ``preview_rig`` shape."""
    socket = _RecordingSocket()
    app = web.Application()
    routes_module.install(app, context, manager)
    connection = routes_module.PluginConnection(ws=cast("object", socket), ready=True)
    app[routes_module._APP_KEY_PLUGIN].connection = connection
    nodes_module.configure(context, manager, app, loop_thread)
    yield live_module.PhotoshopAddLayer(), socket
    nodes_module._state = None


def make_image_tensor(color: tuple[int, int, int], size: tuple[int, int] = (24, 16)):
    import torch

    array = np.zeros((size[1], size[0], 3), dtype=np.float32)
    array[..., 0], array[..., 1], array[..., 2] = (c / 255.0 for c in color)
    return torch.from_numpy(array)[None, ...]


class TestLivePrompt:
    """`PhotoshopLivePrompt`: serves the panel prompt, falling back to its own
    node widget so the ComfyUI-only path still works."""

    WIDGET = "a moody watercolor"

    def test_falls_back_to_widget_with_no_streamed_prompt(self, live_app):
        node = live_module.PhotoshopLivePrompt()
        assert node.execute(prompt=self.WIDGET) == (self.WIDGET,)
        assert live_module.PhotoshopLivePrompt.IS_CHANGED(prompt=self.WIDGET) == "no-live-prompt"

    def test_falls_back_to_widget_with_no_plugin(self, no_plugin_app):
        """No connection at all -> use the node widget (ComfyUI-only)."""
        node = live_module.PhotoshopLivePrompt()
        assert node.execute(prompt=self.WIDGET) == (self.WIDGET,)
        assert live_module.PhotoshopLivePrompt.IS_CHANGED(prompt=self.WIDGET) == "no-live-prompt"

    def test_serves_streamed_panel_prompt_over_widget(self, context, live_app):
        _app, connection = live_app
        push_prompt(context, connection, "a red origami bird")
        node = live_module.PhotoshopLivePrompt()
        assert node.execute(prompt=self.WIDGET) == ("a red origami bird",)
        # IS_CHANGED namespaces the streamed value so it can never alias the
        # empty-state sentinel (review-caught, 2026-07-24).
        assert (
            live_module.PhotoshopLivePrompt.IS_CHANGED(prompt=self.WIDGET)
            == "live:a red origami bird"
        )

    def test_empty_panel_prompt_clears_back_to_widget(self, context, live_app):
        _app, connection = live_app
        push_prompt(context, connection, "temporary override")
        push_prompt(context, connection, "")
        node = live_module.PhotoshopLivePrompt()
        assert node.execute(prompt=self.WIDGET) == (self.WIDGET,)
        assert live_module.PhotoshopLivePrompt.IS_CHANGED(prompt=self.WIDGET) == "no-live-prompt"

    def test_whitespace_panel_prompt_clears_back_to_widget(self, context, live_app):
        _app, connection = live_app
        push_prompt(context, connection, "   \n  ")
        node = live_module.PhotoshopLivePrompt()
        assert node.execute(prompt=self.WIDGET) == (self.WIDGET,)

    def test_is_changed_tracks_each_panel_edit(self, context, live_app):
        _app, connection = live_app
        push_prompt(context, connection, "a cat")
        first = live_module.PhotoshopLivePrompt.IS_CHANGED(prompt=self.WIDGET)
        push_prompt(context, connection, "a dog")
        assert live_module.PhotoshopLivePrompt.IS_CHANGED(prompt=self.WIDGET) != first

    def test_streamed_text_cannot_alias_empty_sentinel(self, context, live_app):
        """Regression (review-caught, 2026-07-24): a user typing the literal
        sentinel string then clearing the field must still re-execute and fall
        back to the widget -- the streamed key is namespaced so it can never
        equal the empty-state key."""
        _app, connection = live_app
        empty_key = live_module.PhotoshopLivePrompt.IS_CHANGED(prompt=self.WIDGET)
        push_prompt(context, connection, "no-live-prompt")
        typed_key = live_module.PhotoshopLivePrompt.IS_CHANGED(prompt=self.WIDGET)
        assert typed_key != empty_key  # no aliasing -> clearing re-runs


class TestLiveCreativity:
    """`PhotoshopLiveCreativity`: maps the panel slider (0..1) onto a denoise
    band, falling back to its own widget so ComfyUI-only works."""

    def test_falls_back_to_widget_denoise(self, live_app):
        node = live_module.PhotoshopLiveCreativity()
        # creativity 0.5 over band [0.4, 0.85] -> 0.4 + 0.5*0.45 = 0.625
        (denoise,) = node.execute(creativity=0.5, min_denoise=0.4, max_denoise=0.85)
        assert denoise == pytest.approx(0.625)
        assert (
            live_module.PhotoshopLiveCreativity.IS_CHANGED(
                creativity=0.5, min_denoise=0.4, max_denoise=0.85
            )
            == "no-live-creativity"
        )

    def test_falls_back_with_no_plugin(self, no_plugin_app):
        node = live_module.PhotoshopLiveCreativity()
        (denoise,) = node.execute(creativity=0.0, min_denoise=0.4, max_denoise=0.85)
        assert denoise == pytest.approx(0.4)  # creativity 0 -> min

    def test_panel_slider_overrides_widget(self, context, live_app):
        _app, connection = live_app
        push_creativity(context, connection, 1.0)  # max creativity -> max_denoise
        node = live_module.PhotoshopLiveCreativity()
        (denoise,) = node.execute(creativity=0.0, min_denoise=0.4, max_denoise=0.85)
        assert denoise == pytest.approx(0.85)
        assert (
            live_module.PhotoshopLiveCreativity.IS_CHANGED(
                creativity=0.0, min_denoise=0.4, max_denoise=0.85
            )
            == "live:1.0"
        )

    def test_value_is_clamped_to_unit_range(self, context, live_app):
        _app, connection = live_app
        push_creativity(context, connection, 5.0)  # out of range -> clamped to 1.0
        assert routes_module.get_live_creativity(_app) == 1.0
        node = live_module.PhotoshopLiveCreativity()
        (denoise,) = node.execute(creativity=0.0, min_denoise=0.4, max_denoise=0.85)
        assert denoise == pytest.approx(0.85)

    def test_inverted_band_does_not_invert_output(self, live_app):
        """min>max is sorted so the map can't run backwards."""
        node = live_module.PhotoshopLiveCreativity()
        (denoise,) = node.execute(creativity=1.0, min_denoise=0.9, max_denoise=0.3)
        assert denoise == pytest.approx(0.9)  # sorted -> [0.3, 0.9], creativity 1 -> 0.9

    def test_is_changed_tracks_each_slider_move(self, context, live_app):
        _app, connection = live_app
        push_creativity(context, connection, 0.3)
        first = live_module.PhotoshopLiveCreativity.IS_CHANGED(
            creativity=0.5, min_denoise=0.4, max_denoise=0.85
        )
        push_creativity(context, connection, 0.7)
        assert (
            live_module.PhotoshopLiveCreativity.IS_CHANGED(
                creativity=0.5, min_denoise=0.4, max_denoise=0.85
            )
            != first
        )


class TestCreativityBand:
    """The panel sends a per-capture-size denoise band with each level (owner
    report 2026-07-24: the levels behaved very differently at 512/768/1024).
    When present it WINS over the node widgets; absent, nothing changes."""

    def test_panel_band_overrides_widgets(self, context, live_app):
        _app, connection = live_app
        push_creativity(context, connection, 1.0, band=(0.30, 0.60))
        node = live_module.PhotoshopLiveCreativity()
        # High creativity over the PANEL band -> its max, not the widgets'.
        (denoise,) = node.execute(creativity=0.0, min_denoise=0.4, max_denoise=0.85)
        assert denoise == pytest.approx(0.60)

    def test_widgets_used_when_no_band_sent(self, context, live_app):
        _app, connection = live_app
        push_creativity(context, connection, 1.0)  # older plugin: value only
        node = live_module.PhotoshopLiveCreativity()
        (denoise,) = node.execute(creativity=0.0, min_denoise=0.4, max_denoise=0.85)
        assert denoise == pytest.approx(0.85)

    def test_band_is_clamped_and_ordered(self, context, live_app):
        app, connection = live_app
        push_creativity(context, connection, 1.0, band=(9.0, -3.0))
        assert routes_module.get_live_creativity_band(app) == (0.0, 1.0)

    def test_half_band_is_ignored(self, context, live_app):
        """A malformed (half-sent) band must fall back to the widgets rather
        than silently mis-mapping every level."""
        app, connection = live_app
        routes_module._handle_live_creativity(
            context, connection, {"type": "live_creativity", "value": 1.0, "min_denoise": 0.3}
        )
        assert routes_module.get_live_creativity_band(app) is None

    def test_is_changed_tracks_band_changes(self, context, live_app):
        _app, connection = live_app
        push_creativity(context, connection, 0.5, band=(0.4, 0.85))
        first = live_module.PhotoshopLiveCreativity.IS_CHANGED(
            creativity=0.5, min_denoise=0.4, max_denoise=0.85
        )
        # Same level, DIFFERENT band (a capture-size switch) must re-run.
        push_creativity(context, connection, 0.5, band=(0.3, 0.6))
        assert (
            live_module.PhotoshopLiveCreativity.IS_CHANGED(
                creativity=0.5, min_denoise=0.4, max_denoise=0.85
            )
            != first
        )


class TestLastRenderSlot:
    """Refine-pass cornerstone (R1): `PhotoshopLivePreview` keeps the newest
    render at FULL quality in the app-level slot -- the display JPEG stays
    capped, the slot holds the real pixels."""

    def test_full_resolution_kept_while_display_is_capped(
        self, context, preview_rig, monkeypatch
    ):
        node, socket, _connection = preview_rig
        # Tiny display cap so a small tensor exercises the same "slot keeps
        # more than the wire" relationship a full-size render would.
        monkeypatch.setattr(live_module, "_RESULT_MAX_SIDE", 8)
        node.execute(image=make_image_tensor((10, 200, 30), size=(24, 16)))

        state = nodes_module._require_state()
        stored = routes_module.get_last_render(state.app)
        assert stored is not None
        image, seq = stored
        assert image.size == (24, 16)  # FULL size, not the display cap
        assert seq == 1
        # The wire copy really was capped.
        sent = socket.sent[-1]
        assert sent["type"] == "result_frame"
        wire = Image.open(io.BytesIO(base64.b64decode(sent["data_b64"])))
        assert max(wire.size) <= 8

    def test_slot_is_keep_latest(self, context, preview_rig):
        node, _socket, _connection = preview_rig
        node.execute(image=make_image_tensor((255, 0, 0)))
        node.execute(image=make_image_tensor((0, 0, 255)))
        state = nodes_module._require_state()
        image, seq = routes_module.get_last_render(state.app)
        assert seq == 2
        assert image.getpixel((0, 0))[2] > 200  # newest (blue) won


class TestRefineSource:
    """`PhotoshopRefineSource` (R1): serves render + canvas with the decided
    fallbacks; interrupts only when NOTHING exists to refine."""

    def _canvas_png(self, color=(20, 40, 60), size=(30, 20)) -> bytes:
        buffer = io.BytesIO()
        Image.new("RGB", size, color).save(buffer, format="PNG")
        return buffer.getvalue()

    def _push_canvas(self, app, color=(20, 40, 60), size=(30, 20)) -> None:
        slot = app[routes_module._APP_KEY_REFINE]
        slot.canvas_png = self._canvas_png(color, size)
        slot.canvas_seq += 1
        slot.request_id += 1

    def test_interrupts_when_nothing_exists(self, live_app):
        node = live_module.PhotoshopRefineSource()
        with raises_interrupt():
            node.execute()

    def test_serves_both_when_both_exist(self, context, live_app):
        app, _connection = live_app
        routes_module.set_last_render(app, Image.new("RGB", (24, 16), (255, 0, 0)))
        self._push_canvas(app, color=(0, 0, 255), size=(30, 20))
        node = live_module.PhotoshopRefineSource()
        render_t, canvas_t = node.execute()
        assert tuple(render_t.shape) == (1, 16, 24, 3)
        assert tuple(canvas_t.shape) == (1, 20, 30, 3)

    def test_missing_canvas_falls_back_to_render(self, context, live_app):
        app, _connection = live_app
        routes_module.set_last_render(app, Image.new("RGB", (24, 16), (255, 0, 0)))
        node = live_module.PhotoshopRefineSource()
        render_t, canvas_t = node.execute()
        assert tuple(render_t.shape) == tuple(canvas_t.shape) == (1, 16, 24, 3)

    def test_missing_render_falls_back_to_canvas(self, context, live_app):
        app, _connection = live_app
        self._push_canvas(app, size=(30, 20))
        node = live_module.PhotoshopRefineSource()
        render_t, canvas_t = node.execute()
        assert tuple(render_t.shape) == tuple(canvas_t.shape) == (1, 20, 30, 3)

    def test_is_changed_tracks_requests_renders_and_canvases(self, context, live_app):
        app, _connection = live_app
        first = live_module.PhotoshopRefineSource.IS_CHANGED()
        routes_module.set_last_render(app, Image.new("RGB", (8, 8), (1, 1, 1)))
        second = live_module.PhotoshopRefineSource.IS_CHANGED()
        assert second != first
        self._push_canvas(app)
        third = live_module.PhotoshopRefineSource.IS_CHANGED()
        assert third != second
        assert live_module.PhotoshopRefineSource.IS_CHANGED() == third  # stable -> cached


class TestAddLayer:
    """`PhotoshopAddLayer` (R1): full-quality PNG out to the plugin as
    chunked `add_layer_chunk` messages; capped; no-fail without a plugin."""

    def test_sends_chunked_png_that_reassembles(self, context, addlayer_rig):
        node, socket = addlayer_rig
        node.execute(image=make_image_tensor((10, 200, 30), size=(24, 16)), layer_name="My layer")

        chunks = [m for m in socket.sent if m["type"] == "add_layer_chunk"]
        assert chunks, "no add_layer_chunk messages sent"
        total = chunks[0]["total"]
        assert len(chunks) == total
        assert all(c["layer_name"] == "My layer" for c in chunks)
        assert len({c["transfer_id"] for c in chunks}) == 1
        ordered = sorted(chunks, key=lambda c: c["seq"])
        png = base64.b64decode("".join(c["data_b64"] for c in ordered))
        image = Image.open(io.BytesIO(png))
        assert image.size == (24, 16)
        assert image.convert("RGB").getpixel((0, 0)) == (10, 200, 30)

    def test_caps_long_side(self, context, addlayer_rig, monkeypatch):
        node, socket = addlayer_rig
        monkeypatch.setattr(live_module, "_ADD_LAYER_MAX_SIDE", 12)
        node.execute(image=make_image_tensor((5, 5, 5), size=(24, 16)), layer_name="x")
        chunks = [m for m in socket.sent if m["type"] == "add_layer_chunk"]
        ordered = sorted(chunks, key=lambda c: c["seq"])
        png = base64.b64decode("".join(c["data_b64"] for c in ordered))
        image = Image.open(io.BytesIO(png))
        assert max(image.size) <= 12

    def test_blank_layer_name_defaults(self, context, addlayer_rig):
        node, socket = addlayer_rig
        node.execute(image=make_image_tensor((1, 2, 3)), layer_name="   ")
        chunks = [m for m in socket.sent if m["type"] == "add_layer_chunk"]
        assert chunks and chunks[0]["layer_name"] == "ComfyUI refined"

    def test_no_plugin_is_logged_noop(self, context, no_plugin_app, caplog):
        node = live_module.PhotoshopAddLayer()
        with caplog.at_level(logging.WARNING, logger="cpsb"):
            result = node.execute(image=make_image_tensor((1, 2, 3)), layer_name="x")
        assert result == {}
        assert any("not delivered" in r.message for r in caplog.records)


class TestLivePreview:
    def test_sends_result_frame_jpeg(self, context, preview_rig):
        node, socket, connection = preview_rig
        push_frame(context, connection, (1, 1, 1), title="sketch.psd")

        result = node.execute(image=make_image_tensor((0, 200, 0)))

        assert result == {}
        frames = [m for m in socket.sent if m.get("type") == "result_frame"]
        assert len(frames) == 1
        assert frames[0]["doc_title"] == "sketch.psd"
        decoded = Image.open(io.BytesIO(base64.b64decode(frames[0]["data_b64"])))
        decoded.load()
        assert decoded.size == (24, 16)
        red, green, blue = decoded.getpixel((0, 0))
        assert green > 150 and red < 60 and blue < 60  # JPEG-lossy green

    def test_no_plugin_is_a_logged_noop_not_a_failure(
        self, context, manager, loop_thread, caplog
    ):
        """The preview surface going missing must never kill a finished
        render (class docstring) -- unlike the CANVAS node's hard gate."""
        import logging as logging_module

        app = web.Application()
        routes_module.install(app, context, manager)
        nodes_module.configure(context, manager, app, loop_thread)
        try:
            node = live_module.PhotoshopLivePreview()
            with caplog.at_level(logging_module.WARNING, logger="cpsb"):
                result = node.execute(image=make_image_tensor((9, 9, 9)))
            assert result == {}
            assert any("not delivered" in record.message for record in caplog.records)
        finally:
            nodes_module._state = None

    def test_output_node_contract(self):
        assert live_module.PhotoshopLivePreview.OUTPUT_NODE is True
        assert live_module.PhotoshopLivePreview.RETURN_TYPES == ()
        spec = live_module.PhotoshopLivePreview.INPUT_TYPES()
        assert spec["required"]["image"][0] == "IMAGE"
