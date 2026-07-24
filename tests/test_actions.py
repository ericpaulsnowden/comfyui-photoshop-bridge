"""Tests for ``cpsb.actions``: the ``PhotoshopAction`` node ("the capstone").

Mirrors ``tests/test_nodes.py``/``tests/test_annotate.py``'s conventions
throughout: a plain ``numpy``-array tensor stand-in wherever torch isn't
needed, a ``loop_thread`` fixture standing in for ComfyUI's own server loop,
a recording FAKE plugin websocket (never a real Photoshop) for the Tier-2
send/open path, and a ``threading.Timer`` delivering a delayed edit/cancel/
error from a background thread while ``execute()`` blocks on the main
thread -- exactly like real Photoshop usage (a human -- or here, the plugin
playing an Action -- always finishes long after the open/send calls return).

Nothing here runs a real Photoshop Action: that is precisely the one thing
this test suite CANNOT cover (see the implementation report) -- these tests
instead prove the handoff/blocking/consume plumbing and, especially, the
Tier-2-required gate (this node, unlike every other one in this pack, never
falls back to a Tier 1 OS-launch: PROTOCOL.md's "everything possible must
work with the ComfyUI plugin ALONE" ethos EXCEPT running a Photoshop Action
is exactly the "impossible" case that ethos itself carves out, per this
node's own module docstring).
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
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

import cpsb.actions as actions_module
import cpsb.nodes as nodes_module
import cpsb.routes as routes_module
from cpsb.context import CpsbContext
from cpsb.handoff import HandoffManager, SourceRef, compute_source_hash

RED = (255, 0, 0)
GREEN = (0, 255, 0)

#: Short bound for the blocking-wait tests below -- real tests either
#: deliver an edit/cancel/error from a background thread well before this
#: elapses, or deliberately let it expire to exercise TIMEOUT.
_SHORT_TIMEOUT = 1


def make_tensor(color: tuple[int, int, int], size: tuple[int, int] = (24, 16)) -> np.ndarray:
    """A ``1xHxWx3`` ComfyUI-layout float32 tensor of a solid color (test_nodes.py's own helper)."""
    width, height = size
    img = Image.new("RGB", (width, height), color)
    return np.asarray(img, dtype=np.float32)[None, ...] / 255.0


def tensor_from_image(image: Image.Image) -> np.ndarray:
    """A ``1xHxWx3`` ComfyUI-layout float32 tensor from an already-built PIL image.

    Needed whenever a test pre-seeds a handoff directly via
    ``manager.create()`` (bypassing ``execute()``'s own creation path) and
    then calls ``execute()`` against it: ``compute_source_hash`` must match
    exactly, or ``execute()``'s own "input changed" check (correctly)
    supersedes the pre-seeded handoff instead of consuming it.
    """
    return np.asarray(image.convert("RGB"), dtype=np.float32)[None, ...] / 255.0


def raises_interrupt():
    """The interrupt this test environment surfaces as (no real ComfyUI installed).

    ``nodes._raise_interrupt`` falls back to a plain ``RuntimeError`` when
    ``comfy.model_management`` isn't importable -- the same fallback every
    other node's test suite in this repo relies on.
    """
    return pytest.raises(RuntimeError, match=r"comfy\.model_management")


class _RecordingSocket:
    """Stands in for the plugin's real websocket: records every ``send_json`` call."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)


@pytest.fixture
def manager(context: CpsbContext) -> HandoffManager:
    return HandoffManager(context)


@pytest.fixture
def configured(context: CpsbContext, manager: HandoffManager):
    """Wire ``nodes.configure`` with a fake app/loop -- fine for IS_CHANGED,
    which never opens Photoshop or touches the loop.
    """
    nodes_module.configure(context, manager, cast("object", None), cast("object", None))
    yield
    nodes_module._state = None


@pytest.fixture
def loop_thread():
    """A live event loop on a background thread, like PromptServer's own
    (identical fixture to ``tests/test_nodes.py``'s own).
    """
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=5)


@pytest.fixture
def plugin_socket() -> _RecordingSocket:
    return _RecordingSocket()


@pytest.fixture
def tier2_action(context: CpsbContext, manager: HandoffManager, loop_thread, plugin_socket):
    """A configured ``PhotoshopAction`` with a connected, READY Tier-2 plugin
    (a recording fake socket, never a real Photoshop) -- this node REQUIRES
    Tier 2 (module docstring), so every execute()-path test that should get
    PAST the gate needs one connected.
    """
    app = web.Application()
    routes_module.install(app, context, manager)
    connection = routes_module.PluginConnection(ws=plugin_socket, ready=True)
    app[routes_module._APP_KEY_PLUGIN].connection = connection
    nodes_module.configure(context, manager, app, loop_thread)
    yield actions_module.PhotoshopAction()
    nodes_module._state = None


@pytest.fixture
def no_tier2_action(context: CpsbContext, manager: HandoffManager):
    """A configured ``PhotoshopAction`` with NO plugin connected at all --
    for the Tier-2-required gate tests. No ``loop_thread``/launch patching
    needed: a properly-gated ``execute()`` must never reach the open/launch
    machinery in this state at all.
    """
    app = web.Application()
    routes_module.install(app, context, manager)
    nodes_module.configure(context, manager, app, cast("object", None))
    yield actions_module.PhotoshopAction()
    nodes_module._state = None


def make_action_handoff(manager: HandoffManager, node_id: str, source: Image.Image) -> str:
    """A bare ``bridge_node`` handoff for *source* (no Photoshop open, no PSD
    written) -- for the "already has an edit" consume tests, which only care
    about ingest + tensor output. The caller must later call ``execute()``
    with a tensor built from this SAME *source* image (:func:`tensor_from_image`)
    -- ``manager.create()`` records ``compute_source_hash(source)``, and
    ``execute()`` supersedes any handoff whose recorded hash doesn't match
    the input it's given (by design -- see ``cpsb.actions``' own docstring).
    """
    meta = manager.create(
        origin_node_id=node_id,
        origin_kind="bridge_node",
        workflow_name="",
        source=SourceRef(filename=f"action_{node_id}.png", subfolder="", type="temp"),
        original_image=source,
    )
    return meta.handoff_id


class TestImportability:
    def test_module_imports_without_torch(self):
        """Importing ``cpsb.actions`` alone must not pull in torch -- same
        isolated-subprocess check as every other node module's own test
        (see ``tests/test_nodes.py``'s identical test for why a subprocess).
        """
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import cpsb.actions as m, sys\n"
                "assert m.PhotoshopAction is not None\n"
                "print('torch' in sys.modules)",
            ],
            cwd=str(Path(__file__).resolve().parent.parent),
            capture_output=True,
            text=True,
            check=True,
        )
        assert result.stdout.strip() == "False", result.stderr


class TestRegistration:
    @staticmethod
    def _load_top_level_init():
        """Mirrors ``tests/test_nodes.py``'s ``TestDisplayNameMapping``'s own
        helper: load the repo's top-level ``__init__.py`` from its file path
        (it isn't part of an importable package -- the repo directory name
        has a hyphen), exactly how ComfyUI's own custom-node loader does it.
        """
        repo_root = Path(__file__).resolve().parent.parent
        spec = importlib.util.spec_from_file_location(
            "cpsb_pack_entry_under_test_actions", repo_root / "__init__.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_node_registers_with_stable_class_id_and_display_name(self):
        entry = self._load_top_level_init()
        assert entry.NODE_CLASS_MAPPINGS["PhotoshopAction"] is actions_module.PhotoshopAction
        assert entry.NODE_DISPLAY_NAME_MAPPINGS["PhotoshopAction"] == "Run Photoshop Action"
        # All seven nodes present -- the pack still imports cleanly with this
        # one added, not just this node in isolation.
        assert set(entry.NODE_CLASS_MAPPINGS) == {
            "PhotoshopBridge",
            "PhotoshopLoadPSD",
            "PhotoshopComposePSD",
            "PhotoshopAnnotate",
            "PhotoshopAction",
            "PhotoshopLiveCanvas",
            "PhotoshopLivePreview",
        }


class TestContractShape:
    def test_input_types(self):
        spec = actions_module.PhotoshopAction.INPUT_TYPES()
        assert spec["required"]["image"] == ("IMAGE",)
        assert spec["required"]["action_name"] == ("STRING", {"default": ""})
        assert spec["required"]["action_set"] == ("STRING", {"default": ""})
        assert spec["required"]["timeout_seconds"] == (
            "INT",
            {"default": 1800, "min": 10, "max": 86400},
        )
        assert spec["hidden"] == {"unique_id": "UNIQUE_ID"}

    def test_node_attributes(self):
        node = actions_module.PhotoshopAction
        assert node.CATEGORY == "image/photoshop"
        assert node.RETURN_TYPES == ("IMAGE", "MASK")
        assert node.FUNCTION == "execute"


class TestIsChanged:
    def test_constant_without_handoff(self, configured):
        value = actions_module.PhotoshopAction.IS_CHANGED(
            image=None, action_name="A", action_set="S", timeout_seconds=1800, unique_id="42"
        )
        assert value == "no-handoff"

    def test_changes_when_edit_arrives(self, configured, manager):
        handoff_id = make_action_handoff(manager, "42", Image.new("RGB", (24, 16), (1, 2, 3)))
        before = actions_module.PhotoshopAction.IS_CHANGED(
            image=None, action_name="A", action_set="S", timeout_seconds=1800, unique_id="42"
        )
        assert before == handoff_id  # active handoff, no edit yet

        manager.ingest_edit(handoff_id, Image.new("RGB", (24, 16), (9, 9, 9)), "plugin")
        after = actions_module.PhotoshopAction.IS_CHANGED(
            image=None, action_name="A", action_set="S", timeout_seconds=1800, unique_id="42"
        )
        assert after != before
        assert len(after) == 64  # SHA256 hex of the edit file

    def test_unconfigured_module_raises(self):
        assert nodes_module._state is None
        with pytest.raises(RuntimeError, match="configure"):
            actions_module.PhotoshopAction.IS_CHANGED(
                image=None, action_name="A", action_set="S", timeout_seconds=1800, unique_id="42"
            )


class TestEmptyActionName:
    """A blank ``action_name`` interrupts immediately -- before even checking
    Tier 2 -- rather than sending a request Photoshop could never satisfy.
    """

    def test_blank_action_name_interrupts_without_touching_tier2(
        self, no_tier2_action, manager, caplog
    ):
        caplog.set_level(logging.WARNING, logger="cpsb")
        tensor = make_tensor(RED)
        with raises_interrupt():
            no_tier2_action.execute(
                image=tensor, action_name="   ", action_set="Set", timeout_seconds=60, unique_id="1"
            )
        messages = [r.message for r in caplog.records if r.name == "cpsb"]
        assert any("action_name is empty" in message for message in messages)
        assert manager.find_active_for_node("1") is None  # no orphaned handoff


class TestTier2Required:
    """The core new gate this node adds (PROTOCOL.md ethos exception, module
    docstring): unlike every other node in this pack, this one NEVER falls
    back to a Tier 1 OS-launch -- there is no Tier 1 equivalent of "run a
    saved Photoshop Action."
    """

    def test_no_plugin_connected_interrupts_with_clear_message_and_no_orphan_handoff(
        self, no_tier2_action, manager, caplog
    ):
        caplog.set_level(logging.WARNING, logger="cpsb")
        tensor = make_tensor(RED)
        with raises_interrupt():
            no_tier2_action.execute(
                image=tensor,
                action_name="My Action",
                action_set="My Set",
                timeout_seconds=60,
                unique_id="2",
            )
        messages = [r.message for r in caplog.records if r.name == "cpsb"]
        # The exact, actionable wording this node's docstring promises:
        # "clear, actionable message ... pointing the user to install/
        # connect the Tier-2 plugin" (not just a bare "Tier 2 required").
        assert any(
            "no Tier-2 plugin connected" in message and "Install/connect the plugin" in message
            for message in messages
        )
        # Nothing was created -- the gate fires before any handoff/PSD write.
        assert manager.find_active_for_node("2") is None

    def test_existing_edit_is_served_even_with_no_plugin_connected(self, no_tier2_action, manager):
        """The Tier-2 gate only applies when this node would need to (re)open
        Photoshop -- an already-arrived edit is served regardless (mirrors
        the bridge node's identical "arrived edit served without reopening").
        """
        source_image = Image.new("RGB", (24, 16), (1, 2, 3))
        handoff_id = make_action_handoff(manager, "3", source_image)
        manager.ingest_edit(handoff_id, Image.new("RGB", (24, 16), (0, 0, 255)), "plugin")

        tensor = tensor_from_image(source_image)
        image_out, _mask_out = no_tier2_action.execute(
            image=tensor, action_name="A", action_set="S", timeout_seconds=60, unique_id="3"
        )
        assert image_out is not tensor  # derived from the saved edit file, not passthrough


class TestRunActionMessage:
    def test_run_action_sent_with_exact_fields(self, tier2_action, manager, plugin_socket):
        """Mirrors the bridge node's ``open_handoff`` send: ``run_action`` is
        the SECOND message this node sends, right after ``open_handoff``.
        """

        def _deliver_shortly_after_open():
            active = manager.find_active_for_node("77")
            manager.ingest_edit(
                active.handoff_id, Image.new("RGB", (24, 16), (0, 0, 255)), "plugin"
            )

        threading.Timer(0.3, _deliver_shortly_after_open).start()

        tensor = make_tensor(RED)
        tier2_action.execute(
            image=tensor,
            action_name="Resize And Sharpen",
            action_set="ComfyUI Actions",
            timeout_seconds=10,
            unique_id="77",
        )

        types_sent = [msg["type"] for msg in plugin_socket.sent]
        assert types_sent == ["open_handoff", "run_action"]
        run_action_msg = plugin_socket.sent[1]
        active = manager.find_active_for_node("77")
        assert run_action_msg == {
            "type": "run_action",
            "handoff_id": active.handoff_id,
            "action_name": "Resize And Sharpen",
            "action_set": "ComfyUI Actions",
        }


class TestBlockingWait:
    """Mirrors ``tests/test_nodes.py``'s ``TestWaitForFirstSaveMode`` /
    ``tests/test_annotate.py``'s ``TestWaitForFirstSaveBlocking`` shape: a
    ``threading.Timer`` delivers a delayed edit/cancel/error from a
    background thread while ``execute()`` blocks on the main thread.
    """

    def test_blocks_until_edit_then_delivers_in_run(self, tier2_action, manager, monkeypatch):
        tensor = make_tensor(RED)

        def _deliver_shortly_after_open():
            # Simulates runAction.js: play the Action, export, upload --
            # collapsed here to the one server-side effect that matters for
            # this node, `manager.ingest_edit`, since a real Action can't
            # run in pytest (see this module's own docstring).
            active = manager.find_active_for_node("77")
            manager.ingest_edit(
                active.handoff_id, Image.new("RGB", (24, 16), (0, 0, 255)), "plugin"
            )

        threading.Timer(0.3, _deliver_shortly_after_open).start()
        monkeypatch.setattr(
            nodes_module, "_pil_to_tensor", lambda img: ("tensor-sentinel", img.size)
        )

        result = tier2_action.execute(
            image=tensor, action_name="A", action_set="S", timeout_seconds=10, unique_id="77"
        )
        assert result[0] == ("tensor-sentinel", (24, 16))
        assert manager.find_active_for_node("77").status == "edited"

    def test_timeout_interrupts_and_handoff_stays_active(self, tier2_action, manager):
        """On timeout the handoff stays ACTIVE (not error/cancelled), so a
        later re-queue resumes the same PSD (PROTOCOL.md precedent: the
        bridge node's own identical timeout behavior). Status here is
        `pending`, not `editing`: Tier 2's `open_in_photoshop` only sends
        `open_handoff` and returns -- the transition to `editing` happens
        when the plugin replies `opened` (`cpsb.routes._handle_plugin_message`),
        which this test's bare recording socket never simulates (that reply
        is covered by ``tests/test_routes.py``'s own websocket tests).
        """
        tensor = make_tensor(RED)
        with raises_interrupt():
            tier2_action.execute(
                image=tensor,
                action_name="A",
                action_set="S",
                timeout_seconds=_SHORT_TIMEOUT,
                unique_id="88",
            )
        active = manager.find_active_for_node("88")
        assert active is not None  # not error/cancelled -- a re-queue can resume it
        assert active.status == "pending"

    def test_cancel_interrupts_promptly(self, tier2_action, manager):
        tensor = make_tensor(RED)

        def _cancel_shortly_after_open():
            active = manager.find_active_for_node("99")
            manager.mark_cancelled(active.handoff_id)

        threading.Timer(0.3, _cancel_shortly_after_open).start()

        start = time.monotonic()
        with raises_interrupt():
            tier2_action.execute(
                image=tensor, action_name="A", action_set="S", timeout_seconds=30, unique_id="99"
            )
        elapsed = time.monotonic() - start
        assert elapsed < 5  # unblocked by cancellation, not the 30s timeout
        assert manager.find_active_for_node("99") is None  # cancelled: no longer "active"

    def test_action_error_interrupts_promptly_and_marks_handoff_error(self, tier2_action, manager):
        """The plugin's ``action_error`` reply (bad Action/Set name, or a
        delivery failure -- ``cpsb.routes``' ``_handle_plugin_message``)
        turns into ``manager.mark_error``, which unblocks a waiting node
        with :data:`~cpsb.handoff.WaitOutcome.ERROR` -- reproduced here at
        the manager level (the same effect ``_handle_plugin_message``'s
        ``action_error`` branch has; the full websocket round trip is
        covered in ``tests/test_routes.py``).
        """
        tensor = make_tensor(RED)

        def _action_error_shortly_after_open():
            active = manager.find_active_for_node("111")
            manager.mark_error(active.handoff_id, 'Action "Bogus" not found in set "Bogus Set"')

        threading.Timer(0.3, _action_error_shortly_after_open).start()

        start = time.monotonic()
        with raises_interrupt():
            tier2_action.execute(
                image=tensor, action_name="Bogus", action_set="Bogus Set",
                timeout_seconds=30, unique_id="111",
            )
        elapsed = time.monotonic() - start
        assert elapsed < 5  # unblocked by the error, not the 30s timeout

        active = manager.find_active_for_node("111")
        assert active is None  # "error" is a terminal, non-active status
        matching = [h for h in manager.list_all(limit=10) if h.origin_node_id == "111"]
        assert len(matching) == 1
        assert matching[0].status == "error"
        assert matching[0].error == 'Action "Bogus" not found in set "Bogus Set"'


class TestConsumePath:
    def test_consumes_existing_edit_without_reopening_or_resending(
        self, tier2_action, manager, plugin_socket, monkeypatch
    ):
        source_image = Image.new("RGB", (24, 16), (1, 2, 3))
        handoff_id = make_action_handoff(manager, "5", source_image)
        manager.ingest_edit(handoff_id, Image.new("RGB", (24, 16), (0, 0, 255)), "plugin")
        monkeypatch.setattr(
            nodes_module, "_pil_to_tensor", lambda img: ("tensor-sentinel", img.size)
        )

        result = tier2_action.execute(
            image=tensor_from_image(source_image), action_name="A", action_set="S",
            timeout_seconds=60, unique_id="5",
        )
        assert result[0] == ("tensor-sentinel", (24, 16))
        assert plugin_socket.sent == []  # served from the existing edit, no open/run_action sent


class TestNodeReuseSemantics:
    def test_changed_input_supersedes_and_reopens_fresh_handoff(self, tier2_action, manager):
        """B1 parity with the bridge node: an edit made for OLD pixels must
        never be served for a new input.
        """
        red_tensor = make_tensor(RED)

        def _deliver_for(node_id: str, color: tuple[int, int, int]):
            active = manager.find_active_for_node(node_id)
            manager.ingest_edit(active.handoff_id, Image.new("RGB", (24, 16), color), "plugin")

        threading.Timer(0.3, lambda: _deliver_for("6", (0, 0, 255))).start()
        tier2_action.execute(
            image=red_tensor, action_name="A", action_set="S", timeout_seconds=10, unique_id="6"
        )
        old = manager.find_active_for_node("6")
        assert old.status == "edited"

        green_tensor = make_tensor(GREEN)
        threading.Timer(0.3, lambda: _deliver_for("6", (0, 255, 0))).start()
        tier2_action.execute(
            image=green_tensor, action_name="A", action_set="S", timeout_seconds=10, unique_id="6"
        )

        assert manager.get(old.handoff_id).status == "superseded"
        fresh = manager.find_active_for_node("6")
        assert fresh.handoff_id != old.handoff_id
        assert fresh.source_hash == compute_source_hash(Image.new("RGB", (24, 16), GREEN))

    def test_requeue_after_timeout_reuses_and_reopens_same_handoff(
        self, tier2_action, manager, plugin_socket
    ):
        """A manual re-queue after a timeout resumes the SAME handoff and
        resends ``run_action`` -- this node has no non-blocking "don't
        reopen" mode (unlike the bridge/annotate nodes' rerun modes): every
        call with no consumable edit (re)opens and (re)sends, mirroring the
        bridge node's own "Wait for first save" mode exactly.
        """
        tensor = make_tensor(RED)
        with raises_interrupt():
            tier2_action.execute(
                image=tensor, action_name="A", action_set="S",
                timeout_seconds=_SHORT_TIMEOUT, unique_id="7",
            )
        first_handoff = manager.find_active_for_node("7")
        assert first_handoff is not None
        assert plugin_socket.sent[-1]["type"] == "run_action"
        sent_after_first = len(plugin_socket.sent)

        def _deliver():
            active = manager.find_active_for_node("7")
            manager.ingest_edit(active.handoff_id, Image.new("RGB", (24, 16), (1, 2, 3)), "plugin")

        threading.Timer(0.3, _deliver).start()
        tier2_action.execute(
            image=tensor, action_name="A", action_set="S", timeout_seconds=10, unique_id="7"
        )

        second_handoff = manager.find_active_for_node("7")
        assert second_handoff.handoff_id == first_handoff.handoff_id  # reused, not replaced
        assert len(plugin_socket.sent) > sent_after_first  # reopened + resent run_action
