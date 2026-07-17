"""HandoffManager: lifecycle, ingest semantics, wait table, recovery, cleanup."""

from __future__ import annotations

import json
import threading
import time

import pytest
from PIL import Image

from cpsb.context import DEFAULT_MANAGED_FOLDER_NAME, CpsbContext
from cpsb.handoff import (
    TERMINAL_STATUSES,
    HandoffManager,
    HandoffMeta,
    HandoffNotFoundError,
    SourceRef,
    WaitOutcome,
    compute_source_hash,
)


def make_image(color: tuple[int, int, int], size: tuple[int, int] = (32, 24)) -> Image.Image:
    return Image.new("RGB", size, color)


def output_source(filename: str = "ComfyUI_00042_.png") -> SourceRef:
    return SourceRef(filename=filename, subfolder="", type="output")


@pytest.fixture
def manager(context: CpsbContext) -> HandoffManager:
    return HandoffManager(context)


def create_handoff(manager: HandoffManager, **overrides):
    kwargs = {
        "origin_node_id": "17",
        "origin_kind": "load_image",
        "workflow_name": "wf",
        "source": output_source(),
        "original_image": make_image((10, 20, 30)),
    }
    kwargs.update(overrides)
    return manager.create(**kwargs)


class TestCreate:
    def test_creates_folder_meta_and_thumbnail(self, manager, context):
        meta = create_handoff(manager)
        folder = context.cpsb_input_dir / meta.handoff_id
        assert folder.is_dir()
        assert (folder / "meta.json").is_file()
        assert (folder / "orig_thumb.png").is_file()
        assert len(meta.handoff_id) == 8
        assert meta.status == "pending"
        stored = json.loads((folder / "meta.json").read_text())
        assert stored["handoff_id"] == meta.handoff_id
        assert stored["source"] == {
            "filename": "ComfyUI_00042_.png",
            "subfolder": "",
            "type": "output",
        }

    def test_thumbnail_capped_at_256(self, manager, context):
        meta = create_handoff(manager, original_image=make_image((1, 2, 3), size=(1024, 512)))
        with Image.open(context.cpsb_input_dir / meta.handoff_id / "orig_thumb.png") as thumb:
            assert max(thumb.size) <= 256

    def test_source_hash_recorded_and_persisted(self, manager, context):
        original = make_image((10, 20, 30))
        meta = create_handoff(manager, original_image=original)
        assert meta.source_hash == compute_source_hash(original)
        assert meta.source_hash != compute_source_hash(make_image((11, 20, 30)))
        stored = json.loads(
            (context.cpsb_input_dir / meta.handoff_id / "meta.json").read_text()
        )
        assert stored["source_hash"] == meta.source_hash

    def test_legacy_meta_without_source_hash_loads_as_none(self):
        legacy = {
            "handoff_id": "a1b2c3d4",
            "origin_node_id": "17",
            "origin_kind": "load_image",
            "workflow_name": "wf",
            "source": {"filename": "x.png", "subfolder": "", "type": "output"},
            "created_ts": 1.0,
            "updated_ts": 2.0,
            "status": "editing",
            "error": None,
            "edits": [],
        }
        meta = HandoffMeta.from_dict(legacy)
        assert meta.source_hash is None
        # And it round-trips explicitly (not silently re-invented).
        assert meta.to_dict()["source_hash"] is None


class TestLifecycle:
    def test_create_edit_ingest_flow(self, manager, context, events):
        meta = create_handoff(manager)
        manager.mark_editing(meta.handoff_id)
        assert manager.get(meta.handoff_id).status == "editing"

        edit = manager.ingest_edit(meta.handoff_id, make_image((99, 0, 0)), "composite")
        assert edit is not None
        assert edit.filename == "edit_001.png"
        assert edit.fidelity == "composite"

        refreshed = manager.get(meta.handoff_id)
        assert refreshed.status == "edited"
        assert len(refreshed.edits) == 1
        assert (context.cpsb_input_dir / meta.handoff_id / "edit_001.png").is_file()

        updated_events = events.of_type("cpsb.updated")
        assert len(updated_events) == 1
        payload = updated_events[0]
        assert payload["handoff_id"] == meta.handoff_id
        assert payload["origin_node_id"] == "17"
        assert payload["origin_kind"] == "load_image"
        assert payload["filename"] == "edit_001.png"
        assert payload["subfolder"] == f"{DEFAULT_MANAGED_FOLDER_NAME}/{meta.handoff_id}"
        assert payload["type"] == "input"
        assert payload["fidelity"] == "composite"
        assert payload["sibling_output"] is None

    def test_status_events_emitted_on_transitions(self, manager, events):
        meta = create_handoff(manager)
        manager.mark_editing(meta.handoff_id)
        statuses = [p["status"] for p in events.of_type("cpsb.status")]
        assert statuses == ["editing"]

    def test_second_edit_appends(self, manager):
        meta = create_handoff(manager)
        manager.ingest_edit(meta.handoff_id, make_image((1, 0, 0)), "composite")
        edit2 = manager.ingest_edit(meta.handoff_id, make_image((2, 0, 0)), "recomposite")
        assert edit2.filename == "edit_002.png"
        refreshed = manager.get(meta.handoff_id)
        assert [e.filename for e in refreshed.edits] == ["edit_001.png", "edit_002.png"]
        assert refreshed.status == "edited"

    def test_transitions_unknown_id_raise(self, manager):
        with pytest.raises(HandoffNotFoundError):
            manager.mark_editing("deadbeef")
        with pytest.raises(HandoffNotFoundError):
            manager.mark_cancelled("deadbeef")

    def test_ingest_on_cancelled_handoff_is_noop(self, manager):
        meta = create_handoff(manager)
        manager.mark_cancelled(meta.handoff_id)
        assert manager.ingest_edit(meta.handoff_id, make_image((5, 5, 5)), "plugin") is None
        assert manager.get(meta.handoff_id).status == "cancelled"

    def test_ingest_unknown_handoff_is_noop(self, manager):
        assert manager.ingest_edit("deadbeef", make_image((5, 5, 5)), "plugin") is None


class TestDedup:
    def test_identical_edit_skipped(self, manager, events):
        meta = create_handoff(manager)
        first = manager.ingest_edit(meta.handoff_id, make_image((50, 60, 70)), "composite")
        duplicate = manager.ingest_edit(meta.handoff_id, make_image((50, 60, 70)), "plugin")
        assert first is not None
        assert duplicate is None
        assert len(manager.get(meta.handoff_id).edits) == 1
        assert len(events.of_type("cpsb.updated")) == 1

    def test_different_edit_after_duplicate_ingests(self, manager):
        meta = create_handoff(manager)
        manager.ingest_edit(meta.handoff_id, make_image((50, 60, 70)), "composite")
        manager.ingest_edit(meta.handoff_id, make_image((50, 60, 70)), "composite")
        third = manager.ingest_edit(meta.handoff_id, make_image((51, 60, 70)), "composite")
        assert third is not None
        assert third.filename == "edit_002.png"


class TestSiblingOutputs:
    def test_terminal_output_writes_sibling(self, manager, context):
        meta = create_handoff(manager, origin_kind="terminal_output")
        edit = manager.ingest_edit(meta.handoff_id, make_image((9, 9, 9)), "composite")
        assert edit.sibling_output is not None
        assert edit.sibling_output.filename == "ComfyUI_00042__ps1.png"
        assert edit.sibling_output.subfolder == ""
        assert (context.output_dir / "ComfyUI_00042__ps1.png").is_file()

    def test_sibling_index_increments_per_round_trip(self, manager, context):
        meta = create_handoff(manager, origin_kind="terminal_output")
        manager.ingest_edit(meta.handoff_id, make_image((9, 9, 9)), "composite")
        edit2 = manager.ingest_edit(meta.handoff_id, make_image((8, 8, 8)), "composite")
        assert edit2.sibling_output.filename == "ComfyUI_00042__ps2.png"
        assert (context.output_dir / "ComfyUI_00042__ps2.png").is_file()

    def test_sibling_respects_subfolder(self, manager, context):
        meta = create_handoff(
            manager,
            origin_kind="terminal_output",
            source=SourceRef(filename="img.png", subfolder="batch1", type="output"),
        )
        edit = manager.ingest_edit(meta.handoff_id, make_image((9, 9, 9)), "composite")
        assert edit.sibling_output.subfolder == "batch1"
        assert (context.output_dir / "batch1" / "img_ps1.png").is_file()

    def test_no_sibling_for_load_image_origin(self, manager, context):
        meta = create_handoff(manager, origin_kind="load_image")
        edit = manager.ingest_edit(meta.handoff_id, make_image((9, 9, 9)), "composite")
        assert edit.sibling_output is None

    def test_no_sibling_for_non_output_source(self, manager):
        meta = create_handoff(
            manager,
            origin_kind="terminal_output",
            source=SourceRef(filename="img.png", subfolder="", type="temp"),
        )
        edit = manager.ingest_edit(meta.handoff_id, make_image((9, 9, 9)), "composite")
        assert edit.sibling_output is None

    def test_no_sibling_when_setting_disabled(self, manager, context):
        context.settings.update({"sibling_outputs": False})
        meta = create_handoff(manager, origin_kind="terminal_output")
        edit = manager.ingest_edit(meta.handoff_id, make_image((9, 9, 9)), "composite")
        assert edit.sibling_output is None


class TestSupersede:
    def test_supersede_frees_the_node_for_a_new_handoff(self, manager):
        first = create_handoff(manager)
        assert manager.find_active_for_node("17").handoff_id == first.handoff_id
        manager.supersede(first.handoff_id)
        assert manager.get(first.handoff_id).status == "superseded"
        assert manager.find_active_for_node("17") is None
        second = create_handoff(manager)
        assert manager.find_active_for_node("17").handoff_id == second.handoff_id

    def test_superseded_rejects_further_ingest(self, manager):
        meta = create_handoff(manager)
        manager.supersede(meta.handoff_id)
        assert manager.ingest_edit(meta.handoff_id, make_image((1, 1, 1)), "plugin") is None


class TestFindActive:
    def test_newest_active_wins(self, manager):
        create_handoff(manager)
        time.sleep(0.01)
        second = create_handoff(manager, origin_kind="terminal_output")
        active = manager.find_active_for_node("17")
        assert active.handoff_id == second.handoff_id

    def test_other_nodes_are_invisible(self, manager):
        create_handoff(manager, origin_node_id="99")
        assert manager.find_active_for_node("17") is None


class TestWorkflowScoping:
    """Node ids are only unique per workflow; lookups must not cross-match."""

    def test_different_workflow_does_not_match(self, manager):
        create_handoff(manager, workflow_name="workflow-a")
        assert manager.find_active_for_node("17", "workflow-b") is None
        assert manager.find_active_for_node("17", "workflow-a") is not None

    def test_empty_request_name_is_wildcard(self, manager):
        meta = create_handoff(manager, workflow_name="workflow-a")
        assert manager.find_active_for_node("17", "").handoff_id == meta.handoff_id
        assert manager.find_active_for_node("17").handoff_id == meta.handoff_id

    def test_empty_stored_name_is_wildcard(self, manager):
        meta = create_handoff(manager, workflow_name="")
        assert manager.find_active_for_node("17", "workflow-b").handoff_id == meta.handoff_id

    def test_same_node_id_coexists_across_workflows(self, manager):
        first = create_handoff(manager, workflow_name="workflow-a")
        time.sleep(0.01)
        second = create_handoff(manager, workflow_name="workflow-b")
        assert manager.find_active_for_node("17", "workflow-a").handoff_id == first.handoff_id
        assert manager.find_active_for_node("17", "workflow-b").handoff_id == second.handoff_id


class TestManagedFolderSwitch:
    """A handoff keeps resolving to the folder it was created under even
    after ``managed_folder_name`` changes underneath it, without a restart
    (PROTOCOL.md §1: ``managed_dir`` is recorded per handoff precisely so
    this holds).
    """

    def test_handoff_dir_survives_a_setting_switch(self, context, manager):
        context.settings.update({"managed_folder_name": "folder-a"})
        meta = create_handoff(manager)
        assert meta.managed_dir == "folder-a"
        original_dir = manager.handoff_dir(meta.handoff_id)
        assert original_dir == context.input_dir / "folder-a" / meta.handoff_id
        assert original_dir.is_dir()

        context.settings.update({"managed_folder_name": "folder-b"})
        assert context.managed_folder_name == "folder-b"
        # The EXISTING handoff still resolves under folder-a, not folder-b.
        assert manager.handoff_dir(meta.handoff_id) == original_dir
        assert manager.handoff_dir(meta.handoff_id).is_dir()

    def test_emitted_subfolder_survives_a_setting_switch(self, context, manager, events):
        context.settings.update({"managed_folder_name": "folder-a"})
        meta = create_handoff(manager)

        context.settings.update({"managed_folder_name": "folder-b"})
        edit = manager.ingest_edit(meta.handoff_id, make_image((9, 9, 9)), "plugin")
        assert edit is not None

        payload = events.of_type("cpsb.updated")[-1]
        assert payload["subfolder"] == f"folder-a/{meta.handoff_id}"
        # The edit file itself was written into folder-a, not folder-b.
        assert (context.input_dir / "folder-a" / meta.handoff_id / edit.filename).is_file()
        assert not (context.input_dir / "folder-b").exists()

    def test_meta_json_survives_a_setting_switch(self, context, manager):
        context.settings.update({"managed_folder_name": "folder-a"})
        meta = create_handoff(manager)

        context.settings.update({"managed_folder_name": "folder-b"})
        manager.mark_editing(meta.handoff_id)  # a second write, post-switch

        assert (context.input_dir / "folder-a" / meta.handoff_id / "meta.json").is_file()
        assert manager.get(meta.handoff_id).status == "editing"

    def test_new_handoff_after_switch_uses_the_new_folder(self, context, manager):
        context.settings.update({"managed_folder_name": "folder-a"})
        create_handoff(manager)

        context.settings.update({"managed_folder_name": "folder-b"})
        second = create_handoff(manager, origin_node_id="99")
        assert second.managed_dir == "folder-b"
        assert manager.handoff_dir(second.handoff_id) == (
            context.input_dir / "folder-b" / second.handoff_id
        )


class TestScanOnBoot:
    def test_state_recovered_from_meta_files(self, context):
        first_manager = HandoffManager(context)
        meta = first_manager.create(
            origin_node_id="17",
            origin_kind="load_image",
            workflow_name="wf",
            source=output_source(),
            original_image=make_image((10, 20, 30)),
        )
        first_manager.mark_editing(meta.handoff_id)
        first_manager.ingest_edit(meta.handoff_id, make_image((1, 2, 3)), "composite")

        rebooted = HandoffManager(context)
        recovered = rebooted.get(meta.handoff_id)
        assert recovered is not None
        assert recovered.status == "edited"
        assert [e.filename for e in recovered.edits] == ["edit_001.png"]
        assert rebooted.find_active_for_node("17").handoff_id == meta.handoff_id

    def test_unreadable_meta_skipped(self, context):
        broken_dir = context.cpsb_input_dir / "deadbeef"
        broken_dir.mkdir(parents=True)
        (broken_dir / "meta.json").write_text("{not json", encoding="utf-8")
        manager = HandoffManager(context)
        assert manager.get("deadbeef") is None

    def test_boot_scan_only_finds_handoffs_under_the_current_setting(self, context):
        """PROTOCOL.md §1: changing ``managed_folder_name`` takes effect at
        the next server start for NEW handoffs; a handoff already living
        under the previous name "stays where it is" rather than being
        rediscovered -- the boot scan globs only the CURRENT setting's
        folder.
        """
        context.settings.update({"managed_folder_name": "folder-a"})
        first_manager = HandoffManager(context)
        meta = first_manager.create(
            origin_node_id="17",
            origin_kind="load_image",
            workflow_name="wf",
            source=output_source(),
            original_image=make_image((10, 20, 30)),
        )

        context.settings.update({"managed_folder_name": "folder-b"})
        rebooted = HandoffManager(context)

        assert rebooted.get(meta.handoff_id) is None
        assert rebooted.find_active_for_node("17") is None
        # Untouched on disk -- not purged, just not rescanned into memory.
        assert (context.input_dir / "folder-a" / meta.handoff_id / "meta.json").is_file()

    def test_boot_scan_finds_handoffs_when_setting_reverted(self, context):
        """The corollary of the above: switching BACK to the folder a
        handoff was created under makes it discoverable again, since the
        scan is purely a function of the current setting at boot time.
        """
        context.settings.update({"managed_folder_name": "folder-a"})
        first_manager = HandoffManager(context)
        meta = first_manager.create(
            origin_node_id="17",
            origin_kind="load_image",
            workflow_name="wf",
            source=output_source(),
            original_image=make_image((10, 20, 30)),
        )

        context.settings.update({"managed_folder_name": "folder-b"})
        HandoffManager(context)  # a reboot under folder-b, handoff not seen

        context.settings.update({"managed_folder_name": "folder-a"})
        rebooted_again = HandoffManager(context)
        assert rebooted_again.get(meta.handoff_id) is not None


class TestCleanup:
    @staticmethod
    def _age_meta(context: CpsbContext, handoff_id: str, days: float) -> None:
        meta_path = context.cpsb_input_dir / handoff_id / "meta.json"
        data = json.loads(meta_path.read_text())
        data["updated_ts"] = time.time() - days * 86400
        meta_path.write_text(json.dumps(data))

    def test_old_terminal_handoffs_purged_at_boot(self, context):
        manager = HandoffManager(context)
        meta = manager.create(
            origin_node_id="17",
            origin_kind="load_image",
            workflow_name="wf",
            source=output_source(),
            original_image=make_image((10, 20, 30)),
        )
        manager.ingest_edit(meta.handoff_id, make_image((1, 2, 3)), "composite")
        self._age_meta(context, meta.handoff_id, days=20)

        rebooted = HandoffManager(context)
        assert rebooted.get(meta.handoff_id) is None
        assert not (context.cpsb_input_dir / meta.handoff_id).exists()

    def test_pending_and_editing_never_purged(self, context):
        manager = HandoffManager(context)
        pending = manager.create(
            origin_node_id="17",
            origin_kind="load_image",
            workflow_name="wf",
            source=output_source(),
            original_image=make_image((10, 20, 30)),
        )
        self._age_meta(context, pending.handoff_id, days=90)

        rebooted = HandoffManager(context)
        assert rebooted.get(pending.handoff_id) is not None

    def test_recent_terminal_handoffs_kept(self, context):
        manager = HandoffManager(context)
        meta = manager.create(
            origin_node_id="17",
            origin_kind="load_image",
            workflow_name="wf",
            source=output_source(),
            original_image=make_image((10, 20, 30)),
        )
        manager.ingest_edit(meta.handoff_id, make_image((1, 2, 3)), "composite")
        self._age_meta(context, meta.handoff_id, days=2)

        rebooted = HandoffManager(context)
        assert rebooted.get(meta.handoff_id) is not None

    def test_cleanup_days_setting_respected(self, context):
        context.settings.update({"cleanup_days": 1})
        manager = HandoffManager(context)
        meta = manager.create(
            origin_node_id="17",
            origin_kind="load_image",
            workflow_name="wf",
            source=output_source(),
            original_image=make_image((10, 20, 30)),
        )
        manager.ingest_edit(meta.handoff_id, make_image((1, 2, 3)), "composite")
        self._age_meta(context, meta.handoff_id, days=3)

        rebooted = HandoffManager(context)
        assert rebooted.get(meta.handoff_id) is None


class TestWaitTable:
    def test_wait_unblocked_by_ingest(self, manager):
        meta = create_handoff(manager)
        outcomes: list[str] = []

        def waiter() -> None:
            outcomes.append(manager.wait_for_edit(meta.handoff_id, 10, poll_interval=0.02))

        thread = threading.Thread(target=waiter)
        thread.start()
        time.sleep(0.1)
        manager.ingest_edit(meta.handoff_id, make_image((3, 3, 3)), "plugin")
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert outcomes == [WaitOutcome.EDITED]

    def test_wait_unblocked_by_cancel(self, manager):
        meta = create_handoff(manager)
        outcomes: list[str] = []

        def waiter() -> None:
            outcomes.append(manager.wait_for_edit(meta.handoff_id, 10, poll_interval=0.02))

        thread = threading.Thread(target=waiter)
        thread.start()
        time.sleep(0.1)
        manager.mark_cancelled(meta.handoff_id)
        thread.join(timeout=5)
        assert outcomes == [WaitOutcome.CANCELLED]

    def test_wait_times_out(self, manager):
        meta = create_handoff(manager)
        start = time.monotonic()
        outcome = manager.wait_for_edit(meta.handoff_id, 0.2, poll_interval=0.02)
        assert outcome == WaitOutcome.TIMEOUT
        assert time.monotonic() - start >= 0.2

    def test_edit_landing_before_wait_registers_still_unblocks(self, manager):
        """The open-vs-wait race: an edit arriving before the waiter exists.

        Photoshop is opened before wait_for_edit() runs; a save ingested in
        that window flips no waiter (none registered yet). The wait must
        still return EDITED immediately via the baseline-edits check rather
        than polling to timeout with the edit sitting on disk.
        """
        meta = create_handoff(manager)
        manager.ingest_edit(meta.handoff_id, make_image((7, 7, 7)), "plugin")

        start = time.monotonic()
        outcome = manager.wait_for_edit(meta.handoff_id, 30, poll_interval=0.02)
        assert outcome == WaitOutcome.EDITED
        assert time.monotonic() - start < 1.0

    def test_edit_hash_tracks_latest_edit(self, manager):
        meta = create_handoff(manager)
        assert manager.latest_edit_hash(meta.handoff_id) is None
        manager.ingest_edit(meta.handoff_id, make_image((1, 1, 1)), "composite")
        first_hash = manager.latest_edit_hash(meta.handoff_id)
        manager.ingest_edit(meta.handoff_id, make_image((2, 2, 2)), "composite")
        second_hash = manager.latest_edit_hash(meta.handoff_id)
        assert first_hash is not None and second_hash is not None
        assert first_hash != second_hash


class TestOwnSourceWrites:
    def test_signature_matches_until_file_changes(self, manager, context):
        meta = create_handoff(manager)
        psd_path = manager.handoff_dir(meta.handoff_id) / "source.psd"
        psd_path.write_bytes(b"fake psd contents")
        manager.note_source_written(meta.handoff_id)

        stat = psd_path.stat()
        assert manager.is_own_source_write(meta.handoff_id, stat.st_size, stat.st_mtime_ns)

        psd_path.write_bytes(b"photoshop rewrote this with more bytes")
        new_stat = psd_path.stat()
        assert not manager.is_own_source_write(
            meta.handoff_id, new_stat.st_size, new_stat.st_mtime_ns
        )


class TestIdempotentCancel:
    """mark_cancelled must be safe to mash (PROTOCOL.md §2): calling it again
    on an already-terminal handoff is a pure no-op, not a re-transition.
    """

    @pytest.mark.parametrize("terminal_status", sorted(TERMINAL_STATUSES))
    def test_cancel_on_terminal_handoff_is_a_noop(self, manager, events, terminal_status):
        meta = create_handoff(manager)
        if terminal_status == "error":
            manager.mark_error(meta.handoff_id, "boom")
        elif terminal_status == "cancelled":
            manager.mark_cancelled(meta.handoff_id)
        elif terminal_status == "discarded":
            manager.mark_discarded(meta.handoff_id)
        elif terminal_status == "superseded":
            manager.supersede(meta.handoff_id)
        before = manager.get(meta.handoff_id)
        assert before.status == terminal_status
        status_events_before = len(events.of_type("cpsb.status"))

        result = manager.mark_cancelled(meta.handoff_id)

        assert result.status == terminal_status  # unchanged, NOT "cancelled"
        assert result.error == before.error
        assert result.updated_ts == before.updated_ts  # no bump
        after = manager.get(meta.handoff_id)
        assert after.status == terminal_status
        assert after.updated_ts == before.updated_ts
        # No duplicate cpsb.status event for the no-op.
        assert len(events.of_type("cpsb.status")) == status_events_before

    def test_cancel_on_pending_handoff_transitions_normally(self, manager, events):
        """Sanity check that the idempotency guard doesn't over-fire: a
        genuinely active (non-terminal) handoff still transitions.
        """
        meta = create_handoff(manager)
        assert meta.status == "pending"

        result = manager.mark_cancelled(meta.handoff_id)

        assert result.status == "cancelled"
        assert manager.get(meta.handoff_id).status == "cancelled"
        assert [p["status"] for p in events.of_type("cpsb.status")] == ["cancelled"]

    def test_double_cancel_emits_exactly_one_status_event(self, manager, events):
        meta = create_handoff(manager)
        manager.mark_cancelled(meta.handoff_id)
        manager.mark_cancelled(meta.handoff_id)
        manager.mark_cancelled(meta.handoff_id)

        assert [p["status"] for p in events.of_type("cpsb.status")] == ["cancelled"]
        assert manager.get(meta.handoff_id).status == "cancelled"

    def test_cancel_unknown_id_still_raises(self, manager):
        with pytest.raises(HandoffNotFoundError):
            manager.mark_cancelled("deadbeef")

    def test_cancel_on_terminal_handoff_does_not_rewrite_meta_json(self, manager, context):
        meta = create_handoff(manager)
        manager.mark_cancelled(meta.handoff_id)
        meta_path = context.cpsb_input_dir / meta.handoff_id / "meta.json"
        written_before = meta_path.read_text()

        manager.mark_cancelled(meta.handoff_id)

        assert meta_path.read_text() == written_before

    def test_tier1_fallback_recovery_from_error_is_not_blocked(self, manager):
        """The idempotency guard is scoped to mark_cancelled only: a handoff
        in 'error' must still be able to move to 'editing' (the Tier 2
        open_failed -> Tier 1 fallback-succeeds path, PROTOCOL.md §3) --
        this is NOT the same kind of no-op as cancel-on-terminal.
        """
        meta = create_handoff(manager)
        manager.mark_error(meta.handoff_id, "plugin open failed")
        assert manager.get(meta.handoff_id).status == "error"

        result = manager.mark_editing(meta.handoff_id)

        assert result.status == "editing"
        assert manager.get(meta.handoff_id).status == "editing"
