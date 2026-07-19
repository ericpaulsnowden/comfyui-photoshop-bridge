"""PhotoshopComposePSD node (PROTOCOL.md §6c): torch-free import, contract
shape, group-write structure/round-trip, centering math, flatten/mask
outputs, IS_CHANGED sensitivity, the consume path, filename collision
safety, and the three ``mode`` behaviors -- "Don't open (composite only)"
(old always-flat), "Re-run on every save" (non-blocking open + passthrough),
and "Wait for first save" (blocking open-then-wait) -- with their
``bridge_node`` handoff creation. Also covers the "append to existing
document" feature (``append_to_existing``/``existing_psd``/
``existing_psd_path``): target resolution, run-numbered grouping across
successive appends, the missing-target/non-RGB/canvas-mismatch guards, the
atomic-write crash guarantee, and the duplicate-append caching semantics.
"""

from __future__ import annotations

import asyncio
import logging
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


def _assert_written_path(payload: dict, context: CpsbContext) -> None:
    """Shared assertions for the ``path`` field ``_emit_compose_written`` adds
    to a ``cpsb.compose_written`` event payload alongside the pre-existing
    ``filename`` (PROTOCOL-adjacent build brief: "Copy Path" needs the full,
    absolute, server-side location, not just the bare filename): *path* is
    absolute, sits directly inside this test's fixture ``context.input_dir``
    (every compose write lands there or at an ``existing_psd_path`` override
    that these tests always also point inside ``input_dir``, never in a
    subfolder -- matching the payload's own always-``""`` ``subfolder``), and
    ends with the same bare name the payload's ``filename`` field carries.
    """
    path = Path(payload["path"])
    assert path.is_absolute()
    assert path.parent == context.input_dir.resolve()
    assert payload["path"].endswith(payload["filename"])


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
        # `layers` (v0.5.25) is APPENDED so saved workflows' existing links,
        # which ComfyUI stores by output slot index, keep their meaning.
        assert ComposePSD.RETURN_TYPES == ("IMAGE", "MASK", "STRING", "IMAGE")
        assert ComposePSD.RETURN_NAMES == ("image", "mask", "filename", "layers")
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
        # The handoff's OWN source_hash is the mode/prefix-FREE identity hash
        # (the defect-3 fix: a bridge_node handoff's source_hash must never
        # bake `mode` in, or a mere mode flip would strand it) -- NOT the
        # mode-sensitive inputs_hash above, which is IS_CHANGED's own return
        # value only.
        identity_hash = compose_module._compute_identity_hash(pil_images, "Group")

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
            source_hash=identity_hash,
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
        image_out, mask_out, filename_out, _layers_out = node.execute(
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
        _, _, filename_out, _layers_out = node.execute(
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
        _, _, filename_out, _layers_out = node.execute(
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
        image_out, _mask_out, _filename, _layers_out = node.execute(
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
        image_out, mask_out, filename_out, _layers_out = node.execute(
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
        _image_out, _mask_out, filename_out, _layers_out = node.execute(
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
        _image_out, mask_out, _filename, _layers_out = node.execute(
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
        _, _, first, _layers_out = node.execute(
            filename_prefix="dup",
            group_name="G",
            mode=DONT_OPEN,
            timeout_seconds=1800,
            unique_id="1",
            image_1=make_tensor(RED),
        )
        _, _, second, _layers_out = node.execute(
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
        image_out, _mask_out, filename_out, _layers_out = node.execute(
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


class TestLayersOutput:
    """The 4th output, ``layers`` (v0.5.25, product-owner request: "when I
    connect this node to a preview node so I can see all of the layers it
    only shows one image"): an IMAGE batch with one canvas-sized frame per
    placed layer, so a Preview node fans out to one image per layer.
    """

    @pytest.fixture(autouse=True)
    def _require_torch(self):
        pytest.importorskip("torch")

    def test_one_frame_per_layer_at_real_positions(self, context, manager, configured):
        node = ComposePSD()
        # Two different-sized inputs -> canvas 24x16; the 8x8 one is centered.
        _img, _mask, _name, layers = node.execute(
            filename_prefix="compose",
            group_name="Group",
            mode=DONT_OPEN,
            timeout_seconds=1800,
            unique_id="1",
            image_1=make_tensor(RED, size=(24, 16)),
            image_2=make_tensor(BLUE, size=(8, 8)),
        )
        arr = (layers.numpy() * 255.0).round().astype("uint8")
        assert arr.shape == (2, 16, 24, 3)  # N frames, canvas HxW
        # Frame 0: layer 1 alone, filling its own extent.
        assert tuple(arr[0, 0, 0]) == RED
        assert tuple(arr[0, 8, 12]) == RED
        # Frame 1: layer 2 alone, centered ((24-8)//2=8, (16-8)//2=4),
        # black (flattened transparency) everywhere else.
        assert tuple(arr[1, 8, 12]) == BLUE  # inside the centered 8x8
        assert tuple(arr[1, 0, 0]) == (0, 0, 0)  # outside it
        assert tuple(arr[1, 15, 23]) == (0, 0, 0)

    def test_batched_input_expands_to_one_frame_each(self, context, manager, configured):
        """A batched image_1 becomes one PSD layer per frame (v0.5.9), and
        `layers` must mirror that expansion, not the socket count."""
        node = ComposePSD()
        _img, _mask, _name, layers = node.execute(
            filename_prefix="compose",
            group_name="Group",
            mode=DONT_OPEN,
            timeout_seconds=1800,
            unique_id="1",
            image_1=make_batch([RED, GREEN, BLUE], size=(8, 8)),
        )
        arr = (layers.numpy() * 255.0).round().astype("uint8")
        assert arr.shape[0] == 3
        assert tuple(arr[0, 4, 4]) == RED
        assert tuple(arr[1, 4, 4]) == GREEN
        assert tuple(arr[2, 4, 4]) == BLUE

    def test_append_mode_frames_use_target_canvas(self, context, manager, configured, tmp_path):
        """Appending into an existing doc places layers against ITS fixed
        canvas -- the layers frames must be sized to match."""
        target = context.input_dir / "accumulate.psd"
        existing = PSDImage.new(mode="RGB", size=(40, 30), depth=8)
        existing.create_pixel_layer(
            Image.new("RGB", (40, 30), (9, 9, 9)), name="Base", top=0, left=0, opacity=255
        )
        existing.save(target)

        node = ComposePSD()
        _img, _mask, _name, layers = node.execute(
            filename_prefix="compose",
            group_name="Run",
            mode=DONT_OPEN,
            timeout_seconds=1800,
            unique_id="1",
            image_1=make_tensor(RED, size=(8, 8)),
            append_to_existing=True,
            existing_psd="accumulate.psd",
        )
        arr = (layers.numpy() * 255.0).round().astype("uint8")
        assert arr.shape == (1, 30, 40, 3)  # the TARGET's canvas, not 8x8
        assert tuple(arr[0, 15, 20]) == RED  # centered on that canvas

    def test_consume_path_still_returns_layers(self, context, manager, configured):
        """The consume path (an edit came back from Photoshop) returns the
        edited image/mask -- and `layers` must still be this run's WRITTEN
        layers, since identity matched, not crash or go empty. Handoff built
        directly, mirroring test_consumes_latest_edit_instead_of_recomposing.
        """
        tensor = make_tensor(RED, size=(8, 8))
        pil_images = [nodes_module._tensor_to_pil(tensor)]
        identity_hash = compose_module._compute_identity_hash(pil_images, "Group")
        meta = manager.create(
            origin_node_id="1",
            origin_kind="bridge_node",
            workflow_name="",
            source=SourceRef(filename="compose_00001.psd", subfolder="", type="input"),
            original_image=Image.new("RGBA", (8, 8), (0, 0, 0, 0)),
            source_hash=identity_hash,
        )
        manager.ingest_edit(meta.handoff_id, Image.new("RGB", (8, 8), GREEN), "plugin")

        node = ComposePSD()
        image_out, _mask, _name, layers = node.execute(
            filename_prefix="compose",
            group_name="Group",
            mode=RERUN,
            timeout_seconds=1800,
            unique_id="1",
            image_1=tensor,
        )
        image_arr = (image_out[0].numpy() * 255.0).round().astype("uint8")
        assert tuple(image_arr[4, 4]) == GREEN  # the EDIT came through
        arr = (layers.numpy() * 255.0).round().astype("uint8")
        assert arr.shape[0] == 1
        assert tuple(arr[0, 4, 4]) == RED  # `layers` = what was written


class TestComposeWrittenEvent:
    """``cpsb.compose_written`` (product owner gap: "for 'don't open' how do
    I later find and open the file?"): a NEW, minimal, non-handoff event
    fired immediately after every REAL PSD write, for all three ``mode``
    values alike, carrying enough for the frontend to show "Written:
    <filename>" -- and, critically, never creating a handoff/meta.json/
    thumbnail, so "Don't open"'s zero-Photoshop-entanglement contract holds.
    """

    @pytest.fixture(autouse=True)
    def _require_torch(self):
        pytest.importorskip("torch")

    def test_event_payload_shape(self, context, manager, configured, events):
        node = ComposePSD()
        node.execute(
            filename_prefix="compose",
            group_name="Group",
            mode=DONT_OPEN,
            timeout_seconds=1800,
            unique_id="42",
            image_1=make_tensor(RED, size=(8, 8)),
        )
        written_events = events.of_type(compose_module.COMPOSE_WRITTEN_EVENT)
        assert len(written_events) == 1
        payload = written_events[0]
        assert payload["node_id"] == "42"
        assert payload["filename"] == "compose_00001.psd"
        assert payload["subfolder"] == ""
        assert payload["type"] == "input"
        _assert_written_path(payload, context)

    def test_dont_open_emits_the_event_but_creates_no_handoff_or_meta_json(
        self, context, manager, configured, events
    ):
        """The event fires alongside (not instead of) the pre-existing "no
        handoff" guarantee (see ``TestExecuteCompose.
        test_dont_open_never_opens_or_hands_off``) -- this is the regression
        test for the build's own CRITICAL CONSTRAINT: adding the event must
        not create a handoff, a thumbnail, or a ``meta.json``.
        """
        node = ComposePSD()
        _image_out, _mask_out, filename_out, _layers_out = node.execute(
            filename_prefix="compose",
            group_name="Group",
            mode=DONT_OPEN,
            timeout_seconds=1800,
            unique_id="1",
            image_1=make_tensor(RED, size=(8, 8)),
        )
        assert len(events.of_type(compose_module.COMPOSE_WRITTEN_EVENT)) == 1
        assert manager.find_active_for_node("1") is None  # no handoff
        assert manager.list_all(limit=50) == []  # not even a terminal/superseded one
        # No meta.json anywhere under the managed folder -- Path.glob on a
        # not-yet-created directory yields nothing rather than raising, so
        # this holds whether or not cpsb_input_dir exists at all.
        assert list(context.cpsb_input_dir.glob("*/meta.json")) == []
        assert filename_out == "compose_00001.psd"

    def test_rerun_every_save_emits_the_event(
        self, context, manager, configured_with_app, launches, events
    ):
        node = ComposePSD()
        node.execute(
            filename_prefix="compose",
            group_name="Group",
            mode=RERUN,
            timeout_seconds=1800,
            unique_id="2",
            image_1=make_tensor(RED, size=(8, 8)),
        )
        written_events = events.of_type(compose_module.COMPOSE_WRITTEN_EVENT)
        assert len(written_events) == 1
        assert written_events[0]["filename"] == "compose_00001.psd"
        assert written_events[0]["node_id"] == "2"
        # The event is unconditional and unrelated to the handoff this mode
        # ALSO creates -- both exist side by side, not one instead of the
        # other.
        assert manager.find_active_for_node("2") is not None

    def test_wait_first_save_emits_the_event(
        self, context, manager, configured_with_app, monkeypatch, events
    ):
        node_id = "3"

        def _save_shortly_after_open(psd_path, override=""):
            def _do_ingest():
                active = manager.find_active_for_node(node_id)
                edit = Image.new("RGB", (8, 8), (1, 2, 3))
                manager.ingest_edit(active.handoff_id, edit, "plugin")

            threading.Timer(0.2, _do_ingest).start()
            return LaunchResult(ok=True)

        monkeypatch.setattr(routes_module, "launch_photoshop", _save_shortly_after_open)

        node = ComposePSD()
        node.execute(
            filename_prefix="compose",
            group_name="Group",
            mode=WAIT,
            timeout_seconds=10,
            unique_id=node_id,
            image_1=make_tensor(RED, size=(8, 8)),
        )
        written_events = events.of_type(compose_module.COMPOSE_WRITTEN_EVENT)
        # Fired once, for the ORIGINAL compose write -- before the blocking
        # wait even starts, not per-edit.
        assert len(written_events) == 1
        assert written_events[0]["filename"] == "compose_00001.psd"
        assert written_events[0]["node_id"] == node_id

    def test_append_into_existing_target_emits_event_with_target_filename(
        self, context, manager, configured, events
    ):
        target = context.input_dir / "review.psd"
        prior_images = [Image.new("RGB", (10, 10), RED)]
        psd, _, _, _ = compose_module._build_group_psd(prior_images, "Review 1")
        psd.save(target)

        node = ComposePSD()
        node.execute(
            group_name="Review",
            layer_name="Layer",
            mode=DONT_OPEN,
            timeout_seconds=1800,
            unique_id="4",
            append_to_existing=True,
            existing_psd_path=str(target),
            image_1=make_tensor(GREEN, size=(10, 10)),
        )
        written_events = events.of_type(compose_module.COMPOSE_WRITTEN_EVENT)
        assert len(written_events) == 1
        assert written_events[0]["filename"] == "review.psd"
        assert written_events[0]["subfolder"] == ""
        assert written_events[0]["type"] == "input"
        _assert_written_path(written_events[0], context)

    def test_append_creating_missing_target_emits_event(self, context, manager, configured, events):
        target = context.input_dir / "not_yet_created.psd"
        node = ComposePSD()
        node.execute(
            group_name="Review",
            layer_name="Layer",
            mode=DONT_OPEN,
            timeout_seconds=1800,
            unique_id="5",
            append_to_existing=True,
            existing_psd_path=str(target),
            image_1=make_tensor(RED, size=(8, 8)),
        )
        written_events = events.of_type(compose_module.COMPOSE_WRITTEN_EVENT)
        assert len(written_events) == 1
        assert written_events[0]["filename"] == "not_yet_created.psd"
        _assert_written_path(written_events[0], context)

    def test_requeue_while_unsaved_does_not_emit_a_second_event(
        self, context, manager, configured_with_app, launches, events
    ):
        """The duplicate-append guard (``TestAppendDuplicateGuard``) skips the
        real write on a re-queue against an already-open, still-unsaved
        handoff -- so this event, which only ever fires alongside a REAL
        write, must not double-fire either.
        """
        target = context.input_dir / "review.psd"
        node = ComposePSD()
        tensor = make_tensor(RED, size=(8, 8))
        node_id = "6"

        with raises_interrupt():
            node.execute(
                group_name="Review",
                layer_name="Layer",
                mode=WAIT,
                timeout_seconds=_SHORT_TIMEOUT,
                unique_id=node_id,
                append_to_existing=True,
                existing_psd_path=str(target),
                image_1=tensor,
            )
        assert len(events.of_type(compose_module.COMPOSE_WRITTEN_EVENT)) == 1

        with raises_interrupt():
            node.execute(
                group_name="Review",
                layer_name="Layer",
                mode=WAIT,
                timeout_seconds=_SHORT_TIMEOUT,
                unique_id=node_id,
                append_to_existing=True,
                existing_psd_path=str(target),
                image_1=tensor,
            )
        # Still just one -- the second call reused the pending handoff and
        # skipped the re-append (TestAppendDuplicateGuard), so no second
        # write means no second event.
        assert len(events.of_type(compose_module.COMPOSE_WRITTEN_EVENT)) == 1

    def test_consume_path_does_not_emit_a_new_event(self, context, manager, configured, events):
        """Serving an already-arrived edit (the consume path, run FIRST for
        every mode) never recomposes or rewrites the PSD -- so it must not
        emit this event either, exactly like it must not touch
        ``_build_group_psd`` (see ``TestExecuteConsumePath.
        test_consumes_latest_edit_instead_of_recomposing``, whose
        monkeypatch guard this test mirrors).
        """
        tensor = make_tensor(RED, size=(8, 8))
        pil_images = [nodes_module._tensor_to_pil(tensor)]
        identity_hash = compose_module._compute_identity_hash(pil_images, "Group")

        meta = manager.create(
            origin_node_id="7",
            origin_kind="bridge_node",
            workflow_name="",
            source=SourceRef(filename="compose_00001.psd", subfolder="", type="input"),
            original_image=Image.new("RGBA", (8, 8), (0, 0, 0, 0)),
            source_hash=identity_hash,
        )
        manager.ingest_edit(meta.handoff_id, Image.new("RGB", (8, 8), (200, 150, 100)), "plugin")
        events.events.clear()  # only care about what execute() itself emits below

        node = ComposePSD()
        node.execute(
            filename_prefix="compose",
            group_name="Group",
            mode=WAIT,
            timeout_seconds=1800,
            unique_id="7",
            image_1=tensor,
        )
        assert events.of_type(compose_module.COMPOSE_WRITTEN_EVENT) == []


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
        # The handoff's own source_hash is the mode/prefix-FREE identity hash
        # (see _compute_identity_hash's docstring) -- NOT the mode-sensitive
        # _compute_inputs_hash value, which would make execute()'s reuse
        # check treat this handoff as stale (source_hash mismatch) and
        # supersede it instead of consuming its edit.
        identity_hash = compose_module._compute_identity_hash(pil_images, "Group")

        meta = manager.create(
            origin_node_id="1",
            origin_kind="bridge_node",
            workflow_name="",
            source=SourceRef(filename="compose_00001.psd", subfolder="", type="input"),
            original_image=Image.new("RGBA", (8, 8), (0, 0, 0, 0)),
            source_hash=identity_hash,
        )
        manager.ingest_edit(meta.handoff_id, Image.new("RGB", (8, 8), (200, 150, 100)), "plugin")

        def _must_not_be_called(*args, **kwargs):
            raise AssertionError("_build_group_psd must not run on the consume path")

        monkeypatch.setattr(compose_module, "_build_group_psd", _must_not_be_called)

        node = ComposePSD()
        image_out, _mask_out, filename_out, _layers_out = node.execute(
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
        image_out, _mask_out, filename_out, _layers_out = node.execute(
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
        image_out, _mask_out, _filename, _layers_out = node.execute(
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
        image_out, _mask_out, filename_out, _layers_out = node.execute(
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
        handoff_psd = manager.psd_path(active)
        assert handoff_psd.name == "compose_00001.psd"  # derived from the compose output's own name
        assert launches[0] == str(handoff_psd)

    def test_rerun_while_already_open_does_not_relaunch_photoshop(
        self, context, manager, configured_with_app, launches
    ):
        """Reusing an already-open handoff in a NON-BLOCKING mode must not
        reopen Photoshop -- same rule as cpsb/nodes.py:434-452.

        "Re-run on every save" re-executes on EVERY save, so relaunching on
        reuse would yank focus back to Photoshop (and re-issue an OS open on
        Tier 1) on every single one. That is the user-reported "fires off a
        bunch of quick commands" disruption, and it is distinct from the
        duplicate-handoff bug: even with reuse working correctly, an
        unconditional reopen here would still be wrong.
        """
        node = ComposePSD()
        tensor = make_tensor(RED, size=(8, 8))
        kwargs = dict(
            filename_prefix="compose",
            group_name="Group",
            mode=RERUN,
            timeout_seconds=1800,
            unique_id="1",
            image_1=tensor,
        )

        node.execute(**kwargs)
        first = manager.find_active_for_node("1")
        assert len(launches) == 1  # the genuinely new handoff opened once

        # Re-run with IDENTICAL inputs, exactly as a save-triggered re-queue
        # does. Same handoff, and crucially no second launch.
        node.execute(**kwargs)
        assert manager.find_active_for_node("1").handoff_id == first.handoff_id
        assert len(launches) == 1, "reused an open handoff but relaunched Photoshop anyway"

    def test_handoff_is_a_managed_copy_not_edit_in_place(
        self, context, manager, configured_with_app, launches
    ):
        # v1: a MANAGED COPY of the generated LAYERED file, not edit_in_place.
        node = ComposePSD()
        _image_out, _mask_out, filename_out, _layers_out = node.execute(
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
        handoff_psd = manager.psd_path(active)
        assert handoff_psd.is_file()
        # Byte-for-byte copy of the generated file (layers preserved).
        assert handoff_psd.read_bytes() == (context.input_dir / filename_out).read_bytes()
        assert active.handoff_id in manager._own_source_writes

    def test_source_hash_is_mode_free_identity(self, context, manager, configured_with_app):
        """A ``bridge_node`` handoff's ``source_hash`` is the mode/prefix-FREE
        identity hash, NOT :func:`compose_module._compute_inputs_hash`'s
        mode-sensitive value.

        This test used to assert the OPPOSITE (``source_hash ==
        _compute_inputs_hash(..., RERUN)``) -- that was the confirmed bug:
        folding ``mode`` into the recorded ``source_hash`` meant a later
        mode flip (e.g. to "Wait for first save") changed the identity a
        reused handoff is matched against, so the already-open handoff could
        never be recognized again and a second one (and a second live
        Photoshop document) got created underneath it. See
        ``_compute_identity_hash``'s docstring in ``cpsb/compose_psd.py``.
        """
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
        expected_hash = compose_module._compute_identity_hash(pil_images, "Group")
        assert active.source_hash == expected_hash

    def test_open_failure_is_logged_and_marked_error_not_a_crash(
        self, context, manager, configured_with_app, monkeypatch
    ):
        def fake_launch_fails(psd_path, override=""):
            return LaunchResult(ok=False, error="no Photoshop found")

        monkeypatch.setattr(routes_module, "launch_photoshop", fake_launch_fails)

        node = ComposePSD()
        # Must not raise -- the composed outputs are still returned.
        image_out, mask_out, filename_out, _layers_out = node.execute(
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
        image_out, _mask_out, filename_out, _layers_out = node.execute(
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


class TestHandoffIdentityReuseAndSupersede:
    """Regression coverage for the confirmed "spins forever" / "slew of new
    documents" bug: HANDOFF IDENTITY. The waiter must poll the SAME
    handoff_id ingest_edit writes to -- across a plain re-queue while
    unsaved (defect 1: execute() had no reuse path at all, only
    ``_find_matching_active_handoff``, whose predicate requires non-empty
    ``edits`` and so never recognized an open-but-unsaved handoff as
    anything but "no handoff") and across a bare ``mode`` widget flip
    (defect 3: the OLD code folded ``mode`` into the very value recorded as
    ``source_hash``, so a mode flip alone changed a handoff's identity and
    it could never be matched again). Mirrors
    ``tests/test_annotate.py::TestPsModeBlocking``'s
    ``test_requeue_after_timeout_reuses_and_reopens_same_handoff`` /
    ``test_stale_handoff_from_changed_input_is_superseded_and_reopened``
    shapes.
    """

    @pytest.fixture(autouse=True)
    def _require_torch(self):
        pytest.importorskip("torch")

    def test_requeue_while_unsaved_reuses_same_handoff_not_a_second_one(
        self, context, manager, configured_with_app, launches
    ):
        """THE actual spin bug: re-queueing "Wait for first save" while the
        previously-opened handoff is still unsaved must reuse it (and
        reopen it), never mint a second handoff/document.
        """
        node_id = "500"
        tensor = make_tensor(RED, size=(8, 8))
        node = ComposePSD()

        # First queue: nobody saves before the short timeout -- the user's
        # first (uninterrupted) attempt. The handoff stays "editing".
        with raises_interrupt():
            node.execute(
                group_name="Group",
                mode=WAIT,
                timeout_seconds=_SHORT_TIMEOUT,
                unique_id=node_id,
                image_1=tensor,
            )
        first_active = manager.find_active_for_node(node_id)
        assert first_active is not None
        assert first_active.status == "editing"
        assert len(launches) == 1

        # Re-queue: same inputs, same mode, STILL unsaved -- exactly the
        # user's "it just spins" report.
        with raises_interrupt():
            node.execute(
                group_name="Group",
                mode=WAIT,
                timeout_seconds=_SHORT_TIMEOUT,
                unique_id=node_id,
                image_1=tensor,
            )
        second_active = manager.find_active_for_node(node_id)
        assert second_active is not None
        assert second_active.handoff_id == first_active.handoff_id  # reused, not a fresh one
        assert len(launches) == 2  # reopened the SAME handoff's source.psd

        # No second handoff was ever created for this node.
        matching = [h for h in manager.list_all(limit=50) if h.origin_node_id == node_id]
        assert len(matching) == 1

    def test_mode_flip_alone_does_not_strand_or_duplicate_the_handoff(
        self, context, manager, configured_with_app, launches
    ):
        """Flipping ONLY the `mode` widget, with identical images, must not
        strand the open handoff nor mint a second one (the OLD code folded
        `mode` into the handoff's own source_hash, so a mode flip changed
        its identity and it could never be matched again).
        """
        node_id = "501"
        tensor = make_tensor(RED, size=(8, 8))
        node = ComposePSD()

        # Open via "Re-run on every save" (non-blocking) -- creates the handoff.
        node.execute(
            group_name="Group",
            mode=RERUN,
            timeout_seconds=1800,
            unique_id=node_id,
            image_1=tensor,
        )
        first_active = manager.find_active_for_node(node_id)
        assert first_active is not None
        assert len(launches) == 1

        # Flip ONLY mode -- to "Wait for first save" -- with the SAME image.
        # Nobody saves, so this blocks until the short timeout.
        with raises_interrupt():
            node.execute(
                group_name="Group",
                mode=WAIT,
                timeout_seconds=_SHORT_TIMEOUT,
                unique_id=node_id,
                image_1=tensor,
            )
        second_active = manager.find_active_for_node(node_id)
        assert second_active is not None
        assert second_active.handoff_id == first_active.handoff_id  # same handoff, mode-flip-proof
        assert len(launches) == 2  # reopened (mode == WAIT_FIRST_SAVE always reopens)

        matching = [h for h in manager.list_all(limit=50) if h.origin_node_id == node_id]
        assert len(matching) == 1  # never stranded/duplicated across the mode flip

    def test_changed_images_still_supersede_and_create_a_new_handoff(
        self, context, manager, configured_with_app, launches
    ):
        """The behavior that MUST be preserved: genuinely different inputs
        retire the old handoff and open a real new one (unlike a mere mode
        flip, this really is "a different desired output").
        """
        node_id = "502"
        node = ComposePSD()
        node.execute(
            group_name="Group",
            mode=RERUN,
            timeout_seconds=1800,
            unique_id=node_id,
            image_1=make_tensor(RED, size=(8, 8)),
        )
        old_active = manager.find_active_for_node(node_id)
        assert old_active is not None

        node.execute(
            group_name="Group",
            mode=RERUN,
            timeout_seconds=1800,
            unique_id=node_id,
            image_1=make_tensor(GREEN, size=(8, 8)),  # genuinely different pixels
        )
        assert manager.get(old_active.handoff_id).status == "superseded"
        new_active = manager.find_active_for_node(node_id)
        assert new_active is not None
        assert new_active.handoff_id != old_active.handoff_id
        assert len(launches) == 2  # both the old and the new handoff got opened

    def test_dont_open_mode_supersedes_an_active_handoff(
        self, context, manager, configured_with_app, launches
    ):
        """Switching to "Don't open (composite only)" must retire a still-open
        handoff for this node -- otherwise it strands a live Photoshop
        document nobody will ever consume (item D of the fix).
        """
        node_id = "503"
        tensor = make_tensor(RED, size=(8, 8))
        node = ComposePSD()
        node.execute(
            group_name="Group",
            mode=RERUN,
            timeout_seconds=1800,
            unique_id=node_id,
            image_1=tensor,
        )
        active = manager.find_active_for_node(node_id)
        assert active is not None

        image_out, _mask_out, _filename_out, _layers_out = node.execute(
            group_name="Group",
            mode=DONT_OPEN,
            timeout_seconds=1800,
            unique_id=node_id,
            image_1=tensor,
        )
        assert manager.get(active.handoff_id).status == "superseded"
        assert manager.find_active_for_node(node_id) is None
        array = (image_out[0].numpy() * 255.0).round().astype("uint8")
        assert tuple(array[0, 0]) == RED  # still returns the flat composite

    def test_ingest_into_reused_handoff_unblocks_the_waiting_node(
        self, context, manager, configured_with_app, monkeypatch
    ):
        """End-to-end-ish reproduction of the exact user report: open (wait),
        then simulate the Photoshop plugin's upload route ingesting a save
        into the handoff the node is ACTUALLY waiting on -- the wait must
        return the edited pixels, not time out.

        Before the fix, a re-queue while unsaved minted a brand-new second
        handoff and waited on THAT one, while the plugin/user's save always
        lands on the file physically open in Photoshop -- the ORIGINAL
        handoff's ``source.psd``, unchanged since the first call. Ingesting
        into ``first_active.handoff_id`` below models exactly that: it only
        unblocks the second call if that original handoff is the one being
        reused/waited on.
        """
        node_id = "504"
        tensor = make_tensor(RED, size=(8, 8))
        node = ComposePSD()

        monkeypatch.setattr(
            routes_module, "launch_photoshop", lambda p, override="": LaunchResult(ok=True)
        )
        with raises_interrupt():
            node.execute(
                group_name="Group",
                mode=WAIT,
                timeout_seconds=_SHORT_TIMEOUT,
                unique_id=node_id,
                image_1=tensor,
            )
        first_active = manager.find_active_for_node(node_id)
        assert first_active is not None
        assert first_active.status == "editing"

        edit_color = (11, 22, 33)

        def _save_partway_through(psd_path, override=""):
            def _do_ingest():
                manager.ingest_edit(
                    first_active.handoff_id,
                    Image.new("RGB", (8, 8), edit_color),
                    "plugin",
                )

            threading.Timer(0.3, _do_ingest).start()
            return LaunchResult(ok=True)

        monkeypatch.setattr(routes_module, "launch_photoshop", _save_partway_through)

        # Re-queue: the user still hasn't saved when this second call starts.
        image_out, _mask_out, filename_out, _layers_out = node.execute(
            group_name="Group",
            mode=WAIT,
            timeout_seconds=10,
            unique_id=node_id,
            image_1=tensor,
        )
        array = (image_out[0].numpy() * 255.0).round().astype("uint8")
        assert tuple(array[0, 0]) == edit_color  # the save's pixels, NOT a timeout

        second_active = manager.find_active_for_node(node_id)
        assert second_active is not None
        assert second_active.handoff_id == first_active.handoff_id  # same handoff throughout
        assert filename_out == first_active.source.filename


def _write_target(path: Path, images, group_name: str, layer_name: str = "Layer") -> None:
    """Test helper: build a grouped PSD (:func:`compose_module._build_group_psd`)
    and save it directly to *path* -- used to set up a pre-existing
    ``append_to_existing`` target the way a prior run (or a hand-authored
    review document) would have left one.
    """
    psd, _w, _h, _placements = compose_module._build_group_psd(images, group_name, layer_name)
    psd.save(path)


class TestAppendWidgetsShape:
    """INPUT_TYPES shape for the three new "append to existing document"
    widgets -- appended at the END of ``required`` (widget-value-by-position
    means anywhere else would corrupt every saved workflow's existing
    values).
    """

    def test_widgets_appended_at_the_end_in_order(self):
        required = ComposePSD.INPUT_TYPES()["required"]
        keys = list(required.keys())
        assert keys[-3:] == ["append_to_existing", "existing_psd", "existing_psd_path"]

    def test_defaults(self):
        required = ComposePSD.INPUT_TYPES()["required"]
        assert required["append_to_existing"] == ("BOOLEAN", {"default": False})
        assert required["existing_psd_path"] == ("STRING", {"default": ""})
        assert required["existing_psd"] == ([],)  # unconfigured backend: no crash, empty combo

    def test_existing_psd_combo_lists_psd_files_when_configured(self, context, manager, configured):
        (context.input_dir / "a.psd").write_bytes(b"x")
        (context.input_dir / "b.psb").write_bytes(b"x")
        (context.input_dir / "not_a_psd.png").write_bytes(b"x")
        (options,) = ComposePSD.INPUT_TYPES()["required"]["existing_psd"]
        assert options == ["a.psd", "b.psb"]


class TestListPsdFiles:
    def test_lists_psd_and_psb_sorted(self, tmp_path):
        (tmp_path / "z.psd").write_bytes(b"x")
        (tmp_path / "a.psb").write_bytes(b"x")
        (tmp_path / "ignored.txt").write_bytes(b"x")
        assert compose_module._list_psd_files(tmp_path) == ["a.psb", "z.psd"]

    def test_missing_dir_is_empty(self, tmp_path):
        assert compose_module._list_psd_files(tmp_path / "does_not_exist") == []

    def test_non_recursive(self, tmp_path):
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "nested.psd").write_bytes(b"x")
        assert compose_module._list_psd_files(tmp_path) == []


class TestAppendTargetKey:
    def test_override_wins_when_non_empty(self):
        assert compose_module._append_target_key("combo.psd", "  /abs/override.psd  ") == (
            "/abs/override.psd"
        )

    def test_falls_back_to_combo(self):
        assert compose_module._append_target_key("combo.psd", "") == "combo.psd"

    def test_both_empty_is_empty(self):
        assert compose_module._append_target_key("", "") == ""


class TestResolveAppendTarget:
    def test_override_used_verbatim(self, context):
        result = compose_module._resolve_append_target(context, "", "/somewhere/else/target.psd")
        assert result == Path("/somewhere/else/target.psd")

    def test_override_with_bad_suffix_raises(self, context):
        with pytest.raises(ValueError, match=r"existing_psd_path must be a \.psd/\.psb"):
            compose_module._resolve_append_target(context, "", "/somewhere/else/target.png")

    def test_combo_resolves_relative_to_input_dir(self, context):
        (context.input_dir / "review.psd").write_bytes(b"x")
        result = compose_module._resolve_append_target(context, "review.psd", "")
        assert result == (context.input_dir / "review.psd").resolve()

    def test_combo_with_bad_suffix_raises(self, context):
        with pytest.raises(ValueError, match=r"existing_psd must be a \.psd/\.psb"):
            compose_module._resolve_append_target(context, "review.png", "")

    def test_empty_combo_and_empty_override_raises(self, context):
        with pytest.raises(ValueError, match="no existing_psd file is selected"):
            compose_module._resolve_append_target(context, "", "")

    def test_path_traversal_in_combo_is_rejected(self, context):
        with pytest.raises(ValueError, match="escapes the input directory"):
            compose_module._resolve_append_target(context, "../outside.psd", "")

    def test_missing_target_is_still_a_valid_resolution(self, context):
        # Resolution succeeds even though nothing exists at the path yet --
        # "point at a file that isn't there yet" is a first-run convenience,
        # not a resolution-time error (execute() creates it fresh).
        result = compose_module._resolve_append_target(context, "not_yet_created.psd", "")
        assert result == (context.input_dir / "not_yet_created.psd").resolve()
        assert not result.exists()


class TestNextRunGroupName:
    def test_missing_target_is_run_one(self):
        assert compose_module._next_run_group_name(None, "Review") == "Review 1"

    def test_increments_past_highest_existing_run(self, tmp_path):
        images = [Image.new("RGB", (4, 4), RED)]
        psd, _, _, _placements = compose_module._build_group_psd(images, "Review 1")
        compose_module._append_run_into_psd(psd, images, "Review 2", "Layer")
        out = tmp_path / "t.psd"
        psd.save(out)
        reopened = PSDImage.open(out)
        assert compose_module._next_run_group_name(reopened, "Review") == "Review 3"

    def test_bare_unnumbered_group_is_ignored(self, tmp_path):
        images = [Image.new("RGB", (4, 4), RED)]
        psd, _, _, _ = compose_module._build_group_psd(images, "Review")  # no trailing number
        out = tmp_path / "t.psd"
        psd.save(out)
        reopened = PSDImage.open(out)
        # A bare "Review" (no number) must not be treated as "run 0".
        assert compose_module._next_run_group_name(reopened, "Review") == "Review 1"

    def test_unrelated_group_names_are_not_counted(self, tmp_path):
        images = [Image.new("RGB", (4, 4), RED)]
        psd, _, _, _ = compose_module._build_group_psd(images, "Manual Edits")
        out = tmp_path / "t.psd"
        psd.save(out)
        reopened = PSDImage.open(out)
        assert compose_module._next_run_group_name(reopened, "Review") == "Review 1"


class TestEnsureRgbTarget:
    def test_rgb_passes(self, tmp_path):
        psd = PSDImage.new(mode="RGB", size=(4, 4), depth=8)
        compose_module._ensure_rgb_target(psd, tmp_path / "x.psd")  # no raise

    def test_grayscale_raises_naming_the_mode(self, tmp_path):
        psd = PSDImage.new(mode="L", size=(4, 4), depth=8)
        with pytest.raises(ValueError, match="GRAYSCALE"):
            compose_module._ensure_rgb_target(psd, tmp_path / "gray.psd")

    def test_cmyk_raises_naming_the_mode(self, tmp_path):
        psd = PSDImage.new(mode="CMYK", size=(4, 4), depth=8)
        with pytest.raises(ValueError, match="CMYK"):
            compose_module._ensure_rgb_target(psd, tmp_path / "cmyk.psd")


class TestAtomicSave:
    """Unit-level coverage of :func:`compose_module._atomic_save` directly
    (CONSTRAINT 3 -- ``PSDImage.save`` truncates its destination immediately),
    complementing the execute()-level crash test in ``TestAppendExecute``.
    """

    def test_writes_successfully_and_no_temp_file_left_behind(self, tmp_path):
        target = tmp_path / "out.psd"
        images = [Image.new("RGB", (4, 4), RED)]
        psd, _, _, _ = compose_module._build_group_psd(images, "G")
        compose_module._atomic_save(psd, target)
        assert target.is_file()
        assert list(tmp_path.iterdir()) == [target]

    def test_failure_leaves_pre_existing_target_untouched_and_cleans_up_temp(
        self, tmp_path, monkeypatch
    ):
        target = tmp_path / "protected.psd"
        original_images = [Image.new("RGB", (6, 6), RED)]
        _write_target(target, original_images, "Review 1")
        original_bytes = target.read_bytes()

        def _boom(self, fp, mode="wb", **kwargs):
            raise RuntimeError("simulated crash mid-serialize")

        monkeypatch.setattr(PSDImage, "save", _boom)

        new_images = [Image.new("RGB", (6, 6), BLUE)]
        psd, _, _, _ = compose_module._build_group_psd(new_images, "Review 2")
        with pytest.raises(RuntimeError, match="simulated crash"):
            compose_module._atomic_save(psd, target)

        assert target.read_bytes() == original_bytes  # byte-for-byte untouched
        assert list(tmp_path.iterdir()) == [target]  # no leftover .tmp file


class TestAppendExecute:
    """``append_to_existing=True`` end-to-end, via :meth:`ComposePSD.execute`
    (mode="Don't open (composite only)" unless a test specifically needs the
    handoff/Photoshop-open machinery).
    """

    @pytest.fixture(autouse=True)
    def _require_torch(self):
        pytest.importorskip("torch")

    def test_appends_into_existing_doc_preserves_prior_layers_and_groups(
        self, context, manager, configured
    ):
        target = context.input_dir / "review.psd"
        # A prior run's own group, PLUS an unrelated hand-authored group --
        # both must survive completely untouched.
        prior_images = [Image.new("RGB", (10, 10), RED)]
        psd, _, _, _ = compose_module._build_group_psd(prior_images, "Review 1")
        manual_layer = psd.create_pixel_layer(
            Image.new("RGB", (4, 4), (9, 9, 9)), name="Manual Layer", top=0, left=0
        )
        psd.create_group(layer_list=[manual_layer], name="Manual Edits")
        psd.save(target)

        node = ComposePSD()
        node.execute(
            group_name="Review",
            layer_name="Layer",
            mode=DONT_OPEN,
            timeout_seconds=1800,
            unique_id="1",
            append_to_existing=True,
            existing_psd_path=str(target),
            image_1=make_tensor(GREEN, size=(10, 10)),
        )

        reopened = PSDImage.open(target)
        assert [c.name for c in reopened] == ["Review 1", "Manual Edits", "Review 2"]
        # Prior content byte-identical in structure/position/pixels.
        prior_group = reopened[0]
        assert prior_group.kind == "group"
        assert [layer.name for layer in prior_group] == ["Layer 1"]
        assert prior_group[0].bbox == (0, 0, 10, 10)
        arr = np.asarray(prior_group[0].composite().convert("RGB"))
        assert tuple(int(v) for v in arr[0, 0]) == RED
        # The unrelated manual group is untouched too.
        manual_group = reopened[1]
        assert [layer.name for layer in manual_group] == ["Manual Layer"]
        # The new run landed on top, correctly named/numbered.
        new_group = reopened[2]
        assert new_group.name == "Review 2"
        assert [layer.name for layer in new_group] == ["Layer 1"]
        new_arr = np.asarray(new_group[0].composite().convert("RGB"))
        assert tuple(int(v) for v in new_arr[0, 0]) == GREEN

    def test_three_successive_appends_accumulate_distinguishably(
        self, context, manager, configured
    ):
        target = context.input_dir / "review.psd"
        node = ComposePSD()
        colors = [RED, GREEN, BLUE]
        for color in colors:
            node.execute(
                group_name="Review",
                layer_name="Layer",
                mode=DONT_OPEN,
                timeout_seconds=1800,
                unique_id="1",
                append_to_existing=True,
                existing_psd_path=str(target),
                image_1=make_tensor(color, size=(8, 8)),
            )

        reopened = PSDImage.open(target)
        assert [c.name for c in reopened] == ["Review 1", "Review 2", "Review 3"]
        for group, color in zip(reopened, colors, strict=True):
            arr = np.asarray(group[0].composite().convert("RGB"))
            assert tuple(int(v) for v in arr[0, 0]) == color

    def test_missing_target_is_created_fresh(self, context, manager, configured):
        target = context.input_dir / "not_yet_created.psd"
        assert not target.exists()
        node = ComposePSD()
        _, _, filename_out, _layers_out = node.execute(
            group_name="Review",
            layer_name="Layer",
            mode=DONT_OPEN,
            timeout_seconds=1800,
            unique_id="1",
            append_to_existing=True,
            existing_psd_path=str(target),
            image_1=make_tensor(RED, size=(8, 8)),
        )
        assert target.is_file()
        assert filename_out == "not_yet_created.psd"
        reopened = PSDImage.open(target)
        assert [c.name for c in reopened] == ["Review 1"]  # first run: numbered from 1

    def test_non_rgb_target_is_refused_with_clear_error(self, context, manager, configured):
        target = context.input_dir / "cmyk.psd"
        psd = PSDImage.new(mode="CMYK", size=(8, 8), depth=8)
        psd.save(target)

        node = ComposePSD()
        with pytest.raises(ValueError, match="CMYK"):
            node.execute(
                group_name="Review",
                layer_name="Layer",
                mode=DONT_OPEN,
                timeout_seconds=1800,
                unique_id="1",
                append_to_existing=True,
                existing_psd_path=str(target),
                image_1=make_tensor(RED, size=(8, 8)),
            )
        # Refused before any mutation: the file is untouched.
        reopened = PSDImage.open(target)
        assert len(reopened) == 0

    def test_canvas_mismatch_warns_but_succeeds(self, context, manager, configured, caplog):
        target = context.input_dir / "small_canvas.psd"
        _write_target(target, [Image.new("RGB", (6, 6), RED)], "Review 1")

        node = ComposePSD()
        with caplog.at_level(logging.WARNING, logger="cpsb"):
            node.execute(
                group_name="Review",
                layer_name="Layer",
                mode=DONT_OPEN,
                timeout_seconds=1800,
                unique_id="1",
                append_to_existing=True,
                existing_psd_path=str(target),
                image_1=make_tensor(GREEN, size=(20, 20)),  # bigger than the 6x6 canvas
            )

        assert "clipped" in caplog.text
        assert "6x6" in caplog.text
        assert "20x20" in caplog.text
        # Still succeeds and writes -- canvas itself cannot be resized.
        reopened = PSDImage.open(target)
        assert reopened.size == (6, 6)
        assert [c.name for c in reopened] == ["Review 1", "Review 2"]

    def test_existing_psd_path_override_wins_over_combo(self, context, manager, configured):
        combo_target = context.input_dir / "combo_target.psd"
        override_target = context.input_dir / "override_target.psd"
        _write_target(combo_target, [Image.new("RGB", (4, 4), RED)], "Review 1")
        # No pre-existing file at the override path -- created fresh, proving
        # the override (not the combo) is what actually got used.

        node = ComposePSD()
        node.execute(
            group_name="Review",
            layer_name="Layer",
            mode=DONT_OPEN,
            timeout_seconds=1800,
            unique_id="1",
            append_to_existing=True,
            existing_psd="combo_target.psd",
            existing_psd_path=str(override_target),
            image_1=make_tensor(GREEN, size=(4, 4)),
        )
        assert override_target.is_file()
        assert PSDImage.open(combo_target).__len__() == 1  # combo target untouched

    def test_path_traversal_in_existing_psd_combo_is_rejected(self, context, manager, configured):
        node = ComposePSD()
        with pytest.raises(ValueError, match="escapes the input directory"):
            node.execute(
                group_name="Review",
                layer_name="Layer",
                mode=DONT_OPEN,
                timeout_seconds=1800,
                unique_id="1",
                append_to_existing=True,
                existing_psd="../outside.psd",
                image_1=make_tensor(RED, size=(4, 4)),
            )

    def test_atomic_write_crash_leaves_original_file_intact(
        self, context, manager, configured, monkeypatch
    ):
        """The mandatory atomic-write test (build brief item 4 / CONSTRAINT 3):
        force an exception partway through the append/serialize path and
        assert the original file still opens and still has its original
        layers.
        """
        target = context.input_dir / "protected.psd"
        _write_target(target, [Image.new("RGB", (8, 8), RED)], "Review 1")
        original_bytes = target.read_bytes()

        def _boom(self, fp, mode="wb", **kwargs):
            raise RuntimeError("simulated crash mid-serialize")

        monkeypatch.setattr(PSDImage, "save", _boom)

        node = ComposePSD()
        with pytest.raises(RuntimeError, match="simulated crash"):
            node.execute(
                group_name="Review",
                layer_name="Layer",
                mode=DONT_OPEN,
                timeout_seconds=1800,
                unique_id="1",
                append_to_existing=True,
                existing_psd_path=str(target),
                image_1=make_tensor(GREEN, size=(8, 8)),
            )

        # Original file byte-for-byte intact and still opens with its
        # original content -- never truncated by the failed write attempt.
        assert target.read_bytes() == original_bytes
        reopened = PSDImage.open(target)
        assert [c.name for c in reopened] == ["Review 1"]
        arr = np.asarray(reopened[0][0].composite().convert("RGB"))
        assert tuple(int(v) for v in arr[0, 0]) == RED
        # No leftover temp file in the input directory.
        assert list(context.input_dir.iterdir()) == [target]

    def test_flatten_output_reflects_only_this_runs_layers(self, context, manager, configured):
        """The IMAGE/MASK outputs stay THIS run's own composite -- never the
        whole accumulated document (class docstring: appending is
        orthogonal to the outputs).
        """
        target = context.input_dir / "review.psd"
        _write_target(target, [Image.new("RGB", (8, 8), RED)], "Review 1")

        node = ComposePSD()
        image_out, _mask_out, _filename, _layers_out = node.execute(
            group_name="Review",
            layer_name="Layer",
            mode=DONT_OPEN,
            timeout_seconds=1800,
            unique_id="1",
            append_to_existing=True,
            existing_psd_path=str(target),
            image_1=make_tensor(GREEN, size=(8, 8)),
        )
        array = (image_out[0].numpy() * 255.0).round().astype("uint8")
        assert tuple(array[0, 0]) == GREEN  # this run's own color, not RED from Review 1


class TestAppendIsChanged:
    """``append_to_existing``/target folded into IS_CHANGED (build brief
    item 7: "toggling target/flag with unchanged upstream images will [now]
    re-execute").
    """

    def test_toggling_append_flag_changes_hash(self, configured):
        tensor = make_tensor(RED)
        base = ComposePSD.IS_CHANGED(
            filename_prefix="compose",
            group_name="Group",
            mode=DONT_OPEN,
            timeout_seconds=1800,
            unique_id="1",
            image_1=tensor,
        )
        toggled = ComposePSD.IS_CHANGED(
            filename_prefix="compose",
            group_name="Group",
            mode=DONT_OPEN,
            timeout_seconds=1800,
            unique_id="1",
            image_1=tensor,
            append_to_existing=True,
            existing_psd_path="/tmp/x.psd",
        )
        assert base != toggled

    def test_changing_target_changes_hash(self, configured):
        tensor = make_tensor(RED)
        a = ComposePSD.IS_CHANGED(
            filename_prefix="compose",
            group_name="Group",
            mode=DONT_OPEN,
            timeout_seconds=1800,
            unique_id="1",
            image_1=tensor,
            append_to_existing=True,
            existing_psd_path="/tmp/a.psd",
        )
        b = ComposePSD.IS_CHANGED(
            filename_prefix="compose",
            group_name="Group",
            mode=DONT_OPEN,
            timeout_seconds=1800,
            unique_id="1",
            image_1=tensor,
            append_to_existing=True,
            existing_psd_path="/tmp/b.psd",
        )
        assert a != b

    def test_target_irrelevant_when_append_is_off(self, configured):
        """A different existing_psd_path with append_to_existing=False must
        NOT change the hash -- the target is irrelevant when not appending.
        """
        tensor = make_tensor(RED)
        a = ComposePSD.IS_CHANGED(
            filename_prefix="compose",
            group_name="Group",
            mode=DONT_OPEN,
            timeout_seconds=1800,
            unique_id="1",
            image_1=tensor,
            append_to_existing=False,
            existing_psd_path="/tmp/a.psd",
        )
        b = ComposePSD.IS_CHANGED(
            filename_prefix="compose",
            group_name="Group",
            mode=DONT_OPEN,
            timeout_seconds=1800,
            unique_id="1",
            image_1=tensor,
            append_to_existing=False,
            existing_psd_path="/tmp/b.psd",
        )
        assert a == b


class TestAppendDuplicateGuard:
    """The chosen duplicate-append caching semantics (build brief item 7):
    a real append happens at most once per distinct identity per node --
    re-queuing "Wait for first save" while still unsaved (an ACTIVE handoff
    already matches the current identity) must NOT append a second group.
    """

    @pytest.fixture(autouse=True)
    def _require_torch(self):
        pytest.importorskip("torch")

    def test_requeue_while_unsaved_does_not_duplicate_the_appended_group(
        self, context, manager, configured_with_app, launches
    ):
        target = context.input_dir / "review.psd"
        node = ComposePSD()
        tensor = make_tensor(RED, size=(8, 8))
        node_id = "700"

        with raises_interrupt():
            node.execute(
                group_name="Review",
                layer_name="Layer",
                mode=WAIT,
                timeout_seconds=_SHORT_TIMEOUT,
                unique_id=node_id,
                append_to_existing=True,
                existing_psd_path=str(target),
                image_1=tensor,
            )
        assert len(launches) == 1
        assert [c.name for c in PSDImage.open(target)] == ["Review 1"]

        # Re-queue: SAME inputs, STILL unsaved -- must reuse the handoff and
        # must NOT append a second "Review 2" group for the same identity.
        with raises_interrupt():
            node.execute(
                group_name="Review",
                layer_name="Layer",
                mode=WAIT,
                timeout_seconds=_SHORT_TIMEOUT,
                unique_id=node_id,
                append_to_existing=True,
                existing_psd_path=str(target),
                image_1=tensor,
            )
        assert len(launches) == 2  # Photoshop reopened against the SAME handoff
        assert [c.name for c in PSDImage.open(target)] == ["Review 1"]  # still just one group

        active = manager.find_active_for_node(node_id)
        assert active is not None
        matching = [h for h in manager.list_all(limit=50) if h.origin_node_id == node_id]
        assert len(matching) == 1  # never a second handoff either

    def test_genuinely_new_identity_after_requeue_does_append_again(
        self, context, manager, configured_with_app, launches
    ):
        """The behavior that MUST be preserved alongside the guard above:
        once the images genuinely change (a real new generation), a new
        append DOES happen -- the guard only suppresses same-identity
        duplicates, never legitimate new runs.
        """
        target = context.input_dir / "review.psd"
        node = ComposePSD()
        node_id = "701"

        with raises_interrupt():
            node.execute(
                group_name="Review",
                layer_name="Layer",
                mode=WAIT,
                timeout_seconds=_SHORT_TIMEOUT,
                unique_id=node_id,
                append_to_existing=True,
                existing_psd_path=str(target),
                image_1=make_tensor(RED, size=(8, 8)),
            )
        assert [c.name for c in PSDImage.open(target)] == ["Review 1"]

        with raises_interrupt():
            node.execute(
                group_name="Review",
                layer_name="Layer",
                mode=WAIT,
                timeout_seconds=_SHORT_TIMEOUT,
                unique_id=node_id,
                append_to_existing=True,
                existing_psd_path=str(target),
                image_1=make_tensor(GREEN, size=(8, 8)),  # genuinely different pixels
            )
        assert [c.name for c in PSDImage.open(target)] == ["Review 1", "Review 2"]
