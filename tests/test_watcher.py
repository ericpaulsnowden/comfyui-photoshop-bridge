"""CpsbWatcher against a real watchdog Observer on tmp_path.

Simulates both Photoshop save-write patterns (PLAN.md §8 spike 4): plain
in-place rewrite and write-temp-then-rename. Both must ingest exactly once
per save, and the watcher must ignore the ``source.psd`` this package wrote
itself when creating the handoff.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from PIL import Image

from cpsb.context import CpsbContext
from cpsb.handoff import HandoffManager, HandoffMeta, SourceRef
from cpsb.psd_io import write_psd
from cpsb.watcher import CpsbWatcher

DEBOUNCE_MS = 200
#: Generous ceiling for FSEvents/inotify delivery plus the debounce window.
SETTLE_TIMEOUT = 15.0


@pytest.fixture
def manager(context: CpsbContext) -> HandoffManager:
    context.settings.update({"debounce_ms": DEBOUNCE_MS})
    return HandoffManager(context)


@pytest.fixture
def watcher(context: CpsbContext, manager: HandoffManager):
    watcher = CpsbWatcher(context, manager)
    watcher.start()
    yield watcher
    watcher.stop()


def create_handoff_with_psd(manager: HandoffManager) -> tuple[HandoffMeta, Path]:
    """Create a handoff and write its source.psd the way /cpsb/open does."""
    meta = manager.create(
        origin_node_id="17",
        origin_kind="load_image",
        workflow_name="wf",
        source=SourceRef(filename="x.png", subfolder="", type="output"),
        original_image=Image.new("RGB", (24, 16), (10, 20, 30)),
    )
    psd_path = manager.handoff_dir(meta.handoff_id) / "source.psd"
    write_psd(psd_path, Image.new("RGB", (24, 16), (10, 20, 30)))
    manager.note_source_written(meta.handoff_id)
    return meta, psd_path


def create_edit_in_place_handoff(manager: HandoffManager, original_path: Path) -> HandoffMeta:
    """A `load_psd` handoff whose edit target is *original_path* itself
    (PROTOCOL.md §6b) -- never a managed `source.psd` copy, unlike
    :func:`create_handoff_with_psd`.
    """
    return manager.create(
        origin_node_id="1",
        origin_kind="load_psd",
        workflow_name="wf",
        source=SourceRef(filename=original_path.name, subfolder="", type="input"),
        original_image=Image.new("RGB", (24, 16), (10, 20, 30)),
        source_hash="deadbeef" * 8,
        edit_in_place=True,
        original_path=str(original_path.resolve()),
    )


def wait_for_edit_count(manager: HandoffManager, handoff_id: str, count: int) -> None:
    deadline = time.monotonic() + SETTLE_TIMEOUT
    while time.monotonic() < deadline:
        meta = manager.get(handoff_id)
        if meta is not None and len(meta.edits) >= count:
            return
        time.sleep(0.05)
    meta = manager.get(handoff_id)
    raise AssertionError(
        f"Timed out waiting for {count} edit(s); have {len(meta.edits) if meta else 0}"
    )


def settle_quietly(seconds: float) -> None:
    """Wait long enough for any stray debounce timer to have fired."""
    time.sleep(seconds)


class TestInPlaceSave:
    def test_in_place_rewrite_ingests_exactly_once(self, watcher, manager):
        meta, psd_path = create_handoff_with_psd(manager)
        settle_quietly(DEBOUNCE_MS / 1000 * 3)

        # Photoshop-style in-place save: same path, new pixel content.
        write_psd(psd_path, Image.new("RGB", (24, 16), (200, 0, 0)))

        wait_for_edit_count(manager, meta.handoff_id, 1)
        settle_quietly(DEBOUNCE_MS / 1000 * 3)

        refreshed = manager.get(meta.handoff_id)
        assert len(refreshed.edits) == 1
        assert refreshed.status == "edited"
        assert refreshed.edits[0].fidelity == "composite"
        with Image.open(manager.handoff_dir(meta.handoff_id) / refreshed.edits[0].filename) as edit:
            assert edit.getpixel((0, 0)) == (200, 0, 0)

    def test_own_initial_write_is_not_ingested(self, watcher, manager):
        meta, _ = create_handoff_with_psd(manager)
        settle_quietly(DEBOUNCE_MS / 1000 * 4)
        refreshed = manager.get(meta.handoff_id)
        assert refreshed.edits == []
        assert refreshed.status == "pending"


class TestTempRenameSave:
    def test_temp_then_rename_ingests_exactly_once(self, watcher, manager):
        meta, psd_path = create_handoff_with_psd(manager)
        settle_quietly(DEBOUNCE_MS / 1000 * 3)

        # Photoshop-style atomic save: write a temp file, rename over target.
        temp_path = psd_path.with_name("psdtemp_a1b2c3")
        write_psd(temp_path, Image.new("RGB", (24, 16), (0, 200, 0)))
        os.replace(temp_path, psd_path)

        wait_for_edit_count(manager, meta.handoff_id, 1)
        settle_quietly(DEBOUNCE_MS / 1000 * 3)

        refreshed = manager.get(meta.handoff_id)
        assert len(refreshed.edits) == 1
        with Image.open(manager.handoff_dir(meta.handoff_id) / refreshed.edits[0].filename) as edit:
            assert edit.getpixel((0, 0)) == (0, 200, 0)


class TestDebounce:
    def test_rapid_writes_coalesce_to_one_ingest(self, watcher, manager):
        meta, psd_path = create_handoff_with_psd(manager)
        settle_quietly(DEBOUNCE_MS / 1000 * 3)

        # A burst of writes well inside one debounce window: only the final
        # content may be ingested, exactly once.
        write_psd(psd_path, Image.new("RGB", (24, 16), (1, 1, 1)))
        time.sleep(0.03)
        write_psd(psd_path, Image.new("RGB", (24, 16), (2, 2, 2)))
        time.sleep(0.03)
        write_psd(psd_path, Image.new("RGB", (24, 16), (3, 3, 3)))

        wait_for_edit_count(manager, meta.handoff_id, 1)
        settle_quietly(DEBOUNCE_MS / 1000 * 4)

        refreshed = manager.get(meta.handoff_id)
        assert len(refreshed.edits) == 1
        with Image.open(manager.handoff_dir(meta.handoff_id) / refreshed.edits[0].filename) as edit:
            assert edit.getpixel((0, 0)) == (3, 3, 3)


class TestIgnoredFiles:
    def test_our_own_artifacts_never_trigger_ingest(self, watcher, manager, context):
        meta, _ = create_handoff_with_psd(manager)
        folder = manager.handoff_dir(meta.handoff_id)
        settle_quietly(DEBOUNCE_MS / 1000 * 3)

        # Files this package writes itself must not be treated as saves.
        (folder / "edit_001.png").write_bytes(b"not a real edit")
        (folder / "orig_thumb.png").write_bytes(b"thumb rewrite")
        (folder / "meta.json").write_text('{"touched": true}')

        settle_quietly(DEBOUNCE_MS / 1000 * 4)
        assert manager.get(meta.handoff_id).status == "pending"

    def test_save_for_inactive_handoff_ignored(self, watcher, manager):
        meta, psd_path = create_handoff_with_psd(manager)
        manager.mark_cancelled(meta.handoff_id)
        settle_quietly(DEBOUNCE_MS / 1000 * 3)

        write_psd(psd_path, Image.new("RGB", (24, 16), (250, 250, 250)))
        settle_quietly(DEBOUNCE_MS / 1000 * 4)

        refreshed = manager.get(meta.handoff_id)
        assert refreshed.status == "cancelled"
        assert refreshed.edits == []


class TestUnreadableSave:
    def test_unreadable_save_keeps_handoff_active_and_recovers(
        self, watcher, manager, events, monkeypatch
    ):
        """A failed read must never terminally kill the handoff.

        Worst case otherwise: a huge layered PSD whose first read window was
        too short would flip to `error` (terminal), and every subsequent
        save of that same document would be silently ignored. The watcher
        must instead stay quiet (warning + status event, state unchanged)
        and ingest normally on the next save.
        """
        import cpsb.watcher as watcher_module

        monkeypatch.setattr(watcher_module, "_READ_RETRY_SECONDS", 0.01)
        meta, psd_path = create_handoff_with_psd(manager)
        settle_quietly(DEBOUNCE_MS / 1000 * 3)
        status_events_before = len(events.of_type("cpsb.status"))

        psd_path.write_bytes(b"definitely not a psd file")

        # The failed-read path emits a cpsb.status event with the UNCHANGED,
        # still-active status -- wait for that instead of a state change.
        deadline = time.monotonic() + SETTLE_TIMEOUT
        while time.monotonic() < deadline:
            if len(events.of_type("cpsb.status")) > status_events_before:
                break
            time.sleep(0.05)
        status_payloads = events.of_type("cpsb.status")[status_events_before:]
        assert status_payloads, "expected a cpsb.status event after the failed read"
        assert status_payloads[-1] == {
            "handoff_id": meta.handoff_id,
            "origin_node_id": "17",
            "status": "pending",
        }
        refreshed = manager.get(meta.handoff_id)
        assert refreshed.status == "pending"  # still active, NOT error
        assert refreshed.error is None
        assert refreshed.edits == []

        # The next (valid) save of the same document must ingest normally.
        write_psd(psd_path, Image.new("RGB", (24, 16), (0, 0, 200)))
        wait_for_edit_count(manager, meta.handoff_id, 1)
        recovered = manager.get(meta.handoff_id)
        assert recovered.status == "edited"
        assert len(recovered.edits) == 1


class TestStartStop:
    def test_start_is_idempotent_and_stop_cleans_up(self, context, manager):
        watcher = CpsbWatcher(context, manager)
        watcher.start()
        watcher.start()  # no-op, no second Observer
        watcher.stop()
        watcher.stop()  # safe to call twice

    def test_no_ingest_after_stop(self, context, manager):
        watcher = CpsbWatcher(context, manager)
        watcher.start()
        meta, psd_path = create_handoff_with_psd(manager)
        settle_quietly(DEBOUNCE_MS / 1000 * 3)
        watcher.stop()

        write_psd(psd_path, Image.new("RGB", (24, 16), (9, 9, 9)))
        settle_quietly(DEBOUNCE_MS / 1000 * 4)
        assert manager.get(meta.handoff_id).edits == []


class TestEditInPlace:
    """PROTOCOL.md §6b: a specific `edit_in_place` handoff's own original
    file, watched independently of (but alongside) the managed-folder tree.
    """

    def test_watch_original_ingests_a_save_into_managed_storage(self, watcher, manager, tmp_path):
        original = tmp_path / "art.psd"
        write_psd(original, Image.new("RGB", (24, 16), (1, 1, 1)))
        meta = create_edit_in_place_handoff(manager, original)
        watcher.watch_original(meta.handoff_id, original)
        settle_quietly(DEBOUNCE_MS / 1000 * 3)

        # Simulate the user's plain Cmd+S on their OWN file.
        write_psd(original, Image.new("RGB", (24, 16), (200, 0, 0)))

        wait_for_edit_count(manager, meta.handoff_id, 1)
        refreshed = manager.get(meta.handoff_id)
        assert refreshed.status == "edited"
        edit_path = manager.handoff_dir(meta.handoff_id) / refreshed.edits[0].filename
        with Image.open(edit_path) as edit:
            assert edit.getpixel((0, 0)) == (200, 0, 0)
        # This handoff never has a source.psd copy in the managed folder.
        assert not (manager.handoff_dir(meta.handoff_id) / "source.psd").exists()
        # And the original itself is exactly what was "saved" -- untouched
        # by this package.
        with Image.open(original) as saved:
            assert saved.getpixel((0, 0)) == (200, 0, 0)

    def test_unwatch_stops_further_ingestion(self, watcher, manager, tmp_path):
        original = tmp_path / "art.psd"
        write_psd(original, Image.new("RGB", (24, 16), (1, 1, 1)))
        meta = create_edit_in_place_handoff(manager, original)
        watcher.watch_original(meta.handoff_id, original)
        settle_quietly(DEBOUNCE_MS / 1000 * 3)

        watcher.unwatch_original(meta.handoff_id)
        write_psd(original, Image.new("RGB", (24, 16), (0, 200, 0)))
        settle_quietly(DEBOUNCE_MS / 1000 * 4)

        assert manager.get(meta.handoff_id).edits == []

    def test_unwatch_is_a_noop_for_an_unwatched_handoff(self, watcher):
        watcher.unwatch_original("never-watched")  # must not raise

    def test_refcounted_parent_shared_between_two_handoffs(self, watcher, manager, tmp_path):
        """Two edit_in_place files sharing one parent directory (the common
        case: both live directly under ComfyUI's `input/` root) must keep
        working independently -- unwatching ONE must not disturb the
        other's still-active watch on the same shared parent.
        """
        original_a = tmp_path / "a.psd"
        original_b = tmp_path / "b.psd"
        write_psd(original_a, Image.new("RGB", (8, 8), (1, 1, 1)))
        write_psd(original_b, Image.new("RGB", (8, 8), (2, 2, 2)))
        meta_a = create_edit_in_place_handoff(manager, original_a)
        meta_b = create_edit_in_place_handoff(manager, original_b)
        watcher.watch_original(meta_a.handoff_id, original_a)
        watcher.watch_original(meta_b.handoff_id, original_b)
        settle_quietly(DEBOUNCE_MS / 1000 * 3)

        watcher.unwatch_original(meta_a.handoff_id)
        write_psd(original_b, Image.new("RGB", (8, 8), (250, 250, 250)))
        wait_for_edit_count(manager, meta_b.handoff_id, 1)

        write_psd(original_a, Image.new("RGB", (8, 8), (100, 100, 100)))
        settle_quietly(DEBOUNCE_MS / 1000 * 4)
        assert manager.get(meta_a.handoff_id).edits == []  # unwatched, no ingest

    def test_source_psd_and_edit_in_place_watches_coexist(self, watcher, manager, tmp_path):
        """Regression: the edit_in_place watch must not disturb the
        pre-existing managed-folder recursive watch, or vice versa.
        """
        normal_meta, normal_psd_path = create_handoff_with_psd(manager)
        original = tmp_path / "art.psd"
        write_psd(original, Image.new("RGB", (12, 12), (1, 1, 1)))
        eip_meta = create_edit_in_place_handoff(manager, original)
        watcher.watch_original(eip_meta.handoff_id, original)
        settle_quietly(DEBOUNCE_MS / 1000 * 3)

        write_psd(normal_psd_path, Image.new("RGB", (24, 16), (9, 9, 9)))
        write_psd(original, Image.new("RGB", (12, 12), (99, 99, 99)))

        wait_for_edit_count(manager, normal_meta.handoff_id, 1)
        wait_for_edit_count(manager, eip_meta.handoff_id, 1)


class TestEditInPlaceBootRecovery:
    """PROTOCOL.md §6b: a server restart rebuilds `CpsbWatcher` from
    scratch; :meth:`CpsbWatcher.start` alone must restore a watch for every
    still-ACTIVE `edit_in_place` handoff, with no explicit
    `watch_original()` call needed.
    """

    def test_active_handoff_watch_is_restored_on_start(self, context, manager, tmp_path):
        original = tmp_path / "art.psd"
        write_psd(original, Image.new("RGB", (16, 16), (5, 5, 5)))
        meta = create_edit_in_place_handoff(manager, original)
        manager.mark_editing(meta.handoff_id)  # an ordinary in-progress handoff

        fresh_watcher = CpsbWatcher(context, manager)
        fresh_watcher.start()
        try:
            write_psd(original, Image.new("RGB", (16, 16), (150, 20, 20)))
            wait_for_edit_count(manager, meta.handoff_id, 1)
        finally:
            fresh_watcher.stop()

    def test_terminal_handoff_is_not_restored(self, context, manager, tmp_path):
        original = tmp_path / "art.psd"
        write_psd(original, Image.new("RGB", (16, 16), (5, 5, 5)))
        meta = create_edit_in_place_handoff(manager, original)
        manager.mark_cancelled(meta.handoff_id)

        fresh_watcher = CpsbWatcher(context, manager)
        fresh_watcher.start()
        try:
            assert fresh_watcher._original_by_handoff == {}
        finally:
            fresh_watcher.stop()
