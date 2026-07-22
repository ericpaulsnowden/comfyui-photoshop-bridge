"""PhotoshopLoadPSD node: torch-free import, listing, contract shape,
VALIDATE_INPUTS, IS_CHANGED, and the flatten/mask/consume execute() paths.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest
from PIL import Image
from psd_tools import PSDImage

import cpsb.load_psd as load_psd_module
import cpsb.nodes as nodes_module
from cpsb.context import CpsbContext
from cpsb.handoff import HandoffManager, SourceRef
from cpsb.psd_io import write_psd


@pytest.fixture
def manager(context: CpsbContext) -> HandoffManager:
    return HandoffManager(context)


@pytest.fixture
def configured(context: CpsbContext, manager: HandoffManager):
    """Wire nodes.configure -- cpsb.load_psd reuses this exact same shared state."""
    nodes_module.configure(context, manager, cast("object", None), cast("object", None))
    yield
    nodes_module._state = None


def write_test_psd(
    path: Path, color: tuple[int, int, int] = (10, 20, 30), size: tuple[int, int] = (16, 16)
) -> None:
    write_psd(path, Image.new("RGB", size, color))


def write_test_cmyk_psd(
    path: Path, standard_cmyk: tuple[int, int, int, int], size: tuple[int, int] = (16, 16)
) -> None:
    """A native CMYK ``.psd`` whose on-disk bytes match real Photoshop's.

    Mirrors ``tests/test_psd_io.py``'s ``write_photoshop_convention_cmyk_psd``
    (see its docstring for the full "why"): ``PSDImage.frompil()`` writes raw
    PIL channel bytes with no CMYK inversion of its own, so *standard_cmyk*
    (Pillow ink-direct convention) is manually pre-inverted here before
    writing, landing on bytes shaped like a genuine Photoshop save
    (``255 * (1 - ink)``). Used to exercise this node's actual ``execute()``
    IMAGE-output path end-to-end for the "CMYK PSD loads as solid black"
    regression, not just ``cpsb.psd_io`` directly.
    """
    standard = Image.new("CMYK", size, standard_cmyk)
    photoshop_convention = Image.eval(standard, lambda value: 255 - value)
    PSDImage.frompil(photoshop_convention).save(path)


class TestImportability:
    def test_module_imports_without_torch(self):
        """Importing ``cpsb.load_psd`` alone must not pull in torch.

        Checked in an isolated subprocess for the same reason
        ``test_nodes.py``'s equivalent check is: independent of whichever
        other test file happens to import torch first in this session.
        """
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import cpsb.load_psd as m, sys\n"
                "assert m.PhotoshopLoadPSD is not None\n"
                "print('torch' in sys.modules)",
            ],
            cwd=str(Path(__file__).resolve().parent.parent),
            capture_output=True,
            text=True,
            check=True,
        )
        assert result.stdout.strip() == "False", result.stderr


class TestContractShape:
    def test_node_attributes(self):
        node = load_psd_module.PhotoshopLoadPSD
        assert node.CATEGORY == "image/photoshop"
        assert node.RETURN_TYPES == ("IMAGE", "MASK")
        assert node.FUNCTION == "execute"

    def test_input_types_hidden_shape(self, configured):
        spec = load_psd_module.PhotoshopLoadPSD.INPUT_TYPES()
        assert spec["hidden"] == {"unique_id": "UNIQUE_ID"}
        assert "psd" in spec["required"]
        # No `image_upload` option: that flag is what the frontend's own
        # image-upload-widget detection keys off (PROTOCOL.md §6b / this
        # module's docstring) -- carrying it here would make ComfyUI try to
        # attach its stock png/jpeg/webp-only upload widget.
        combo_options = spec["required"]["psd"][1] if len(spec["required"]["psd"]) > 1 else {}
        assert "image_upload" not in combo_options

    def test_input_types_unconfigured_returns_empty_combo(self):
        assert nodes_module._state is None
        spec = load_psd_module.PhotoshopLoadPSD.INPUT_TYPES()
        assert spec["required"]["psd"] == ([],)

    def test_input_types_declares_edit_original_boolean_default_false(self, configured):
        """PROTOCOL.md §6b "Edit-original option": a BOOLEAN widget,
        default False (the safe, non-destructive copy behavior).
        """
        spec = load_psd_module.PhotoshopLoadPSD.INPUT_TYPES()
        assert spec["required"]["edit_original"] == ("BOOLEAN", {"default": False})

    def test_on_save_declares_combo_defaulting_to_rerun(self, configured):
        """Product-owner requirement 2026-07-18: `on_save` is a COMBO of the
        three OnSaveMode strings, defaulting to RERUN -- today's exact
        pre-existing behavior for every already-saved workflow.
        """
        spec = load_psd_module.PhotoshopLoadPSD.INPUT_TYPES()
        assert spec["required"]["on_save"] == (
            [
                load_psd_module.OnSaveMode.RERUN,
                load_psd_module.OnSaveMode.UPDATE_ONLY,
                load_psd_module.OnSaveMode.IGNORE,
            ],
            {"default": load_psd_module.OnSaveMode.RERUN},
        )
        assert load_psd_module.OnSaveMode.RERUN == "Re-run workflow"

    def test_policy_strings_are_identical_across_all_four_layers(self):
        """DRIFT GUARD. The `on_save`/`trigger_policy` value is a bare string
        compared for equality in four independently-maintained places:

          1. cpsb/load_psd.py       OnSaveMode           (the widget's options)
          2. cpsb/handoff.py        TriggerPolicy        (persisted + the gate)
          3. cpsb/routes.py         _VALID_TRIGGER_POLICIES (request validation)
          4. web/cpsb/pasteback.js  LOAD_PSD_TRIGGER_*   (the auto-queue gate)

        They cannot import each other (2 would be a circular import, 4 is a
        different language), so nothing but this test stops one from being
        reworded in isolation. The failure mode is SILENT and nasty: a policy
        that simply stops being honored, or an open request rejected with a 400,
        with no type error anywhere to catch it. Assert the actual strings, and
        read the JS as text so the frontend is covered too.
        """
        from cpsb import handoff as handoff_module
        from cpsb import routes as routes_module

        rerun = load_psd_module.OnSaveMode.RERUN
        update_only = load_psd_module.OnSaveMode.UPDATE_ONLY
        ignore = load_psd_module.OnSaveMode.IGNORE

        assert rerun == handoff_module.DEFAULT_TRIGGER_POLICY
        assert ignore == handoff_module._IGNORE_TRIGGER_POLICY
        assert set(routes_module._VALID_TRIGGER_POLICIES) == {rerun, update_only, ignore}

        pasteback = (
            Path(__file__).resolve().parents[1] / "web" / "cpsb" / "pasteback.js"
        ).read_text(encoding="utf-8")
        # Only the two the frontend actually gates on; RERUN is the fall-through.
        assert update_only in pasteback
        assert ignore in pasteback

    def test_on_save_is_the_last_required_input(self, configured):
        """Critical for backward compatibility: ComfyUI's frontend restores
        a saved workflow's serialized widget values BY POSITION (index into
        `widgets_values`, zipped against `node.widgets` in INPUT_TYPES
        declaration order), not by name. Appending `on_save` last means a
        workflow saved before this change (whose `widgets_values` only has
        two entries) never touches this widget's slot at all, leaving it at
        its own default -- inserting it anywhere else would instead shift
        every already-serialized value after that point onto the wrong
        widget for every existing saved workflow.
        """
        spec = load_psd_module.PhotoshopLoadPSD.INPUT_TYPES()
        required_names = list(spec["required"].keys())
        assert required_names == ["psd", "edit_original", "on_save"]
        assert required_names[-1] == "on_save"


class TestEditOriginalParam:
    """`edit_original` is accepted by execute()/IS_CHANGED() (ComfyUI passes
    every declared INPUT_TYPES field as a real keyword argument) but never
    changes their behavior (PROTOCOL.md §6b): it only governs how a handoff
    gets OPENED (menu.js's `/cpsb/open` request), a decision already made by
    the time any edit reaches this node's consume path.
    """

    def test_execute_defaults_to_false_for_pre_existing_callers(self, context, manager, configured):
        """Every call site written before this option existed omits the
        argument entirely -- must keep working unchanged.
        """
        write_test_psd(context.input_dir / "flat.psd", color=(7, 7, 7), size=(4, 4))
        node = load_psd_module.PhotoshopLoadPSD()
        node.execute(psd="flat.psd", unique_id="1")  # no TypeError

    def test_execute_accepts_edit_original_without_changing_flatten_output(
        self, context, manager, configured
    ):
        pytest.importorskip("torch")
        psd_path = context.input_dir / "flat.psd"
        write_test_psd(psd_path, color=(40, 80, 120), size=(12, 8))

        node = load_psd_module.PhotoshopLoadPSD()
        image_tensor, _mask = node.execute(psd="flat.psd", unique_id="1", edit_original=True)

        array = (image_tensor[0].numpy() * 255.0).round().astype("uint8")
        assert tuple(array[0, 0]) == (40, 80, 120)

    def test_execute_accepts_edit_original_on_the_consume_path(self, context, manager, configured):
        """edit_original must not disturb the "consume the latest edit"
        path either -- an arrived edit always lands in the managed folder
        the same way regardless of how the handoff was opened.
        """
        pytest.importorskip("torch")
        psd_path = context.input_dir / "sample.psd"
        write_test_psd(psd_path, color=(1, 1, 1), size=(8, 8))
        raw_hash = hashlib.sha256(psd_path.read_bytes()).hexdigest()
        meta = manager.create(
            origin_node_id="1",
            origin_kind="load_psd",
            workflow_name="",
            source=SourceRef(filename="sample.psd", subfolder="", type="input"),
            original_image=Image.new("RGB", (8, 8), (1, 1, 1)),
            source_hash=raw_hash,
            edit_in_place=True,
            original_path=str(psd_path.resolve()),
        )
        manager.ingest_edit(meta.handoff_id, Image.new("RGB", (8, 8), (200, 150, 100)), "plugin")

        node = load_psd_module.PhotoshopLoadPSD()
        image_tensor, _mask = node.execute(psd="sample.psd", unique_id="1", edit_original=True)

        array = (image_tensor[0].numpy() * 255.0).round().astype("uint8")
        assert tuple(array[0, 0]) == (200, 150, 100)

    def test_is_changed_defaults_to_false_for_pre_existing_callers(
        self, context, manager, configured
    ):
        write_test_psd(context.input_dir / "sample.psd")
        value = load_psd_module.PhotoshopLoadPSD.IS_CHANGED(psd="sample.psd", unique_id="1")
        assert len(value) == 64

    def test_is_changed_accepts_edit_original_without_changing_the_value(
        self, context, manager, configured
    ):
        write_test_psd(context.input_dir / "sample.psd", color=(3, 3, 3))
        without = load_psd_module.PhotoshopLoadPSD.IS_CHANGED(psd="sample.psd", unique_id="1")
        with_flag = load_psd_module.PhotoshopLoadPSD.IS_CHANGED(
            psd="sample.psd", unique_id="1", edit_original=True
        )
        assert without == with_flag


class TestOnSaveParam:
    """`on_save` (product-owner requirement 2026-07-18) is accepted by
    execute()/IS_CHANGED() (ComfyUI passes every declared INPUT_TYPES field
    as a real keyword argument) but never changes their output -- mirrors
    TestEditOriginalParam's identical convention for `edit_original`: it
    only governs whether an arriving edit is ingested/triggers a re-run at
    all (`HandoffManager.should_ingest`, the frontend's `maybeAutoQueue`), a
    decision already settled by the time any edit reaches this node's
    consume path.
    """

    def test_execute_defaults_to_rerun_for_pre_existing_callers(self, context, manager, configured):
        write_test_psd(context.input_dir / "flat.psd", color=(7, 7, 7), size=(4, 4))
        node = load_psd_module.PhotoshopLoadPSD()
        node.execute(psd="flat.psd", unique_id="1")  # no TypeError

    def test_execute_accepts_on_save_without_changing_flatten_output(
        self, context, manager, configured
    ):
        pytest.importorskip("torch")
        psd_path = context.input_dir / "flat.psd"
        write_test_psd(psd_path, color=(40, 80, 120), size=(12, 8))

        node = load_psd_module.PhotoshopLoadPSD()
        image_tensor, _mask = node.execute(
            psd="flat.psd", unique_id="1", on_save=load_psd_module.OnSaveMode.IGNORE
        )

        array = (image_tensor[0].numpy() * 255.0).round().astype("uint8")
        assert tuple(array[0, 0]) == (40, 80, 120)

    def test_is_changed_defaults_to_rerun_for_pre_existing_callers(
        self, context, manager, configured
    ):
        write_test_psd(context.input_dir / "sample.psd")
        value = load_psd_module.PhotoshopLoadPSD.IS_CHANGED(psd="sample.psd", unique_id="1")
        assert len(value) == 64

    def test_is_changed_accepts_on_save_without_changing_the_value(
        self, context, manager, configured
    ):
        write_test_psd(context.input_dir / "sample.psd", color=(3, 3, 3))
        without = load_psd_module.PhotoshopLoadPSD.IS_CHANGED(psd="sample.psd", unique_id="1")
        with_flag = load_psd_module.PhotoshopLoadPSD.IS_CHANGED(
            psd="sample.psd", unique_id="1", on_save=load_psd_module.OnSaveMode.UPDATE_ONLY
        )
        assert without == with_flag


class TestListPsdFiles:
    def test_filters_to_psd_and_psb_case_insensitive(self, tmp_path):
        for name in ("a.psd", "B.PSD", "c.psb", "d.PSB", "e.png", "f.txt"):
            (tmp_path / name).write_bytes(b"x")
        result = load_psd_module._list_psd_files(tmp_path)
        assert result == sorted(["a.psd", "B.PSD", "c.psb", "d.PSB"])

    def test_non_recursive(self, tmp_path):
        (tmp_path / "top.psd").write_bytes(b"x")
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "nested.psd").write_bytes(b"x")
        assert load_psd_module._list_psd_files(tmp_path) == ["top.psd"]

    def test_ignores_non_file_entries(self, tmp_path):
        (tmp_path / "real.psd").write_bytes(b"x")
        (tmp_path / "adir.psd").mkdir()  # a directory that happens to end in .psd
        assert load_psd_module._list_psd_files(tmp_path) == ["real.psd"]

    def test_missing_directory_returns_empty(self, tmp_path):
        assert load_psd_module._list_psd_files(tmp_path / "does-not-exist") == []

    def test_input_types_reflects_directory_contents(self, context, configured):
        (context.input_dir / "one.psd").write_bytes(b"x")
        (context.input_dir / "two.PSB").write_bytes(b"x")
        (context.input_dir / "photo.png").write_bytes(b"x")
        spec = load_psd_module.PhotoshopLoadPSD.INPUT_TYPES()
        assert spec["required"]["psd"] == (["one.psd", "two.PSB"],)


class TestBroadenedFormatsListing:
    """The combo/``VALIDATE_INPUTS`` accept-list is PSD-native plus TIFF
    (:func:`load_psd_module._accepted_extensions` /
    :func:`cpsb.raster_io.available_extensions`). ``.ai``/raw are NOT here --
    they moved to the Tier-2 "Open via Photoshop" node, so Load PSD needs no
    optional decoder and this listing is deterministic with no monkeypatching.
    """

    def test_tiff_always_listed_no_dependency_needed(self, tmp_path):
        for name in ("a.tif", "B.TIF", "c.tiff", "d.TIFF"):
            (tmp_path / name).write_bytes(b"x")
        result = load_psd_module._list_psd_files(tmp_path)
        assert result == sorted(["a.tif", "B.TIF", "c.tiff", "d.TIFF"])

    def test_combo_includes_tiff_alongside_psd(self, context, configured):
        (context.input_dir / "one.psd").write_bytes(b"x")
        (context.input_dir / "photo.tif").write_bytes(b"x")
        spec = load_psd_module.PhotoshopLoadPSD.INPUT_TYPES()
        assert spec["required"]["psd"] == (["one.psd", "photo.tif"],)


class TestBroadenedFormatsValidateInputs:
    def test_accepts_tiff(self, context, configured):
        (context.input_dir / "ok.tif").write_bytes(b"x")
        assert load_psd_module.PhotoshopLoadPSD.VALIDATE_INPUTS("ok.tif") is True

    def test_rejects_unsupported_extension(self, context, configured):
        (context.input_dir / "ok.bmp").write_bytes(b"x")
        result = load_psd_module.PhotoshopLoadPSD.VALIDATE_INPUTS("ok.bmp")
        assert result is not True
        assert isinstance(result, str)


class TestResolvePsdPath:
    def test_resolves_within_input_dir(self, context):
        (context.input_dir / "ok.psd").write_bytes(b"x")
        assert load_psd_module._resolve_psd_path(context, "ok.psd") == context.input_dir / "ok.psd"

    def test_rejects_path_traversal(self, context):
        (context.input_dir.parent / "secret.psd").write_bytes(b"x")
        assert load_psd_module._resolve_psd_path(context, "../secret.psd") is None


class TestValidateInputs:
    def test_valid_file_returns_true(self, context, configured):
        (context.input_dir / "ok.psd").write_bytes(b"x")
        assert load_psd_module.PhotoshopLoadPSD.VALIDATE_INPUTS("ok.psd") is True

    def test_valid_psb_returns_true(self, context, configured):
        (context.input_dir / "ok.psb").write_bytes(b"x")
        assert load_psd_module.PhotoshopLoadPSD.VALIDATE_INPUTS("ok.psb") is True

    def test_wrong_extension_returns_message(self, context, configured):
        (context.input_dir / "ok.png").write_bytes(b"x")
        result = load_psd_module.PhotoshopLoadPSD.VALIDATE_INPUTS("ok.png")
        assert result is not True
        assert isinstance(result, str)

    def test_missing_file_returns_message(self, context, configured):
        result = load_psd_module.PhotoshopLoadPSD.VALIDATE_INPUTS("ghost.psd")
        assert result is not True
        assert isinstance(result, str)

    def test_unconfigured_returns_true(self):
        assert nodes_module._state is None
        assert load_psd_module.PhotoshopLoadPSD.VALIDATE_INPUTS("anything.psd") is True


class TestIsChanged:
    def test_changes_when_file_bytes_change(self, context, manager, configured):
        psd_path = context.input_dir / "sample.psd"
        write_test_psd(psd_path, color=(1, 2, 3))
        before = load_psd_module.PhotoshopLoadPSD.IS_CHANGED(psd="sample.psd", unique_id="1")
        assert len(before) == 64  # bare sha256 hex: no active handoff yet

        write_test_psd(psd_path, color=(9, 9, 9))
        after = load_psd_module.PhotoshopLoadPSD.IS_CHANGED(psd="sample.psd", unique_id="1")

        assert after != before
        assert len(after) == 64

    def test_changes_when_matching_edit_arrives(self, context, manager, configured):
        psd_path = context.input_dir / "sample.psd"
        write_test_psd(psd_path)
        raw_hash = hashlib.sha256(psd_path.read_bytes()).hexdigest()

        before = load_psd_module.PhotoshopLoadPSD.IS_CHANGED(psd="sample.psd", unique_id="1")
        assert before == raw_hash

        meta = manager.create(
            origin_node_id="1",
            origin_kind="load_psd",
            workflow_name="",
            source=SourceRef(filename="sample.psd", subfolder="", type="input"),
            original_image=Image.new("RGB", (4, 4), (0, 0, 0)),
            source_hash=raw_hash,
        )
        manager.ingest_edit(meta.handoff_id, Image.new("RGB", (4, 4), (5, 5, 5)), "plugin")

        after = load_psd_module.PhotoshopLoadPSD.IS_CHANGED(psd="sample.psd", unique_id="1")
        assert after != before
        assert after.startswith(raw_hash + ":")

        # A second edit changes the value again.
        manager.ingest_edit(meta.handoff_id, Image.new("RGB", (4, 4), (6, 6, 6)), "plugin")
        after2 = load_psd_module.PhotoshopLoadPSD.IS_CHANGED(psd="sample.psd", unique_id="1")
        assert after2 != after
        assert after2.startswith(raw_hash + ":")

    def test_mismatched_source_hash_handoff_is_ignored(self, context, manager, configured):
        """A stale handoff (created from bytes the file no longer has) must
        not influence IS_CHANGED -- only the bare file hash is returned.
        """
        psd_path = context.input_dir / "sample.psd"
        write_test_psd(psd_path)
        meta = manager.create(
            origin_node_id="1",
            origin_kind="load_psd",
            workflow_name="",
            source=SourceRef(filename="sample.psd", subfolder="", type="input"),
            original_image=Image.new("RGB", (4, 4), (0, 0, 0)),
            source_hash="deadbeef" * 8,
        )
        manager.ingest_edit(meta.handoff_id, Image.new("RGB", (4, 4), (5, 5, 5)), "plugin")

        raw_hash = hashlib.sha256(psd_path.read_bytes()).hexdigest()
        value = load_psd_module.PhotoshopLoadPSD.IS_CHANGED(psd="sample.psd", unique_id="1")
        assert value == raw_hash

    def test_wrong_origin_kind_handoff_is_ignored(self, context, manager, configured):
        psd_path = context.input_dir / "sample.psd"
        write_test_psd(psd_path)
        raw_hash = hashlib.sha256(psd_path.read_bytes()).hexdigest()
        meta = manager.create(
            origin_node_id="1",
            origin_kind="bridge_node",
            workflow_name="",
            source=SourceRef(filename="bridge_1.png", subfolder="", type="temp"),
            original_image=Image.new("RGB", (4, 4), (0, 0, 0)),
            source_hash=raw_hash,
        )
        manager.ingest_edit(meta.handoff_id, Image.new("RGB", (4, 4), (5, 5, 5)), "plugin")

        value = load_psd_module.PhotoshopLoadPSD.IS_CHANGED(psd="sample.psd", unique_id="1")
        assert value == raw_hash

    def test_missing_file_returns_bare_selector(self, configured):
        value = load_psd_module.PhotoshopLoadPSD.IS_CHANGED(psd="ghost.psd", unique_id="1")
        assert value == "ghost.psd"

    def test_unconfigured_raises(self):
        assert nodes_module._state is None
        with pytest.raises(RuntimeError, match="configure"):
            load_psd_module.PhotoshopLoadPSD.IS_CHANGED(psd="x.psd", unique_id="1")


class TestExecuteErrors:
    def test_missing_file_raises_file_not_found(self, context, manager, configured):
        node = load_psd_module.PhotoshopLoadPSD()
        with pytest.raises(FileNotFoundError):
            node.execute(psd="ghost.psd", unique_id="1")

    def test_path_traversal_raises_file_not_found(self, context, manager, configured):
        (context.input_dir.parent / "secret.psd").write_bytes(b"x")
        node = load_psd_module.PhotoshopLoadPSD()
        with pytest.raises(FileNotFoundError):
            node.execute(psd="../secret.psd", unique_id="1")

    def test_unconfigured_raises_runtime_error(self):
        assert nodes_module._state is None
        node = load_psd_module.PhotoshopLoadPSD()
        with pytest.raises(RuntimeError, match="configure"):
            node.execute(psd="x.psd", unique_id="1")


class TestExecuteFlatten:
    """Flatten output correctness vs a psd-tools-authored fixture (PROTOCOL.md §6b)."""

    @pytest.fixture(autouse=True)
    def _require_torch(self):
        pytest.importorskip("torch")

    def test_flatten_matches_source_pixels(self, context, manager, configured):
        source_color = (40, 80, 120)
        psd_path = context.input_dir / "flat.psd"
        write_test_psd(psd_path, color=source_color, size=(12, 8))

        node = load_psd_module.PhotoshopLoadPSD()
        image_tensor, mask_tensor = node.execute(psd="flat.psd", unique_id="1")

        array = (image_tensor[0].numpy() * 255.0).round().astype("uint8")
        assert array.shape == (8, 12, 3)  # (height, width, channels)
        assert tuple(array[0, 0]) == source_color
        assert mask_tensor.shape == (1, 8, 12)

    def test_flatten_cmyk_psd_is_not_black(self, context, manager, configured):
        """Regression test for the user-reported bug: "Opening a CMYK file
        in Load PSD shows a black square." Exercises this node's real
        ``execute()`` IMAGE output (not just ``cpsb.psd_io`` directly) --
        ``execute()`` calls ``cpsb.psd_io.read_edited_psd`` the same way
        ``GET /cpsb/psd_preview`` (``cpsb/routes.py``) does for the node's
        own preview thumbnail, so this also confirms the preview and IMAGE
        output share the same healed conversion path.
        """
        psd_path = context.input_dir / "cmyk.psd"
        # C=0, M=~0.5, Y=1, K=0 -- orange, standard (ink-direct) convention.
        write_test_cmyk_psd(psd_path, (0, 127, 255, 0), size=(8, 8))

        node = load_psd_module.PhotoshopLoadPSD()
        image_tensor, _mask_tensor = node.execute(psd="cmyk.psd", unique_id="1")

        array = (image_tensor[0].numpy() * 255.0).round().astype("uint8")
        red, green, blue = (int(v) for v in array[0, 0])
        assert red == 255
        assert 120 <= green <= 135
        assert blue == 0
        assert array.astype("float64").mean() > 60.0  # far from solid black


class TestExecuteBroadenedFormats:
    """``execute()``'s PSD-native-vs-``raster_io`` dispatch (2026-07-19).

    ``TestBroadenedFormatsListing``/``TestBroadenedFormatsValidateInputs``
    above cover the combo/upfront-check side; these exercise the actual
    decode dispatch inside :meth:`PhotoshopLoadPSD.execute` itself --
    :func:`cpsb.raster_io.decode_to_rgb8`'s own correctness (per format) is
    already covered directly in ``tests/test_psd_io.py``, so these just
    confirm the node wires into it, and surfaces its errors, correctly.
    """

    @pytest.fixture(autouse=True)
    def _require_torch(self):
        pytest.importorskip("torch")

    def test_execute_decodes_tiff(self, context, manager, configured):
        tifffile = pytest.importorskip("tifffile")
        import numpy as np

        tiff_path = context.input_dir / "photo.tif"
        array = np.zeros((8, 12, 3), dtype=np.uint8)
        array[..., 0], array[..., 1], array[..., 2] = 40, 80, 120
        tifffile.imwrite(tiff_path, array, photometric="rgb")

        node = load_psd_module.PhotoshopLoadPSD()
        image_tensor, mask_tensor = node.execute(psd="photo.tif", unique_id="1")

        pixels = (image_tensor[0].numpy() * 255.0).round().astype("uint8")
        assert pixels.shape == (8, 12, 3)
        assert tuple(pixels[0, 0]) == (40, 80, 120)
        assert mask_tensor.shape == (1, 8, 12)  # no alpha in a plain RGB TIFF -> zeros mask


class TestExecuteMaskChain:
    """MASK derivation: ``1 - alpha`` when present, else zeros (PROTOCOL.md §4/§6b).

    (A prior third tier -- an extracted document channel mask -- was
    removed, PROTOCOL.md §4: "owner's call", plain alpha-based masking
    already covers the need.)
    """

    @pytest.fixture(autouse=True)
    def _require_torch(self):
        pytest.importorskip("torch")

    def test_zeros_when_no_alpha(self, context, manager, configured):
        import torch

        psd_path = context.input_dir / "plain.psd"
        write_test_psd(psd_path, color=(3, 3, 3), size=(6, 6))

        node = load_psd_module.PhotoshopLoadPSD()
        _, mask_tensor = node.execute(psd="plain.psd", unique_id="1")

        assert mask_tensor.shape == (1, 6, 6)
        assert torch.count_nonzero(mask_tensor).item() == 0

    def test_alpha_complement_when_alpha_present(self, context, manager, configured):
        import torch

        psd_path = context.input_dir / "alpha.psd"
        write_psd(psd_path, Image.new("RGBA", (6, 6), (1, 2, 3, 64)))

        node = load_psd_module.PhotoshopLoadPSD()
        _, mask_tensor = node.execute(psd="alpha.psd", unique_id="1")

        expected = 1.0 - 64 / 255.0
        assert torch.allclose(mask_tensor, torch.full((1, 6, 6), expected), atol=1e-3)


class TestExecuteConsumePath:
    """The bridge-node "consume the edit" pattern, reused (PROTOCOL.md §6b)."""

    @pytest.fixture(autouse=True)
    def _require_torch(self):
        pytest.importorskip("torch")

    def test_consumes_latest_edit_instead_of_reflattening(
        self, context, manager, configured, monkeypatch
    ):
        psd_path = context.input_dir / "sample.psd"
        write_test_psd(psd_path, color=(1, 1, 1), size=(8, 8))
        raw_hash = hashlib.sha256(psd_path.read_bytes()).hexdigest()

        meta = manager.create(
            origin_node_id="1",
            origin_kind="load_psd",
            workflow_name="",
            source=SourceRef(filename="sample.psd", subfolder="", type="input"),
            original_image=Image.new("RGB", (8, 8), (1, 1, 1)),
            source_hash=raw_hash,
        )
        manager.ingest_edit(meta.handoff_id, Image.new("RGB", (8, 8), (200, 150, 100)), "plugin")

        def _must_not_be_called(path):
            raise AssertionError("read_edited_psd must not run on the consume path")

        monkeypatch.setattr(load_psd_module, "read_edited_psd", _must_not_be_called)

        node = load_psd_module.PhotoshopLoadPSD()
        image_tensor, _mask_tensor = node.execute(psd="sample.psd", unique_id="1")

        array = (image_tensor[0].numpy() * 255.0).round().astype("uint8")
        assert tuple(array[0, 0]) == (200, 150, 100)  # the EDIT's pixels, not the original

    def test_stale_handoff_with_different_hash_reflattens_instead(
        self, context, manager, configured
    ):
        psd_path = context.input_dir / "sample.psd"
        write_test_psd(psd_path, color=(9, 9, 9), size=(8, 8))

        meta = manager.create(
            origin_node_id="1",
            origin_kind="load_psd",
            workflow_name="",
            source=SourceRef(filename="sample.psd", subfolder="", type="input"),
            original_image=Image.new("RGB", (8, 8), (1, 1, 1)),
            source_hash="deadbeef" * 8,  # does not match the current file's real hash
        )
        manager.ingest_edit(meta.handoff_id, Image.new("RGB", (8, 8), (200, 150, 100)), "plugin")

        node = load_psd_module.PhotoshopLoadPSD()
        image_tensor, _mask_tensor = node.execute(psd="sample.psd", unique_id="1")

        array = (image_tensor[0].numpy() * 255.0).round().astype("uint8")
        assert tuple(array[0, 0]) == (9, 9, 9)  # flattened fresh, not the stale edit

    def test_no_edits_yet_flattens_instead(self, context, manager, configured):
        psd_path = context.input_dir / "sample.psd"
        write_test_psd(psd_path, color=(7, 7, 7), size=(8, 8))
        raw_hash = hashlib.sha256(psd_path.read_bytes()).hexdigest()

        manager.create(
            origin_node_id="1",
            origin_kind="load_psd",
            workflow_name="",
            source=SourceRef(filename="sample.psd", subfolder="", type="input"),
            original_image=Image.new("RGB", (8, 8), (1, 1, 1)),
            source_hash=raw_hash,
        )  # pending: no edit ingested yet

        node = load_psd_module.PhotoshopLoadPSD()
        image_tensor, _mask_tensor = node.execute(psd="sample.psd", unique_id="1")

        array = (image_tensor[0].numpy() * 255.0).round().astype("uint8")
        assert tuple(array[0, 0]) == (7, 7, 7)
