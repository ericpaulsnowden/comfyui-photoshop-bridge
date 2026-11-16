"""Shared fixtures: a fully fake CpsbContext wired to tmp_path directories.

No ComfyUI anywhere: the context-injection pattern (``cpsb/context.py``)
means every test gets real behavior against throwaway directories and a
recording ``send_event``.
"""

from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path
from typing import cast

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cpsb.nodes as nodes_module
import cpsb.routes as routes_module
from cpsb.context import CpsbContext, SettingsStore
from cpsb.handoff import HandoffManager
from cpsb.launcher import LaunchResult


class RecordingEvents:
    """Callable ``send_event`` stand-in that records every emitted event."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def __call__(self, event: str, payload: dict) -> None:
        self.events.append((event, payload))

    def of_type(self, event: str) -> list[dict]:
        """Payloads of every recorded event named *event*, in order."""
        return [payload for name, payload in self.events if name == event]


@pytest.fixture
def events() -> RecordingEvents:
    return RecordingEvents()


@pytest.fixture
def context(tmp_path: Path, events: RecordingEvents) -> CpsbContext:
    """A CpsbContext over fresh tmp_path dirs with a recording send_event."""
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    temp_dir = tmp_path / "temp"
    user_dir = tmp_path / "user"
    for directory in (input_dir, output_dir, temp_dir, user_dir):
        directory.mkdir()
    return CpsbContext(
        input_dir=input_dir,
        output_dir=output_dir,
        temp_dir=temp_dir,
        user_dir=user_dir,
        send_event=events,
        settings=SettingsStore(user_dir / "cpsb.json"),
    )


@pytest.fixture
def manager(context: CpsbContext) -> HandoffManager:
    """A real HandoffManager over the fake context. (The watcher suites
    override this to shorten the debounce.)"""
    return HandoffManager(context)


@pytest.fixture
def configured(context: CpsbContext, manager: HandoffManager):
    """Wire ``nodes.configure`` -- the state every node module shares -- with
    no real app/loop. Enough for anything that never opens Photoshop:
    ``IS_CHANGED``, pass-through/"don't open" modes, or consuming an edit a
    handoff already has.
    """
    nodes_module.configure(context, manager, cast("object", None), cast("object", None))
    yield
    nodes_module._state = None


@pytest.fixture
def loop_thread():
    """A live event loop on a background thread, like PromptServer's own."""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=5)
    if not loop.is_running():
        loop.close()


@pytest.fixture
def launches(monkeypatch):
    """Records every ``launch_photoshop`` call (the Tier 1 open path) as its
    PSD path. Patched on ``cpsb.routes`` because the nodes always call it
    through that module object. (test_nodes.py and test_routes.py override
    this with richer recorders.)
    """
    calls: list[str] = []

    def fake_launch(psd_path, override=""):
        calls.append(str(psd_path))
        return LaunchResult(ok=True)

    monkeypatch.setattr(routes_module, "launch_photoshop", fake_launch)
    return calls
