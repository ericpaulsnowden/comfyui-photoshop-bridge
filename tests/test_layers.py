"""cpsb.layers (PSD → LAYERS, docs/roadmap/layered-images.md L1) + the Load
PSD node's appended ``layers`` output and TAIL-positioned ``flatten_groups``
widget.

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
