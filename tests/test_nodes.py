"""PhotoshopBridge node: torch-free import, contract shape, IS_CHANGED, execute paths.

``execute()``'s pixel plumbing needs torch only in ``_pil_to_tensor``, so the
execute tests below patch that one function with a sentinel builder and drive
everything else for real: a live event loop on a background thread (standing
in for ComfyUI's server loop), the real routes tier-selection coroutine, and
a recorded fake Photoshop launch.
"""

from __future__ import annotations

import asyncio
import importlib.util
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import cast

import numpy as np
import pytest
from aiohttp import web
from PIL import Image

import cpsb.load_psd as load_psd_module
import cpsb.nodes as nodes_module
import cpsb.routes as routes_module
from cpsb.context import CpsbContext
from cpsb.handoff import HandoffManager, SourceRef, compute_source_hash
from cpsb.launcher import LaunchResult

BridgeMode = nodes_module.BridgeMode


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
        """Importing ``cpsb.nodes`` alone must not pull in torch.

        Checked in an isolated subprocess rather than this process's own
        ``sys.modules``: a plain in-process check would be a false failure
        whenever some OTHER, earlier-collected test file has already
        imported torch for its own reasons by the time this test runs
        (pytest collects files alphabetically, and this repo's tests span
        several files -- e.g. ``tests/test_load_psd.py`` -- some of which
        legitimately need torch for pixel-level tensor assertions). The
        subprocess makes this assertion about ``cpsb.nodes``'s own import
        graph, independent of whatever else the test session has done.
        """
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import cpsb.nodes as m, sys\n"
                "assert m.PhotoshopBridge is not None\n"
                "print('torch' in sys.modules)",
            ],
            cwd=str(Path(__file__).resolve().parent.parent),
            capture_output=True,
            text=True,
            check=True,
        )
        assert result.stdout.strip() == "False", result.stderr


class TestContractShape:
    def test_input_types_match_protocol(self):
        """PROTOCOL.md §6: the frontend string-matches on these exact mode
        values -- asserted here as literals (not via BridgeMode) so a typo
        in the constant's own definition would still fail this test.
        """
        spec = nodes_module.PhotoshopBridge.INPUT_TYPES()
        assert spec["required"]["image"] == ("IMAGE",)
        assert spec["required"]["mode"] == (
            ["Wait for first save", "Re-run on every save", "Open only (don't wait)"],
            {"default": "Wait for first save"},
        )
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
        assert node.RETURN_TYPES == ("IMAGE", "MASK")
        assert node.FUNCTION == "execute"


class TestDisplayNameMapping:
    """PROTOCOL.md §6: renamed display name, stable class id."""

    @staticmethod
    def _load_top_level_init():
        """Load the repo's top-level __init__.py as a standalone module.

        It is not part of an importable package (the repo directory name
        has a hyphen), so an ordinary dotted import can't reach it. This
        mirrors how ComfyUI's own custom-node loader loads a node pack's
        __init__.py: from a file path via importlib, not a dotted import
        (which is exactly why that file's own docstring documents a flat-
        import fallback).
        """
        repo_root = Path(__file__).resolve().parent.parent
        spec = importlib.util.spec_from_file_location(
            "cpsb_pack_entry_under_test", repo_root / "__init__.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_display_name_renamed_class_id_stable(self):
        entry = self._load_top_level_init()
        assert entry.NODE_DISPLAY_NAME_MAPPINGS == {
            "PhotoshopBridge": "Edit in Photoshop",
            "PhotoshopLoadPSD": "Load PSD",
            "PhotoshopComposePSD": "Compose Layers to PSD",
            "PhotoshopAnnotate": "Annotate for Edit",
            "PhotoshopAction": "Run Photoshop Action",
            "PhotoshopLiveCanvas": "Photoshop Live Canvas",
            "PhotoshopLivePrompt": "Photoshop Live Prompt",
            "PhotoshopLiveCreativity": "Photoshop Live Creativity",
            "PhotoshopLivePreview": "Photoshop Live Preview",
        }
        # The class ids themselves -- what saved workflows reference -- must be untouched.
        assert set(entry.NODE_CLASS_MAPPINGS) == {
            "PhotoshopBridge",
            "PhotoshopLoadPSD",
            "PhotoshopComposePSD",
            "PhotoshopAnnotate",
            "PhotoshopAction",
            "PhotoshopLiveCanvas",
            "PhotoshopLivePrompt",
            "PhotoshopLiveCreativity",
            "PhotoshopLivePreview",
        }
        assert entry.NODE_CLASS_MAPPINGS["PhotoshopBridge"] is nodes_module.PhotoshopBridge
        assert entry.NODE_CLASS_MAPPINGS["PhotoshopLoadPSD"] is load_psd_module.PhotoshopLoadPSD


class TestIsChanged:
    def test_constant_without_handoff(self, configured):
        value = nodes_module.PhotoshopBridge.IS_CHANGED(
            image=None, mode=BridgeMode.WAIT_FIRST_SAVE, timeout_seconds=1800, unique_id="42"
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
            image=None, mode=BridgeMode.WAIT_FIRST_SAVE, timeout_seconds=1800, unique_id="42"
        )
        assert before == meta.handoff_id  # active handoff, no edit yet

        manager.ingest_edit(meta.handoff_id, Image.new("RGB", (8, 8), (9, 9, 9)), "plugin")
        after = nodes_module.PhotoshopBridge.IS_CHANGED(
            image=None, mode=BridgeMode.WAIT_FIRST_SAVE, timeout_seconds=1800, unique_id="42"
        )
        assert after != before
        assert len(after) == 64  # SHA256 hex of the edit file

    def test_unconfigured_module_raises(self):
        assert nodes_module._state is None
        with pytest.raises(RuntimeError, match="configure"):
            nodes_module.PhotoshopBridge.IS_CHANGED(
                image=None, mode=BridgeMode.WAIT_FIRST_SAVE, timeout_seconds=1800, unique_id="42"
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
    """Records every ``launch_photoshop`` call as ``(psd_path, calling_thread_ident)``.

    Patched on ``cpsb.routes`` (not a name imported directly into
    ``cpsb.nodes``) because ``cpsb.nodes`` always calls it through the
    ``routes`` module object (``routes.launch_photoshop(...)``) precisely so
    this patch intercepts it -- see ``PhotoshopBridge._launch_tier1_direct``'s
    docstring. Existing assertions only ever check ``len(launches)``, so
    recording the calling thread's identity alongside the path is additive.
    """
    calls: list[tuple[str, int]] = []

    def fake_launch(psd_path, override=""):
        calls.append((str(psd_path), threading.get_ident()))
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
            image=tensor, mode=BridgeMode.OPEN_ONLY, timeout_seconds=60, unique_id="42"
        )
        assert result[0] is tensor  # non-blocking mode, no prior edit: pass-through

        active = manager.find_active_for_node("42")
        assert active is not None
        assert active.status == "editing"
        assert active.source_hash == compute_source_hash(Image.new("RGB", (24, 16), RED))
        assert len(launches) == 1

    def test_tier1_direct_launch_runs_on_calling_thread_no_loop_hop(
        self, bridge, manager, launches
    ):
        """Restructure guarantee: with no Tier 2 plugin connected, the Tier 1
        launch happens in-line on execute()'s own thread -- it never hops
        through state.loop at all (module docstring: deadlock-proofing).
        """
        calling_thread = threading.get_ident()
        tensor = make_tensor(RED)
        bridge.execute(
            image=tensor, mode=BridgeMode.OPEN_ONLY, timeout_seconds=60, unique_id="42"
        )
        assert len(launches) == 1
        _, launch_thread = launches[0]
        assert launch_thread == calling_thread

    def test_arrived_edit_served_without_reopening(self, bridge, manager, launches, monkeypatch):
        tensor = make_tensor(RED)
        bridge.execute(
            image=tensor, mode=BridgeMode.OPEN_ONLY, timeout_seconds=60, unique_id="42"
        )
        active = manager.find_active_for_node("42")
        manager.ingest_edit(active.handoff_id, Image.new("RGB", (24, 16), (0, 0, 255)), "plugin")

        # Stand in for the torch-dependent conversion with a sentinel builder.
        monkeypatch.setattr(
            nodes_module, "_pil_to_tensor", lambda img: ("tensor-sentinel", img.size)
        )
        result = bridge.execute(
            image=tensor, mode=BridgeMode.OPEN_ONLY, timeout_seconds=60, unique_id="42"
        )
        assert result[0] == ("tensor-sentinel", (24, 16))
        assert len(launches) == 1  # served from the existing handoff, no reopen

    def test_changed_input_supersedes_and_never_serves_stale_edit(
        self, bridge, manager, launches
    ):
        """B1: an edit made for OLD pixels must not be served for a new input."""
        red_tensor = make_tensor(RED)
        bridge.execute(
            image=red_tensor, mode=BridgeMode.OPEN_ONLY, timeout_seconds=60, unique_id="42"
        )
        old = manager.find_active_for_node("42")
        manager.ingest_edit(old.handoff_id, Image.new("RGB", (24, 16), (0, 0, 255)), "plugin")

        # Upstream re-generated: same node, different pixels.
        green_tensor = make_tensor(GREEN)
        result = bridge.execute(
            image=green_tensor, mode=BridgeMode.OPEN_ONLY, timeout_seconds=60, unique_id="42"
        )
        assert result[0] is green_tensor  # pass-through, NOT the stale blue edit

        assert manager.get(old.handoff_id).status == "superseded"
        fresh = manager.find_active_for_node("42")
        assert fresh.handoff_id != old.handoff_id
        assert fresh.edits == []
        assert fresh.source_hash == compute_source_hash(Image.new("RGB", (24, 16), GREEN))
        assert len(launches) == 2  # reopened for the fresh handoff

    def test_legacy_handoff_without_source_hash_is_reused(self, bridge, manager, launches):
        """A pre-source_hash handoff (None) is treated as matching -- documented choice.

        Uses a non-blocking mode, so this also proves the new reuse-gating:
        a reused (not newly-created) handoff must not be reopened.
        """
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
            image=make_tensor(GREEN),
            mode=BridgeMode.OPEN_ONLY,
            timeout_seconds=60,
            unique_id="42",
        )
        refreshed = manager.get(meta.handoff_id)
        assert refreshed.handoff_id == meta.handoff_id  # same handoff: reused, not replaced
        assert refreshed.status != "superseded"  # not treated as a changed-input mismatch
        assert len(launches) == 0  # non-blocking mode + reused (not new) handoff: no (re)open


class TestRerunEverySaveMode:
    """PROTOCOL.md §6 "Re-run on every save": never blocks, opens once."""

    def test_first_run_opens_and_passes_through(self, bridge, manager, launches):
        tensor = make_tensor(RED)
        result = bridge.execute(
            image=tensor, mode=BridgeMode.RERUN_EVERY_SAVE, timeout_seconds=60, unique_id="55"
        )
        assert result[0] is tensor  # passthrough, no block
        active = manager.find_active_for_node("55")
        assert active is not None
        assert active.status == "editing"  # opened
        assert len(launches) == 1

    def test_second_run_after_save_consumes_edit_without_relaunching(
        self, bridge, manager, launches, monkeypatch
    ):
        tensor = make_tensor(RED)
        bridge.execute(
            image=tensor, mode=BridgeMode.RERUN_EVERY_SAVE, timeout_seconds=60, unique_id="55"
        )
        active = manager.find_active_for_node("55")
        manager.ingest_edit(active.handoff_id, Image.new("RGB", (24, 16), (0, 0, 255)), "plugin")

        monkeypatch.setattr(
            nodes_module, "_pil_to_tensor", lambda img: ("tensor-sentinel", img.size)
        )
        result = bridge.execute(
            image=tensor, mode=BridgeMode.RERUN_EVERY_SAVE, timeout_seconds=60, unique_id="55"
        )
        assert result[0] == ("tensor-sentinel", (24, 16))
        assert len(launches) == 1  # still just the first-run launch -- no relaunch

    def test_reexecution_before_any_save_does_not_relaunch(self, bridge, manager, launches):
        """The new gating this restructure adds: a re-run-mode passthrough
        execution against a handoff that is already open -- whether or not
        it has been saved yet -- must never reopen Photoshop. Only a
        genuinely new handoff does (PROTOCOL.md §6).
        """
        tensor = make_tensor(RED)
        bridge.execute(
            image=tensor, mode=BridgeMode.RERUN_EVERY_SAVE, timeout_seconds=60, unique_id="55"
        )
        assert len(launches) == 1

        # Re-executed again with the SAME input and no edit having arrived
        # yet (e.g. a stray manual re-queue) -- must reuse, not relaunch.
        result = bridge.execute(
            image=tensor, mode=BridgeMode.RERUN_EVERY_SAVE, timeout_seconds=60, unique_id="55"
        )
        assert result[0] is tensor
        assert len(launches) == 1  # still just one launch


class TestWaitForFirstSaveMode:
    """PROTOCOL.md §6 "Wait for first save": today's wait_for_edit=True, renamed."""

    def test_blocks_until_edit_then_delivers_in_run(self, bridge, manager, monkeypatch):
        tensor = make_tensor(RED)

        def _save_shortly_after_open(psd_path, override=""):
            # Ingest from a delayed background thread, not synchronously
            # in-line here: launch_photoshop returning ok must be visible
            # (mark_editing) BEFORE the edit lands (mark_editing otherwise
            # clobbers the "edited" status back to "editing"), exactly as
            # real Photoshop usage always sequences -- a human editing and
            # saving takes far longer than this launch call itself.
            def _do_ingest():
                active = manager.find_active_for_node("77")
                manager.ingest_edit(
                    active.handoff_id, Image.new("RGB", (24, 16), (0, 0, 255)), "plugin"
                )

            threading.Timer(0.3, _do_ingest).start()
            return LaunchResult(ok=True)

        monkeypatch.setattr(routes_module, "launch_photoshop", _save_shortly_after_open)
        monkeypatch.setattr(
            nodes_module, "_pil_to_tensor", lambda img: ("tensor-sentinel", img.size)
        )

        result = bridge.execute(
            image=tensor, mode=BridgeMode.WAIT_FIRST_SAVE, timeout_seconds=10, unique_id="77"
        )
        assert result[0] == ("tensor-sentinel", (24, 16))
        assert manager.find_active_for_node("77").status == "edited"

    def test_timeout_interrupts_and_stays_editing(self, bridge, manager):
        """PROTOCOL.md §6: on timeout the handoff stays `editing`, not
        error/cancelled, so a later save or re-queue resumes the same PSD.
        In the test environment (no real ComfyUI), the interrupt surfaces
        as _raise_interrupt's own RuntimeError fallback.
        """
        tensor = make_tensor(RED)
        with pytest.raises(RuntimeError, match=r"comfy\.model_management"):
            bridge.execute(
                image=tensor, mode=BridgeMode.WAIT_FIRST_SAVE, timeout_seconds=1, unique_id="88"
            )
        active = manager.find_active_for_node("88")
        assert active is not None
        assert active.status == "editing"

    def test_cancel_interrupts_promptly(self, bridge, manager, monkeypatch):
        """`/cpsb/cancel` (mark_cancelled) unblocks a waiting bridge node
        immediately, without waiting out the full timeout (PROTOCOL.md §2).
        """
        tensor = make_tensor(RED)

        def _cancel_shortly_after_open(psd_path, override=""):
            active = manager.find_active_for_node("99")
            threading.Timer(0.3, lambda: manager.mark_cancelled(active.handoff_id)).start()
            return LaunchResult(ok=True)

        monkeypatch.setattr(routes_module, "launch_photoshop", _cancel_shortly_after_open)

        start = time.monotonic()
        with pytest.raises(RuntimeError, match=r"comfy\.model_management"):
            bridge.execute(
                image=tensor, mode=BridgeMode.WAIT_FIRST_SAVE, timeout_seconds=30, unique_id="99"
            )
        elapsed = time.monotonic() - start
        assert elapsed < 5  # unblocked by cancellation, not the 30s timeout

        assert manager.find_active_for_node("99") is None  # cancelled: no longer "active"


class TestTier2BoundedSend:
    """The restructure's core guarantee: a wedged Tier 2 send fails within
    _TIER2_SEND_TIMEOUT_SECONDS instead of hanging prompt_worker (and
    therefore ComfyUI's whole prompt queue) forever.
    """

    def test_scheduling_runtime_error_marks_handoff_error_without_hanging(
        self, context, manager, monkeypatch
    ):
        """A RuntimeError raised by run_coroutine_threadsafe itself (e.g. the
        loop already stopped) must be caught the same as a result() timeout
        -- never propagate raw, never hang. Fast unit test: no real loop.
        """
        meta = manager.create(
            origin_node_id="7",
            origin_kind="bridge_node",
            workflow_name="",
            source=SourceRef(filename="bridge_7.png", subfolder="", type="temp"),
            original_image=Image.new("RGB", (8, 8), (1, 2, 3)),
        )
        app = web.Application()
        routes_module.install(app, context, manager)
        connection = routes_module.PluginConnection(ws=object(), ready=True)
        app[routes_module._APP_KEY_PLUGIN].connection = connection

        state = nodes_module._NodeState(
            context=context, manager=manager, app=app, loop=cast("object", object())
        )

        def _raise_runtime_error(coro, loop):
            coro.close()  # Avoid an "coroutine was never awaited" warning.
            raise RuntimeError("Event loop is closed")

        monkeypatch.setattr(
            nodes_module.asyncio, "run_coroutine_threadsafe", _raise_runtime_error
        )

        psd_path = manager.psd_path(meta)
        attempt = nodes_module.PhotoshopBridge._send_tier2_open(state, meta, psd_path)

        assert attempt.ok is False
        assert attempt.tier == 2
        assert manager.get(meta.handoff_id).status == "error"

    def test_wedged_websocket_send_fails_bounded_not_infinite(
        self, context, manager, loop_thread, monkeypatch
    ):
        """Integration-level: a real event loop in a background thread, a
        Tier 2 coroutine that never resolves -- execute() must fail within
        ~the (shortened, for test speed) bound, not hang forever.
        """
        monkeypatch.setattr(nodes_module, "_TIER2_SEND_TIMEOUT_SECONDS", 1.5)

        app = web.Application()
        routes_module.install(app, context, manager)

        class _NeverSendsSocket:
            async def send_json(self, payload):
                await asyncio.Event().wait()  # Never set: simulates a wedged send.

        connection = routes_module.PluginConnection(ws=_NeverSendsSocket(), ready=True)
        app[routes_module._APP_KEY_PLUGIN].connection = connection

        nodes_module.configure(context, manager, app, loop_thread)
        bridge = nodes_module.PhotoshopBridge()
        try:
            tensor = make_tensor(RED)
            start = time.monotonic()
            with pytest.raises(RuntimeError, match=r"comfy\.model_management"):
                bridge.execute(
                    image=tensor,
                    mode=BridgeMode.WAIT_FIRST_SAVE,
                    timeout_seconds=60,
                    unique_id="99",
                )
            elapsed = time.monotonic() - start
        finally:
            nodes_module._state = None

        assert elapsed < 1.5 + 3  # bounded, generous slack for CI scheduling jitter

        matching = [h for h in manager.list_all(limit=10) if h.origin_node_id == "99"]
        assert len(matching) == 1
        assert matching[0].status == "error"


class TestRunningOnStateLoopGuard:
    """Defensive belt-and-suspenders coverage for the (currently unreachable
    in stock ComfyUI, per the module docstring) case where execute() somehow
    runs on state.loop's own thread.
    """

    async def test_falls_back_to_tier1_when_already_on_state_loop(
        self, context, manager, launches
    ):
        running_loop = asyncio.get_running_loop()
        app = web.Application()
        routes_module.install(app, context, manager)
        connection = routes_module.PluginConnection(ws=object(), ready=True)
        app[routes_module._APP_KEY_PLUGIN].connection = connection

        state = nodes_module._NodeState(
            context=context, manager=manager, app=app, loop=running_loop
        )
        meta = manager.create(
            origin_node_id="7",
            origin_kind="bridge_node",
            workflow_name="",
            source=SourceRef(filename="bridge_7.png", subfolder="", type="temp"),
            original_image=Image.new("RGB", (8, 8), (1, 2, 3)),
        )
        psd_path = manager.psd_path(meta)

        attempt = nodes_module.PhotoshopBridge._open_in_photoshop(state, meta, psd_path)

        assert attempt.tier == 1  # Tier 2 skipped even though a plugin is "connected"
        assert attempt.ok is True
        assert len(launches) == 1


def make_bridge_handoff(manager: HandoffManager, node_id: str) -> str:
    """A bare bridge_node handoff (no Photoshop open, no PSD written) for
    the mask-output tests below, which only care about ingest + tensor
    output, not the open/launch machinery already covered elsewhere.
    """
    meta = manager.create(
        origin_node_id=node_id,
        origin_kind="bridge_node",
        workflow_name="",
        source=SourceRef(filename=f"bridge_{node_id}.png", subfolder="", type="temp"),
        original_image=Image.new("RGB", (24, 16), (1, 2, 3)),
    )
    return meta.handoff_id


class TestLoadEditTensors:
    """Direct coverage of ``PhotoshopBridge._load_edit_tensors`` -- the MASK
    derivation logic itself (PROTOCOL.md §6) -- without the open/launch
    machinery ``TestExecute`` et al. already cover.

    Needs real ``torch`` tensors to check actual mask *values* (the whole
    point of this coverage), so every test here skips cleanly via the
    autouse fixture below when torch isn't installed --
    this project deliberately doesn't declare it as a dependency (ComfyUI
    provides it at runtime) -- rather than failing the whole module import,
    which is exactly why ``import torch`` never happens at this file's
    module level (see ``TestImportability.test_module_imports_without_torch``
    above, which must keep observing "torch not yet imported" -- this class
    is appended at the end of the file, after that test, for exactly that
    reason).
    """

    @pytest.fixture(autouse=True)
    def _require_torch(self):
        pytest.importorskip("torch")

    def test_no_edit_yet_returns_fallback_image_and_zeros_mask(self, manager):
        import torch

        handoff_id = make_bridge_handoff(manager, "1")
        fallback = make_tensor(RED)

        image_out, mask_out = nodes_module.PhotoshopBridge._load_edit_tensors(
            manager, handoff_id, fallback
        )

        assert image_out is fallback
        assert mask_out.shape == (1, 16, 24)
        assert torch.count_nonzero(mask_out).item() == 0

    def test_edit_without_alpha_or_mask_returns_zeros(self, manager):
        import torch

        handoff_id = make_bridge_handoff(manager, "2")
        manager.ingest_edit(handoff_id, Image.new("RGB", (24, 16), (9, 9, 9)), "plugin")

        _, mask_out = nodes_module.PhotoshopBridge._load_edit_tensors(
            manager, handoff_id, make_tensor(RED)
        )

        assert mask_out.shape == (1, 16, 24)
        assert torch.count_nonzero(mask_out).item() == 0

    def test_edit_with_alpha_returns_one_minus_alpha(self, manager):
        import torch

        handoff_id = make_bridge_handoff(manager, "3")
        manager.ingest_edit(handoff_id, Image.new("RGBA", (24, 16), (9, 9, 9, 64)), "plugin")

        _, mask_out = nodes_module.PhotoshopBridge._load_edit_tensors(
            manager, handoff_id, make_tensor(RED)
        )

        expected = 1.0 - 64 / 255.0
        assert mask_out.shape == (1, 16, 24)
        assert torch.allclose(mask_out, torch.full((1, 16, 24), expected), atol=1e-4)

    def test_image_tensor_still_comes_from_the_edit_file(self, manager, monkeypatch):
        handoff_id = make_bridge_handoff(manager, "6")
        manager.ingest_edit(handoff_id, Image.new("RGB", (24, 16), (9, 9, 9)), "plugin")
        monkeypatch.setattr(
            nodes_module, "_pil_to_tensor", lambda img: ("tensor-sentinel", img.size)
        )

        image_out, _ = nodes_module.PhotoshopBridge._load_edit_tensors(
            manager, handoff_id, make_tensor(RED)
        )

        assert image_out == ("tensor-sentinel", (24, 16))


class TestMaskOutputEndToEnd:
    """``execute()``'s public ``(IMAGE, MASK)`` contract (PROTOCOL.md §6),
    proving the MASK derivation is wired all the way through the real node
    entry point -- not just ``_load_edit_tensors`` in isolation above.
    """

    @pytest.fixture(autouse=True)
    def _require_torch(self):
        pytest.importorskip("torch")

    def test_passthrough_pairs_input_image_with_zeros_mask(self, bridge, manager, launches):
        import torch

        tensor = make_tensor(RED)

        image_out, mask_out = bridge.execute(
            image=tensor, mode=BridgeMode.OPEN_ONLY, timeout_seconds=60, unique_id="10"
        )

        assert image_out is tensor  # unchanged passthrough
        assert mask_out.shape == (1, 16, 24)
        assert torch.count_nonzero(mask_out).item() == 0

    def test_consumed_edit_carries_its_alpha_mask(self, bridge, manager, launches):
        import torch

        tensor = make_tensor(RED)
        bridge.execute(
            image=tensor, mode=BridgeMode.OPEN_ONLY, timeout_seconds=60, unique_id="11"
        )
        active = manager.find_active_for_node("11")
        manager.ingest_edit(
            active.handoff_id, Image.new("RGBA", (24, 16), (0, 0, 255, 64)), "plugin"
        )

        _, mask_out = bridge.execute(
            image=tensor, mode=BridgeMode.OPEN_ONLY, timeout_seconds=60, unique_id="11"
        )

        expected = 1.0 - 64 / 255.0
        assert torch.allclose(mask_out, torch.full((1, 16, 24), expected), atol=1e-4)

    def test_return_value_is_a_two_tuple(self, bridge, manager, launches):
        tensor = make_tensor(RED)
        result = bridge.execute(
            image=tensor, mode=BridgeMode.OPEN_ONLY, timeout_seconds=60, unique_id="12"
        )
        assert len(result) == 2
