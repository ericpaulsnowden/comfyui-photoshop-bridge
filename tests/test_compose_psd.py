"""PhotoshopComposePSD node (PROTOCOL.md §6c): torch-free import, contract
shape, group-write structure/round-trip, centering math, flatten/mask
outputs, IS_CHANGED sensitivity, the consume path, filename collision
safety, and the three ``mode`` behaviors -- "Don't open (composite only)"
(old always-flat), "Re-run on every save" (non-blocking open + passthrough),
and "Wait for first save" (blocking open-then-wait) -- with their
``bridge_node`` handoff creation.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import threading
import time
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

#: The three ``mode`` COMBO strings, named once here (the first two reuse
#: ``BridgeMode``'s constants verbatim, the third is compose-specific).
WAIT = nodes_module.BridgeMode.WAIT_FIRST_SAVE
RERUN = nodes_module.BridgeMode.RERUN_EVERY_SAVE
DONT_OPEN = compose_module.MODE_DONT_OPEN

#: A short bound for the blocking-wait tests: they either deliver an
#: edit/cancel from a background thread well before this elapses, or
#: deliberately let it expire to exercise the TIMEOUT outcome -- keeping it
#: small keeps the suite fast.
_SHORT_TIMEOUT = 1


def make_tensor(color: tuple[int, int, int], size: tuple[int, int] = (24, 16)) -> np.ndarray:
    """A 1xHxWx3 ComfyUI-layout float32 tensor of a solid color. *size* = (width, height)."""
    img = Image.new("RGB", size, color)
    return np.asarray(img, dtype=np.float32)[None, ...] / 255.0


RED = (255, 0, 0)
GREEN = (0, 255, 0)
BLUE = (0, 0, 255)


def make_batch(colors, size: tuple[int, int] = (24, 16)) -> np.ndarray:
    """An NxHxWx3 ComfyUI-layout tensor: one solid-color frame per color.

    Models a batched IMAGE socket (e.g. a VAE Decode emitting multiple images).
    """
    return np.concatenate([make_tensor(c, size) for c in colors], axis=0)


def make_rgba(
    color: tuple[int, int, int], alpha: int = 255, size: tuple[int, int] = (24, 16)
) -> np.ndarray:
    """A 1xHxWx4 ComfyUI-layout float32 RGBA tensor of a solid color + *alpha*.

    Models a 4-channel IMAGE socket, e.g. a layer-decomposition model like "Qwen
    Image Layered Control" emitting transparent decomposed layers. *size* =
    (width, height).
    """
    r, g, b = color
    img = Image.new("RGBA", size, (r, g, b, alpha))
    return np.asarray(img, dtype=np.float32)[None, ...] / 255.0


def raises_interrupt():
    """The interrupt this test environment surfaces as (no real ComfyUI installed).

    ``nodes._raise_interrupt`` falls back to a plain ``RuntimeError`` when
    ``comfy.model_management`` isn't importable -- exactly the same fallback
    ``test_nodes.py``/``test_annotate.py``'s own blocking-wait tests rely on.
    """
    return pytest.raises(RuntimeError, match=r"comfy\.model_management")


@pytest.fixture
def manager(context: CpsbContext) -> HandoffManager:
    return HandoffManager(context)


@pytest.fixture
def configured(context: CpsbContext, manager: HandoffManager):
    """Lightweight wiring: no real app/loop -- enough for IS_CHANGED/consume/compose
    tests that never reach an open-Photoshop path (i.e. "Don't open" mode, or a
    consume against a handoff that already has an edit).
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
    """Full wiring (real loop + real routes app) for the open-Photoshop modes,
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
        assert "filename_prefix" not in spec["required"]  # removed widget
        assert spec["required"]["group_name"] == ("STRING", {"default": "ComfyUI Layers"})
        assert spec["required"]["layer_name"] == ("STRING", {"default": "Layer"})
        assert spec["hidden"] == {"unique_id": "UNIQUE_ID"}

    def test_mode_combo_is_the_three_protocol_strings(self):
        """PROTOCOL.md §6c: the SAME two BridgeMode strings + the compose-only
        third, default "Wait for first save".
        """
        mode_spec = ComposePSD.INPUT_TYPES()["required"]["mode"]
        options, config = mode_spec
        assert options == [WAIT, RERUN, DONT_OPEN]
        assert options == [
            "Wait for first save",
            "Re-run on every save",
            "Don't open (composite only)",
        ]
        assert config == {"default": WAIT}
        # The third string is NOT the bridge node's OPEN_ONLY ("Open only (don't
        # wait)") -- different text, different behavior (PROTOCOL.md §6c).
        assert DONT_OPEN != nodes_module.BridgeMode.OPEN_ONLY

    def test_timeout_seconds_input(self):
        spec = ComposePSD.INPUT_TYPES()
        assert spec["required"]["timeout_seconds"] == (
            "INT",
            {"default": 1800, "min": 10, "max": 86400},
        )

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

    def test_custom_layer_name_increments(self, tmp_path):
        """The `layer_name` widget names layers `<name> 1..N` (replaces the
        removed `filename_prefix`)."""
        images = [Image.new("RGB", (4, 4), RED), Image.new("RGB", (4, 4), GREEN)]
        psd, _, _, _ = compose_module._build_group_psd(images, "G", "Frame")
        out = tmp_path / "named.psd"
        psd.save(out)
        group = PSDImage.open(out)[0]
        assert [layer.name for layer in group] == ["Frame 1", "Frame 2"]

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

    def test_rgba_layer_reopens_with_transparency(self, tmp_path):
        """PROTOCOL.md §6c use case: a transparent decomposed layer written into
        the PSD reopens (fresh ``PSDImage.open``) still carrying per-pixel
        transparency -- some alpha < 255 -- not flattened to opaque.
        """
        opaque = Image.new("RGB", (8, 8), RED)  # bottom, fully opaque
        top = Image.new("RGBA", (8, 8), (0, 255, 0, 0))  # start fully transparent
        for x in range(4):  # left half half-opaque, right half fully transparent
            for y in range(8):
                top.putpixel((x, y), (0, 255, 0, 128))

        psd, _w, _h, placements = compose_module._build_group_psd([opaque, top], "G")
        # The RGBA source is kept RGBA (alpha not dropped before the write).
        assert placements[1][0].mode == "RGBA"

        out = tmp_path / "rgba.psd"
        psd.save(out)
        group = PSDImage.open(out)[0]
        top_layer = group[1]  # Layer 2 == the RGBA top
        alphas = np.asarray(top_layer.composite().convert("RGBA"))[..., 3]
        assert alphas.min() < 255  # transparency survived the round trip
        assert alphas.max() > 0  # ... and it is not uniformly transparent either


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

    def test_semi_transparent_top_blends_with_layer_below(self):
        """Alpha-aware "over" compositing: a half-opaque top layer BLENDS with the
        opaque layer beneath it rather than fully replacing it.
        """
        placements = [
            (Image.new("RGBA", (4, 4), (255, 0, 0, 255)), 0, 0),  # opaque red (bottom)
            (Image.new("RGBA", (4, 4), (0, 0, 255, 128)), 0, 0),  # ~half blue (top)
        ]
        result = compose_module._flatten_placements(placements, 4, 4)
        r, g, b, a = result.getpixel((0, 0))
        assert a == 255  # bottom is opaque -> composite fully opaque
        # A plain overwrite would give pure blue (255, 0). Blended: both nonzero.
        assert 0 < r < 255
        assert 0 < b < 255
        assert g == 0

    def test_fully_transparent_top_reveals_layer_below(self):
        """A fully-transparent upper layer lets the layer below show through
        (an opaque overwrite would have hidden it).
        """
        placements = [
            (Image.new("RGBA", (4, 4), (255, 0, 0, 255)), 0, 0),  # opaque red
            (Image.new("RGBA", (4, 4), (0, 0, 255, 0)), 0, 0),  # fully transparent blue
        ]
        result = compose_module._flatten_placements(placements, 4, 4)
        assert result.getpixel((0, 0)) == (255, 0, 0, 255)  # red beneath shows through

    def test_rgb_layer_still_overwrites_bbox_opaquely(self):
        """3-channel (all-opaque) path is unchanged: a later RGB layer overwrites
        the earlier one exactly, alpha 255 across its bbox.
        """
        placements = [
            (Image.new("RGB", (4, 4), RED), 0, 0),
            (Image.new("RGB", (4, 4), BLUE), 0, 0),
        ]
        result = compose_module._flatten_placements(placements, 4, 4)
        assert result.getpixel((0, 0)) == (0, 0, 255, 255)  # top RGB layer wins, opaque


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
        h1 = compose_module._compute_inputs_hash(images, "compose", "Group", DONT_OPEN)
        h2 = compose_module._compute_inputs_hash(images, "compose", "Group", DONT_OPEN)
        assert h1 == h2
        assert len(h1) == 64

    def test_changes_with_pixel_content(self):
        h_red = compose_module._compute_inputs_hash(
            [Image.new("RGB", (4, 4), RED)], "compose", "Group", DONT_OPEN
        )
        h_blue = compose_module._compute_inputs_hash(
            [Image.new("RGB", (4, 4), BLUE)], "compose", "Group", DONT_OPEN
        )
        assert h_red != h_blue

    def test_changes_with_image_count(self):
        one = compose_module._compute_inputs_hash(
            [Image.new("RGB", (4, 4), RED)], "compose", "Group", DONT_OPEN
        )
        two = compose_module._compute_inputs_hash(
            [Image.new("RGB", (4, 4), RED)] * 2, "compose", "Group", DONT_OPEN
        )
        assert one != two

    def test_changes_with_order(self):
        a = compose_module._compute_inputs_hash(
            [Image.new("RGB", (4, 4), RED), Image.new("RGB", (4, 4), BLUE)],
            "compose",
            "Group",
            DONT_OPEN,
        )
        b = compose_module._compute_inputs_hash(
            [Image.new("RGB", (4, 4), BLUE), Image.new("RGB", (4, 4), RED)],
            "compose",
            "Group",
            DONT_OPEN,
        )
        assert a != b

    def test_changes_with_filename_prefix(self):
        images = [Image.new("RGB", (4, 4), RED)]
        a = compose_module._compute_inputs_hash(images, "compose", "Group", DONT_OPEN)
        b = compose_module._compute_inputs_hash(images, "other", "Group", DONT_OPEN)
        assert a != b

    def test_changes_with_group_name(self):
        images = [Image.new("RGB", (4, 4), RED)]
        a = compose_module._compute_inputs_hash(images, "compose", "Group", DONT_OPEN)
        b = compose_module._compute_inputs_hash(images, "compose", "Other", DONT_OPEN)
        assert a != b

    def test_changes_with_mode(self):
        """Switching mode changes the hash (so a handoff created under one mode
        never matches a run under another, PROTOCOL.md §6c).
        """
        images = [Image.new("RGB", (4, 4), RED)]
        a = compose_module._compute_inputs_hash(images, "compose", "Group", DONT_OPEN)
        b = compose_module._compute_inputs_hash(images, "compose", "Group", WAIT)
        c = compose_module._compute_inputs_hash(images, "compose", "Group", RERUN)
        assert a != b
        assert a != c
        assert b != c


class TestCollectLayerImages:
    """Batch expansion + the max_layers cap (multi-image IMAGE input -> layers)."""

    def test_frames_to_pils_expands_batch(self):
        pils = compose_module._tensor_frames_to_pils(make_batch([RED, GREEN]))
        assert [p.getpixel((0, 0)) for p in pils] == [RED, GREEN]

    def test_single_socket_batch_expands_to_layers(self):
        kwargs = {"image_1": make_batch([RED, GREEN, BLUE])}
        pils, total = compose_module._collect_layer_images(kwargs, max_layers=64)
        assert total == 3
        assert [p.getpixel((0, 0)) for p in pils] == [RED, GREEN, BLUE]

    def test_multiple_sockets_concatenate_index_then_batch_order(self):
        kwargs = {"image_1": make_batch([RED, GREEN]), "image_2": make_batch([BLUE])}
        pils, total = compose_module._collect_layer_images(kwargs, max_layers=64)
        assert total == 3
        assert [p.getpixel((0, 0)) for p in pils] == [RED, GREEN, BLUE]

    def test_cap_truncates_and_reports_total(self):
        kwargs = {"image_1": make_batch([RED] * 10)}
        pils, total = compose_module._collect_layer_images(kwargs, max_layers=4)
        assert total == 10
        assert len(pils) == 4

    def test_cap_spans_sockets(self):
        kwargs = {"image_1": make_batch([RED] * 3), "image_2": make_batch([BLUE] * 3)}
        pils, total = compose_module._collect_layer_images(kwargs, max_layers=4)
        assert total == 6
        # 3 from image_1 (bottom), then 1 from image_2 -- cap hit mid-socket.
        assert [p.getpixel((0, 0)) for p in pils] == [RED, RED, RED, BLUE]

    def test_no_sockets_is_empty(self):
        pils, total = compose_module._collect_layer_images({}, max_layers=64)
        assert pils == []
        assert total == 0


class TestChannelAwareConversion:
    """The channel count -- not a forced ``"RGB"`` mode -- drives the PIL mode, so
    a 4-channel (RGBA) IMAGE tensor (e.g. "Qwen Image Layered Control") expands
    un-garbled AND keeps its alpha, while the normal 3-channel path is unchanged.
    """

    def test_four_channel_batch_expands_ungarbled_full_array(self):
        """The confirmed bug: forcing mode="RGB" on a 4-byte-per-pixel buffer
        byte-misaligned every pixel past (0, 0) into noise. A per-pixel-distinct
        RGBA frame must now round-trip on the FULL array (not just the corner).
        """
        rng = np.random.default_rng(1234)
        frame = rng.integers(0, 256, size=(5, 7, 4), dtype=np.uint8)  # (H, W, RGBA)
        tensor = frame[None, ...].astype(np.float32) / 255.0

        pil = compose_module._tensor_frames_to_pils(tensor)[0]
        assert pil.mode == "RGBA"
        arr = np.asarray(pil)
        assert arr.shape == (5, 7, 4)
        # Whole-array match, not just (0, 0): proves un-garbling.
        assert np.array_equal(arr[..., :3], frame[..., :3])
        # Alpha preserved end to end.
        assert np.array_equal(arr[..., 3], frame[..., 3])

    def test_solid_rgba_batch_keeps_alpha(self):
        pils = compose_module._tensor_frames_to_pils(make_rgba(RED, alpha=64, size=(6, 6)))
        assert pils[0].mode == "RGBA"
        assert pils[0].getpixel((0, 0)) == (255, 0, 0, 64)

    def test_three_channel_path_unchanged(self):
        """The normal-VAE (3-channel) path stays RGB with the same pixels."""
        pils = compose_module._tensor_frames_to_pils(make_batch([RED, GREEN]))
        assert all(p.mode == "RGB" for p in pils)
        assert [p.getpixel((0, 0)) for p in pils] == [RED, GREEN]

    def test_single_channel_becomes_rgb(self):
        arr = np.zeros((1, 3, 5, 1), dtype=np.float32)
        arr[0, 1, 2, 0] = 1.0  # white at (x=2, y=1)
        pils = compose_module._tensor_frames_to_pils(arr)
        assert pils[0].mode == "RGB"
        assert pils[0].getpixel((2, 1)) == (255, 255, 255)

    def test_two_channel_treated_as_grayscale_rgb(self):
        arr = np.zeros((1, 2, 2, 2), dtype=np.float32)
        arr[0, 0, 0, 0] = 1.0  # first band drives luminance; second band ignored
        pils = compose_module._tensor_frames_to_pils(arr)
        assert pils[0].mode == "RGB"
        assert pils[0].getpixel((0, 0)) == (255, 255, 255)

    def test_frame_without_channel_dim_is_grayscale(self):
        arr = np.zeros((1, 2, 2), dtype=np.float32)  # (N, H, W): each frame is 2-D
        arr[0, 0, 0] = 1.0
        pils = compose_module._tensor_frames_to_pils(arr)
        assert pils[0].mode == "RGB"
        assert pils[0].getpixel((0, 0)) == (255, 255, 255)

    def test_five_channel_keeps_first_four_as_rgba(self):
        """Extra channels beyond RGBA are dropped (first four kept); the sliced
        view is made contiguous so ``Image.fromarray`` reads it correctly.
        """
        rng = np.random.default_rng(99)
        frame = rng.integers(0, 256, size=(4, 4, 5), dtype=np.uint8)
        tensor = frame[None, ...].astype(np.float32) / 255.0
        pil = compose_module._tensor_frames_to_pils(tensor)[0]
        assert pil.mode == "RGBA"
        assert np.array_equal(np.asarray(pil), frame[..., :4])


class TestFlattenAlphaDrivesMask:
    """The flatten's alpha becomes the node's MASK output (via
    ``nodes._tensors_from_image``): opaque -> MASK 0, transparent -> MASK 1.
    """

    @pytest.fixture(autouse=True)
    def _require_torch(self):
        pytest.importorskip("torch")

    def test_transparent_region_yields_mask_one_covered_yields_zero(self):
        # One opaque 4x4 layer on an 8x8 canvas: covered corner opaque, far
        # corner never reached (transparent).
        placements = [(Image.new("RGBA", (4, 4), (0, 255, 0, 255)), 0, 0)]
        result = compose_module._flatten_placements(placements, 8, 8)
        _image, mask = nodes_module._tensors_from_image(result)
        assert mask[0, 0, 0].item() == 0.0  # covered/opaque -> mask 0
        assert mask[0, 7, 7].item() == 1.0  # uncovered/transparent -> mask 1


class TestIsChanged:
    def test_bare_hash_when_no_active_handoff(self, configured):
        value = ComposePSD.IS_CHANGED(
            filename_prefix="compose",
            group_name="Group",
            mode=WAIT,
            timeout_seconds=1800,
            unique_id="1",
            image_1=make_tensor(RED),
        )
        assert len(value) == 64

    def test_changes_when_input_pixels_change(self, configured):
        before = ComposePSD.IS_CHANGED(
            filename_prefix="compose",
            group_name="Group",
            mode=WAIT,
            timeout_seconds=1800,
            unique_id="1",
            image_1=make_tensor(RED),
        )
        after = ComposePSD.IS_CHANGED(
            filename_prefix="compose",
            group_name="Group",
            mode=WAIT,
            timeout_seconds=1800,
            unique_id="1",
            image_1=make_tensor(GREEN),
        )
        assert before != after

    def test_changes_when_param_changes(self, configured):
        before = ComposePSD.IS_CHANGED(
            filename_prefix="compose",
            group_name="Group",
            mode=WAIT,
            timeout_seconds=1800,
            unique_id="1",
            image_1=make_tensor(RED),
        )
        after = ComposePSD.IS_CHANGED(
            filename_prefix="other",
            group_name="Group",
            mode=WAIT,
            timeout_seconds=1800,
            unique_id="1",
            image_1=make_tensor(RED),
        )
        assert before != after

    def test_changes_when_mode_changes(self, configured):
        before = ComposePSD.IS_CHANGED(
            filename_prefix="compose",
            group_name="Group",
            mode=WAIT,
            timeout_seconds=1800,
            unique_id="1",
            image_1=make_tensor(RED),
        )
        after = ComposePSD.IS_CHANGED(
            filename_prefix="compose",
            group_name="Group",
            mode=DONT_OPEN,
            timeout_seconds=1800,
            unique_id="1",
            image_1=make_tensor(RED),
        )
        assert before != after

    def test_timeout_seconds_excluded_from_hash(self, configured):
        """PROTOCOL.md §6c/§6: timeout only bounds the wait, never the output,
        so it must NOT force re-execution -- mirrors the bridge/annotate nodes.
        """
        a = ComposePSD.IS_CHANGED(
            filename_prefix="compose",
            group_name="Group",
            mode=WAIT,
            timeout_seconds=10,
            unique_id="1",
            image_1=make_tensor(RED),
        )
        b = ComposePSD.IS_CHANGED(
            filename_prefix="compose",
            group_name="Group",
            mode=WAIT,
            timeout_seconds=54321,
            unique_id="1",
            image_1=make_tensor(RED),
        )
        assert a == b

    def test_changes_when_matching_edit_arrives(self, context, manager, configured):
        tensor = make_tensor(RED)
        pil_images = [nodes_module._tensor_to_pil(tensor)]
        prefix = compose_module._sanitize_filename_prefix("compose")
        inputs_hash = compose_module._compute_inputs_hash(pil_images, prefix, "Group", WAIT)

        before = ComposePSD.IS_CHANGED(
            filename_prefix="compose",
            group_name="Group",
            mode=WAIT,
            timeout_seconds=1800,
            unique_id="1",
            image_1=tensor,
        )
        assert before == inputs_hash

        meta = manager.create(
            origin_node_id="1",
            origin_kind="bridge_node",
            workflow_name="",
            source=SourceRef(filename="compose_00001.psd", subfolder="", type="input"),
            original_image=Image.new("RGBA", (4, 4), (0, 0, 0, 0)),
            source_hash=inputs_hash,
        )
        manager.ingest_edit(meta.handoff_id, Image.new("RGB", (4, 4), (5, 5, 5)), "plugin")

        after = ComposePSD.IS_CHANGED(
            filename_prefix="compose",
            group_name="Group",
            mode=WAIT,
            timeout_seconds=1800,
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
            mode=WAIT,
            timeout_seconds=1800,
            unique_id="1",
            image_1=tensor,
        )
        assert after2 != after
        assert after2.startswith(inputs_hash + ":")

    def test_mismatched_source_hash_handoff_is_ignored(self, context, manager, configured):
        tensor = make_tensor(RED)
        meta = manager.create(
            origin_node_id="1",
            origin_kind="bridge_node",
            workflow_name="",
            source=SourceRef(filename="compose_00001.psd", subfolder="", type="input"),
            original_image=Image.new("RGBA", (4, 4), (0, 0, 0, 0)),
            source_hash="deadbeef" * 8,
        )
        manager.ingest_edit(meta.handoff_id, Image.new("RGB", (4, 4), (5, 5, 5)), "plugin")

        pil_images = [nodes_module._tensor_to_pil(tensor)]
        expected_bare = compose_module._compute_inputs_hash(pil_images, "compose", "Group", WAIT)
        value = ComposePSD.IS_CHANGED(
            filename_prefix="compose",
            group_name="Group",
            mode=WAIT,
            timeout_seconds=1800,
            unique_id="1",
            image_1=tensor,
        )
        assert value == expected_bare

    def test_wrong_origin_kind_handoff_is_ignored(self, context, manager, configured):
        """A ``load_psd`` handoff (the wrong kind now -- this node writes
        ``bridge_node``) must be ignored even with a matching source_hash+edit.
        """
        tensor = make_tensor(RED)
        pil_images = [nodes_module._tensor_to_pil(tensor)]
        inputs_hash = compose_module._compute_inputs_hash(pil_images, "compose", "Group", WAIT)
        meta = manager.create(
            origin_node_id="1",
            origin_kind="load_psd",
            workflow_name="",
            source=SourceRef(filename="compose_00001.psd", subfolder="", type="input"),
            original_image=Image.new("RGB", (4, 4), (0, 0, 0)),
            source_hash=inputs_hash,
        )
        manager.ingest_edit(meta.handoff_id, Image.new("RGB", (4, 4), (5, 5, 5)), "plugin")

        value = ComposePSD.IS_CHANGED(
            filename_prefix="compose",
            group_name="Group",
            mode=WAIT,
            timeout_seconds=1800,
            unique_id="1",
            image_1=tensor,
        )
        assert value == inputs_hash

    def test_unconfigured_returns_bare_hash_without_raising(self):
        assert nodes_module._state is None
        value = ComposePSD.IS_CHANGED(
            filename_prefix="compose",
            group_name="Group",
            mode=WAIT,
            timeout_seconds=1800,
            unique_id="1",
            image_1=make_tensor(RED),
        )
        assert len(value) == 64


class TestExecuteErrors:
    def test_no_images_raises_value_error(self, context, manager, configured):
        node = ComposePSD()
        with pytest.raises(ValueError, match="at least one"):
            node.execute(
                filename_prefix="compose",
                group_name="Group",
                mode=DONT_OPEN,
                timeout_seconds=1800,
                unique_id="1",
            )

    def test_unconfigured_raises_runtime_error(self):
        assert nodes_module._state is None
        node = ComposePSD()
        with pytest.raises(RuntimeError, match="configure"):
            node.execute(
                filename_prefix="compose",
                group_name="Group",
                mode=DONT_OPEN,
                timeout_seconds=1800,
                unique_id="1",
                image_1=make_tensor(RED),
            )


class TestExecuteCompose:
    """Compose-only behavior, exercised via "Don't open (composite only)"
    mode: build/write the PSD and return the flat composite, never opening
    Photoshop and never creating a handoff.
    """

    @pytest.fixture(autouse=True)
    def _require_torch(self):
        pytest.importorskip("torch")

    def test_writes_psd_and_returns_outputs(self, context, manager, configured):
        node = ComposePSD()
        image_out, mask_out, filename_out = node.execute(
            filename_prefix="compose",
            group_name="My Layers",
            mode=DONT_OPEN,
            timeout_seconds=1800,
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

    def test_batched_input_becomes_layers(self, context, manager, configured):
        """A single batched IMAGE socket (e.g. a VAE Decode's multiple outputs)
        expands frame-by-frame into one PSD layer each."""
        node = ComposePSD()
        _, _, filename_out = node.execute(
            filename_prefix="compose",
            group_name="Batch",
            mode=DONT_OPEN,
            timeout_seconds=1800,
            unique_id="1",
            image_1=make_batch([RED, GREEN, BLUE], size=(8, 8)),
        )
        group = PSDImage.open(context.input_dir / filename_out)[0]
        assert [layer.name for layer in group] == ["Layer 1", "Layer 2", "Layer 3"]

    def test_max_layers_caps_batch(self, context, manager, configured):
        """max_layers bounds the batch: only the first N images become layers."""
        node = ComposePSD()
        _, _, filename_out = node.execute(
            filename_prefix="compose",
            group_name="Capped",
            mode=DONT_OPEN,
            timeout_seconds=1800,
            unique_id="1",
            max_layers=2,
            image_1=make_batch([RED, GREEN, BLUE, RED, GREEN], size=(8, 8)),
        )
        group = PSDImage.open(context.input_dir / filename_out)[0]
        assert [layer.name for layer in group] == ["Layer 1", "Layer 2"]

    def test_image_1_is_bottom_image_n_is_top(self, context, manager, configured):
        """image_1 bottom, image_2 top -- same-bbox overlap: top wins (PROTOCOL.md §6c)."""
        node = ComposePSD()
        image_out, _mask_out, _filename = node.execute(
            filename_prefix="stack",
            group_name="G",
            mode=DONT_OPEN,
            timeout_seconds=1800,
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
            mode=DONT_OPEN,
            timeout_seconds=1800,
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
            filename_prefix="eight",
            group_name="G",
            mode=DONT_OPEN,
            timeout_seconds=1800,
            unique_id="1",
            **kwargs,
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
            mode=DONT_OPEN,
            timeout_seconds=1800,
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
            mode=DONT_OPEN,
            timeout_seconds=1800,
            unique_id="1",
            image_1=make_tensor(RED),
        )
        _, _, second = node.execute(
            filename_prefix="dup",
            group_name="G",
            mode=DONT_OPEN,
            timeout_seconds=1800,
            unique_id="1",
            image_1=make_tensor(GREEN),  # different pixels -> genuinely re-executes
        )
        assert first != second
        assert first == "dup_00001.psd"
        assert second == "dup_00002.psd"

    def test_dont_open_never_opens_or_hands_off(self, context, manager, configured):
        """PROTOCOL.md §6c: "Don't open (composite only)" is the old always-flat
        behavior -- no Photoshop, no handoff.
        """
        node = ComposePSD()
        image_out, _mask_out, filename_out = node.execute(
            filename_prefix="compose",
            group_name="Group",
            mode=DONT_OPEN,
            timeout_seconds=1800,
            unique_id="1",
            image_1=make_tensor(RED, size=(8, 8)),
        )
        array = (image_out[0].numpy() * 255.0).round().astype("uint8")
        assert tuple(array[0, 0]) == RED  # the flat composite, not an edit
        assert filename_out == "compose_00001.psd"
        assert manager.find_active_for_node("1") is None  # never handed off


class TestExecuteConsumePath:
    """The consume path fires FIRST for every mode: an active ``bridge_node``
    handoff whose source_hash matches the current inputs and that carries an
    edit is returned (flattened) without recomposing or reopening.
    """

    @pytest.fixture(autouse=True)
    def _require_torch(self):
        pytest.importorskip("torch")

    def test_consumes_latest_edit_instead_of_recomposing(
        self, context, manager, configured, monkeypatch
    ):
        tensor = make_tensor(RED, size=(8, 8))
        pil_images = [nodes_module._tensor_to_pil(tensor)]
        # WAIT mode (the blocking one) -- proving the consume check runs BEFORE
        # any open/block: an already-saved edit is served without touching PS.
        inputs_hash = compose_module._compute_inputs_hash(pil_images, "compose", "Group", WAIT)

        meta = manager.create(
            origin_node_id="1",
            origin_kind="bridge_node",
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
            mode=WAIT,
            timeout_seconds=1800,
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
            origin_kind="bridge_node",
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
            mode=DONT_OPEN,
            timeout_seconds=1800,
            unique_id="1",
            image_1=tensor,
        )
        array = (image_out[0].numpy() * 255.0).round().astype("uint8")
        assert tuple(array[0, 0]) == RED  # composed fresh, not the stale edit
        assert filename_out == "compose_00001.psd"  # freshly allocated (nothing existed yet)

    def test_no_edits_yet_composes_fresh(self, context, manager, configured):
        tensor = make_tensor(RED, size=(8, 8))
        pil_images = [nodes_module._tensor_to_pil(tensor)]
        inputs_hash = compose_module._compute_inputs_hash(pil_images, "compose", "Group", DONT_OPEN)
        manager.create(
            origin_node_id="1",
            origin_kind="bridge_node",
            workflow_name="",
            source=SourceRef(filename="compose_00001.psd", subfolder="", type="input"),
            original_image=Image.new("RGBA", (8, 8), (0, 0, 0, 0)),
            source_hash=inputs_hash,
        )  # pending: no edit ingested yet

        node = ComposePSD()
        image_out, _mask_out, _filename = node.execute(
            filename_prefix="compose",
            group_name="Group",
            mode=DONT_OPEN,
            timeout_seconds=1800,
            unique_id="1",
            image_1=tensor,
        )
        array = (image_out[0].numpy() * 255.0).round().astype("uint8")
        assert tuple(array[0, 0]) == RED


class TestModeRerunEverySave:
    """PROTOCOL.md §6c: "Re-run on every save" never blocks -- it opens
    Photoshop once, passes the flat composite through, and relies on the
    frontend auto-queueing a re-run per save (each consuming the latest edit).
    """

    @pytest.fixture(autouse=True)
    def _require_torch(self):
        pytest.importorskip("torch")

    def test_opens_once_and_passes_flat_composite_through(
        self, context, manager, configured_with_app, launches
    ):
        node = ComposePSD()
        image_out, _mask_out, filename_out = node.execute(
            filename_prefix="compose",
            group_name="Group",
            mode=RERUN,
            timeout_seconds=1800,
            unique_id="1",
            image_1=make_tensor(RED, size=(8, 8)),
        )
        # Passthrough: the flat composite, NOT a blocked-for edit.
        array = (image_out[0].numpy() * 255.0).round().astype("uint8")
        assert tuple(array[0, 0]) == RED
        assert filename_out == "compose_00001.psd"

        active = manager.find_active_for_node("1")
        assert active is not None
        assert active.origin_kind == "bridge_node"
        assert active.source.filename == filename_out
        assert active.source.subfolder == ""
        assert active.source.type == "input"
        assert active.status == "editing"  # launch_photoshop (fake) succeeded

        # The REAL Tier-1 launch seam fired exactly once, on the handoff's copy.
        assert len(launches) == 1
        handoff_psd = manager.handoff_dir(active.handoff_id) / "source.psd"
        assert launches[0] == str(handoff_psd)

    def test_handoff_is_a_managed_copy_not_edit_in_place(
        self, context, manager, configured_with_app, launches
    ):
        # v1: a MANAGED COPY of the generated LAYERED file, not edit_in_place.
        node = ComposePSD()
        _image_out, _mask_out, filename_out = node.execute(
            filename_prefix="compose",
            group_name="Group",
            mode=RERUN,
            timeout_seconds=1800,
            unique_id="1",
            image_1=make_tensor(RED, size=(8, 8)),
        )
        active = manager.find_active_for_node("1")
        assert active.edit_in_place is False
        assert active.original_path is None
        handoff_psd = manager.handoff_dir(active.handoff_id) / "source.psd"
        assert handoff_psd.is_file()
        # Byte-for-byte copy of the generated file (layers preserved).
        assert handoff_psd.read_bytes() == (context.input_dir / filename_out).read_bytes()
        assert active.handoff_id in manager._own_source_writes

    def test_source_hash_folds_mode_for_consume(self, context, manager, configured_with_app):
        tensor = make_tensor(RED, size=(8, 8))
        node = ComposePSD()
        node.execute(
            filename_prefix="compose",
            group_name="Group",
            mode=RERUN,
            timeout_seconds=1800,
            unique_id="1",
            image_1=tensor,
        )
        active = manager.find_active_for_node("1")
        pil_images = [nodes_module._tensor_to_pil(tensor)]
        expected_hash = compose_module._compute_inputs_hash(pil_images, "compose", "Group", RERUN)
        assert active.source_hash == expected_hash

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
            mode=RERUN,
            timeout_seconds=1800,
            unique_id="1",
            image_1=make_tensor(RED, size=(8, 8)),
        )
        assert filename_out == "compose_00001.psd"
        array = (image_out[0].numpy() * 255.0).round().astype("uint8")
        assert tuple(array[0, 0]) == RED  # the flat composite still comes back
        assert mask_out is not None

        active = manager.find_active_for_node("1")
        assert active is None  # error is a terminal status: no longer "active"
        matching = [h for h in manager.list_all(limit=10) if h.origin_node_id == "1"]
        assert len(matching) == 1
        assert matching[0].status == "error"
        assert matching[0].origin_kind == "bridge_node"


class TestModeWaitFirstSave:
    """PROTOCOL.md §6c (the default): "Wait for first save" BLOCKS execute()
    in manager.wait_for_edit until the first save, then returns that SAVED
    edit (flattened). Cancel/timeout/open-failure interrupt via
    InterruptProcessingException. Mirrors the bridge/annotate nodes.
    """

    @pytest.fixture(autouse=True)
    def _require_torch(self):
        pytest.importorskip("torch")

    def test_blocks_until_edit_then_returns_saved_edit(
        self, context, manager, configured_with_app, monkeypatch
    ):
        node_id = "1"
        edit_color = (10, 20, 30)

        def _save_shortly_after_open(psd_path, override=""):
            # Deliver the edit from a delayed background thread: the launch
            # returning "ok" (mark_editing) is visible before the edit lands,
            # exactly as real Photoshop usage sequences.
            def _do_ingest():
                active = manager.find_active_for_node(node_id)
                manager.ingest_edit(
                    active.handoff_id, Image.new("RGB", (8, 8), edit_color), "plugin"
                )

            threading.Timer(0.3, _do_ingest).start()
            return LaunchResult(ok=True)

        monkeypatch.setattr(routes_module, "launch_photoshop", _save_shortly_after_open)

        node = ComposePSD()
        image_out, _mask_out, filename_out = node.execute(
            filename_prefix="compose",
            group_name="Group",
            mode=WAIT,
            timeout_seconds=10,
            unique_id=node_id,
            image_1=make_tensor(RED, size=(8, 8)),
        )
        # IMAGE is the SAVED edit's pixels, not the flat RED composite.
        array = (image_out[0].numpy() * 255.0).round().astype("uint8")
        assert tuple(array[0, 0]) == edit_color
        # STRING is still the written psd filename (unchanged from the old build).
        assert filename_out == "compose_00001.psd"

        active = manager.find_active_for_node(node_id)
        assert active is not None
        assert active.status == "edited"
        assert active.origin_kind == "bridge_node"

    def test_reaches_the_real_bridge_open_seam(
        self, context, manager, configured_with_app, monkeypatch
    ):
        """Regression: compose must reach ``nodes.PhotoshopBridge._open_in_photoshop``
        (the shared tier-selecting seam) and, through it, the real Tier-1
        launch -- not a private copy.
        """
        node_id = "1"
        seam_calls: list[Path] = []
        real_open = nodes_module.PhotoshopBridge._open_in_photoshop

        def spy_open(state, meta, psd_path):
            seam_calls.append(psd_path)
            return real_open(state, meta, psd_path)

        monkeypatch.setattr(
            nodes_module.PhotoshopBridge, "_open_in_photoshop", staticmethod(spy_open)
        )

        launch_calls: list[str] = []

        def _save_after_open(psd_path, override=""):
            launch_calls.append(str(psd_path))

            def _do_ingest():
                active = manager.find_active_for_node(node_id)
                edit = Image.new("RGB", (8, 8), (1, 2, 3))
                manager.ingest_edit(active.handoff_id, edit, "plugin")

            threading.Timer(0.2, _do_ingest).start()
            return LaunchResult(ok=True)

        monkeypatch.setattr(routes_module, "launch_photoshop", _save_after_open)

        node = ComposePSD()
        node.execute(
            filename_prefix="compose",
            group_name="Group",
            mode=WAIT,
            timeout_seconds=10,
            unique_id=node_id,
            image_1=make_tensor(RED, size=(8, 8)),
        )
        assert len(seam_calls) == 1  # the real bridge seam fired exactly once
        assert len(launch_calls) == 1  # ... and reached the underlying Tier-1 launch
        assert seam_calls[0] == Path(launch_calls[0])  # both on the handoff's own copy

    def test_open_failure_marks_error_and_interrupts_without_hanging(
        self, context, manager, configured_with_app, monkeypatch
    ):
        monkeypatch.setattr(
            routes_module,
            "launch_photoshop",
            lambda psd_path, override="": LaunchResult(ok=False, error="no Photoshop found"),
        )

        node = ComposePSD()
        with raises_interrupt():
            node.execute(
                filename_prefix="compose",
                group_name="Group",
                mode=WAIT,
                timeout_seconds=_SHORT_TIMEOUT,
                unique_id="1",
                image_1=make_tensor(RED, size=(8, 8)),
            )

        assert manager.find_active_for_node("1") is None  # error: no longer active
        matching = [h for h in manager.list_all() if h.origin_node_id == "1"]
        assert len(matching) == 1
        assert matching[0].status == "error"

    def test_timeout_interrupts_and_handoff_stays_editing(
        self, context, manager, configured_with_app, launches
    ):
        """PROTOCOL.md §6c/§6: on timeout the handoff stays `editing`, so a
        later save or re-queue resumes the same PSD.
        """
        node = ComposePSD()
        with raises_interrupt():
            node.execute(
                filename_prefix="compose",
                group_name="Group",
                mode=WAIT,
                timeout_seconds=_SHORT_TIMEOUT,
                unique_id="1",
                image_1=make_tensor(RED, size=(8, 8)),
            )
        active = manager.find_active_for_node("1")
        assert active is not None
        assert active.status == "editing"
        assert len(launches) == 1

    def test_cancel_interrupts_promptly(self, context, manager, configured_with_app, monkeypatch):
        """`/cpsb/cancel` (mark_cancelled) unblocks a waiting node immediately,
        without waiting out the full timeout (PROTOCOL.md §2).
        """
        node_id = "1"

        def _cancel_shortly_after_open(psd_path, override=""):
            def _do_cancel():
                active = manager.find_active_for_node(node_id)
                manager.mark_cancelled(active.handoff_id)

            threading.Timer(0.3, _do_cancel).start()
            return LaunchResult(ok=True)

        monkeypatch.setattr(routes_module, "launch_photoshop", _cancel_shortly_after_open)

        node = ComposePSD()
        start = time.monotonic()
        with raises_interrupt():
            node.execute(
                filename_prefix="compose",
                group_name="Group",
                mode=WAIT,
                timeout_seconds=30,
                unique_id=node_id,
                image_1=make_tensor(RED, size=(8, 8)),
            )
        elapsed = time.monotonic() - start
        assert elapsed < 5  # unblocked by cancellation, not the 30s timeout
