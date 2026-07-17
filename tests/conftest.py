"""Shared fixtures: a fully fake CpsbContext wired to tmp_path directories.

No ComfyUI anywhere: the context-injection pattern (``cpsb/context.py``)
means every test gets real behavior against throwaway directories and a
recording ``send_event``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cpsb.context import CpsbContext, SettingsStore


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
