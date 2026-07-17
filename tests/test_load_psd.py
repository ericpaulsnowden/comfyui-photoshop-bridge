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
