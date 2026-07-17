"""PhotoshopBridge node: torch-free import, contract shape, IS_CHANGED, execute paths.

``execute()``'s pixel plumbing needs torch only in ``_pil_to_tensor``, so the
execute tests below patch that one function with a sentinel builder and drive
everything else for real: a live event loop on a background thread (standing
in for ComfyUI's server loop), the real routes tier-selection coroutine, and
a recorded fake Photoshop launch.
"""

from __future__ import annotations

import asyncio
import sys
import threading
from typing import cast

import numpy as np
import pytest
from aiohttp import web
from PIL import Image

import cpsb.nodes as nodes_module
import cpsb.routes as routes_module
from cpsb.context import CpsbContext
from cpsb.handoff import HandoffManager, SourceRef, compute_source_hash
from cpsb.launcher import LaunchResult


@pytest.fixture
def manager(context: CpsbContext) -> HandoffManager:
    return HandoffManager(context)


@pytest.fixture
def configured(context: CpsbContext, manager: HandoffManager):
    """Wire nodes.configure with the fake context; app/loop are unused by IS_CHANGED."""
    nodes_module.configure(context, manager, cast("object", None), cast("object", None))
    yield
    nodes_module._state = None


class TestImportability:
    def test_module_imports_without_torch(self):
        assert "torch" not in sys.modules
        assert nodes_module.PhotoshopBridge is not None


class TestContractShape:
    def test_input_types_match_protocol(self):
        spec = nodes_module.PhotoshopBridge.INPUT_TYPES()
        assert spec["required"]["image"] == ("IMAGE",)
        assert spec["required"]["wait_for_edit"] == ("BOOLEAN", {"default": True})
        assert spec["required"]["timeout_seconds"] == (
            "INT",
            {"default": 1800, "min": 10, "max": 86400},
        )
        assert spec["hidden"] == {
            "unique_id": "UNIQUE_ID",
            "prompt": "PROMPT",
            "extra_pnginfo": "EXTRA_PNGINFO",
        }

    def test_node_attributes(self):
        node = nodes_module.PhotoshopBridge
        assert node.CATEGORY == "image/photoshop"
        assert node.RETURN_TYPES == ("IMAGE",)
        assert node.FUNCTION == "execute"


class TestIsChanged:
    def test_constant_without_handoff(self, configured):
        value = nodes_module.PhotoshopBridge.IS_CHANGED(
            image=None, wait_for_edit=True, timeout_seconds=1800, unique_id="42"
        )
        assert value == "no-handoff"

    def test_changes_when_edit_arrives(self, configured, manager):
        meta = manager.create(
            origin_node_id="42",
            origin_kind="bridge_node",
            workflow_name="",
            source=SourceRef(filename="bridge_42.png", subfolder="", type="temp"),
            original_image=Image.new("RGB", (8, 8), (1, 2, 3)),
        )
        before = nodes_module.PhotoshopBridge.IS_CHANGED(
            image=None, wait_for_edit=True, timeout_seconds=1800, unique_id="42"
        )
        assert before == meta.handoff_id  # active handoff, no edit yet

        manager.ingest_edit(meta.handoff_id, Image.new("RGB", (8, 8), (9, 9, 9)), "plugin")
        after = nodes_module.PhotoshopBridge.IS_CHANGED(
            image=None, wait_for_edit=True, timeout_seconds=1800, unique_id="42"
        )
        assert after != before
        assert len(after) == 64  # SHA256 hex of the edit file

    def test_unconfigured_module_raises(self):
        assert nodes_module._state is None
        with pytest.raises(RuntimeError, match="configure"):
            nodes_module.PhotoshopBridge.IS_CHANGED(
                image=None, wait_for_edit=True, timeout_seconds=1800, unique_id="42"
            )


class TestTensorConversion:
    def test_tensor_to_pil_accepts_plain_arrays(self):
        # ComfyUI IMAGE layout: (batch, height, width, channels) float32 0..1.
        tensor = np.zeros((1, 16, 24, 3), dtype=np.float32)
        tensor[0, :, :, 0] = 1.0  # pure red
        image = nodes_module._tensor_to_pil(tensor)
        assert image.mode == "RGB"
        assert image.size == (24, 16)
        assert image.getpixel((0, 0)) == (255, 0, 0)


def make_tensor(color: tuple[int, int, int]) -> np.ndarray:
    """A 1x16x24 ComfyUI-layout float tensor of a solid color."""
    img = Image.new("RGB", (24, 16), color)
    return np.asarray(img, dtype=np.float32)[None, ...] / 255.0


# Pure channel values (0/255) survive the float round trip exactly, keeping
# compute_source_hash comparisons deterministic across conversions.
RED = (255, 0, 0)
GREEN = (0, 255, 0)


@pytest.fixture
def loop_thread():
    """A live event loop on a background thread, like PromptServer's own."""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=5)


@pytest.fixture
def launches(monkeypatch):
    calls: list[str] = []

    def fake_launch(psd_path, override=""):
        calls.append(str(psd_path))
        return LaunchResult(ok=True)

    monkeypatch.setattr(routes_module, "launch_photoshop", fake_launch)
    return calls


@pytest.fixture
def bridge(context: CpsbContext, manager: HandoffManager, loop_thread, launches):
    """A fully wired PhotoshopBridge instance (real loop, real routes glue)."""
    app = web.Application()
    routes_module.install(app, context, manager)
    nodes_module.configure(context, manager, app, loop_thread)
    yield nodes_module.PhotoshopBridge()
    nodes_module._state = None


class TestExecute:
    def test_first_run_creates_handoff_and_passes_through(self, bridge, manager, launches):
        tensor = make_tensor(RED)
        result = bridge.execute(
            image=tensor, wait_for_edit=False, timeout_seconds=60, unique_id="42"
        )
        assert result[0] is tensor  # wait_for_edit=False, no prior edit: pass-through

        active = manager.find_active_for_node("42")
        assert active is not None
        assert active.status == "editing"
        assert active.source_hash == compute_source_hash(Image.new("RGB", (24, 16), RED))
        assert len(launches) == 1

    def test_arrived_edit_served_without_reopening(self, bridge, manager, launches, monkeypatch):
        tensor = make_tensor(RED)
        bridge.execute(image=tensor, wait_for_edit=False, timeout_seconds=60, unique_id="42")
        active = manager.find_active_for_node("42")
        manager.ingest_edit(active.handoff_id, Image.new("RGB", (24, 16), (0, 0, 255)), "plugin")

        # Stand in for the torch-dependent conversion with a sentinel builder.
        monkeypatch.setattr(
            nodes_module, "_pil_to_tensor", lambda img: ("tensor-sentinel", img.size)
        )
        result = bridge.execute(
            image=tensor, wait_for_edit=False, timeout_seconds=60, unique_id="42"
        )
        assert result[0] == ("tensor-sentinel", (24, 16))
        assert len(launches) == 1  # served from the existing handoff, no reopen

    def test_changed_input_supersedes_and_never_serves_stale_edit(
        self, bridge, manager, launches
    ):
        """B1: an edit made for OLD pixels must not be served for a new input."""
        red_tensor = make_tensor(RED)
        bridge.execute(image=red_tensor, wait_for_edit=False, timeout_seconds=60, unique_id="42")
        old = manager.find_active_for_node("42")
        manager.ingest_edit(old.handoff_id, Image.new("RGB", (24, 16), (0, 0, 255)), "plugin")

        # Upstream re-generated: same node, different pixels.
        green_tensor = make_tensor(GREEN)
        result = bridge.execute(
            image=green_tensor, wait_for_edit=False, timeout_seconds=60, unique_id="42"
        )
        assert result[0] is green_tensor  # pass-through, NOT the stale blue edit

        assert manager.get(old.handoff_id).status == "superseded"
        fresh = manager.find_active_for_node("42")
        assert fresh.handoff_id != old.handoff_id
        assert fresh.edits == []
        assert fresh.source_hash == compute_source_hash(Image.new("RGB", (24, 16), GREEN))
        assert len(launches) == 2  # reopened for the fresh handoff

    def test_legacy_handoff_without_source_hash_is_reused(self, bridge, manager, launches):
        """A pre-source_hash handoff (None) is treated as matching -- documented choice."""
        meta = manager.create(
            origin_node_id="42",
            origin_kind="bridge_node",
            workflow_name="",
            source=SourceRef(filename="bridge_42.png", subfolder="", type="temp"),
            original_image=Image.new("RGB", (24, 16), (1, 2, 3)),
        )
        # Simulate a legacy meta.json: no source_hash recorded.
        with manager._lock:
            manager._handoffs[meta.handoff_id].source_hash = None

        bridge.execute(
            image=make_tensor(GREEN), wait_for_edit=False, timeout_seconds=60, unique_id="42"
        )
        refreshed = manager.get(meta.handoff_id)
        assert refreshed.status == "editing"  # reused (reopened), not superseded
