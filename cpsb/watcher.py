"""Tier 1 save detection: a watchdog Observer over ``input/cpsb/`` (PLAN.md §3).

One :class:`~watchdog.observers.Observer` covers the whole ``input/cpsb/``
tree rather than one per handoff -- cheaper, and directory-level events catch
both save-write patterns Photoshop might use (in-place modification, or
write-to-temp-then-rename) without needing to know in advance which one a
given OS/Photoshop version picks: every event type watchdog can raise
(``created``, ``modified``, ``moved``) is routed through the same handler.

Only ``source.psd`` is ever acted on -- ``meta.json``, ``orig_thumb.png``,
and ``edit_*.png`` are files this package writes itself, and are ignored by
construction (the filename check below only matches ``source.psd``).
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from PIL import Image
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from .context import CpsbContext
from .handoff import ACTIVE_STATUSES, HandoffManager
from .psd_io import PsdFidelity, read_edited_psd

logger = logging.getLogger("cpsb")

#: Retry budget for reading a just-settled ``source.psd`` (PLAN.md §3: "retry
#: with backoff reads (5 attempts, 150ms)" against transient OS locks).
_READ_ATTEMPTS = 5
_READ_RETRY_SECONDS = 0.15

_WATCHED_FILENAME = "source.psd"


class _HandoffEventHandler(FileSystemEventHandler):
    """Forwards every raw watchdog event for ``source.psd`` to the watcher."""

    def __init__(self, watcher: CpsbWatcher) -> None:
        self._watcher = watcher

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._watcher.notice(event.src_path)

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._watcher.notice(event.src_path)

    def on_moved(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._watcher.notice(event.dest_path)


class CpsbWatcher:
    """Watches ``input/cpsb/`` for settled Photoshop saves and ingests them.

    Each handoff folder gets its own debounce timer (default 800ms, from
    settings): any event resets it, and it only fires once no further event
    arrives for the full window. When it fires, an additional mtime-stability
    check guards against the OS having coalesced multiple writes into fewer
    filesystem notifications than we'd need to debounce correctly.
    """

    def __init__(self, context: CpsbContext, manager: HandoffManager) -> None:
        self._ctx = context
        self._manager = manager
        self._observer: Observer | None = None
        self._lock = threading.Lock()
        self._timers: dict[str, threading.Timer] = {}
        self._scheduled_mtimes: dict[str, int] = {}

    def start(self) -> None:
        """Start watching ``input/cpsb/``. Idempotent -- a no-op if already running."""
        if self._observer is not None:
            return
        root = self._ctx.cpsb_input_dir
        root.mkdir(parents=True, exist_ok=True)
        observer = Observer()
        observer.schedule(_HandoffEventHandler(self), str(root), recursive=True)
        observer.start()
        self._observer = observer
        logger.info("Watching %s for Photoshop saves", root)

    def stop(self) -> None:
        """Stop watching and cancel pending debounce timers (server shutdown/restart)."""
        with self._lock:
            for timer in self._timers.values():
                timer.cancel()
            self._timers.clear()
            self._scheduled_mtimes.clear()
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None

    def notice(self, path_str: str) -> None:
        """Handle one raw filesystem event path, ignoring anything but ``source.psd``."""
        path = Path(path_str)
        if path.name != _WATCHED_FILENAME:
            return
        handoff_id = path.parent.name
        self._schedule(handoff_id, path)

    def _schedule(self, handoff_id: str, path: Path) -> None:
        debounce_seconds = self._ctx.settings.get("debounce_ms", 800) / 1000
        with self._lock:
            existing = self._timers.get(handoff_id)
            if existing is not None:
                existing.cancel()
            try:
                self._scheduled_mtimes[handoff_id] = path.stat().st_mtime_ns
            except OSError:
                self._scheduled_mtimes[handoff_id] = 0
            timer = threading.Timer(debounce_seconds, self._settle, args=(handoff_id, path))
            timer.daemon = True
            self._timers[handoff_id] = timer
            timer.start()

    def _settle(self, handoff_id: str, path: Path) -> None:
        with self._lock:
            self._timers.pop(handoff_id, None)
            scheduled_mtime_ns = self._scheduled_mtimes.get(handoff_id)

        try:
            stat = path.stat()
        except OSError:
            logger.debug("%s vanished before it could be read", path)
            return

        if scheduled_mtime_ns is not None and stat.st_mtime_ns != scheduled_mtime_ns:
            # Touched again after we armed the timer for the previous event --
            # an already-in-flight write. A fresh event should reschedule us
            # anyway, but do it defensively in case the OS coalesced events.
            self._schedule(handoff_id, path)
            return

        if self._manager.is_own_source_write(handoff_id, stat.st_size, stat.st_mtime_ns):
            logger.debug("Ignoring our own source.psd write for handoff %s", handoff_id)
            return

        self._ingest_settled(handoff_id, path)

    def _ingest_settled(self, handoff_id: str, path: Path) -> None:
        meta = self._manager.get(handoff_id)
        if meta is None or meta.status not in ACTIVE_STATUSES:
            logger.debug("Ignoring settled save for inactive handoff %s", handoff_id)
            return

        image, fidelity = self._read_with_retry(path)
        if image is None or fidelity is None:
            # Deliberately NOT mark_error: "error" is terminal, and a save
            # that failed to read once (file still locked, or the retry
            # window too short for a huge layered PSD) is usually readable
            # on the user's next save -- which fires a fresh event and
            # retries ingestion naturally, but only while the handoff is
            # still active. A cpsb.status event (unchanged, still-active
            # status) keeps the UI honest meanwhile. mark_error stays
            # reserved for genuinely terminal launch failures (routes).
            logger.warning(
                "Could not read %s after %d attempts; keeping handoff %s active and "
                "waiting for the next save",
                path,
                _READ_ATTEMPTS,
                handoff_id,
            )
            self._ctx.send_event(
                "cpsb.status",
                {
                    "handoff_id": meta.handoff_id,
                    "origin_node_id": meta.origin_node_id,
                    "status": meta.status,
                },
            )
            return
        self._manager.ingest_edit(handoff_id, image, fidelity)

    @staticmethod
    def _read_with_retry(path: Path) -> tuple[Image.Image | None, PsdFidelity | None]:
        last_error: Exception | None = None
        for attempt in range(1, _READ_ATTEMPTS + 1):
            try:
                return read_edited_psd(path)
            except Exception as exc:
                # Deliberately broad: a PSD mid-write can fail in many ways
                # (truncated compression stream, incomplete header, a lock
                # held by Photoshop); any of them is worth a retry, and the
                # final attempt's failure is reported via mark_error rather
                # than raised, which would crash this background thread.
                last_error = exc
                logger.debug(
                    "Read attempt %d/%d for %s failed: %s", attempt, _READ_ATTEMPTS, path, exc
                )
                if attempt < _READ_ATTEMPTS:
                    time.sleep(_READ_RETRY_SECONDS)
        logger.warning(
            "Giving up reading %s after %d attempts: %s", path, _READ_ATTEMPTS, last_error
        )
        return None, None
