"""PhotoshopComposePSD node (PROTOCOL.md §6c): torch-free import, contract
shape, group-write structure/round-trip, centering math, flatten/mask
outputs, IS_CHANGED sensitivity, the consume path, filename collision
safety, and edit_after handoff creation.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import threading
from pathlib import Path
from typing import cast

import numpy as np
import pytest
from aiohttp import web
from PIL import Image
from psd_tools import PSDImage

import cpsb.compose_psd as compose_module
import cpsb.nodes as nodes_module
import cpsb.routes as routes_module
from cpsb.context import CpsbContext
from cpsb.handoff import HandoffManager, SourceRef
from cpsb.launcher import LaunchResult

ComposePSD = compose_module.PhotoshopComposePSD


def make_tensor(color: tuple[int, int, int], size: tuple[int, int] = (24, 16)) -> np.ndarray:
    """A 1xHxWx3 ComfyUI-layout float32 tensor of a solid color. *size* = (width, height)."""
    img = Image.new("RGB", size, color)
    return np.asarray(img, dtype=np.float32)[None, ...] / 255.0


RED = (255, 0, 0)
GREEN = (0, 255, 0)
BLUE = (0, 0, 255)


@pytest.fixture
def manager(context: CpsbContext) -> HandoffManager:
    return HandoffManager(context)


@pytest.fixture
def configured(context: CpsbContext, manager: HandoffManager):
    """Lightweight wiring: no real app/loop -- enough for IS_CHANGED/consume/compose
    tests that never reach the ``edit_after`` open-Photoshop path.
    """
    nodes_module.configure(context, manager, cast("object", None), cast("object", None))
    yield
    nodes_module._state = None


@pytest.fixture
def launches(monkeypatch):
    """Records every ``launch_photoshop`` call, patched the same way
    ``tests/test_nodes.py`` does (through the ``routes`` module object, since
    ``cpsb.nodes`` always calls it that way -- see that fixture's docstring).
    """
    calls: list[str] = []

    def fake_launch(psd_path, override=""):
        calls.append(str(psd_path))
        return LaunchResult(ok=True)

    monkeypatch.setattr(routes_module, "launch_photoshop", fake_launch)
    return calls


@pytest.fixture
def loop_thread():
    """A live event loop on a background thread, like PromptServer's own."""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=5)


@pytest.fixture
def configured_with_app(context: CpsbContext, manager: HandoffManager, loop_thread, launches):
    """Full wiring (real loop + real routes app) for ``edit_after`` tests,
    which need ``_open_in_photoshop`` -> ``routes.tier2_connected(state.app)``
    to have a real ``aiohttp.web.Application`` to inspect.
    """
    app = web.Application()
    routes_module.install(app, context, manager)
    nodes_module.configure(context, manager, app, loop_thread)
    yield
    nodes_module._state = None


class TestImportability:
    def test_module_imports_without_torch(self):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import cpsb.compose_psd as m, sys\n"
                "assert m.PhotoshopComposePSD is not None\n"
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
        assert ComposePSD.CATEGORY == "image/photoshop"
        assert ComposePSD.RETURN_TYPES == ("IMAGE", "MASK", "STRING")
        assert ComposePSD.FUNCTION == "execute"

    def test_input_types_shape(self):
        spec = ComposePSD.INPUT_TYPES()
        assert spec["required"]["filename_prefix"] == (
            "STRING",
            {"default": "compose"},
        )
        assert spec["required"]["group_name"] == ("STRING", {"default": "ComfyUI Layers"})
        assert spec["required"]["edit_after"] == ("BOOLEAN", {"default": False})
        assert spec["hidden"] == {"unique_id": "UNIQUE_ID"}

    def test_optional_images_generous_and_all_image_type(self):
        spec = ComposePSD.INPUT_TYPES()
        optional = spec["optional"]
        assert len(optional) == compose_module.MAX_IMAGE_INPUTS
        assert "image_1" in optional
        assert f"image_{compose_module.MAX_IMAGE_INPUTS}" in optional
        assert all(value == ("IMAGE",) for value in optional.values())


class TestSanitizeFilenamePrefix:
    def test_blank_falls_back_to_default(self):
        assert compose_module._sanitize_filename_prefix("") == "compose"
        assert compose_module._sanitize_filename_prefix("   ") == "compose"

    def test_dot_and_dotdot_fall_back(self):
        assert compose_module._sanitize_filename_prefix(".") == "compose"
        assert compose_module._sanitize_filename_prefix("..") == "compose"

    def test_path_separators_are_stripped(self):
        assert compose_module._sanitize_filename_prefix("../secret") == ".._secret"
        assert compose_module._sanitize_filename_prefix("a/b\\c") == "a_b_c"

    def test_ordinary_value_passes_through(self):
        assert compose_module._sanitize_filename_prefix("my_comp") == "my_comp"

    def test_strips_surrounding_whitespace(self):
        assert compose_module._sanitize_filename_prefix("  padded  ") == "padded"


class TestCollectConnectedImages:
    def test_only_connected_indices_in_order(self):
        kwargs = {"image_3": "c", "image_1": "a", "image_20": "t"}
        assert compose_module._collect_connected_images(kwargs) == ["a", "c", "t"]

    def test_empty_when_none_connected(self):
        assert compose_module._collect_connected_images({}) == []

    def test_ignores_indices_beyond_max(self):
        kwargs = {f"image_{compose_module.MAX_IMAGE_INPUTS + 1}": "ignored", "image_1": "a"}
        assert compose_module._collect_connected_images(kwargs) == ["a"]


class TestCenteredOffset:
    def test_exact_fit_is_zero(self):
        assert compose_module._centered_offset(10, 10) == 0

    def test_even_difference(self):
        assert compose_module._centered_offset(4, 10) == 3

    def test_odd_difference_floors(self):
        # (9 - 4) // 2 == 2 (not 2.5) -- floor-division centering.
        assert compose_module._centered_offset(4, 9) == 2


class TestBuildGroupPsd:
    """Low-level structural verification, same discipline as
    ``tests/test_psd_io.py``: build, save, REOPEN via a fresh ``PSDImage.open``,
    assert on the reopened structure -- not the in-memory object.
    """

    def test_group_structure_and_layer_order(self, tmp_path):
        images = [
            Image.new("RGB", (10, 6), RED),
            Image.new("RGB", (8, 8), GREEN),
            Image.new("RGB", (4, 4), BLUE),
        ]
        psd, canvas_w, canvas_h, placements = compose_module._build_group_psd(images, "My Group")
        assert (canvas_w, canvas_h) == (10, 8)
        assert len(placements) == 3

        out = tmp_path / "grouped.psd"
        psd.save(out)
        reopened = PSDImage.open(out)

        assert reopened.size == (10, 8)
        assert len(reopened) == 1  # one top-level child: the group
        group = reopened[0]
        assert group.kind == "group"
        assert group.name == "My Group"
        assert len(group) == 3
        assert [layer.name for layer in group] == ["Layer 1", "Layer 2", "Layer 3"]

    def test_odd_size_centering_reopens_at_expected_bbox(self, tmp_path):
        images = [Image.new("RGB", (7, 9), RED), Image.new("RGB", (4, 4), BLUE)]
        psd, canvas_w, canvas_h, _ = compose_module._build_group_psd(images, "G")
        assert (canvas_w, canvas_h) == (7, 9)

        out = tmp_path / "odd.psd"
        psd.save(out)
        group = PSDImage.open(out)[0]
        bboxes = {layer.name: layer.bbox for layer in group}
        assert bboxes["Layer 1"] == (0, 0, 7, 9)  # exact-fit dimension: offset 0
        assert bboxes["Layer 2"] == (1, 2, 5, 6)  # (7-4)//2=1, (9-4)//2=2

    def test_single_image_n1(self, tmp_path):
        images = [Image.new("RGB", (13, 17), RED)]
        psd, canvas_w, canvas_h, placements = compose_module._build_group_psd(images, "Solo")
        assert (canvas_w, canvas_h) == (13, 17)
        assert placements[0][1:] == (0, 0)  # single image: no offset

        out = tmp_path / "n1.psd"
        psd.save(out)
        group = PSDImage.open(out)[0]
        assert len(group) == 1
        assert group[0].bbox == (0, 0, 13, 17)

    def test_eight_images_n8_order_preserved(self, tmp_path):
        images = [Image.new("RGB", (10 + i, 10 + i * 2), (i * 10 % 255, 0, 0)) for i in range(1, 9)]
        psd, _, _, _ = compose_module._build_group_psd(images, "Eight")
        out = tmp_path / "n8.psd"
        psd.save(out)
        group = PSDImage.open(out)[0]
        assert len(group) == 8
        assert [layer.name for layer in group] == [f"Layer {i}" for i in range(1, 9)]

    def test_canvas_is_max_across_mismatched_dimensions(self, tmp_path):
        # Widest image is short; tallest image is narrow -- canvas takes the
        # max of EACH dimension independently, from possibly different inputs.
        images = [Image.new("RGB", (20, 5), RED), Image.new("RGB", (5, 20), BLUE)]
        _, canvas_w, canvas_h, _ = compose_module._build_group_psd(images, "G")
        assert (canvas_w, canvas_h) == (20, 20)


class TestFlattenPlacements:
    def test_covers_union_of_layer_bboxes_only(self):
        placements = [
            (Image.new("RGB", (4, 4), RED), 0, 0),
            (Image.new("RGB", (2, 2), BLUE), 6, 6),
        ]
        result = compose_module._flatten_placements(placements, 10, 10)
        assert result.mode == "RGBA"
        assert result.size == (10, 10)
        assert result.getpixel((0, 0)) == (255, 0, 0, 255)  # inside layer 1
        assert result.getpixel((7, 7)) == (0, 0, 255, 255)  # inside layer 2
        assert result.getpixel((9, 9))[3] == 0  # outside every layer: transparent

    def test_later_placement_paints_over_earlier(self):
        placements = [
            (Image.new("RGB", (4, 4), RED), 0, 0),
            (Image.new("RGB", (4, 4), BLUE), 0, 0),  # same bbox, on top
        ]
        result = compose_module._flatten_placements(placements, 4, 4)
        assert result.getpixel((0, 0)) == (0, 0, 255, 255)  # top layer wins

    def test_full_coverage_when_bottom_layer_fills_canvas(self):
        placements = [(Image.new("RGB", (5, 5), GREEN), 0, 0)]
        result = compose_module._flatten_placements(placements, 5, 5)
        assert all(result.getpixel((x, y))[3] == 255 for x in range(5) for y in range(5))


class TestAllocateOutputPath:
    def test_first_call_uses_index_one(self, tmp_path):
        path = compose_module._allocate_output_path(tmp_path, "compose")
        assert path == tmp_path / "compose_00001.psd"

    def test_skips_existing_indices(self, tmp_path):
        (tmp_path / "compose_00001.psd").write_bytes(b"x")
        (tmp_path / "compose_00002.psd").write_bytes(b"x")
        path = compose_module._allocate_output_path(tmp_path, "compose")
        assert path == tmp_path / "compose_00003.psd"

    def test_creates_missing_input_dir(self, tmp_path):
        missing = tmp_path / "not_yet_created"
        path = compose_module._allocate_output_path(missing, "compose")
        assert missing.is_dir()
        assert path == missing / "compose_00001.psd"

    def test_different_prefixes_do_not_collide(self, tmp_path):
        (tmp_path / "a_00001.psd").write_bytes(b"x")
        path = compose_module._allocate_output_path(tmp_path, "b")
        assert path == tmp_path / "b_00001.psd"


class TestComputeInputsHash:
    def test_deterministic_for_identical_inputs(self):
        images = [Image.new("RGB", (4, 4), RED)]
        h1 = compose_module._compute_inputs_hash(images, "compose", "Group", False)
        h2 = compose_module._compute_inputs_hash(images, "compose", "Group", False)
        assert h1 == h2
        assert len(h1) == 64

    def test_changes_with_pixel_content(self):
        h_red = compose_module._compute_inputs_hash(
            [Image.new("RGB", (4, 4), RED)], "compose", "Group", False
        )
        h_blue = compose_module._compute_inputs_hash(
            [Image.new("RGB", (4, 4), BLUE)], "compose", "Group", False
        )
        assert h_red != h_blue

    def test_changes_with_image_count(self):
        one = compose_module._compute_inputs_hash(
            [Image.new("RGB", (4, 4), RED)], "compose", "Group", False
        )
        two = compose_module._compute_inputs_hash(
            [Image.new("RGB", (4, 4), RED)] * 2, "compose", "Group", False
        )
        assert one != two

    def test_changes_with_order(self):
        a = compose_module._compute_inputs_hash(
            [Image.new("RGB", (4, 4), RED), Image.new("RGB", (4, 4), BLUE)],
            "compose",
            "Group",
            False,
        )
        b = compose_module._compute_inputs_hash(
            [Image.new("RGB", (4, 4), BLUE), Image.new("RGB", (4, 4), RED)],
            "compose",
            "Group",
            False,
        )
        assert a != b

    def test_changes_with_filename_prefix(self):
        images = [Image.new("RGB", (4, 4), RED)]
        a = compose_module._compute_inputs_hash(images, "compose", "Group", False)
        b = compose_module._compute_inputs_hash(images, "other", "Group", False)
        assert a != b

    def test_changes_with_group_name(self):
        images = [Image.new("RGB", (4, 4), RED)]
        a = compose_module._compute_inputs_hash(images, "compose", "Group", False)
        b = compose_module._compute_inputs_hash(images, "compose", "Other", False)
        assert a != b

    def test_changes_with_edit_after(self):
        images = [Image.new("RGB", (4, 4), RED)]
        a = compose_module._compute_inputs_hash(images, "compose", "Group", False)
        b = compose_module._compute_inputs_hash(images, "compose", "Group", True)
        assert a != b


class TestIsChanged:
    def test_bare_hash_when_no_active_handoff(self, configured):
        value = ComposePSD.IS_CHANGED(
            filename_prefix="compose",
            group_name="Group",
            edit_after=False,
            unique_id="1",
            image_1=make_tensor(RED),
        )
        assert len(value) == 64

    def test_changes_when_input_pixels_change(self, configured):
        before = ComposePSD.IS_CHANGED(
            filename_prefix="compose",
            group_name="Group",
            edit_after=False,
            unique_id="1",
            image_1=make_tensor(RED),
        )
        after = ComposePSD.IS_CHANGED(
            filename_prefix="compose",
            group_name="Group",
            edit_after=False,
            unique_id="1",
            image_1=make_tensor(GREEN),
        )
        assert before != after

    def test_changes_when_param_changes(self, configured):
        before = ComposePSD.IS_CHANGED(
            filename_prefix="compose",
            group_name="Group",
            edit_after=False,
            unique_id="1",
            image_1=make_tensor(RED),
        )
        after = ComposePSD.IS_CHANGED(
            filename_prefix="other",
            group_name="Group",
            edit_after=False,
            unique_id="1",
            image_1=make_tensor(RED),
        )
        assert before != after

    def test_changes_when_matching_edit_arrives(self, context, manager, configured):
        tensor = make_tensor(RED)
        pil_images = [nodes_module._tensor_to_pil(tensor)]
        prefix = compose_module._sanitize_filename_prefix("compose")
        inputs_hash = compose_module._compute_inputs_hash(pil_images, prefix, "Group", False)

        before = ComposePSD.IS_CHANGED(
            filename_prefix="compose",
            group_name="Group",
            edit_after=False,
            unique_id="1",
            image_1=tensor,
        )
        assert before == inputs_hash

        meta = manager.create(
            origin_node_id="1",
            origin_kind="load_psd",
            workflow_name="",
            source=SourceRef(filename="compose_00001.psd", subfolder="", type="input"),
            original_image=Image.new("RGBA", (4, 4), (0, 0, 0, 0)),
            source_hash=inputs_hash,
        )
        manager.ingest_edit(meta.handoff_id, Image.new("RGB", (4, 4), (5, 5, 5)), "plugin")

        after = ComposePSD.IS_CHANGED(
            filename_prefix="compose",
            group_name="Group",
            edit_after=False,
            unique_id="1",
            image_1=tensor,
        )
        assert after != before
        assert after.startswith(inputs_hash + ":")

        # A second edit changes the value again.
        manager.ingest_edit(meta.handoff_id, Image.new("RGB", (4, 4), (6, 6, 6)), "plugin")
        after2 = ComposePSD.IS_CHANGED(
            filename_prefix="compose",
            group_name="Group",
            edit_after=False,
            unique_id="1",
            image_1=tensor,
        )
        assert after2 != after
        assert after2.startswith(inputs_hash + ":")

    def test_mismatched_source_hash_handoff_is_ignored(self, context, manager, configured):
        tensor = make_tensor(RED)
        meta = manager.create(
            origin_node_id="1",
            origin_kind="load_psd",
            workflow_name="",
            source=SourceRef(filename="compose_00001.psd", subfolder="", type="input"),
            original_image=Image.new("RGBA", (4, 4), (0, 0, 0, 0)),
            source_hash="deadbeef" * 8,
        )
        manager.ingest_edit(meta.handoff_id, Image.new("RGB", (4, 4), (5, 5, 5)), "plugin")

        pil_images = [nodes_module._tensor_to_pil(tensor)]
        expected_bare = compose_module._compute_inputs_hash(pil_images, "compose", "Group", False)
        value = ComposePSD.IS_CHANGED(
            filename_prefix="compose",
            group_name="Group",
            edit_after=False,
            unique_id="1",
            image_1=tensor,
        )
        assert value == expected_bare

    def test_wrong_origin_kind_handoff_is_ignored(self, context, manager, configured):
        tensor = make_tensor(RED)
        pil_images = [nodes_module._tensor_to_pil(tensor)]
        inputs_hash = compose_module._compute_inputs_hash(pil_images, "compose", "Group", False)
        meta = manager.create(
            origin_node_id="1",
            origin_kind="bridge_node",
            workflow_name="",
            source=SourceRef(filename="bridge_1.png", subfolder="", type="temp"),
            original_image=Image.new("RGB", (4, 4), (0, 0, 0)),
            source_hash=inputs_hash,
        )
        manager.ingest_edit(meta.handoff_id, Image.new("RGB", (4, 4), (5, 5, 5)), "plugin")

        value = ComposePSD.IS_CHANGED(
            filename_prefix="compose",
            group_name="Group",
            edit_after=False,
            unique_id="1",
            image_1=tensor,
        )
        assert value == inputs_hash

    def test_unconfigured_returns_bare_hash_without_raising(self):
        assert nodes_module._state is None
        value = ComposePSD.IS_CHANGED(
            filename_prefix="compose",
            group_name="Group",
            edit_after=False,
            unique_id="1",
            image_1=make_tensor(RED),
        )
        assert len(value) == 64


class TestExecuteErrors:
    def test_no_images_raises_value_error(self, context, manager, configured):
        node = ComposePSD()
        with pytest.raises(ValueError, match="at least one"):
            node.execute(
                filename_prefix="compose", group_name="Group", edit_after=False, unique_id="1"
            )

    def test_unconfigured_raises_runtime_error(self):
        assert nodes_module._state is None
        node = ComposePSD()
        with pytest.raises(RuntimeError, match="configure"):
            node.execute(
                filename_prefix="compose",
                group_name="Group",
                edit_after=False,
                unique_id="1",
                image_1=make_tensor(RED),
            )


class TestExecuteCompose:
    @pytest.fixture(autouse=True)
    def _require_torch(self):
        pytest.importorskip("torch")

    def test_writes_psd_and_returns_outputs(self, context, manager, configured):
        node = ComposePSD()
        image_out, mask_out, filename_out = node.execute(
            filename_prefix="compose",
            group_name="My Layers",
            edit_after=False,
            unique_id="1",
            image_1=make_tensor(RED, size=(10, 6)),
            image_2=make_tensor(BLUE, size=(6, 10)),
        )
        written = context.input_dir / filename_out
        assert written.is_file()
        assert filename_out == "compose_00001.psd"

        reopened = PSDImage.open(written)
        assert reopened.size == (10, 10)  # max(10,6) x max(6,10)
        group = reopened[0]
        assert group.kind == "group"
        assert group.name == "My Layers"
        assert [layer.name for layer in group] == ["Layer 1", "Layer 2"]

        array = (image_out[0].numpy() * 255.0).round().astype("uint8")
        assert array.shape == (10, 10, 3)  # (height, width, channels)
        assert mask_out.shape == (1, 10, 10)

    def test_image_1_is_bottom_image_n_is_top(self, context, manager, configured):
        """image_1 bottom, image_2 top -- same-bbox overlap: top wins (PROTOCOL.md §6c)."""
        node = ComposePSD()
        image_out, _mask_out, _filename = node.execute(
            filename_prefix="stack",
            group_name="G",
            edit_after=False,
            unique_id="1",
            image_1=make_tensor(RED, size=(8, 8)),
            image_2=make_tensor(BLUE, size=(8, 8)),
        )
        array = (image_out[0].numpy() * 255.0).round().astype("uint8")
        assert tuple(array[4, 4]) == BLUE  # top layer (image_2) wins the overlap

    def test_single_image_n1(self, context, manager, configured):
        node = ComposePSD()
        image_out, mask_out, filename_out = node.execute(
            filename_prefix="solo",
            group_name="G",
            edit_after=False,
            unique_id="1",
            image_1=make_tensor(GREEN, size=(12, 9)),
        )
        reopened = PSDImage.open(context.input_dir / filename_out)
        assert len(reopened[0]) == 1
        array = (image_out[0].numpy() * 255.0).round().astype("uint8")
        assert array.shape == (9, 12, 3)
        assert tuple(array[0, 0]) == GREEN
        # A single image exactly fills the canvas: fully opaque mask.
        import torch

        assert torch.count_nonzero(mask_out).item() == 0  # 1-alpha == 0 everywhere: fully covered

    def test_eight_images_n8(self, context, manager, configured):
        node = ComposePSD()
        kwargs = {
            f"image_{i}": make_tensor((i * 10 % 255, 0, 0), size=(10 + i, 10 + i))
            for i in range(1, 9)
        }
        _image_out, _mask_out, filename_out = node.execute(
            filename_prefix="eight", group_name="G", edit_after=False, unique_id="1", **kwargs
        )
        reopened = PSDImage.open(context.input_dir / filename_out)
        group = reopened[0]
        assert len(group) == 8
        assert [layer.name for layer in group] == [f"Layer {i}" for i in range(1, 9)]

    def test_mask_reflects_uncovered_canvas_regions(self, context, manager, configured):
        """Canvas dims taken from DIFFERENT inputs: neither alone covers it all."""
        node = ComposePSD()
        _image_out, mask_out, _filename = node.execute(
            filename_prefix="uneven",
            group_name="G",
            edit_after=False,
            unique_id="1",
            image_1=make_tensor(RED, size=(20, 4)),
            image_2=make_tensor(BLUE, size=(4, 20)),
        )
        import torch

        assert torch.count_nonzero(mask_out).item() > 0  # some canvas region is uncovered

    def test_filename_collision_safety_across_executions(self, context, manager, configured):
        node = ComposePSD()
        _, _, first = node.execute(
            filename_prefix="dup",
            group_name="G",
            edit_after=False,
            unique_id="1",
            image_1=make_tensor(RED),
        )
        _, _, second = node.execute(
            filename_prefix="dup",
            group_name="G",
            edit_after=False,
            unique_id="1",
            image_1=make_tensor(GREEN),  # different pixels -> genuinely re-executes
        )
        assert first != second
        assert first == "dup_00001.psd"
        assert second == "dup_00002.psd"


class TestExecuteConsumePath:
    @pytest.fixture(autouse=True)
    def _require_torch(self):
        pytest.importorskip("torch")

    def test_consumes_latest_edit_instead_of_recomposing(
        self, context, manager, configured, monkeypatch
    ):
        tensor = make_tensor(RED, size=(8, 8))
        pil_images = [nodes_module._tensor_to_pil(tensor)]
        inputs_hash = compose_module._compute_inputs_hash(pil_images, "compose", "Group", False)

        meta = manager.create(
            origin_node_id="1",
            origin_kind="load_psd",
            workflow_name="",
            source=SourceRef(filename="compose_00001.psd", subfolder="", type="input"),
            original_image=Image.new("RGBA", (8, 8), (0, 0, 0, 0)),
            source_hash=inputs_hash,
        )
        manager.ingest_edit(meta.handoff_id, Image.new("RGB", (8, 8), (200, 150, 100)), "plugin")

        def _must_not_be_called(*args, **kwargs):
            raise AssertionError("_build_group_psd must not run on the consume path")

        monkeypatch.setattr(compose_module, "_build_group_psd", _must_not_be_called)

        node = ComposePSD()
        image_out, _mask_out, filename_out = node.execute(
            filename_prefix="compose",
            group_name="Group",
            edit_after=False,
            unique_id="1",
            image_1=tensor,
        )
        array = (image_out[0].numpy() * 255.0).round().astype("uint8")
        assert tuple(array[0, 0]) == (200, 150, 100)  # the EDIT's pixels
        assert filename_out == "compose_00001.psd"  # the ORIGINAL generated filename

    def test_stale_handoff_with_different_hash_composes_fresh_instead(
        self, context, manager, configured
    ):
        tensor = make_tensor(RED, size=(8, 8))
        meta = manager.create(
            origin_node_id="1",
            origin_kind="load_psd",
            workflow_name="",
            source=SourceRef(filename="compose_00001.psd", subfolder="", type="input"),
            original_image=Image.new("RGBA", (8, 8), (0, 0, 0, 0)),
            source_hash="deadbeef" * 8,  # does not match the current inputs' real hash
        )
        manager.ingest_edit(meta.handoff_id, Image.new("RGB", (8, 8), (200, 150, 100)), "plugin")

        node = ComposePSD()
        image_out, _mask_out, filename_out = node.execute(
            filename_prefix="compose",
            group_name="Group",
            edit_after=False,
            unique_id="1",
            image_1=tensor,
        )
        array = (image_out[0].numpy() * 255.0).round().astype("uint8")
        assert tuple(array[0, 0]) == RED  # composed fresh, not the stale edit
        assert filename_out == "compose_00001.psd"  # freshly allocated (nothing existed yet)

    def test_no_edits_yet_composes_fresh(self, context, manager, configured):
        tensor = make_tensor(RED, size=(8, 8))
        pil_images = [nodes_module._tensor_to_pil(tensor)]
        inputs_hash = compose_module._compute_inputs_hash(pil_images, "compose", "Group", False)
        manager.create(
            origin_node_id="1",
            origin_kind="load_psd",
            workflow_name="",
            source=SourceRef(filename="compose_00001.psd", subfolder="", type="input"),
            original_image=Image.new("RGBA", (8, 8), (0, 0, 0, 0)),
            source_hash=inputs_hash,
        )  # pending: no edit ingested yet

        node = ComposePSD()
        image_out, _mask_out, _filename = node.execute(
            filename_prefix="compose",
            group_name="Group",
            edit_after=False,
            unique_id="1",
            image_1=tensor,
        )
        array = (image_out[0].numpy() * 255.0).round().astype("uint8")
        assert tuple(array[0, 0]) == RED


class TestEditAfter:
    @pytest.fixture(autouse=True)
    def _require_torch(self):
        pytest.importorskip("torch")

    def test_creates_handoff_and_opens_photoshop(
        self, context, manager, configured_with_app, launches
    ):
        node = ComposePSD()
        _image_out, _mask_out, filename_out = node.execute(
            filename_prefix="compose",
            group_name="Group",
            edit_after=True,
            unique_id="1",
            image_1=make_tensor(RED, size=(8, 8)),
        )

        active = manager.find_active_for_node("1")
        assert active is not None
        assert active.origin_kind == "load_psd"
        assert active.source.filename == filename_out
        assert active.source.subfolder == ""
        assert active.source.type == "input"
        assert active.status == "editing"  # launch_photoshop (fake) succeeded

        # v1: a MANAGED COPY, not edit_in_place.
        assert active.edit_in_place is False
        assert active.original_path is None
        handoff_psd = manager.handoff_dir(active.handoff_id) / "source.psd"
        assert handoff_psd.is_file()
        assert handoff_psd.read_bytes() == (context.input_dir / filename_out).read_bytes()
        assert active.handoff_id in manager._own_source_writes

        assert len(launches) == 1
        assert launches[0] == str(handoff_psd)

    def test_source_hash_matches_inputs_hash_for_consume(
        self, context, manager, configured_with_app
    ):
        tensor = make_tensor(RED, size=(8, 8))
        node = ComposePSD()
        node.execute(
            filename_prefix="compose",
            group_name="Group",
            edit_after=True,
            unique_id="1",
            image_1=tensor,
        )
        active = manager.find_active_for_node("1")
        pil_images = [nodes_module._tensor_to_pil(tensor)]
        expected_hash = compose_module._compute_inputs_hash(pil_images, "compose", "Group", True)
        assert active.source_hash == expected_hash

    def test_edit_after_false_never_creates_a_handoff(self, context, manager, configured_with_app):
        node = ComposePSD()
        node.execute(
            filename_prefix="compose",
            group_name="Group",
            edit_after=False,
            unique_id="1",
            image_1=make_tensor(RED, size=(8, 8)),
        )
        assert manager.find_active_for_node("1") is None

    def test_open_failure_is_logged_and_marked_error_not_a_crash(
        self, context, manager, configured_with_app, monkeypatch
    ):
        def fake_launch_fails(psd_path, override=""):
            return LaunchResult(ok=False, error="no Photoshop found")

        monkeypatch.setattr(routes_module, "launch_photoshop", fake_launch_fails)

        node = ComposePSD()
        # Must not raise -- the composed outputs are still returned.
        image_out, mask_out, filename_out = node.execute(
            filename_prefix="compose",
            group_name="Group",
            edit_after=True,
            unique_id="1",
            image_1=make_tensor(RED, size=(8, 8)),
        )
        assert filename_out == "compose_00001.psd"
        assert image_out is not None
        assert mask_out is not None

        active = manager.find_active_for_node("1")
        assert active is None  # error is a terminal status: no longer "active"
        matching = [h for h in manager.list_all(limit=10) if h.origin_node_id == "1"]
        assert len(matching) == 1
        assert matching[0].status == "error"
