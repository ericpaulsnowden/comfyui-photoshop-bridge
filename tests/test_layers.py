"""cpsb.layers (PSD ⇄ LAYERS, docs/roadmap/layered-images.md L1+L2) + the
Load PSD node's appended ``layers`` output and TAIL-positioned
``flatten_groups`` widget + the Compose node's optional ``layers`` input.

Every PSD fixture is authored with psd-tools itself (``create_pixel_layer``'s
"later index stacks on top" order is this repo's own verified convention,
cpsb/compose_psd.py module docstring), so assertions about z-order, offsets,
masks, and group composition are grounded in real serialized documents, not
mocks. Core-side validation (``document_items``/``expand_item_frames``) can't
run here -- no ComfyUI in the unit venv -- so the CONTRACT these tests pin is
the key set read verbatim from the rig's v0.31.1 source (cpsb/layers.py
module docstring); the rig-side end-to-end run covers the live half.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import cast

import pytest
from PIL import Image
from psd_tools import PSDImage
from psd_tools.constants import BlendMode

import cpsb.layers as layers_module
import cpsb.load_psd as load_psd_module
import cpsb.nodes as nodes_module
from cpsb.context import CpsbContext
from cpsb.handoff import HandoffManager

pytest.importorskip("torch")


@pytest.fixture
def manager(context: CpsbContext) -> HandoffManager:
    return HandoffManager(context)


@pytest.fixture
def configured(context: CpsbContext, manager: HandoffManager):
    nodes_module.configure(context, manager, cast("object", None), cast("object", None))
    yield
    nodes_module._state = None


def build_grouped_psd(path: Path) -> None:
    """The canonical fixture: bottom pixel layer + a group of two.

    - ``bottom``: 64x48 opaque red at (0, 0), multiply @ 50%.
    - group ``grp`` (opacity 200, visible) holding:
      - ``mid``: 20x10 green at (5, 7), hidden.
      - ``top``: 8x8 blue at (30, 20), screen.
    """
    psd = PSDImage.new(mode="RGB", size=(64, 48), depth=8)
    bottom = psd.create_pixel_layer(Image.new("RGB", (64, 48), (255, 0, 0)), name="bottom")
    bottom.blend_mode = BlendMode.MULTIPLY
    bottom.opacity = 128
    mid = psd.create_pixel_layer(
        Image.new("RGB", (20, 10), (0, 255, 0)), name="mid", left=5, top=7
    )
    mid.visible = False
    top = psd.create_pixel_layer(
        Image.new("RGB", (8, 8), (0, 0, 255)), name="top", left=30, top=20
    )
    top.blend_mode = BlendMode.SCREEN
    grp = psd.create_group(layer_list=[mid, top], name="grp")
    grp.opacity = 200
    psd.save(path)


class TestBlendMap:
    def test_every_emitted_name_is_core_valid(self):
        """A name outside core's _LAYER_MODES raises ValueError inside
        core's document_items -- the map must be a subset by construction.
        """
        emitted = set(layers_module.PSD_TO_LAYERS_BLEND.values()) | set(
            layers_module.LOSSY_PSD_BLEND_FALLBACKS.values()
        )
        assert emitted <= layers_module.LAYERS_BLEND_NAMES

    def test_direct_map_covers_24_modes(self):
        assert len(layers_module.PSD_TO_LAYERS_BLEND) == 24

    def test_lossy_fallbacks(self, caplog):
        with caplog.at_level(logging.INFO, logger="cpsb"):
            assert layers_module._blend_name(BlendMode.DISSOLVE, "l", "s") == "normal"
            assert layers_module._blend_name(BlendMode.DARKER_COLOR, "l", "s") == "darken"
            assert layers_module._blend_name(BlendMode.LIGHTER_COLOR, "l", "s") == "lighten"
        assert caplog.text.count("no LAYERS equivalent") == 3

    def test_unknown_mode_degrades_to_normal_with_warning(self, caplog):
        with caplog.at_level(logging.WARNING, logger="cpsb"):
            assert layers_module._blend_name("bogus", "l", "s") == "normal"
        assert "unmapped blend mode" in caplog.text


class TestFlatProjection:
    def test_leaves_carry_position_order_blend_opacity_visibility(self, tmp_path):
        psd_path = tmp_path / "grouped.psd"
        build_grouped_psd(psd_path)
        doc = layers_module.document_from_psd(
            PSDImage.open(psd_path), flatten_groups=False, source="grouped.psd"
        )

        assert doc["version"] == 1
        assert doc["canvas"] == (64, 48)
        items = doc["layers"]
        assert [item["name"] for item in items] == ["bottom", "mid", "top"]
        assert [item["z_index"] for item in items] == [0, 1, 2]

        bottom, mid, top = items
        assert (bottom["x"], bottom["y"]) == (0, 0)
        assert bottom["blend_mode"] == "multiply"
        assert bottom["opacity"] == pytest.approx(128 / 255)
        assert bottom["visible"] is True

        # Group opacity (200/255) composes onto both children; the group's
        # own visibility (True) leaves the children's own flags in charge.
        assert (mid["x"], mid["y"]) == (5, 7)
        assert mid["visible"] is False
        assert mid["opacity"] == pytest.approx(200 / 255)
        assert (top["x"], top["y"]) == (30, 20)
        assert top["visible"] is True
        assert top["blend_mode"] == "screen"
        assert top["opacity"] == pytest.approx(200 / 255)

    def test_hidden_group_hides_its_leaves(self, tmp_path):
        psd_path = tmp_path / "hidden_grp.psd"
        psd = PSDImage.new(mode="RGB", size=(16, 16), depth=8)
        inner = psd.create_pixel_layer(Image.new("RGB", (4, 4), (1, 2, 3)), name="inner")
        grp = psd.create_group(layer_list=[inner], name="grp")
        grp.visible = False
        psd.save(psd_path)

        doc = layers_module.document_from_psd(
            PSDImage.open(psd_path), flatten_groups=False, source="hidden_grp.psd"
        )
        (item,) = doc["layers"]
        assert item["name"] == "inner"
        assert item["visible"] is False

    def test_item_images_are_rgba_single_frame_tensors(self, tmp_path):
        psd_path = tmp_path / "grouped.psd"
        build_grouped_psd(psd_path)
        doc = layers_module.document_from_psd(
            PSDImage.open(psd_path), flatten_groups=False, source="grouped.psd"
        )
        for item in doc["layers"]:
            shape = item["image"].shape
            assert (len(shape), shape[0], shape[3]) == (4, 1, 4)
            assert item["type"] == "raster"

    def test_transparency_written_by_our_own_compose_survives(self, tmp_path):
        """The mask-baking regression this module exists to prevent:
        create_pixel_layer from RGBA stores transparency as a LAYER MASK
        (topil() alpha comes back fully opaque -- verified empirically
        2026-08-09), so a projection without mask baking would emit this
        layer as opaque.
        """
        psd_path = tmp_path / "alpha.psd"
        psd = PSDImage.new(mode="RGB", size=(10, 10), depth=8)
        psd.create_pixel_layer(
            Image.new("RGBA", (10, 10), (0, 255, 0, 200)), name="semi", left=0, top=0
        )
        psd.save(psd_path)

        doc = layers_module.document_from_psd(
            PSDImage.open(psd_path), flatten_groups=False, source="alpha.psd"
        )
        (item,) = doc["layers"]
        alpha = item["image"][0, 0, 0, 3].item()
        assert alpha == pytest.approx(200 / 255, abs=0.01)

    def test_over_core_cap_warns_but_returns_everything(self, tmp_path, caplog):
        psd_path = tmp_path / "many.psd"
        psd = PSDImage.new(mode="RGB", size=(8, 8), depth=8)
        for index in range(layers_module.CORE_MAX_LAYERS + 1):
            psd.create_pixel_layer(Image.new("RGB", (1, 1), (index % 256, 0, 0)), name=f"l{index}")
        psd.save(psd_path)

        with caplog.at_level(logging.WARNING, logger="cpsb"):
            doc = layers_module.document_from_psd(
                PSDImage.open(psd_path), flatten_groups=False, source="many.psd"
            )
        assert len(doc["layers"]) == layers_module.CORE_MAX_LAYERS + 1
        assert "compositor cap" in caplog.text


class TestFlattenGroupsProjection:
    def test_one_item_per_top_level_entry(self, tmp_path):
        psd_path = tmp_path / "grouped.psd"
        build_grouped_psd(psd_path)
        doc = layers_module.document_from_psd(
            PSDImage.open(psd_path), flatten_groups=True, source="grouped.psd"
        )
        items = doc["layers"]
        assert [item["name"] for item in items] == ["bottom", "grp"]

        grp = items[1]
        # The group's composite covers its visible content ('top' only --
        # 'mid' is hidden): bbox (30, 20)-(38, 28).
        assert (grp["x"], grp["y"]) == (30, 20)
        assert grp["opacity"] == pytest.approx(200 / 255)
        assert grp["visible"] is True
        # Hidden child stayed hidden in the composite: pixel inside 'top'
        # is recognizably blue (psd-tools' own compositor applies the
        # child's screen blend, so exact channel values are its call) --
        # and FULLY OPAQUE: the group's own opacity rides on the item
        # (editable), so composite() must not bake it into the pixels too
        # (the double-apply psd-tools would do by default; caught by this
        # very test 2026-08-09).
        pixel = grp["image"][0, 2, 2]
        assert pixel[2].item() > 0.9
        assert pixel[0].item() < 0.3
        assert pixel[3].item() == pytest.approx(1.0, abs=0.01)

    def test_hidden_group_still_carries_pixels(self, tmp_path):
        """psd-tools composites a hidden layer to nothing no matter the
        layer_filter (verified empirically 2026-08-09) -- the projection
        must temp-toggle visibility to keep the pixels, then restore.
        """
        psd_path = tmp_path / "hidden_grp.psd"
        psd = PSDImage.new(mode="RGB", size=(16, 16), depth=8)
        inner = psd.create_pixel_layer(
            Image.new("RGB", (4, 4), (0, 0, 255)), name="inner", left=2, top=3
        )
        grp = psd.create_group(layer_list=[inner], name="grp")
        grp.visible = False
        psd.save(psd_path)

        opened = PSDImage.open(psd_path)
        doc = layers_module.document_from_psd(
            opened, flatten_groups=True, source="hidden_grp.psd"
        )
        (item,) = doc["layers"]
        assert item["visible"] is False
        assert (item["x"], item["y"]) == (2, 3)
        assert item["image"][0, 0, 0, 2].item() == pytest.approx(1.0, abs=0.01)
        # The projection's temp-toggles restored the opened doc's state.
        assert bool(opened[0].visible) is False
        assert opened[0].opacity == 255

    def test_group_mask_applied_exactly_once(self):
        """composite() applies a group's OWN mask (verified empirically
        2026-08-09) -- the projection must not bake it a second time, and
        the masked-out region must actually be transparent in the item.

        Fixture stays IN-MEMORY deliberately: psd-tools' writer corrupts
        group masks on save (its own suite skips Group masks -- the same
        upstream weakness cpsb/compose_psd.py's docstring cites for never
        writing them), but Photoshop-authored files with group masks READ
        fine, which is the path this projection serves.
        """
        psd = PSDImage.new(mode="RGB", size=(16, 16), depth=8)
        inner = psd.create_pixel_layer(
            Image.new("RGB", (8, 8), (0, 0, 255)), name="inner", left=4, top=4
        )
        grp = psd.create_group(layer_list=[inner], name="grp")
        mask = Image.new("L", (16, 16), 255)
        for y in range(16):
            for x in range(8):
                mask.putpixel((x, y), 0)
        grp.create_mask(mask)

        doc = layers_module.document_from_psd(
            psd, flatten_groups=True, source="masked_grp.psd"
        )
        (item,) = doc["layers"]
        # bbox (4, 4, 12, 12); canvas x<8 is masked out -> item x<4.
        assert item["image"][0, 1, 1, 3].item() == pytest.approx(0.0, abs=0.01)  # masked side
        assert item["image"][0, 1, 6, 3].item() == pytest.approx(1.0, abs=0.01)  # visible side

    def test_empty_group_is_skipped(self, tmp_path, caplog):
        psd_path = tmp_path / "empty_grp.psd"
        psd = PSDImage.new(mode="RGB", size=(8, 8), depth=8)
        psd.create_pixel_layer(Image.new("RGB", (8, 8), (9, 9, 9)), name="base")
        psd.create_group(layer_list=[], name="void")
        psd.save(psd_path)

        with caplog.at_level(logging.INFO, logger="cpsb"):
            doc = layers_module.document_from_psd(
                PSDImage.open(psd_path), flatten_groups=True, source="empty_grp.psd"
            )
        assert [item["name"] for item in doc["layers"]] == ["base"]
        assert "empty group" in caplog.text


class TestFlatImageDocument:
    def test_single_layer_stack(self):
        doc = layers_module.document_from_flat_image(Image.new("RGB", (12, 5), (7, 7, 7)), "photo")
        assert doc["canvas"] == (12, 5)
        (item,) = doc["layers"]
        assert item["name"] == "photo"
        assert (item["x"], item["y"], item["z_index"]) == (0, 0, 0)
        assert item["opacity"] == 1.0
        assert item["blend_mode"] == "normal"
        assert item["image"].shape == (1, 5, 12, 4)

    def test_empty_document_is_version_stamped(self):
        doc = layers_module.empty_document()
        assert doc == {"version": 1, "layers": []}


class TestLoadPsdLayersOutput:
    def test_contract_layers_appended_last(self):
        node = load_psd_module.PhotoshopLoadPSD
        assert node.RETURN_TYPES == ("IMAGE", "MASK", "LAYERS")
        assert len(node.OUTPUT_TOOLTIPS) == 3

    def test_flatten_groups_is_the_last_required_widget(self, configured):
        """The TAIL rule (INPUT_TYPES docstring): widgets_values restore by
        position, so the new widget must stay last forever.
        """
        spec = load_psd_module.PhotoshopLoadPSD.INPUT_TYPES()
        assert list(spec["required"])[-1] == "flatten_groups"
        assert spec["required"]["flatten_groups"][1]["default"] is False

    def test_execute_returns_layer_stack(self, context, configured, tmp_path):
        psd_path = context.input_dir / "grouped.psd"
        build_grouped_psd(psd_path)
        node = load_psd_module.PhotoshopLoadPSD()

        _image, _mask, doc = node.execute(psd="grouped.psd", unique_id="1")
        assert [item["name"] for item in doc["layers"]] == ["bottom", "mid", "top"]

        _image, _mask, doc = node.execute(psd="grouped.psd", unique_id="1", flatten_groups=True)
        assert [item["name"] for item in doc["layers"]] == ["bottom", "grp"]

    def test_tif_gets_single_layer_stack(self, context, configured):
        tif_path = context.input_dir / "photo.tif"
        Image.new("RGB", (9, 6), (10, 20, 30)).save(tif_path)
        node = load_psd_module.PhotoshopLoadPSD()
        _image, _mask, doc = node.execute(psd="photo.tif", unique_id="1")
        (item,) = doc["layers"]
        assert item["name"] == "photo"
        assert doc["canvas"] == (9, 6)

    def test_is_changed_folds_flatten_groups_only_when_on(self, context, configured):
        psd_path = context.input_dir / "grouped.psd"
        build_grouped_psd(psd_path)
        base = load_psd_module.PhotoshopLoadPSD.IS_CHANGED(psd="grouped.psd", unique_id="1")
        flat = load_psd_module.PhotoshopLoadPSD.IS_CHANGED(
            psd="grouped.psd", unique_id="1", flatten_groups=False
        )
        flattened = load_psd_module.PhotoshopLoadPSD.IS_CHANGED(
            psd="grouped.psd", unique_id="1", flatten_groups=True
        )
        # Default keeps the historical bare-hash key byte-identical (no
        # spurious re-run on pack upgrade); True must differ.
        assert base == flat
        assert len(base) == 64
        assert flattened == base + ":flatten"

    def test_extraction_failure_degrades_to_empty_stack(
        self, context, configured, monkeypatch, caplog
    ):
        """Eric's decision 1: a file that flattens today must keep loading
        even if layer extraction blows up -- the layers output alone pays.
        """
        psd_path = context.input_dir / "grouped.psd"
        build_grouped_psd(psd_path)

        def explode(*args, **kwargs):
            raise RuntimeError("exotic document")

        monkeypatch.setattr(layers_module, "document_from_psd", explode)
        node = load_psd_module.PhotoshopLoadPSD()
        with caplog.at_level(logging.ERROR, logger="cpsb"):
            image, _mask, doc = node.execute(psd="grouped.psd", unique_id="1")
        assert doc == {"version": 1, "layers": []}
        assert image.shape[3] == 3  # flat output unharmed
        assert "layers output is empty" in caplog.text


# ---------------------------------------------------------------------------
# L2: LAYERS → PSD (prepare_stack + the Compose node's `layers` input)
# ---------------------------------------------------------------------------


def solid_item(
    color: tuple[int, int, int],
    size: tuple[int, int] = (8, 8),
    alpha: int | None = None,
    batch: int = 1,
    **props,
):
    """A LayerItem dict with a solid-color image tensor (core's shape:
    ``(B, H, W, 3|4)`` float 0..1)."""
    import numpy as np
    import torch

    width, height = size
    pixel = list(color) + ([alpha] if alpha is not None else [])
    array = np.tile(
        np.array(pixel, dtype=np.float32) / 255.0, (batch, height, width, 1)
    )
    return {"type": "raster", "image": torch.from_numpy(array), **props}


class TestPrepareStack:
    def test_sorts_by_z_index_and_applies_defaults(self):
        doc = {
            "version": 1,
            "layers": [
                solid_item((255, 0, 0), z_index=1, name="upper"),
                solid_item((0, 255, 0), z_index=0, name="lower", x=3, y=4),
            ],
        }
        prepared = layers_module.prepare_stack(doc, source="test")
        assert [layer.name for layer in prepared] == ["lower", "upper"]
        lower = prepared[0]
        assert (lower.left, lower.top) == (3, 4)
        assert lower.opacity == 1.0
        assert lower.blend_mode == BlendMode.NORMAL
        assert lower.visible is True

    def test_batch_expands_one_layer_per_frame(self):
        doc = {"version": 1, "layers": [solid_item((9, 9, 9), batch=3, name="b")]}
        prepared = layers_module.prepare_stack(doc, source="test")
        assert len(prepared) == 3
        assert all(layer.name == "b" for layer in prepared)
        assert layers_module.stack_frame_count(doc) == 3

    def test_mask_multiplies_alpha(self):
        import torch

        item = solid_item((0, 0, 255), size=(4, 4))
        item["mask"] = torch.full((1, 4, 4), 0.5)  # 1 = transparent convention
        doc = {"version": 1, "layers": [item]}
        (prepared,) = layers_module.prepare_stack(doc, source="test")
        assert prepared.image.getpixel((0, 0))[3] == pytest.approx(128, abs=2)

    def test_flips_resize_and_rotation_bake_into_pixels(self, caplog):
        import math

        item = solid_item((10, 20, 30), size=(4, 2), x=10, y=10, w=8, h=4)
        item["rotation"] = math.pi / 2  # quarter turn clockwise
        item["flip_h"] = True
        doc = {"version": 1, "layers": [item]}
        with caplog.at_level(logging.INFO, logger="cpsb"):
            (prepared,) = layers_module.prepare_stack(doc, source="test")
        # Display size 8x4 rotated 90deg -> 4x8; placed_bounds about the
        # display box's center (10..18, 10..14): cx=14, cy=12 -> new
        # top-left (12, 8).
        assert prepared.image.size == (4, 8)
        assert (prepared.left, prepared.top) == (12, 8)
        assert "baking display size" in caplog.text
        assert "baking horizontal flip" in caplog.text
        assert "rad rotation" in caplog.text

    def test_gimp_only_blend_degrades_to_normal(self, caplog):
        doc = {
            "version": 1,
            "layers": [solid_item((1, 1, 1), blend_mode="grain-merge", name="g")],
        }
        with caplog.at_level(logging.INFO, logger="cpsb"):
            (prepared,) = layers_module.prepare_stack(doc, source="test")
        assert prepared.blend_mode == BlendMode.NORMAL
        assert "no Photoshop equivalent" in caplog.text

    def test_reverse_blend_map_round_trips(self):
        for psd_mode, name in layers_module.PSD_TO_LAYERS_BLEND.items():
            assert layers_module.LAYERS_TO_PSD_BLEND[name] == psd_mode

    def test_bad_version_type_and_tensor_raise(self):
        with pytest.raises(ValueError, match="version"):
            layers_module.prepare_stack({"version": 2, "layers": []}, source="test")
        with pytest.raises(ValueError, match="not supported yet"):
            layers_module.prepare_stack(
                {"version": 1, "layers": [{"type": "text", "image": None}]}, source="test"
            )
        with pytest.raises(ValueError, match="tensor"):
            layers_module.prepare_stack(
                {"version": 1, "layers": [{"type": "raster", "image": "nope"}]}, source="test"
            )
        with pytest.raises(ValueError, match="dict"):
            layers_module.prepare_stack("nope", source="test")

    def test_stack_digest_tracks_content(self):
        doc_a = {"version": 1, "layers": [solid_item((5, 5, 5), name="a")]}
        doc_b = {"version": 1, "layers": [solid_item((5, 5, 5), name="a")]}
        assert layers_module.stack_digest(doc_a) == layers_module.stack_digest(doc_b)
        doc_b["layers"][0]["opacity"] = 0.5
        assert layers_module.stack_digest(doc_a) != layers_module.stack_digest(doc_b)
        doc_c = {"version": 1, "layers": [solid_item((6, 5, 5), name="a")]}
        assert layers_module.stack_digest(doc_a) != layers_module.stack_digest(doc_c)

    def test_stack_extent_prefers_declared_canvas(self):
        doc = {"version": 1, "canvas": (100, 50), "layers": [solid_item((1, 1, 1))]}
        prepared = layers_module.prepare_stack(doc, source="test")
        assert layers_module.stack_extent(prepared, doc) == (100, 50)
        no_canvas = {"version": 1, "layers": [solid_item((1, 1, 1), size=(8, 8), x=10, y=20)]}
        prepared = layers_module.prepare_stack(no_canvas, source="test")
        assert layers_module.stack_extent(prepared, no_canvas) == (18, 28)


class TestComposeLayersInput:
    @pytest.fixture(autouse=True)
    def _require_torch(self):
        pytest.importorskip("torch")

    def test_stack_writes_real_layer_properties(self, context, configured):
        import cpsb.compose_psd as compose_module

        doc = {
            "version": 1,
            "canvas": (32, 24),
            "layers": [
                solid_item((255, 0, 0), size=(32, 24), name="base", z_index=0),
                solid_item(
                    (0, 255, 0), size=(8, 6), name="tint", z_index=1,
                    x=4, y=5, opacity=0.5, blend_mode="multiply",
                ),
                solid_item((0, 0, 255), size=(4, 4), name="ghost", z_index=2, visible=False),
            ],
        }
        node = compose_module.PhotoshopComposePSD()
        _image, _mask, filename, layers_batch = node.execute(
            group_name="Stack Run",
            mode=compose_module.MODE_DONT_OPEN,
            timeout_seconds=1800,
            unique_id="1",
            layers=doc,
        )
        written = context.input_dir / filename
        reopened = PSDImage.open(written)
        assert reopened.size == (32, 24)  # the doc's declared canvas
        group = reopened[0]
        assert group.name == "Stack Run"
        base, tint, ghost = list(group)
        assert base.name == "base"
        assert (tint.left, tint.top) == (4, 5)
        assert tint.opacity == 128
        assert tint.blend_mode == BlendMode.MULTIPLY
        assert tint.visible is True
        assert ghost.visible is False
        # One preview frame per written layer, invisible ones included.
        assert layers_batch.shape[0] == 3

    def test_stack_sits_below_image_inputs(self, context, configured):
        import numpy as np
        import torch

        import cpsb.compose_psd as compose_module

        doc = {"version": 1, "layers": [solid_item((1, 2, 3), size=(16, 16), name="stacked")]}
        image_1 = torch.from_numpy(
            np.full((1, 8, 8, 3), 0.5, dtype=np.float32)
        )
        node = compose_module.PhotoshopComposePSD()
        _image, _mask, filename, layers_batch = node.execute(
            group_name="Combined",
            mode=compose_module.MODE_DONT_OPEN,
            timeout_seconds=1800,
            unique_id="1",
            layers=doc,
            image_1=image_1,
        )
        reopened = PSDImage.open(context.input_dir / filename)
        group = reopened[0]
        names = [layer.name for layer in group]
        assert names == ["stacked", "Layer 1"]  # stack bottom, image above
        assert layers_batch.shape[0] == 2

    def test_combined_cap_counts_stack_first(self, context, configured, caplog):
        import numpy as np
        import torch

        import cpsb.compose_psd as compose_module

        doc = {
            "version": 1,
            "layers": [
                solid_item((1, 1, 1), name="s1", z_index=0),
                solid_item((2, 2, 2), name="s2", z_index=1),
            ],
        }
        image = torch.from_numpy(np.zeros((1, 4, 4, 3), dtype=np.float32))
        node = compose_module.PhotoshopComposePSD()
        with caplog.at_level(logging.WARNING, logger="cpsb"):
            _image, _mask, filename, layers_batch = node.execute(
                group_name="Capped",
                mode=compose_module.MODE_DONT_OPEN,
                timeout_seconds=1800,
                unique_id="1",
                max_layers=3,
                layers=doc,
                image_1=image,
                image_2=image,
            )
        reopened = PSDImage.open(context.input_dir / filename)
        assert len(list(reopened[0])) == 3  # 2 stack + first image
        assert "exceed max_layers" in caplog.text
        assert layers_batch.shape[0] == 3

    def test_is_changed_folds_stack_and_stays_stable_without(self, context, configured):
        import cpsb.compose_psd as compose_module

        base_kwargs = dict(
            group_name="G",
            mode=compose_module.MODE_DONT_OPEN,
            timeout_seconds=1800,
            unique_id="1",
        )
        doc = {"version": 1, "layers": [solid_item((5, 5, 5), name="a")]}
        without = compose_module.PhotoshopComposePSD.IS_CHANGED(**base_kwargs, layers=None)
        with_stack = compose_module.PhotoshopComposePSD.IS_CHANGED(**base_kwargs, layers=doc)
        assert len(without) == 64
        assert with_stack != without
        doc2 = {"version": 1, "layers": [solid_item((5, 5, 5), name="a", opacity=0.4)]}
        assert compose_module.PhotoshopComposePSD.IS_CHANGED(
            **base_kwargs, layers=doc2
        ) != with_stack

    def test_malformed_stack_fails_the_queue_loudly(self, context, configured):
        import cpsb.compose_psd as compose_module

        node = compose_module.PhotoshopComposePSD()
        with pytest.raises(ValueError, match="version"):
            node.execute(
                group_name="G",
                mode=compose_module.MODE_DONT_OPEN,
                timeout_seconds=1800,
                unique_id="1",
                layers={"version": 99, "layers": []},
            )

    def test_flat_output_applies_stack_opacity_and_visibility(self, context, configured):
        import cpsb.compose_psd as compose_module

        doc = {
            "version": 1,
            "layers": [
                solid_item((255, 0, 0), size=(8, 8), name="base", z_index=0),
                solid_item((0, 0, 255), size=(8, 8), name="hidden", z_index=1, visible=False),
            ],
        }
        node = compose_module.PhotoshopComposePSD()
        image, _mask, _filename, _batch = node.execute(
            group_name="G",
            mode=compose_module.MODE_DONT_OPEN,
            timeout_seconds=1800,
            unique_id="1",
            layers=doc,
        )
        pixel = (image[0, 0, 0].numpy() * 255.0).round()
        # The hidden blue layer must not contribute -- the flat preview
        # shows the red base only.
        assert pixel[0] == 255
        assert pixel[2] == 0
