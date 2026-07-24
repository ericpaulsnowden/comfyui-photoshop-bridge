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
        assert spec["required"]["image"] == ("IMAGE",)
