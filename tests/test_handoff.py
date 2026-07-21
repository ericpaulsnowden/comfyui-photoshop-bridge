"""HandoffManager: lifecycle, ingest semantics, wait table, recovery, cleanup."""

from __future__ import annotations

import json
import threading
import time

import pytest
from PIL import Image

import cpsb.handoff as handoff_module
from cpsb.context import DEFAULT_MANAGED_FOLDER_NAME, CpsbContext
from cpsb.handoff import (
    DEFAULT_PSD_FILENAME,
    DEFAULT_TRIGGER_POLICY,
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
        stored = json.loads((context.cpsb_input_dir / meta.handoff_id / "meta.json").read_text())
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

    def test_legacy_meta_with_removed_mask_field_still_parses(self):
        """PROTOCOL.md §4: channel-mask extraction was removed, but a
        ``meta.json`` written by a pre-removal version of this package may
        still have a ``"mask"`` entry on an edit -- ``from_dict`` must
        tolerate that unknown/extra key rather than crashing on it.
        """
        legacy = {
            "handoff_id": "a1b2c3d4",
            "origin_node_id": "17",
            "origin_kind": "load_image",
            "workflow_name": "wf",
            "source": {"filename": "x.png", "subfolder": "", "type": "output"},
            "created_ts": 1.0,
            "updated_ts": 2.0,
            "status": "edited",
            "error": None,
            "edits": [
                {
                    "filename": "edit_001.png",
                    "ts": 3.0,
                    "fidelity": "composite",
                    "sibling_output": None,
                    "mask": {"filename": "mask_001.png"},
                }
            ],
        }
        meta = HandoffMeta.from_dict(legacy)
        assert len(meta.edits) == 1
        assert meta.edits[0].filename == "edit_001.png"
        assert not hasattr(meta.edits[0], "mask")
        # And it round-trips without resurrecting the removed field.
        assert "mask" not in meta.to_dict()["edits"][0]


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
        # Channel-mask extraction was removed (PROTOCOL.md §4): no "mask"
        # key at all, not even a null one.
        assert "mask" not in payload
        assert not hasattr(edit, "mask")

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

    def test_wait_unblocked_by_error(self, manager):
        """An open failure (mark_error) must stop the wait AT ONCE with ERROR,
        not spin until timeout -- the compose/annotate/bridge hang the plugin's
        open_failed used to cause (30-min timeout with no way to cancel)."""
        meta = create_handoff(manager)
        outcomes: list[str] = []

        def waiter() -> None:
            outcomes.append(manager.wait_for_edit(meta.handoff_id, 30, poll_interval=0.02))

        thread = threading.Thread(target=waiter)
        thread.start()
        time.sleep(0.1)
        start = time.monotonic()
        manager.mark_error(meta.handoff_id, "plugin open failed")
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert outcomes == [WaitOutcome.ERROR]
        assert time.monotonic() - start < 2.0  # returned promptly, not at timeout

    def test_wait_honors_native_interrupt(self, manager, monkeypatch):
        """ComfyUI's own 'Cancel current run' (processing_interrupted) breaks a
        blocking wait -- returned as CANCELLED so the node interrupts."""
        meta = create_handoff(manager)
        monkeypatch.setattr(handoff_module, "_processing_interrupted", lambda: True)
        start = time.monotonic()
        outcome = manager.wait_for_edit(meta.handoff_id, 30, poll_interval=0.02)
        assert outcome == WaitOutcome.CANCELLED
        assert time.monotonic() - start < 2.0

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
    """``note_source_written`` (the handoff-id convenience wrapper) and its
    underlying primitive, ``record_own_write``/``is_own_write`` (PATH-keyed,
    not handoff-id-keyed -- see ``HandoffManager.record_own_write``'s own
    docstring for why: it must suppress a write that lands on a path a
    DIFFERENT handoff, or no handoff at all, is watching -- not just the
    same handoff's own managed copy). ``tests/test_own_write_suppression.py``
    covers the cross-handoff and watcher-integration scenarios this design
    exists for; this class covers the manager-level API contract itself.
    """

    def test_note_source_written_still_works_as_a_wrapper(self, manager, context):
        meta = create_handoff(manager)
        psd_path = manager.psd_path(meta)
        psd_path.write_bytes(b"fake psd contents")
        manager.note_source_written(meta.handoff_id)

        stat = psd_path.stat()
        assert manager.is_own_write(psd_path, stat.st_size, stat.st_mtime_ns)

        psd_path.write_bytes(b"photoshop rewrote this with more bytes")
        new_stat = psd_path.stat()
        assert not manager.is_own_write(psd_path, new_stat.st_size, new_stat.st_mtime_ns)

    def test_note_source_written_is_a_noop_for_an_unknown_handoff(self, manager):
        manager.note_source_written("never-created")  # must not raise

    def test_record_own_write_needs_no_handoff_at_all(self, manager, tmp_path):
        """The primitive's whole point: a path with no handoff (e.g.
        PhotoshopComposePSD's fresh/append output before any handoff exists
        for it) can still be recorded and looked up directly.
        """
        path = tmp_path / "compose_00001.psd"
        path.write_bytes(b"composed layers")

        manager.record_own_write(path)

        stat = path.stat()
        assert manager.is_own_write(path, stat.st_size, stat.st_mtime_ns)
        assert not manager.is_own_write(path, stat.st_size + 1, stat.st_mtime_ns)

    def test_record_own_write_is_a_noop_for_a_missing_file(self, manager, tmp_path):
        """Matches the predecessor's own posture: nothing to stat, so silently skip."""
        manager.record_own_write(tmp_path / "never-written.psd")  # must not raise

    def test_re_recording_moves_a_path_to_the_end(self, manager, tmp_path):
        """Re-recording an already-present path must refresh its stat (the
        file may have changed) rather than being a stale no-op.
        """
        path = tmp_path / "target.psd"
        path.write_bytes(b"run 1")
        manager.record_own_write(path)

        path.write_bytes(b"run 2, more bytes")
        manager.record_own_write(path)

        stat = path.stat()
        assert manager.is_own_write(path, stat.st_size, stat.st_mtime_ns)


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


def load_psd_source(filename: str = "sample.psd") -> SourceRef:
    return SourceRef(filename=filename, subfolder="", type="input")


class TestEditInPlaceFields:
    """PROTOCOL.md §6b "Edit-original option": ``edit_in_place``/
    ``original_path`` on ``HandoffMeta``, and their default/legacy behavior.
    """

    def test_defaults_are_false_and_none(self, manager):
        meta = create_handoff(manager, origin_kind="load_psd", source=load_psd_source())
        assert meta.edit_in_place is False
        assert meta.original_path is None

    def test_create_records_edit_in_place_and_original_path(self, manager, context, tmp_path):
        original = tmp_path / "art.psd"
        original.write_bytes(b"not a real psd, just identity bytes")
        meta = manager.create(
            origin_node_id="1",
            origin_kind="load_psd",
            workflow_name="wf",
            source=load_psd_source(),
            original_image=make_image((1, 2, 3)),
            source_hash="deadbeef" * 8,
            edit_in_place=True,
            original_path=str(original.resolve()),
        )
        assert meta.edit_in_place is True
        assert meta.original_path == str(original.resolve())
        stored = json.loads((context.cpsb_input_dir / meta.handoff_id / "meta.json").read_text())
        assert stored["edit_in_place"] is True
        assert stored["original_path"] == str(original.resolve())
        # orig_thumb.png is still written -- the gallery keeps working even
        # though there is no managed PSD copy for this handoff.
        assert (context.cpsb_input_dir / meta.handoff_id / "orig_thumb.png").is_file()

    def test_to_dict_from_dict_round_trip(self, manager, tmp_path):
        original = tmp_path / "art.psd"
        meta = manager.create(
            origin_node_id="1",
            origin_kind="load_psd",
            workflow_name="wf",
            source=load_psd_source(),
            original_image=make_image((1, 2, 3)),
            source_hash="deadbeef" * 8,
            edit_in_place=True,
            original_path=str(original),
        )
        round_tripped = HandoffMeta.from_dict(meta.to_dict())
        assert round_tripped.edit_in_place is True
        assert round_tripped.original_path == str(original)

    def test_legacy_meta_without_the_new_fields_defaults_to_safe(self):
        """A meta.json written before this option existed must load as a
        non-in-place handoff (PROTOCOL.md §6b: "legacy metas without them
        default to non-in-place (safe)").
        """
        legacy = {
            "handoff_id": "a1b2c3d4",
            "origin_node_id": "1",
            "origin_kind": "load_psd",
            "workflow_name": "wf",
            "source": {"filename": "sample.psd", "subfolder": "", "type": "input"},
            "created_ts": 1.0,
            "updated_ts": 2.0,
            "status": "edited",
            "error": None,
            "edits": [],
        }
        meta = HandoffMeta.from_dict(legacy)
        assert meta.edit_in_place is False
        assert meta.original_path is None


class TestActiveEditInPlaceOriginals:
    def test_lists_only_active_edit_in_place_handoffs(self, manager, tmp_path):
        original = tmp_path / "art.psd"
        edit_in_place_meta = manager.create(
            origin_node_id="1",
            origin_kind="load_psd",
            workflow_name="",
            source=load_psd_source(),
            original_image=make_image((1, 1, 1)),
            source_hash="a" * 64,
            edit_in_place=True,
            original_path=str(original),
        )
        # A normal (copy) load_psd handoff -- must not appear.
        manager.create(
            origin_node_id="2",
            origin_kind="load_psd",
            workflow_name="",
            source=load_psd_source("other.psd"),
            original_image=make_image((2, 2, 2)),
            source_hash="b" * 64,
        )
        # A terminal edit_in_place handoff -- must not appear either.
        terminal = manager.create(
            origin_node_id="3",
            origin_kind="load_psd",
            workflow_name="",
            source=load_psd_source("gone.psd"),
            original_image=make_image((3, 3, 3)),
            source_hash="c" * 64,
            edit_in_place=True,
            original_path=str(tmp_path / "gone.psd"),
        )
        manager.mark_cancelled(terminal.handoff_id)

        result = manager.active_edit_in_place_originals()

        assert result == [(edit_in_place_meta.handoff_id, original.resolve())]

    def test_empty_when_none_active(self, manager):
        assert manager.active_edit_in_place_originals() == []


class TestEditInPlaceDeleteSafety:
    """PROTOCOL.md §6b (critical): cleanup must never delete a user's own
    file -- only managed-folder artifacts.
    """

    def test_cleanup_purges_managed_folder_but_preserves_original_file(
        self, manager, context, tmp_path
    ):
        original = tmp_path / "outside_comfy" / "art.psd"
        original.parent.mkdir(parents=True)
        original.write_bytes(b"the user's real, irreplaceable file")

        meta = manager.create(
            origin_node_id="1",
            origin_kind="load_psd",
            workflow_name="wf",
            source=load_psd_source(),
            original_image=make_image((1, 2, 3)),
            source_hash="deadbeef" * 8,
            edit_in_place=True,
            original_path=str(original.resolve()),
        )
        manager.ingest_edit(meta.handoff_id, make_image((9, 9, 9)), "composite")
        TestCleanup._age_meta(context, meta.handoff_id, days=20)

        rebooted = HandoffManager(context)

        assert rebooted.get(meta.handoff_id) is None
        assert not (context.cpsb_input_dir / meta.handoff_id).exists()  # managed folder gone
        assert original.is_file()  # the user's real file survives, untouched
        assert original.read_bytes() == b"the user's real, irreplaceable file"

    def test_reject_unsafe_delete_raises_when_target_is_the_original(
        self, manager, context, tmp_path
    ):
        original = tmp_path / "art.psd"
        original.write_bytes(b"x")
        meta = manager.create(
            origin_node_id="1",
            origin_kind="load_psd",
            workflow_name="wf",
            source=load_psd_source(),
            original_image=make_image((1, 2, 3)),
            source_hash="deadbeef" * 8,
            edit_in_place=True,
            original_path=str(original.resolve()),
        )
        stored = manager.get(meta.handoff_id)

        with pytest.raises(RuntimeError, match="Refusing to delete"):
            HandoffManager._reject_unsafe_delete(original.resolve(), stored)
        with pytest.raises(RuntimeError, match="Refusing to delete"):
            HandoffManager._reject_unsafe_delete(original.parent, stored)

    def test_reject_unsafe_delete_allows_the_managed_folder(self, manager, context, tmp_path):
        original = tmp_path / "art.psd"
        meta = manager.create(
            origin_node_id="1",
            origin_kind="load_psd",
            workflow_name="wf",
            source=load_psd_source(),
            original_image=make_image((1, 2, 3)),
            source_hash="deadbeef" * 8,
            edit_in_place=True,
            original_path=str(original.resolve()),
        )
        stored = manager.get(meta.handoff_id)

        HandoffManager._reject_unsafe_delete(context.cpsb_input_dir / meta.handoff_id, stored)
        HandoffManager._reject_unsafe_delete(context.cpsb_input_dir / meta.handoff_id, None)

    def test_reject_unsafe_delete_allows_non_edit_in_place_handoffs(self, manager, context):
        meta = create_handoff(manager)
        stored = manager.get(meta.handoff_id)
        assert stored.edit_in_place is False

        # Any target at all is fine for a handoff that never recorded an
        # edit_in_place original_path -- nothing to protect.
        HandoffManager._reject_unsafe_delete(context.input_dir / "anything", stored)


class TestTriggerPolicy:
    """Product-owner requirement 2026-07-18: a per-handoff save-trigger
    policy (``on_save`` on ``PhotoshopLoadPSD``, persisted as
    ``HandoffMeta.trigger_policy``) that governs whether an arriving edit is
    ingested/re-queues the workflow at all, enforced server-side via
    ``HandoffManager.should_ingest``.
    """

    def test_default_is_rerun(self, manager):
        """Every pre-existing caller that omits `trigger_policy` (the whole
        rest of this test file included) must keep getting today's exact
        behavior.
        """
        meta = create_handoff(manager)
        assert meta.trigger_policy == DEFAULT_TRIGGER_POLICY == "Re-run workflow"

    def test_create_records_a_custom_policy(self, manager, context):
        meta = create_handoff(manager, trigger_policy="Ignore (do nothing)")
        assert meta.trigger_policy == "Ignore (do nothing)"
        stored = json.loads((context.cpsb_input_dir / meta.handoff_id / "meta.json").read_text())
        assert stored["trigger_policy"] == "Ignore (do nothing)"

    def test_to_dict_from_dict_round_trip(self, manager):
        meta = create_handoff(manager, trigger_policy="Update only (don't re-run)")
        round_tripped = HandoffMeta.from_dict(meta.to_dict())
        assert round_tripped.trigger_policy == "Update only (don't re-run)"

    def test_legacy_meta_without_trigger_policy_key_defaults_to_rerun(self):
        """A ``meta.json`` written before this field existed (simulating an
        upgrade from an older version) has NO ``trigger_policy`` key at
        all -- it must load as :data:`DEFAULT_TRIGGER_POLICY`, not raise and
        not silently adopt some other value, so an already-in-flight handoff
        keeps behaving exactly as it always has.
        """
        legacy = {
            "handoff_id": "a1b2c3d4",
            "origin_node_id": "1",
            "origin_kind": "load_psd",
            "workflow_name": "wf",
            "source": {"filename": "sample.psd", "subfolder": "", "type": "input"},
            "created_ts": 1.0,
            "updated_ts": 2.0,
            "status": "editing",
            "error": None,
            "edits": [],
        }
        assert "trigger_policy" not in legacy
        meta = HandoffMeta.from_dict(legacy)
        assert meta.trigger_policy == DEFAULT_TRIGGER_POLICY
        # And it round-trips explicitly rather than staying missing.
        assert meta.to_dict()["trigger_policy"] == DEFAULT_TRIGGER_POLICY

    def test_legacy_meta_with_null_trigger_policy_defaults_to_rerun(self):
        """Defensive: a stray ``"trigger_policy": null`` (rather than an
        absent key) must default just as safely.
        """
        legacy = {
            "handoff_id": "a1b2c3d4",
            "origin_node_id": "1",
            "origin_kind": "load_psd",
            "workflow_name": "wf",
            "source": {"filename": "sample.psd", "subfolder": "", "type": "input"},
            "created_ts": 1.0,
            "updated_ts": 2.0,
            "status": "editing",
            "error": None,
            "trigger_policy": None,
            "edits": [],
        }
        meta = HandoffMeta.from_dict(legacy)
        assert meta.trigger_policy == DEFAULT_TRIGGER_POLICY


class TestShouldIngest:
    """``HandoffManager.should_ingest`` -- the single shared gate consulted
    at every ingest call site (the HTTP upload route, the plugin websocket's
    `upload_edit`, and the Tier 1 watcher's settled-save path).
    """

    def test_rerun_policy_ingests(self, manager):
        meta = create_handoff(manager, trigger_policy="Re-run workflow")
        assert manager.should_ingest(meta.handoff_id) is True

    def test_update_only_policy_ingests(self, manager):
        meta = create_handoff(manager, trigger_policy="Update only (don't re-run)")
        assert manager.should_ingest(meta.handoff_id) is True

    def test_ignore_policy_does_not_ingest(self, manager):
        meta = create_handoff(manager, trigger_policy="Ignore (do nothing)")
        assert manager.should_ingest(meta.handoff_id) is False

    def test_unknown_handoff_defaults_to_true(self, manager):
        """Existence/active-status is every caller's own job first --
        `should_ingest` only ever answers the policy question, so an unknown
        handoff must never be silently treated as "ignore."
        """
        assert manager.should_ingest("deadbeef") is True

    def test_ignore_policy_actually_suppresses_ingest_edit(self, manager):
        """End-to-end within this module: a caller that respects
        `should_ingest` (as every real ingest call site now does) never
        appends an edit for an Ignore-policy handoff, while the identical
        call for a Re-run-policy handoff does.
        """
        ignored = create_handoff(manager, trigger_policy="Ignore (do nothing)")
        rerun = create_handoff(manager, trigger_policy="Re-run workflow")

        for meta in (ignored, rerun):
            if manager.should_ingest(meta.handoff_id):
                manager.ingest_edit(meta.handoff_id, make_image((9, 9, 9)), "plugin")

        assert manager.get(ignored.handoff_id).edits == []
        assert manager.get(ignored.handoff_id).status == "pending"
        assert len(manager.get(rerun.handoff_id).edits) == 1
        assert manager.get(rerun.handoff_id).status == "edited"


class TestTriggerPolicyInUpdatedEvent:
    def test_cpsb_updated_carries_the_handoffs_trigger_policy(self, manager, events):
        meta = create_handoff(manager, trigger_policy="Update only (don't re-run)")
        manager.ingest_edit(meta.handoff_id, make_image((1, 2, 3)), "plugin")

        payload = events.of_type("cpsb.updated")[0]
        assert payload["trigger_policy"] == "Update only (don't re-run)"


def named_source(filename: str) -> SourceRef:
    return SourceRef(filename=filename, subfolder="", type="input")


class TestPsdFilename:
    """Product-owner requirement 2026-07-18: the managed PSD copy is named
    after its ORIGIN file (e.g. ``Eric-Headshot.jpg`` -> ``Eric-
    Headshot.psd``) instead of the literal ``source.psd`` every handoff used
    to get, so Photoshop's own document TITLE -- and any dropdown/gallery
    that lists handoffs by filename -- can actually tell them apart.
    """

    def test_create_derives_filename_from_source(self, manager):
        meta = create_handoff(manager, source=named_source("Eric-Headshot.jpg"))
        assert meta.psd_filename == "Eric-Headshot.psd"

    def test_psd_path_uses_the_derived_filename(self, manager, context):
        meta = create_handoff(manager, source=named_source("Eric-Headshot.jpg"))
        assert manager.psd_path(meta) == (
            context.cpsb_input_dir / meta.handoff_id / "Eric-Headshot.psd"
        )

    def test_persisted_in_meta_json(self, manager, context):
        meta = create_handoff(manager, source=named_source("Eric-Headshot.jpg"))
        stored = json.loads((context.cpsb_input_dir / meta.handoff_id / "meta.json").read_text())
        assert stored["psd_filename"] == "Eric-Headshot.psd"

    def test_to_dict_from_dict_round_trip(self, manager):
        meta = create_handoff(manager, source=named_source("Eric-Headshot.jpg"))
        round_tripped = HandoffMeta.from_dict(meta.to_dict())
        assert round_tripped.psd_filename == "Eric-Headshot.psd"

    def test_load_psd_origin_keeps_its_own_psd_name(self, manager):
        """A ``load_psd`` origin whose filename already ends in ``.psd`` with
        a clean stem derives to the EXACT SAME name -- so two Load PSD
        handoffs opened from different source files are distinguishable by
        name too, not just ``bridge_node``/``load_image`` origins.
        """
        meta = create_handoff(
            manager, origin_kind="load_psd", source=named_source("my-artwork.psd")
        )
        assert meta.psd_filename == "my-artwork.psd"

    def test_legacy_meta_without_the_field_defaults_to_source_psd(self):
        legacy = {
            "handoff_id": "a1b2c3d4",
            "origin_node_id": "17",
            "origin_kind": "load_image",
            "workflow_name": "wf",
            "source": {"filename": "Eric-Headshot.jpg", "subfolder": "", "type": "output"},
            "created_ts": 1.0,
            "updated_ts": 2.0,
            "status": "editing",
            "error": None,
            "edits": [],
        }
        assert "psd_filename" not in legacy
        meta = HandoffMeta.from_dict(legacy)
        assert meta.psd_filename == DEFAULT_PSD_FILENAME == "source.psd"
        # And it round-trips explicitly rather than staying missing.
        assert meta.to_dict()["psd_filename"] == DEFAULT_PSD_FILENAME

    def test_legacy_meta_with_null_psd_filename_defaults_to_source_psd(self):
        """Defensive: a stray ``"psd_filename": null`` (rather than an
        absent key) must default just as safely.
        """
        legacy = {
            "handoff_id": "a1b2c3d4",
            "origin_node_id": "17",
            "origin_kind": "load_image",
            "workflow_name": "wf",
            "source": {"filename": "Eric-Headshot.jpg", "subfolder": "", "type": "output"},
            "created_ts": 1.0,
            "updated_ts": 2.0,
            "status": "editing",
            "error": None,
            "psd_filename": None,
            "edits": [],
        }
        meta = HandoffMeta.from_dict(legacy)
        assert meta.psd_filename == DEFAULT_PSD_FILENAME


class TestDerivePsdFilenameSanitization:
    """Direct coverage of :func:`cpsb.handoff._derive_psd_filename` -- the
    sanitization rules themselves, independent of handoff creation.
    """

    def test_simple_name_passes_through(self):
        assert handoff_module._derive_psd_filename("Eric-Headshot.jpg") == "Eric-Headshot.psd"

    def test_underscores_and_digits_pass_through(self):
        assert handoff_module._derive_psd_filename("bridge_17.png") == "bridge_17.psd"

    def test_already_psd_extension_keeps_its_own_stem(self):
        assert handoff_module._derive_psd_filename("Eric-Headshot.psd") == "Eric-Headshot.psd"

    def test_disallowed_characters_become_a_single_dash(self):
        # "!!@@##" (6 disallowed chars in a row) collapses to ONE dash, not six.
        assert handoff_module._derive_psd_filename("weird!!@@##chars.png") == "weird-chars.psd"

    def test_isolated_disallowed_characters_become_isolated_dashes(self):
        # Non-adjacent invalid characters ("(" and ")" separated by "2") each
        # become their own dash -- no run to collapse -- and a resulting
        # TRAILING dash is trimmed off the end.
        assert handoff_module._derive_psd_filename("photo (2).png") == "photo -2.psd"

    def test_leading_and_trailing_whitespace_trimmed(self):
        assert (
            handoff_module._derive_psd_filename("  leading and trailing spaces  .png")
            == "leading and trailing spaces.psd"
        )

    def test_leading_and_trailing_dash_runs_trimmed(self):
        assert (
            handoff_module._derive_psd_filename("---dashes-only-boundary---.png")
            == "dashes-only-boundary.psd"
        )

    def test_caps_at_60_chars(self):
        long_stem = "a" * 200
        result = handoff_module._derive_psd_filename(f"{long_stem}.jpg")
        assert result == ("a" * 60) + ".psd"

    def test_capped_result_trims_a_dash_left_at_the_cut(self):
        """The cap can land exactly ON a dash (the 60th character); the
        implementation trims a SECOND time after capping so the final name
        never ends in a stray dash.
        """
        origin = "a" * 59 + "-" + "b" * 10 + ".jpg"  # dash sits at index 59
        result = handoff_module._derive_psd_filename(origin)
        assert result == "a" * 59 + ".psd"

    @pytest.mark.parametrize(
        "origin_filename",
        ["", "#.jpg", "-----.png", "     .png", ".", "..", "...", "...."],
    )
    def test_empty_or_degenerate_stem_falls_back_to_default(self, origin_filename):
        """Every one of these has a stem (per ``pathlib.Path.stem`` on the
        Python version this project actually runs on -- verified directly
        against the installed interpreter, not assumed from memory: pathlib's
        own suffix-parsing rules for multiple leading dots are NOT the same
        across Python versions, e.g. ``Path("..jpg").stem`` differs between
        3.10 and 3.14) that contains no letter or digit at all, so
        :func:`_derive_psd_filename` must fall back to the default rather
        than emit a punctuation-only filename.
        """
        assert handoff_module._derive_psd_filename(origin_filename) == DEFAULT_PSD_FILENAME

    def test_dotfile_with_real_letters_is_not_degenerate(self):
        """``".hidden.jpg"`` keeps its letters but LOSES the leading dot:
        leading dots are stripped after extension-splitting so a dotfile
        origin can never yield a HIDDEN managed file (a ``.name.psd`` in the
        remote plugin's sandbox would be invisible in Finder, and hidden
        from the very dropdowns this feature exists to improve).
        """
        assert handoff_module._derive_psd_filename(".hidden.jpg") == "hidden.psd"

    def test_dotfile_with_no_real_name_still_has_letters_so_is_not_degenerate(self):
        """``Path(".jpg").stem == ".jpg"`` (pathlib's own single-leading-dot
        convention: with no OTHER dot, nothing is parsed as a suffix at all)
        -- a leading dot is a dotfile marker, not an extension separator, and
        leading dots are stripped so a dotfile origin can never yield a
        HIDDEN managed file. The letters survive as the stem.

        These edge names originally asserted ``Path(...).stem``'s behavior,
        which CHANGED between Python 3.10 and 3.14 -- the derivation now
        splits the extension manually precisely so the persisted filename
        (matched by the watcher, recorded in meta.json) is identical on
        every interpreter a ComfyUI install might run.
        """
        assert handoff_module._derive_psd_filename(".jpg") == "jpg.psd"

    def test_multiple_leading_dots_collapse_to_degenerate(self):
        """``"..jpg"``: the last dot has something before it (a dot), so it
        IS the extension separator -- the stem is ``"."``, which strips to
        empty and falls back to the default. Deterministic on every
        interpreter (see the single-leading-dot case above for why the
        manual split exists).
        """
        assert handoff_module._derive_psd_filename("..jpg") == DEFAULT_PSD_FILENAME
