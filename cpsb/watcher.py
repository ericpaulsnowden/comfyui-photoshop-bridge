"""Tier 1 save detection: a watchdog Observer over the managed folder (PLAN.md §3).

One :class:`~watchdog.observers.Observer` covers the whole managed-folder
tree (``input/<managed_folder_name>/``, PROTOCOL.md §1) rather than one per
handoff -- cheaper, and directory-level events catch
both save-write patterns Photoshop might use (in-place modification, or
write-to-temp-then-rename) without needing to know in advance which one a
given OS/Photoshop version picks: every event type watchdog can raise
(``created``, ``modified``, ``moved``) is routed through the same handler.

Only a handoff's own managed PSD copy is ever acted on within that tree --
``meta.json``, ``orig_thumb.png``, and ``edit_*.png`` are files this package
writes itself, and are ignored by construction. That managed copy's
filename is no longer a fixed literal (product-owner requirement
2026-07-18: it is derived from the handoff's ORIGIN filename at creation
time, ``HandoffMeta.psd_filename``, e.g. ``Eric-Headshot.psd`` rather than
every handoff being the indistinguishable ``source.psd``), so
:meth:`CpsbWatcher.notice` cannot match on a constant filename any more: it
resolves the handoff id from the event path's PARENT directory name first,
looks up that handoff's own recorded ``psd_filename``, and only treats the
event as a managed-copy save when the event path's name matches THAT.

PROTOCOL.md §6b's ``edit_in_place`` option adds a SECOND kind of watch
target: a specific ``load_psd`` handoff's own original file, living OUTSIDE
the managed folder entirely (typically directly under ComfyUI's ``input/``
root). Rather than blanket-watching all of ``input/`` -- which would grow
the OS-level (e.g. inotify) watch count by every subfolder ANY node in the
user's workflow happens to use, most of them irrelevant here -- each such
file's own PARENT directory is watched individually, added when the first
edit_in_place handoff under it opens and removed once the last one closes
(:meth:`CpsbWatcher.watch_original`/:meth:`~CpsbWatcher.unwatch_original`,
refcounted per parent directory since most edit_in_place files share the
same parent -- ComfyUI's ``input/`` root itself, per the Load PSD node's own
flat, non-recursive combo listing).
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

from PIL import Image
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from .context import CpsbContext
from .handoff import ACTIVE_STATUSES, HandoffManager
from .psd_io import PsdFidelity, read_edited_psd

if TYPE_CHECKING:
    from watchdog.observers.api import ObservedWatch

logger = logging.getLogger("cpsb")

#: Retry budget for reading a just-settled saved PSD (PLAN.md §3: "retry
#: with backoff reads (5 attempts, 150ms)" against transient OS locks).
_READ_ATTEMPTS = 5
_READ_RETRY_SECONDS = 0.15


class _HandoffEventHandler(FileSystemEventHandler):
    """Forwards every raw watchdog event under a watched path to the watcher."""

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
    """Watches the managed folder for settled Photoshop saves and ingests them.

    Each handoff folder gets its own debounce timer (default 800ms, from
    settings): any event resets it, and it only fires once no further event
    arrives for the full window. When it fires, an additional mtime-stability
    check guards against the OS having coalesced multiple writes into fewer
    filesystem notifications than we'd need to debounce correctly. This same
    debounce/settle pipeline is shared, unchanged, by an ``edit_in_place``
    handoff's own original-file watch (PROTOCOL.md §6b) -- only the SOURCE of
    the raw filesystem event differs (see this module's own docstring).
    """

    def __init__(self, context: CpsbContext, manager: HandoffManager) -> None:
        self._ctx = context
        self._manager = manager
        self._observer: Observer | None = None
        self._handler: _HandoffEventHandler | None = None
        self._lock = threading.Lock()
        self._timers: dict[str, threading.Timer] = {}
        self._scheduled_mtimes: dict[str, int] = {}
        # edit_in_place bookkeeping (PROTOCOL.md §6b) -- see watch_original's
        # docstring for the refcounted-per-parent-directory design.
        self._original_by_handoff: dict[str, Path] = {}
        self._parent_refcounts: dict[Path, int] = {}
        self._parent_watches: dict[Path, ObservedWatch] = {}
        # (size, mtime_ns) of the original file at the moment watch_original()
        # started watching it -- an edit_in_place handoff's own analogue of
        # is_own_write's "our own write" suppression below, needed for
        # a DIFFERENT reason: unlike the managed copy (which THIS package
        # just wrote and knows the exact signature of), an edit_in_place
        # target already exists with the user's own content the moment
        # watching begins, and a native watch backend (macOS FSEvents in
        # particular) can replay a spurious initial event for a path that
        # changed shortly before the watch was established -- indistinguishable
        # from a real event by type. Comparing every settled stat against
        # this frozen baseline filters that out without needing to guess
        # WHY a given event fired: a genuine subsequent save always changes
        # the file's mtime, so it can never coincidentally match.
        self._original_baseline: dict[str, tuple[int, int]] = {}

    def start(self) -> None:
        """Start watching the managed folder. Idempotent -- a no-op if already running.

        Also re-establishes a watch for every ACTIVE ``edit_in_place``
        handoff already on record (PROTOCOL.md §6b) -- a server restart
        rebuilds this watcher from scratch, and those files live outside the
        managed folder, so unlike a normal handoff's managed PSD copy they
        aren't automatically covered by the recursive watch below.
        """
        if self._observer is not None:
            return
        root = self._ctx.cpsb_input_dir
        root.mkdir(parents=True, exist_ok=True)
        observer = Observer()
        handler = _HandoffEventHandler(self)
        observer.schedule(handler, str(root), recursive=True)
        observer.start()
        self._observer = observer
        self._handler = handler
        logger.info("Watching %s for Photoshop saves", root)

        recovered = self._manager.active_edit_in_place_originals()
        for handoff_id, original_path in recovered:
            self.watch_original(handoff_id, original_path)
        if recovered:
            logger.info("Restored %d edit_in_place watch(es) after restart", len(recovered))

    def stop(self) -> None:
        """Stop watching and cancel pending debounce timers (server shutdown/restart)."""
        with self._lock:
            for timer in self._timers.values():
                timer.cancel()
            self._timers.clear()
            self._scheduled_mtimes.clear()
            self._original_by_handoff.clear()
            self._parent_refcounts.clear()
            self._parent_watches.clear()
            self._original_baseline.clear()
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None
            self._handler = None

    def watch_original(self, handoff_id: str, path: Path) -> None:
        """Start watching *path* -- an ``edit_in_place`` handoff's own file (PROTOCOL.md §6b).

        Call once, right after such a handoff is created (mirrors
        ``HandoffManager.note_source_written``'s "call right after
        creation" convention for the ordinary copy path) -- or let
        :meth:`start` re-establish it after a restart via
        ``HandoffManager.active_edit_in_place_originals``.

        Watches *path*'s PARENT directory (non-recursive), not *path*
        itself -- watchdog only watches directories -- refcounted per
        parent so N handoffs sharing one parent (the common case: every
        file the Load PSD node's combo lists lives directly under
        ComfyUI's ``input/`` root) cost exactly ONE OS-level watch between
        them, not N. Calling this again for a *handoff_id* already watched
        replaces its entry (harmless; not expected in practice, since a
        given handoff is only ever created once).
        """
        # LOCK-ORDERING INVARIANT (deadlock observed 2026-07-19, do not
        # regress): NEVER call ``observer.schedule``/``unschedule`` while
        # holding ``self._lock``. On macOS, fsevents' ``schedule`` blocks
        # coordinating with the observer's own dispatch thread -- and that
        # dispatch thread runs :meth:`notice`, which takes ``self._lock``
        # (via ``_schedule``/``_handoff_for_original``). Holding the lock
        # across ``observer.schedule`` therefore deadlocks the moment a real
        # save event is being dispatched concurrently: main thread waits on
        # fsevents, fsevents' dispatcher waits on our lock. So: decide and
        # record bookkeeping UNDER the lock (with a ``None`` reservation in
        # ``_parent_watches`` keeping concurrent callers idempotent), then
        # do the blocking observer call OUTSIDE it, then store the real
        # watch handle -- re-checking the reservation still exists, since a
        # concurrent unwatch/stop may have retired it meanwhile.
        resolved = path.resolve()
        with self._lock:
            self._original_by_handoff[handoff_id] = resolved
            try:
                stat = resolved.stat()
                self._original_baseline[handoff_id] = (stat.st_size, stat.st_mtime_ns)
            except OSError:
                # Nothing on disk yet to baseline against -- fine, the first
                # real event will simply never match a (missing) baseline and
                # so will always be ingested, same as if this had succeeded.
                self._original_baseline.pop(handoff_id, None)
            parent = resolved.parent
            self._parent_refcounts[parent] = self._parent_refcounts.get(parent, 0) + 1
            need_schedule = (
                parent not in self._parent_watches
                and self._observer is not None
                and self._handler is not None
            )
            if need_schedule:
                self._parent_watches[parent] = None  # reservation, see above
            observer = self._observer
            handler = self._handler
        if not need_schedule:
            return
        watch = observer.schedule(handler, str(parent), recursive=False)
        with self._lock:
            if parent in self._parent_watches:
                self._parent_watches[parent] = watch
                return
        # The reservation vanished while we were scheduling (every ref was
        # unwatched, or stop() cleared the table): this watch is unwanted.
        try:
            observer.unschedule(watch)
        except Exception:  # observer may already be stopped
            logger.debug("unschedule of an orphaned parent watch failed", exc_info=True)

    def unwatch_original(self, handoff_id: str) -> None:
        """Stop watching a handoff's original file (PROTOCOL.md §6b).

        Call once the handoff leaves the active set (cancelled, discarded,
        or superseded -- see ``cpsb.routes``' cancel/discard/open handlers).
        A no-op, safe to call unconditionally, for a *handoff_id* that was
        never watched (every non-``edit_in_place`` handoff, or one already
        unwatched) -- callers don't need to check first. Deliberately NOT
        called on a Tier 1/2 launch failure (``mark_error``): that status
        can still recover back to ``editing`` via a Tier 1 fallback
        (PROTOCOL.md §3), and leaving the watch in place means that
        recovery needs no matching re-watch call; an abandoned errored
        handoff's stale watch is bounded and self-heals on the next server
        restart (:meth:`start` only restores ACTIVE handoffs) or an explicit
        cancel/discard.
        """
        # Same lock-ordering invariant as watch_original: bookkeeping under
        # the lock, the observer call outside it. A popped ``None`` entry is
        # a reservation whose scheduling thread is still mid-`schedule`; by
        # popping it here we hand THAT thread the cleanup (its re-check
        # will find the reservation gone and unschedule its own watch).
        with self._lock:
            resolved = self._original_by_handoff.pop(handoff_id, None)
            self._original_baseline.pop(handoff_id, None)
            if resolved is None:
                return
            parent = resolved.parent
            remaining = self._parent_refcounts.get(parent, 0) - 1
            if remaining > 0:
                self._parent_refcounts[parent] = remaining
                return
            self._parent_refcounts.pop(parent, None)
            watch = self._parent_watches.pop(parent, None)
            observer = self._observer
        if watch is not None and observer is not None:
            try:
                observer.unschedule(watch)
            except Exception:  # observer may already be stopped
                logger.debug("unschedule failed for %s", parent, exc_info=True)

    def notice(self, path_str: str) -> None:
        """Handle one raw filesystem event path.

        Two independent matches, tried in order: a managed-folder handoff's
        own managed PSD copy, or -- PROTOCOL.md §6b -- a currently-
        registered ``edit_in_place`` handoff's own original file. Anything
        matching neither (every other file living under a watched parent
        directory, e.g. an unrelated upload sitting next to a psd-native
        source) is silently ignored.

        The managed-copy match used to be a single constant filename
        (``source.psd``); it is now PER-HANDOFF (product-owner requirement
        2026-07-18, ``HandoffMeta.psd_filename``), so this resolves the
        candidate handoff id from the event path's own PARENT directory
        name first (PROTOCOL.md §1: that IS the handoff id, for anything
        living directly under the managed folder), looks up its current
        ``psd_filename`` via the manager, and only treats *path_str* as a
        managed-copy save when the event path's own name matches that
        recorded value exactly. A parent name that isn't a known handoff at
        all (``meta.json``/``orig_thumb.png``/``edit_*.png`` events all
        still have a real handoff for a parent, but a stray/unrelated file
        wouldn't) simply falls through to the ``edit_in_place`` check below,
        same as before.

        A cheap, lock-free extension check runs FIRST and rejects anything
        that isn't ``.psd``/``.psb``, before ever touching
        ``HandoffManager``'s lock: every ``meta.json``/``orig_thumb.png``/
        ``edit_*.png`` write this package makes itself, and any directory-
        level event bubbling up from a non-recursive ``edit_in_place``
        parent watch (PROTOCOL.md §6b), is exactly this common, high-volume,
        never-relevant case -- a managed copy and an ``edit_in_place``
        original are BOTH always ``.psd``/``.psb`` by construction
        (:data:`cpsb.routes._PSD_NATIVE_EXTENSIONS`), so nothing real is ever
        filtered out here. This is not just an optimization: without it,
        EVERY raw event (however irrelevant) pays a
        :meth:`HandoffManager.get` lock-acquire-and-deepcopy, and under
        heavy event volume (many handoffs, or a long-running server) that
        contention was observed to stall real event processing badly enough
        to look like a hang in this module's own test suite.
        """
        path = Path(path_str)
        if path.suffix.lower() not in (".psd", ".psb"):
            return
        handoff_id = path.parent.name
        meta = self._manager.get(handoff_id)
        if meta is not None and path.name == meta.psd_filename:
            self._schedule(handoff_id, path)
            return
        original_handoff_id = self._handoff_for_original(path)
        if original_handoff_id is not None:
            self._schedule(original_handoff_id, path)

    def _handoff_for_original(self, path: Path) -> str | None:
        resolved = path.resolve()
        with self._lock:
            for handoff_id, original in self._original_by_handoff.items():
                if original == resolved:
                    return handoff_id
        return None

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

        if self._manager.is_own_write(path, stat.st_size, stat.st_mtime_ns):
            # PATH-keyed, not handoff-id-keyed (cpsb.handoff.HandoffManager.
            # record_own_write's own docstring has the full rationale): this
            # is what lets a DIFFERENT handoff's write -- e.g.
            # PhotoshopComposePSD writing its composed output into a file
            # this handoff (a `load_psd` edit_in_place original, or another
            # handoff's own managed copy) happens to be watching -- be
            # recognized as "not a real Photoshop save" too, not just this
            # handoff's own managed-copy write.
            logger.debug("Ignoring our own write to %s (handoff %s)", path, handoff_id)
            return

        if self._matches_original_baseline(handoff_id, stat.st_size, stat.st_mtime_ns):
            # PROTOCOL.md §6b: a spurious event for an edit_in_place file
            # that hasn't actually changed since watch_original() started
            # watching it (see this class's __init__ docstring comment on
            # _original_baseline for why native watch backends can fire
            # one). A no-op for every non-edit_in_place path: their
            # handoff_id was never given a baseline, so this never matches.
            logger.debug("Ignoring unchanged-since-watch-began event for handoff %s", handoff_id)
            return

        self._ingest_settled(handoff_id, path)

    def _matches_original_baseline(self, handoff_id: str, size: int, mtime_ns: int) -> bool:
        """Whether *size*/*mtime_ns* still match the file's stat when watched began."""
        with self._lock:
            return self._original_baseline.get(handoff_id) == (size, mtime_ns)

    def _ingest_settled(self, handoff_id: str, path: Path) -> None:
        meta = self._manager.get(handoff_id)
        if meta is None or meta.status not in ACTIVE_STATUSES:
            logger.debug("Ignoring settled save for inactive handoff %s", handoff_id)
            return

        if not self._manager.should_ingest(handoff_id):
            # "Ignore (do nothing)" (product-owner requirement 2026-07-18):
            # this is the automatic Tier 1 watcher -- the exact "close the
            # PSD without saving [into the graph]" path the product owner
            # asked for -- so a settled save is simply never read/ingested.
            # Logged at INFO (naming the handoff and its policy) so a user
            # who forgot they set Ignore can diagnose "nothing happened"
            # from the console. Deliberately skips `_read_with_retry`
            # entirely: those pixels are never going to be used, and
            # skipping the read also avoids needless file-lock contention
            # with Photoshop's own save.
            logger.info(
                "Ignoring settled save for handoff %s (trigger_policy=%r); not ingesting",
                handoff_id,
                meta.trigger_policy,
            )
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
