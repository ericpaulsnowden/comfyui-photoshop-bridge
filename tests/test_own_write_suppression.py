"""Path-keyed own-write suppression: ``HandoffManager.record_own_write`` /
``is_own_write``, replacing the earlier per-handoff single-snapshot registry
(``note_source_written`` / ``is_own_source_write``, the latter now removed,
the former kept as a thin wrapper over the new primitive -- see
``cpsb/handoff.py``'s own docstrings for the full design rationale).

**The confirmed gap this closes** (product-owner directive: fix this "in a
consistent sharable way across all of the nodes that use it"): the OLD
registry was keyed by handoff id and could only ever suppress a write
against THAT SAME handoff's own managed-copy path. But
``PhotoshopComposePSD`` also writes its composed output at a path that can
be a DIFFERENT handoff's own watched file entirely -- concretely, its
``existing_psd_path`` append target (or, in principle, even its fresh
auto-numbered output, if a stale watch happens to land on that name) can be
the exact file a ``load_psd`` ``edit_in_place`` handoff has open in
Photoshop, watched independently via
:meth:`cpsb.watcher.CpsbWatcher.watch_original`. The old handoff-id-keyed
lookup could never suppress that write -- the watcher would be consulting
the WRONG handoff's registry entry, or none at all -- so it ingested
compose's own output as if Photoshop had just saved an edit. Under "Re-run
on every save" that re-triggers the very node that wrote it: an infinite
loop. Under "Wait for first save" it delivers the wrong pixels.

**Harness note**: :class:`TestLoopRegression` drives a REAL
``watchdog.observers.Observer`` end-to-end (the only test here that needs
one), mirroring ``tests/test_watcher.py``'s own real-Observer harness
(``DEBOUNCE_MS``, ``SETTLE_TIMEOUT``, ``wait_for_edit_count``/
``settle_quietly``) rather than inventing a new timing scheme -- none
exists elsewhere in this suite to reuse directly, since watcher tests are
deliberately black-box against the real filesystem/Observer, not a fake
clock. Every other class here works directly against
:class:`~cpsb.handoff.HandoffManager`'s own registry ("at registry level"),
which is fast and needs no Observer at all.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from PIL import Image

from cpsb.context import CpsbContext
from cpsb.handoff import HandoffManager, HandoffMeta, SourceRef
from cpsb.psd_io import write_psd
from cpsb.watcher import CpsbWatcher

DEBOUNCE_MS = 200
#: Generous ceiling for FSEvents/inotify delivery plus the debounce window
#: (matches tests/test_watcher.py's own constant of the same name).
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


def create_edit_in_place_handoff(manager: HandoffManager, original_path: Path) -> HandoffMeta:
    """A ``load_psd`` handoff whose edit target is *original_path* itself
    (PROTOCOL.md §6b) -- never a managed PSD copy. Mirrors
    ``tests/test_watcher.py``'s identical helper.
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


def create_handoff_with_psd(
    manager: HandoffManager, filename: str = "x.png"
) -> tuple[HandoffMeta, Path]:
    """A normal (non-``edit_in_place``) handoff with its managed PSD copy
    written and recorded the way ``/cpsb/open`` does. Mirrors
    ``tests/test_watcher.py``'s identical helper.
    """
    meta = manager.create(
        origin_node_id="17",
        origin_kind="load_image",
        workflow_name="wf",
        source=SourceRef(filename=filename, subfolder="", type="output"),
        original_image=Image.new("RGB", (24, 16), (10, 20, 30)),
    )
    psd_path = manager.psd_path(meta)
    write_psd(psd_path, Image.new("RGB", (24, 16), (10, 20, 30)))
    manager.note_source_written(meta.handoff_id)
    return meta, psd_path


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


class TestLoopRegression:
    """The confirmed bug, end-to-end through a REAL watchdog Observer.

    A ``load_psd`` ``edit_in_place`` handoff is watching ``original`` --
    exactly a Load PSD node's own watch on the user's file. A
    PhotoshopComposePSD-style write then lands on that EXACT path (an actor
    with NO handoff id in common with the watched handoff at all -- compose
    never even looks up ``watched.handoff_id``) and must be suppressed once
    ``record_own_write`` is called for it, purely by path. A later save that
    is NOT recorded -- the real Photoshop save this mechanism must never
    swallow -- still ingests normally.

    This is the scenario the OLD handoff-id-keyed registry structurally
    could not cover: ``is_own_source_write`` required a matching
    ``handoff_id``, and compose's write was never "for" ``watched``'s id at
    all, so the old check would always answer ``False`` here and the
    settled event would be ingested as a fake edit. Confirmed red against a
    reconstruction of that old algorithm -- see this repo's fix report for
    exactly how (git is off-limits for this change, and reverting the real
    source files in place felt like an unnecessary risk to a live,
    Dropbox-synced working tree just to prove a point already provable more
    safely).
    """

    def test_recorded_compose_style_write_is_not_ingested_then_real_save_is(
        self, watcher, manager, tmp_path
    ):
        original = tmp_path / "art.psd"
        write_psd(original, Image.new("RGB", (24, 16), (1, 1, 1)))
        watched = create_edit_in_place_handoff(manager, original)
        watcher.watch_original(watched.handoff_id, original)
        settle_quietly(DEBOUNCE_MS / 1000 * 3)

        # Simulate PhotoshopComposePSD writing its composed output directly
        # onto the file `watched` has open (e.g. the user pointed
        # `existing_psd_path` at their own original). Recorded purely by
        # PATH -- exactly cpsb/compose_psd.py's own
        # manager.record_own_write(target_path) call after _atomic_save's
        # os.replace, which never mentions `watched.handoff_id` at all.
        write_psd(original, Image.new("RGB", (24, 16), (77, 88, 99)))
        manager.record_own_write(original)

        # Give the real Observer + debounce window every chance to settle
        # and (on the old, handoff-id-keyed design) wrongly ingest this as
        # a Photoshop-authored edit.
        settle_quietly(DEBOUNCE_MS / 1000 * 4)
        refreshed = manager.get(watched.handoff_id)
        assert refreshed.edits == [], (
            "compose's own recorded write was ingested as a Photoshop save -- "
            "the exact cross-handoff loop-feedback bug this mechanism exists to close"
        )
        assert refreshed.status == "pending"

        # The REAL Photoshop save: same path, different content, no
        # record_own_write call. This one MUST still be ingested normally --
        # suppression must never swallow a genuine edit.
        write_psd(original, Image.new("RGB", (24, 16), (200, 0, 0)))
        wait_for_edit_count(manager, watched.handoff_id, 1)
        final = manager.get(watched.handoff_id)
        assert final.status == "edited"
        edit_path = manager.handoff_dir(watched.handoff_id) / final.edits[-1].filename
        with Image.open(edit_path) as edit:
            assert edit.getpixel((0, 0)) == (200, 0, 0)


class TestManagedCopySecondWrite:
    """Same handoff's managed copy, written and recorded twice in
    succession -- each write's own settle-check (performed right after ITS
    OWN recording, mirroring how a real debounce window serializes events)
    must be suppressed at the time it happens. A third write with different
    content that is NOT recorded must still read as a genuine, ingestable
    save.
    """

    def test_two_recorded_writes_suppressed_third_unrecorded_ingested(self, manager, tmp_path):
        psd_path = tmp_path / "handoff.psd"

        write_psd(psd_path, Image.new("RGB", (12, 12), (1, 1, 1)))
        manager.record_own_write(psd_path)
        stat_1 = psd_path.stat()
        assert manager.is_own_write(psd_path, stat_1.st_size, stat_1.st_mtime_ns)

        write_psd(psd_path, Image.new("RGB", (12, 12), (2, 2, 2)))
        manager.record_own_write(psd_path)
        stat_2 = psd_path.stat()
        assert manager.is_own_write(psd_path, stat_2.st_size, stat_2.st_mtime_ns)
        # The registry holds only the LATEST stat per path by design (see
        # HandoffManager.record_own_write's own docstring on why
        # re-recording moves a path to the end rather than accumulating
        # history) -- the first write's own stat is no longer on record, but
        # that's moot: its own settle-check already ran and was suppressed
        # (asserted above) before the second write ever landed, exactly the
        # ordering a real debounce window enforces.

        write_psd(psd_path, Image.new("RGB", (12, 12), (3, 3, 3)))
        stat_3 = psd_path.stat()
        assert not manager.is_own_write(psd_path, stat_3.st_size, stat_3.st_mtime_ns)


class TestCrossHandoffRegistryLevel:
    """The confirmed gap, at the registry level (no real Observer needed):
    a write recorded with NO handoff in common with the handoff whose watch
    it lands under must still suppress. ``is_own_write`` takes no handoff id
    parameter at all -- unlike the removed ``is_own_source_write(handoff_id,
    size, mtime_ns)`` -- so there is structurally no way to "ask under the
    wrong id" anymore.
    """

    def test_write_recorded_under_no_handoff_suppresses_a_different_handoffs_watched_path(
        self, manager, tmp_path
    ):
        # `watched`: an edit_in_place load_psd handoff watching shared_path.
        shared_path = tmp_path / "shared.psd"
        write_psd(shared_path, Image.new("RGB", (10, 10), (1, 1, 1)))
        watched = create_edit_in_place_handoff(manager, shared_path)
        assert watched.handoff_id  # sanity: a real handoff exists to watch

        # `other`: a totally unrelated handoff with its OWN managed copy at
        # a DIFFERENT path -- present only to prove its registry entry
        # neither interferes with, nor is confused for, shared_path's.
        _other_meta, other_psd_path = create_handoff_with_psd(manager, filename="unrelated.png")
        assert other_psd_path != shared_path

        # The "compose" actor: writes shared_path and records it, tied to
        # NEITHER handoff id -- exactly cpsb/compose_psd.py's own
        # manager.record_own_write(target_path) call, which never takes or
        # mentions a handoff id at all.
        write_psd(shared_path, Image.new("RGB", (10, 10), (250, 10, 10)))
        manager.record_own_write(shared_path)

        stat = shared_path.stat()
        # This is the EXACT call cpsb.watcher.CpsbWatcher._settle now makes
        # for ANY handoff whose watched path resolves to shared_path.
        assert manager.is_own_write(shared_path, stat.st_size, stat.st_mtime_ns)
        # `other`'s own unrelated registry entry is untouched/unconfused.
        other_stat = other_psd_path.stat()
        assert manager.is_own_write(other_psd_path, other_stat.st_size, other_stat.st_mtime_ns)


class TestBoundedRegistry:
    """``HandoffManager.record_own_write``'s own documented cap: at most
    ``_OWN_WRITE_REGISTRY_MAX`` (128) entries, oldest-inserted evicted first.
    """

    def test_200_records_keeps_at_most_128_oldest_evicted_newest_matches(self, manager, tmp_path):
        paths = []
        for i in range(200):
            path = tmp_path / f"f{i:04d}.psd"
            path.write_bytes(f"content {i}".encode())
            manager.record_own_write(path)
            paths.append(path)

        # Whitebox check of the documented numeric bound itself -- there is
        # no black-box way to observe "how many are kept", only "does this
        # ONE path still match", so this one assertion deliberately reaches
        # into the manager's own registry
        # (cpsb.handoff.HandoffManager._own_writes).
        assert len(manager._own_writes) <= 128

        oldest_path = paths[0]
        oldest_stat = oldest_path.stat()
        assert not manager.is_own_write(
            oldest_path, oldest_stat.st_size, oldest_stat.st_mtime_ns
        ), "the oldest-inserted entry should have been evicted"

        newest_path = paths[-1]
        newest_stat = newest_path.stat()
        assert manager.is_own_write(newest_path, newest_stat.st_size, newest_stat.st_mtime_ns)

    def test_re_recording_a_path_refreshes_it_rather_than_leaving_it_stale(self, manager, tmp_path):
        """Re-recording an already-present path must move it to the END
        (the "most recently used" position), not merely refresh its VALUE
        while leaving its original insertion position in place -- otherwise
        a path this package keeps writing to repeatedly (e.g. successive
        appends into the same ``existing_psd_path`` target) could still be
        evicted purely because it was FIRST recorded long ago, even though
        it has been recorded again more recently than many entries that
        survive it. A plain ``dict.__setitem__`` on an existing key would
        refresh the VALUE but -- verified CPython dict behavior -- leaves
        the key's iteration position untouched, so this test is deliberately
        shaped to fail against that naive alternative: `path` is re-recorded
        while still well within the cap (not after it would already have
        been evicted), then enough NEWER entries are added to force
        eviction, so the two implementations disagree on WHICH entries get
        evicted first.
        """
        path = tmp_path / "reused.psd"
        path.write_bytes(b"run 1")
        manager.record_own_write(path)  # inserted first -- the "oldest" position

        # Older entries that `path` was originally inserted before. Under a
        # naive (position-preserving) re-record, `path` would remain older
        # than every one of these forever; under the correct (move-to-end)
        # re-record below, `path` becomes NEWER than all of them instead.
        older_fillers = []
        for i in range(50):
            filler = tmp_path / f"older{i:04d}.psd"
            filler.write_bytes(f"older {i}".encode())
            manager.record_own_write(filler)
            older_fillers.append(filler)

        # Re-record `path` while it is STILL present (51 entries total,
        # well under the 128 cap -- nothing has been evicted yet).
        path.write_bytes(b"run 2, more bytes")
        manager.record_own_write(path)

        # Enough NEWER entries to push the registry past its cap and force
        # eviction. If the re-record above had left `path` at its ORIGINAL
        # (oldest) position, it would be the very first thing evicted here;
        # since it correctly moved to the end, `older_fillers` (genuinely
        # untouched since their own single recording) are evicted first
        # instead, and `path` survives.
        for i in range(90):
            filler = tmp_path / f"newer{i:04d}.psd"
            filler.write_bytes(f"newer {i}".encode())
            manager.record_own_write(filler)

        stat = path.stat()
        assert manager.is_own_write(path, stat.st_size, stat.st_mtime_ns), (
            "re-recording an already-present path must refresh its position, "
            "not just its stat -- otherwise it can be evicted purely for "
            "having been FIRST recorded long ago"
        )
