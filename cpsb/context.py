"""Runtime context that decouples ``cpsb`` from ComfyUI itself.

Every other module in this package receives a :class:`CpsbContext` instead of
importing ``server`` or ``folder_paths`` directly. The real context is built
exactly once, in the top-level ``__init__.py``, from ComfyUI's own
``server.PromptServer`` and ``folder_paths`` modules. Tests build a fake
context that points at ``tmp_path`` directories and records emitted events,
so the rest of ``cpsb`` is fully testable without ComfyUI installed.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("cpsb")

#: Signature of the callable used to push a websocket event to every
#: connected ComfyUI frontend client (``PromptServer.instance.send_sync`` in
#: production).
SendEvent = Callable[[str, dict], None]

#: Default values for every backend-persisted setting (PROTOCOL.md §2,
#: ``GET/POST /cpsb/settings``).
DEFAULT_SETTINGS: dict[str, Any] = {
    "photoshop_path": "",
    "debounce_ms": 800,
    "cleanup_days": 14,
    "sibling_outputs": True,
}


class SettingsStore:
    """Thread-safe, disk-persisted store for the ``cpsb.json`` settings file.

    Backs the ``GET``/``POST /cpsb/settings`` routes (PROTOCOL.md §2). Reads
    are served from an in-memory copy; writes are merged and flushed to disk
    atomically (write-to-temp then rename) so a crash mid-write never leaves
    a truncated ``cpsb.json`` behind.
    """

    def __init__(self, path: Path, defaults: dict[str, Any] | None = None) -> None:
        """Load *path* into memory, seeding any missing key from *defaults*.

        Args:
            path: Location of the settings JSON file (``<user_dir>/cpsb.json``
                in production). Does not need to exist yet.
            defaults: Values used for keys absent from both the file and a
                prior in-memory state. Defaults to :data:`DEFAULT_SETTINGS`.
        """
        self._path = path
        self._defaults = dict(defaults if defaults is not None else DEFAULT_SETTINGS)
        self._lock = threading.Lock()
        self._data = self._load()

    def _load(self) -> dict[str, Any]:
        data = dict(self._defaults)
        if self._path.exists():
            try:
                stored = json.loads(self._path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("Could not read settings file %s: %s", self._path, exc)
            else:
                if isinstance(stored, dict):
                    data.update(stored)
                else:
                    logger.warning("Settings file %s did not contain a JSON object", self._path)
        return data

    def as_dict(self) -> dict[str, Any]:
        """Return a snapshot of every current setting."""
        with self._lock:
            return dict(self._data)

    def get(self, key: str, default: Any = None) -> Any:
        """Return a single setting's current value."""
        with self._lock:
            return self._data.get(key, default)

    def update(self, partial: dict[str, Any]) -> dict[str, Any]:
        """Merge *partial* into the stored settings and persist to disk.

        Args:
            partial: Keys to overwrite; keys already present and not in
                *partial* are left untouched.

        Returns:
            The full settings object after the merge.
        """
        with self._lock:
            self._data.update(partial)
            self._save()
            return dict(self._data)

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(self._data, indent=2, sort_keys=True), encoding="utf-8")
        tmp_path.replace(self._path)


def load_settings(user_dir: Path) -> SettingsStore:
    """Build the :class:`SettingsStore` backed by ``<user_dir>/cpsb.json``."""
    return SettingsStore(user_dir / "cpsb.json")


@dataclass
class CpsbContext:
    """Everything ``cpsb`` needs from its host, injected rather than imported.

    Attributes:
        input_dir: ComfyUI's ``input/`` directory. Handoffs live under
            ``input_dir / "cpsb"``.
        output_dir: ComfyUI's ``output/`` directory (for sibling outputs).
        temp_dir: ComfyUI's ``temp/`` directory.
        user_dir: ComfyUI's per-user directory (settings persistence).
        send_event: Pushes a named event with a JSON-serializable payload to
            every connected frontend (``PromptServer.send_sync`` in
            production; a recording stub in tests).
        settings: The backend-persisted settings store.
    """

    input_dir: Path
    output_dir: Path
    temp_dir: Path
    user_dir: Path
    send_event: SendEvent
    settings: SettingsStore

    @property
    def cpsb_input_dir(self) -> Path:
        """The managed ``input/cpsb/`` folder holding every handoff."""
        return self.input_dir / "cpsb"
