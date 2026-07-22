"""Tests for ``cpsb.annotate`` (PROTOCOL.md §6d): the ``PhotoshopAnnotate`` node.

Mirrors ``test_nodes.py``'s conventions: a plain ``numpy``-array tensor
stand-in wherever torch isn't needed, ``pytest.importorskip("torch")`` for
tests that check actual tensor *values*, and a ``launches`` fixture that
monkeypatches ``cpsb.routes.launch_photoshop`` (the module object cpsb.nodes
always calls through, per its own docstring) to observe/avoid real Photoshop
launches. Unlike ``test_nodes.py``'s ``bridge`` fixture, no background event
loop is needed here: with no Tier 2 plugin ever connected in these tests,
every open this node attempts takes the synchronous Tier 1 path, which never
touches the loop at all (see ``cpsb.nodes``'s own module docstring).

"Wait for first save" mode BLOCKS (PROTOCOL.md §6d, 2026-07-17 update), so
its tests mirror ``test_nodes.py``'s ``TestWaitForFirstSaveMode`` shape too: a
``threading.Timer`` delivers a delayed edit/cancel from a background thread
while ``execute()`` blocks on the main thread, exactly like real Photoshop
usage (a human's save always arrives long after the launch call returns).
"Re-run on every save" mode (product-owner spec, 2026-07-19 -- this node's
``mode`` options now match the bridge node's) NEVER blocks; its tests mirror
``test_nodes.py``'s ``TestRerunEverySaveMode`` shape instead.

**The "Instructions" layer redesign (product-owner spec, 2026-07-17).** Both
Photoshop-touching modes no longer diff the whole image against a flat
re-save; they write the handoff PSD LAYERED (input image + a blank
top-level "Instructions" layer) and, on save, reopen that same file with
psd-tools to read the Instructions layer's own painted pixels as the MASK
and the composite of every OTHER layer as the IMAGE. ``make_layered_psd``/
``make_handoff_with_layered_edit`` below build those saved-PSD fixtures
directly (independent of the node's own write helper,
``cpsb.annotate._write_instructions_psd``) so the READ path is never just
asserted against itself. The old whole-image diff machinery
(``_raw_diff_mask``, ``_close_and_fill_mask``, the scipy/PIL-fallback
morphology, and their dedicated test classes) is gone along with the
feature it implemented.
"""

from __future__ import annotations

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
from PIL import Image, ImageDraw
from psd_tools import PSDImage

import cpsb.annotate as annotate_module
import cpsb.nodes as nodes_module
import cpsb.routes as routes_module
from cpsb.context import CpsbContext
from cpsb.handoff import HandoffManager, SourceRef
from cpsb.launcher import LaunchResult

AnnotateMode = annotate_module.AnnotateMode
INSTRUCTIONS_LAYER_NAME = annotate_module.INSTRUCTIONS_LAYER_NAME

RED = (255, 0, 0)
GREEN = (0, 255, 0)
BLUE = (0, 0, 255)

#: A short bound for every blocking-wait test below: real tests either
#: deliver an edit/cancel from a background thread well before this elapses,
#: or deliberately let it expire to exercise the TIMEOUT outcome -- either
#: way, keeping it small keeps the suite fast.
_SHORT_TIMEOUT = 1


def make_tensor(color: tuple[int, int, int], size: tuple[int, int] = (24, 16)) -> np.ndarray:
    """A ``1xHxWx3`` ComfyUI-layout float32 tensor of a solid color.

    Pure 0/255 channel values survive the float round trip exactly (see
    ``test_nodes.py``'s identical helper), keeping ``compute_source_hash``
    comparisons between a handoff's recorded source and a freshly re-decoded
    tensor deterministic.
    """
    width, height = size
    img = Image.new("RGB", (width, height), color)
    return np.asarray(img, dtype=np.float32)[None, ...] / 255.0


def tensor_from_image(image: Image.Image) -> np.ndarray:
    """A ``1xHxWx3`` ComfyUI-layout float32 tensor from an already-built PIL image."""
    return np.asarray(image.convert("RGB"), dtype=np.float32)[None, ...] / 255.0


def image_tensor_to_uint8_array(image_tensor) -> np.ndarray:
    """A resolved ``IMAGE`` tensor's first frame as an ``(H, W, 3)`` uint8 array."""
    return (image_tensor[0].numpy() * 255).round().astype(np.uint8)


def make_layered_psd(
    psd_path: Path,
    base_image: Image.Image,
    instructions_image: Image.Image | None = None,
    instructions_layer_name: str = INSTRUCTIONS_LAYER_NAME,
) -> None:
    """Write a base(+Instructions) layered PSD at *psd_path* -- an independent
    stand-in for "what Photoshop saved".

    Built directly with psd-tools' own construction API
    (``PSDImage.new(mode="RGB")`` -> ``create_pixel_layer``, the same one
    :mod:`cpsb.compose_psd` verified empirically and
    ``cpsb.annotate._write_instructions_psd`` itself uses) rather than by
    calling that node function, so these tests check the READ side
    (``cpsb.annotate._read_ps_saved_psd`` and friends) against an
    independently-built fixture, not against the node's own write path.

    Args:
        psd_path: Destination path.
        base_image: The bottom layer's pixels (any PIL mode; converted to
            RGB).
        instructions_image: If given, an RGBA image written as a second,
            top-level layer named *instructions_layer_name*. Omitted
            entirely (no second layer at all) when ``None`` -- the
            "layer deleted" scenario.
        instructions_layer_name: The name to give that second layer --
            defaults to the real contract name, but a test can pass a
            different one to build the "layer renamed" scenario.
    """
    psd_path.parent.mkdir(parents=True, exist_ok=True)
    width, height = base_image.size
    psd = PSDImage.new(mode="RGB", size=(width, height), depth=8)
    psd.create_pixel_layer(base_image.convert("RGB"), name="Base", top=0, left=0, opacity=255)
    if instructions_image is not None:
        psd.create_pixel_layer(
            instructions_image.convert("RGBA"),
            name=instructions_layer_name,
            top=0,
            left=0,
            opacity=255,
        )
    psd.save(psd_path)


@pytest.fixture
def manager(context: CpsbContext) -> HandoffManager:
    return HandoffManager(context)


@pytest.fixture
def configured(context: CpsbContext, manager: HandoffManager):
    """Wire ``nodes.configure`` with a fake app/loop.

    Fine for anything that never actually opens Photoshop: pass-through mode
    (which never looks up a handoff at all), or either Photoshop-touching
    mode against a handoff that already has an edit
    (``_resolve_wait_for_save_edit``/``_resolve_rerun_edit`` only reach the
    open path when there is no consumable edit yet).
    """
    nodes_module.configure(context, manager, cast("object", None), cast("object", None))
    yield
    nodes_module._state = None


@pytest.fixture
def launches(monkeypatch):
    """Records every ``launch_photoshop`` call (the Tier 1 open path)."""
    calls: list[str] = []

    def fake_launch(psd_path, override=""):
        calls.append(str(psd_path))
        return LaunchResult(ok=True)

    monkeypatch.setattr(routes_module, "launch_photoshop", fake_launch)
    return calls


@pytest.fixture
def node(context: CpsbContext, manager: HandoffManager, launches):
    """A configured ``PhotoshopAnnotate`` instance, real manager, fake launch."""
    app = web.Application()
    nodes_module.configure(context, manager, app, cast("object", None))
    yield annotate_module.PhotoshopAnnotate()
    nodes_module._state = None


def make_handoff_with_edit(
    manager: HandoffManager, node_id: str, source: Image.Image, edit: Image.Image
) -> str:
    """A ``bridge_node`` handoff for *source*, already carrying one ingested *edit*.

    Deliberately does NOT write a managed PSD copy (mirrors
    ``manager.create``'s own docstring: it never writes the copy itself).
    Only usable by tests that never call ``execute()``'s PS-mode consume
    path -- which now re-opens the managed copy with psd-tools
    (``cpsb.annotate._read_ps_saved_psd``) and would fail against a handoff
    with no such file. Safe for ``IS_CHANGED`` tests (:class:`TestIsChanged`),
    which only ever consult ``manager.latest_edit_hash`` (the ingested edit
    PNG's hash), never the managed copy itself -- and for pass-through-mode
    tests, which never look up a handoff at all. See
    :func:`make_handoff_with_layered_edit` for the ``execute()``-safe
    equivalent.
    """
    meta = manager.create(
        origin_node_id=node_id,
        origin_kind="bridge_node",
        workflow_name="",
        source=SourceRef(filename=f"annotate_{node_id}.png", subfolder="", type="temp"),
        original_image=source,
    )
    manager.ingest_edit(meta.handoff_id, edit, "plugin")
    return meta.handoff_id


def make_handoff_with_layered_edit(
    manager: HandoffManager,
    node_id: str,
    source: Image.Image,
    saved_base: Image.Image,
    instructions_image: Image.Image | None,
    instructions_layer_name: str = INSTRUCTIONS_LAYER_NAME,
) -> str:
    """A ``bridge_node`` handoff for *source* with a SAVED layered managed PSD
    copy already on disk, plus one edit recorded so the consume-without-
    reopening path is taken.

    The node under test reads the managed PSD copy directly
    (``cpsb.annotate._read_ps_saved_psd``), not the ingested edit PNG -- so
    this is the layered-PSD analogue of :func:`make_handoff_with_edit`:
    ``manager.ingest_edit`` still needs to fire (that's what makes
    ``active.edits`` non-empty, the signal the consume-without-reopening
    branch checks for), but the pixel content it's given is never looked at
    by the node -- only what :func:`make_layered_psd` writes to the managed
    copy is.
    """
    meta = manager.create(
        origin_node_id=node_id,
        origin_kind="bridge_node",
        workflow_name="",
        source=SourceRef(filename=f"annotate_{node_id}.png", subfolder="", type="temp"),
        original_image=source,
    )
    psd_path = manager.psd_path(meta)
    make_layered_psd(psd_path, saved_base, instructions_image, instructions_layer_name)
    manager.ingest_edit(meta.handoff_id, saved_base, "plugin")
    return meta.handoff_id


def make_handoff_via_remote_psd_upload(
    manager: HandoffManager,
    node_id: str,
    source: Image.Image,
    saved_base: Image.Image,
    instructions_image: Image.Image | None,
    tmp_path: Path,
    instructions_layer_name: str = INSTRUCTIONS_LAYER_NAME,
) -> str:
    """The REMOTE Tier-2 upload analogue of :func:`make_handoff_with_layered_edit`
    (PROTOCOL.md §6d): instead of writing the layered PSD straight to the
    handoff's managed copy path (as a LOCAL save would), this builds it as
    independent BYTES -- what a REMOTE-mode plugin would have uploaded over
    the websocket -- and feeds them through
    ``cpsb.routes._ingest_psd_upload``, the actual server-side entry point
    a `kind: "psd"` `upload_edit` chunk resolves to
    (:func:`cpsb.routes._handle_upload_edit_chunk`). This proves the full
    remote round trip -- upload -> write managed copy -> ingest -> node
    consumption -- produces IDENTICAL results to the local-mode fixture,
    not just that the write function works in isolation.
    """
    meta = manager.create(
        origin_node_id=node_id,
        origin_kind="bridge_node",
        workflow_name="",
        source=SourceRef(filename=f"annotate_{node_id}.png", subfolder="", type="temp"),
        original_image=source,
        wants_layered_psd=True,
    )
    scratch = tmp_path / f"remote_upload_{node_id}.psd"
    make_layered_psd(scratch, saved_base, instructions_image, instructions_layer_name)
    raw_bytes = scratch.read_bytes()
    edit, error, reason = routes_module._ingest_psd_upload(manager, meta, raw_bytes)
    assert error is None, (error, reason)
    assert edit is not None
    return meta.handoff_id


def raises_interrupt():
    """The interrupt this test environment surfaces as (no real ComfyUI installed).

    ``nodes._raise_interrupt`` falls back to a plain ``RuntimeError`` when
    ``comfy.model_management`` isn't importable -- exactly the same fallback
    ``test_nodes.py``'s own blocking-wait tests rely on.
    """
    return pytest.raises(RuntimeError, match=r"comfy\.model_management")


class TestImportability:
    def test_module_imports_without_torch(self):
        """Mirrors ``test_nodes.py``'s identical check for ``cpsb.nodes``."""
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import cpsb.annotate as m, sys\n"
                "assert m.PhotoshopAnnotate is not None\n"
                "print('torch' in sys.modules)",
            ],
            cwd=str(Path(__file__).resolve().parent.parent),
            capture_output=True,
            text=True,
            check=True,
        )
        assert result.stdout.strip() == "False", result.stderr


class TestContractShape:
    def test_input_types_match_protocol(self):
        spec = annotate_module.PhotoshopAnnotate.INPUT_TYPES()
        assert spec["required"]["image"] == ("IMAGE",)
        assert spec["required"]["instruction"] == ("STRING", {"multiline": True, "default": ""})
        assert spec["required"]["mode"] == (
            ["Pass through", "Wait for first save", "Re-run on every save"],
            {"default": "Pass through"},
        )
        assert spec["required"]["box_composite"] == ("BOOLEAN", {"default": False})
        assert spec["required"]["timeout_seconds"] == (
            "INT",
            {"default": 1800, "min": 10, "max": 86400},
        )
        assert spec["optional"] == {"mask": ("MASK",)}
        assert spec["hidden"] == {"unique_id": "UNIQUE_ID"}

    def test_mode_widget_matches_bridge_node_options_exactly(self):
        """Product-owner spec, 2026-07-19: this node's ``mode`` options must be
        CONSISTENT with the bridge node's -- a drift guard. The last two
        options are literally the SAME string objects as
        ``cpsb.nodes.BridgeMode``'s constants (:class:`AnnotateMode` imports
        them rather than duplicating the literals), so this can never
        silently reword itself away from the bridge node's own values.
        """
        spec = annotate_module.PhotoshopAnnotate.INPUT_TYPES()
        options, config = spec["required"]["mode"]
        assert options == [
            annotate_module.AnnotateMode.PASS_THROUGH,
            nodes_module.BridgeMode.WAIT_FIRST_SAVE,
            nodes_module.BridgeMode.RERUN_EVERY_SAVE,
        ]
        assert config == {"default": annotate_module.AnnotateMode.PASS_THROUGH}
        wait_first_save = annotate_module.AnnotateMode.WAIT_FIRST_SAVE
        rerun_every_save = annotate_module.AnnotateMode.RERUN_EVERY_SAVE
        assert wait_first_save == nodes_module.BridgeMode.WAIT_FIRST_SAVE
        assert rerun_every_save == nodes_module.BridgeMode.RERUN_EVERY_SAVE

    def test_node_attributes(self):
        node_cls = annotate_module.PhotoshopAnnotate
        assert node_cls.CATEGORY == "image/photoshop"
        assert node_cls.RETURN_TYPES == ("IMAGE", "MASK", "STRING", "IMAGE")
        assert node_cls.FUNCTION == "execute"


class TestInstructionPassthrough:
    def test_exact_string_returned_pass_through(self, node):
        tensor = make_tensor(RED)
        text = "Remove the red car,\nadd a blue sky. Ünïcödé + emoji 🎨"
        result = node.execute(
            image=tensor,
            instruction=text,
            mode=AnnotateMode.PASS_THROUGH,
            box_composite=False,
            timeout_seconds=60,
            unique_id="60",
            mask=None,
        )
        assert result[2] == text
        assert result[2] is text  # returned verbatim, never rebuilt

    def test_exact_string_returned_ps_mode(self, node, manager):
        """PS mode with a pre-existing edit: the consume path, no blocking."""
        source = Image.new("RGB", (24, 16), RED)
        instructions = Image.new("RGBA", (24, 16), (0, 0, 0, 0))
        ImageDraw.Draw(instructions).rectangle((1, 1, 3, 3), fill=(255, 255, 255, 255))
        make_handoff_with_layered_edit(manager, "61", source, source, instructions)

        result = node.execute(
            image=tensor_from_image(source),
            instruction="fix the sky",
            mode=AnnotateMode.WAIT_FIRST_SAVE,
            box_composite=False,
            timeout_seconds=60,
            unique_id="61",
            mask=None,
        )
        assert result[2] == "fix the sky"


class TestWriteLayeredHandoff:
    """product-owner spec, 2026-07-17: opening PS mode with no active handoff
    writes the handoff PSD LAYERED -- the input image as a base pixel layer
    plus a fully-transparent top-level layer named exactly "Instructions" --
    instead of the old flat, single-layer write.
    """

    def test_source_psd_has_base_plus_blank_instructions_layer(self, node, manager, launches):
        node_id = "120"
        width, height = 12, 10
        tensor = make_tensor(RED, size=(width, height))

        with raises_interrupt():
            node.execute(
                image=tensor,
                instruction="",
                mode=AnnotateMode.WAIT_FIRST_SAVE,
                box_composite=False,
                timeout_seconds=_SHORT_TIMEOUT,
                unique_id=node_id,
                mask=None,
            )

        active = manager.find_active_for_node(node_id)
        psd_path = manager.psd_path(active)
        assert psd_path.name == f"annotate_{node_id}.psd"
        assert psd_path.exists()

        psd = PSDImage.open(psd_path)
        top_level = list(psd)
        assert len(top_level) == 2  # exactly the base layer + Instructions
        names = [layer.name for layer in top_level]
        assert names.count(INSTRUCTIONS_LAYER_NAME) == 1
        assert top_level[-1].name == INSTRUCTIONS_LAYER_NAME  # topmost: last-inserted

        instructions_layer = next(
            layer for layer in psd if layer.name == INSTRUCTIONS_LAYER_NAME
        )
        composite = instructions_layer.composite(viewport=psd.viewbox)
        assert composite is not None
        alpha = np.array(composite.convert("RGBA").split()[-1])
        assert not alpha.any()  # fully transparent everywhere

        base_layer = next(layer for layer in psd if layer.name != INSTRUCTIONS_LAYER_NAME)
        base_composite = base_layer.composite(viewport=psd.viewbox).convert("RGB")
        assert base_composite.size == (width, height)
        assert base_composite.getpixel((0, 0)) == RED

    def test_instructions_layer_is_paintable_not_a_black_masked_layer(
        self, node, manager, launches
    ):
        """REGRESSION (user-reported, 2026-07-18: "a black layer with a black
        mask. So drawing on it does nothing").

        psd-tools picks where a layer's alpha goes from the PARENT document's
        ``pil_mode``: for an "RGB" document ``create_pixel_layer`` converted the
        transparent RGBA source down to RGB -- compositing it onto BLACK -- and
        re-attached the discarded alpha as an all-zero USER_LAYER_MASK. In
        Photoshop that is an opaque black layer behind a mask that hides every
        brush stroke.

        Note the sibling test above cannot catch this: it measures transparency
        through ``composite()``, which APPLIES the layer mask and so reported a
        perfectly transparent layer the whole time the bug was shipping. Only
        the layer's STRUCTURE distinguishes the two, so that is what this
        asserts: opacity carried by the layer's own TRANSPARENCY_MASK channel
        (-1), and no USER_LAYER_MASK (-2) at all.
        """
        node_id = "122"
        with raises_interrupt():
            node.execute(
                image=make_tensor(RED),
                instruction="",
                mode=AnnotateMode.WAIT_FIRST_SAVE,
                box_composite=False,
                timeout_seconds=_SHORT_TIMEOUT,
                unique_id=node_id,
                mask=None,
            )

        active = manager.find_active_for_node(node_id)
        psd = PSDImage.open(manager.psd_path(active))
        instructions_layer = next(
            layer for layer in psd if layer.name == INSTRUCTIONS_LAYER_NAME
        )

        assert instructions_layer.mask is None, (
            "Instructions layer carries a user layer mask -- Photoshop will hide "
            "every brush stroke behind it"
        )
        channel_ids = {int(channel.id) for channel in instructions_layer._record.channel_info}
        assert -2 not in channel_ids  # USER_LAYER_MASK
        assert -1 in channel_ids  # TRANSPARENCY_MASK: the layer's own alpha

        # The pixel data itself must be transparent, read WITHOUT going through
        # composite() -- topil() ignores any mask, so this is the assertion that
        # actually failed under the bug (it read back fully opaque black).
        pixels = instructions_layer.topil()
        assert pixels.mode == "RGBA"
        assert not np.array(pixels.split()[-1]).any()

        # And the file stays a plain 3-channel RGB document, so Photoshop never
        # shows a stray "Alpha 1" in its Channels panel.
        assert psd._record.header.channels == 3

    def test_launched_psd_path_matches_the_written_source_psd(self, node, manager, launches):
        """The exact path handed to ``launch_photoshop`` is the layered file
        just asserted above -- not some other intermediate path.
        """
        node_id = "121"
        tensor = make_tensor(RED)
        with raises_interrupt():
            node.execute(
                image=tensor,
                instruction="",
                mode=AnnotateMode.WAIT_FIRST_SAVE,
                box_composite=False,
                timeout_seconds=_SHORT_TIMEOUT,
                unique_id=node_id,
                mask=None,
            )
        active = manager.find_active_for_node(node_id)
        expected = manager.psd_path(active)
        assert launches == [str(expected)]

    def test_created_handoff_is_flagged_wants_layered_psd(self, node, manager, launches):
        """Remote Tier-2 layered annotate (PROTOCOL.md §6d): every handoff
        this node creates must record ``wants_layered_psd=True`` -- the
        signal ``cpsb.routes.open_in_photoshop`` echoes into the plugin's
        `open_handoff` command so a REMOTE-mode plugin knows to upload its
        save as raw PSD bytes instead of a flattened PNG. NOT derivable from
        ``origin_kind`` alone: the plain Photoshop Bridge node
        (``cpsb.nodes``) also creates ``"bridge_node"``-origin handoffs, with
        a flat (unflagged) managed copy.
        """
        node_id = "123"
        with raises_interrupt():
            node.execute(
                image=make_tensor(RED),
                instruction="",
                mode=AnnotateMode.WAIT_FIRST_SAVE,
                box_composite=False,
                timeout_seconds=_SHORT_TIMEOUT,
                unique_id=node_id,
                mask=None,
            )

        active = manager.find_active_for_node(node_id)
        assert active.origin_kind == "bridge_node"
        assert active.wants_layered_psd is True


class TestReadFoundInstructionsLayer:
    """product-owner spec: a saved PSD with a top-level "Instructions" layer
    yields MASK = that layer's own opacity and IMAGE = the composite of
    every OTHER layer -- so a base-layer edit bakes into the image output,
    while paint on the Instructions layer never does.
    """

    @pytest.fixture(autouse=True)
    def _require_torch(self):
        pytest.importorskip("torch")

    def test_painted_instructions_layer_mask_matches_painted_region(self, node, manager):
        node_id = "130"
        width, height = 24, 16
        source = Image.new("RGB", (width, height), RED)
        instructions = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        ImageDraw.Draw(instructions).rectangle((5, 3, 10, 8), fill=(255, 255, 255, 255))
        make_handoff_with_layered_edit(manager, node_id, source, source, instructions)

        image_out, mask_out, _, _ = node.execute(
            image=tensor_from_image(source),
            instruction="",
            mode=AnnotateMode.WAIT_FIRST_SAVE,
            box_composite=False,
            timeout_seconds=60,
            unique_id=node_id,
            mask=None,
        )

        mask_np = mask_out[0].numpy()
        assert mask_np[3:9, 5:11].min() > 0.9  # painted region: opaque
        assert mask_np[0, 0] == 0  # untouched region: fully zero
        assert mask_np[15, 23] == 0  # opposite corner: fully zero

        # The base layer was never touched -- image output is the source,
        # unaffected by the Instructions layer's own paint.
        image_np = image_tensor_to_uint8_array(image_out)
        assert np.array_equal(image_np, np.array(source))

    def test_annotated_output_carries_the_real_painted_strokes(self, node, manager):
        """End-to-end for the "combined" view: with box_composite off, the
        `annotated` output is the base image WITH the strokes on it, while
        `image` stays clean. The two outputs must genuinely differ -- that is
        the whole point of having both.
        """
        node_id = "137"
        width, height = 24, 16
        source = Image.new("RGB", (width, height), RED)
        instructions = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        ImageDraw.Draw(instructions).rectangle((5, 3, 10, 8), fill=(0, 0, 255, 255))
        make_handoff_with_layered_edit(manager, node_id, source, source, instructions)

        image_out, _mask_out, _instruction, annotated_out = node.execute(
            image=tensor_from_image(source),
            instruction="",
            mode=AnnotateMode.WAIT_FIRST_SAVE,
            box_composite=False,
            timeout_seconds=60,
            unique_id=node_id,
            mask=None,
        )

        annotated_np = image_tensor_to_uint8_array(annotated_out)
        image_np = image_tensor_to_uint8_array(image_out)

        assert tuple(annotated_np[5, 7]) == (0, 0, 255)  # the stroke is visible
        assert tuple(annotated_np[15, 23]) == RED  # base elsewhere
        assert tuple(image_np[5, 7]) == RED  # `image` stays clean
        assert not np.array_equal(annotated_np, image_np)

    def test_edited_base_layer_bakes_into_image_output(self, node, manager):
        """A base layer the user actually painted on (not just the
        Instructions layer) bakes that edit into the IMAGE output --
        "any edits the user made to the base image BAKE INTO the image
        output" (product-owner spec).
        """
        node_id = "132"
        width, height = 20, 14
        source = Image.new("RGB", (width, height), RED)
        saved_base = source.copy()
        ImageDraw.Draw(saved_base).rectangle((2, 2, 6, 6), fill=BLUE)
        # Instructions layer present but left blank -- isolates the "base
        # edit bakes in" behavior from any mask painting.
        blank_instructions = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        make_handoff_with_layered_edit(manager, node_id, source, saved_base, blank_instructions)

        image_out, mask_out, _, _ = node.execute(
            image=tensor_from_image(source),
            instruction="",
            mode=AnnotateMode.WAIT_FIRST_SAVE,
            box_composite=False,
            timeout_seconds=60,
            unique_id=node_id,
            mask=None,
        )

        image_np = image_tensor_to_uint8_array(image_out)
        assert np.array_equal(image_np, np.array(saved_base))  # edit baked in
        assert tuple(image_np[4, 4]) == BLUE  # inside the edited patch
        assert tuple(image_np[0, 0]) == RED  # outside it: untouched

        import torch

        assert torch.count_nonzero(mask_out).item() == 0  # Instructions left blank

    def test_box_composite_draws_box_from_layer_derived_mask(self, node, manager):
        node_id = "131"
        width, height = 60, 50
        source = Image.new("RGB", (width, height), (0, 0, 0))
        instructions = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        # A wide margin around the painted region so the box's 4px stroke
        # leaves genuine untouched interior to check (unlike a thin mask,
        # where the stroke could eat the whole region).
        ImageDraw.Draw(instructions).rectangle((10, 10, 50, 40), fill=(255, 255, 255, 255))
        make_handoff_with_layered_edit(manager, node_id, source, source, instructions)

        _, _, _, annotated_out = node.execute(
            image=tensor_from_image(source),
            instruction="",
            mode=AnnotateMode.WAIT_FIRST_SAVE,
            box_composite=True,
            timeout_seconds=60,
            unique_id=node_id,
            mask=None,
        )

        annotated_np = image_tensor_to_uint8_array(annotated_out)
        assert tuple(annotated_np[10, 10]) == RED  # top-left border pixel of the box
        assert tuple(annotated_np[25, 30]) == (0, 0, 0)  # deep interior: untouched (unfilled box)


class TestRemoteLayeredPsdUpload:
    """PROTOCOL.md §6d, remote Tier-2 layered annotate: a REMOTE-mode
    plugin upload of the document's own raw, layered PSD bytes
    (`cpsb.routes._ingest_psd_upload`, reached via a `kind: "psd"`
    `upload_edit` chunk) must produce the SAME mask/image results as a
    LOCAL save landing directly on the managed copy. Every test here is the
    direct remote-upload counterpart of one in
    :class:`TestReadFoundInstructionsLayer` above -- same scenario, same
    assertions, only :func:`make_handoff_with_layered_edit` swapped for
    :func:`make_handoff_via_remote_psd_upload` -- so a divergence between
    the two paths shows up as a mismatched test, not just a passing new one.
    """

    @pytest.fixture(autouse=True)
    def _require_torch(self):
        pytest.importorskip("torch")

    def test_painted_instructions_layer_mask_matches_painted_region(
        self, node, manager, tmp_path
    ):
        node_id = "140"
        width, height = 24, 16
        source = Image.new("RGB", (width, height), RED)
        instructions = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        ImageDraw.Draw(instructions).rectangle((5, 3, 10, 8), fill=(255, 255, 255, 255))
        make_handoff_via_remote_psd_upload(manager, node_id, source, source, instructions, tmp_path)

        image_out, mask_out, _, _ = node.execute(
            image=tensor_from_image(source),
            instruction="",
            mode=AnnotateMode.WAIT_FIRST_SAVE,
            box_composite=False,
            timeout_seconds=60,
            unique_id=node_id,
            mask=None,
        )

        mask_np = mask_out[0].numpy()
        assert mask_np[3:9, 5:11].min() > 0.9  # painted region: opaque
        assert mask_np[0, 0] == 0  # untouched region: fully zero
        assert mask_np[15, 23] == 0  # opposite corner: fully zero

        image_np = image_tensor_to_uint8_array(image_out)
        assert np.array_equal(image_np, np.array(source))

    def test_edited_base_layer_bakes_into_image_output(self, node, manager, tmp_path):
        node_id = "141"
        width, height = 20, 14
        source = Image.new("RGB", (width, height), RED)
        saved_base = source.copy()
        ImageDraw.Draw(saved_base).rectangle((2, 2, 6, 6), fill=BLUE)
        blank_instructions = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        make_handoff_via_remote_psd_upload(
            manager, node_id, source, saved_base, blank_instructions, tmp_path
        )

        image_out, mask_out, _, _ = node.execute(
            image=tensor_from_image(source),
            instruction="",
            mode=AnnotateMode.WAIT_FIRST_SAVE,
            box_composite=False,
            timeout_seconds=60,
            unique_id=node_id,
            mask=None,
        )

        image_np = image_tensor_to_uint8_array(image_out)
        assert np.array_equal(image_np, np.array(saved_base))  # edit baked in
        assert tuple(image_np[4, 4]) == BLUE
        assert tuple(image_np[0, 0]) == RED

        import torch

        assert torch.count_nonzero(mask_out).item() == 0  # Instructions left blank

    def test_rerun_every_save_mode_also_consumes_the_remote_upload(self, node, manager, tmp_path):
        """The non-blocking mode (`RERUN_EVERY_SAVE`) shares the identical
        consume-without-reopening path -- confirm the remote-upload fixture
        works for it too, not just `WAIT_FIRST_SAVE`.
        """
        node_id = "142"
        width, height = 18, 18
        source = Image.new("RGB", (width, height), RED)
        instructions = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        ImageDraw.Draw(instructions).rectangle((1, 1, 4, 4), fill=(255, 255, 255, 255))
        make_handoff_via_remote_psd_upload(manager, node_id, source, source, instructions, tmp_path)

        result = node.execute(
            image=tensor_from_image(source),
            instruction="",
            mode=AnnotateMode.RERUN_EVERY_SAVE,
            box_composite=False,
            timeout_seconds=1800,
            unique_id=node_id,
            mask=None,
        )
        mask_np = result[1][0].numpy()
        assert mask_np[1:5, 1:5].min() > 0.9
        assert mask_np[10, 10] == 0


class TestReadMissingInstructionsLayer:
    """product-owner spec: no top-level "Instructions" layer (renamed or
    deleted) falls back to treating the saved file as a plain edited image.
    """

    @pytest.fixture(autouse=True)
    def _require_torch(self):
        pytest.importorskip("torch")

    def test_no_instructions_layer_yields_full_composite_and_zero_mask(self, node, manager):
        node_id = "140"
        width, height = 20, 14
        source = Image.new("RGB", (width, height), RED)
        saved = Image.new("RGB", (width, height), GREEN)
        make_handoff_with_layered_edit(manager, node_id, source, saved, instructions_image=None)

        image_out, mask_out, _, _ = node.execute(
            image=tensor_from_image(source),
            instruction="",
            mode=AnnotateMode.WAIT_FIRST_SAVE,
            box_composite=False,
            timeout_seconds=60,
            unique_id=node_id,
            mask=None,
        )

        image_np = image_tensor_to_uint8_array(image_out)
        assert np.array_equal(image_np, np.array(saved))  # full composite, no layer excluded

        import torch

        assert torch.count_nonzero(mask_out).item() == 0

    def test_no_instructions_layer_falls_back_to_mask_socket(self, node, manager):
        import torch

        node_id = "143"
        width, height = 20, 14
        source = Image.new("RGB", (width, height), RED)
        make_handoff_with_layered_edit(
            manager, node_id, source, source, instructions_image=None
        )

        socket_mask = torch.zeros((1, height, width))
        socket_mask[0, 5, 5] = 0.75

        _, mask_out, _, _ = node.execute(
            image=tensor_from_image(source),
            instruction="",
            mode=AnnotateMode.WAIT_FIRST_SAVE,
            box_composite=False,
            timeout_seconds=60,
            unique_id=node_id,
            mask=socket_mask,
        )
        assert torch.allclose(mask_out, socket_mask)

    def test_renamed_instructions_layer_bakes_its_own_paint_into_full_composite(
        self, node, manager
    ):
        """A layer that WOULD have been "Instructions" but got renamed is no
        longer excluded from anything -- it's just another layer in the
        full composite, paint and all (PROTOCOL.md §6d: "if that layer is
        renamed or deleted then the image is just treated like an image").
        """
        node_id = "144"
        width, height = 16, 12
        source = Image.new("RGB", (width, height), RED)
        renamed_layer_image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        ImageDraw.Draw(renamed_layer_image).rectangle((2, 2, 5, 5), fill=(0, 0, 255, 255))
        make_handoff_with_layered_edit(
            manager,
            node_id,
            source,
            source,
            renamed_layer_image,
            instructions_layer_name="Notes",
        )

        image_out, mask_out, _, _ = node.execute(
            image=tensor_from_image(source),
            instruction="",
            mode=AnnotateMode.WAIT_FIRST_SAVE,
            box_composite=False,
            timeout_seconds=60,
            unique_id=node_id,
            mask=None,
        )

        image_np = image_tensor_to_uint8_array(image_out)
        assert tuple(image_np[3, 3]) == BLUE  # the renamed layer's own paint, baked in
        assert tuple(image_np[0, 0]) == RED  # untouched elsewhere

        import torch

        assert torch.count_nonzero(mask_out).item() == 0


class TestBboxOfNonzero:
    def test_simple_rectangle(self):
        mask = np.zeros((10, 10), dtype=np.float32)
        mask[2:5, 3:7] = 1.0
        assert annotate_module._bbox_of_nonzero(mask) == (3, 2, 6, 4)

    def test_all_zero_returns_none(self):
        mask = np.zeros((10, 10), dtype=np.float32)
        assert annotate_module._bbox_of_nonzero(mask) is None

    def test_edge_touching_mask(self):
        mask = np.zeros((10, 12), dtype=np.float32)
        mask[0, 0] = 1.0
        mask[9, 11] = 1.0  # opposite corners, touching every edge
        assert annotate_module._bbox_of_nonzero(mask) == (0, 0, 11, 9)

    def test_multi_region_uses_bbox_of_all_nonzero_pixels(self):
        """PROTOCOL.md §6d: multi-region masks get ONE box spanning
        everything (v1 simplification), not per-region boxes.
        """
        mask = np.zeros((20, 20), dtype=np.float32)
        mask[1, 1] = 1.0
        mask[15, 17] = 1.0
        assert annotate_module._bbox_of_nonzero(mask) == (1, 1, 17, 15)


class TestBuildAnnotated:
    @pytest.fixture(autouse=True)
    def _require_torch(self):
        pytest.importorskip("torch")

    def test_no_box_composite_returns_identical_tensor_object(self):
        tensor = make_tensor(RED)
        mask = np.zeros((16, 24), dtype=np.float32)
        result = annotate_module.PhotoshopAnnotate._build_annotated(tensor, mask, False)
        assert result is tensor

    def test_empty_mask_is_a_no_op_even_with_box_composite_true(self):
        tensor = make_tensor(RED)
        mask = np.zeros((16, 24), dtype=np.float32)
        result = annotate_module.PhotoshopAnnotate._build_annotated(tensor, mask, True)
        assert result is tensor

    def test_without_box_composite_the_real_strokes_are_shown(self):
        """product-owner request, 2026-07-18: "ideally there would be a way to
        see ... the imaging layers and annotations combined".

        With box_composite off, `annotated` is the base image carrying the
        user's ACTUAL painted strokes in their real colors. Before this, the
        branch returned the image completely unannotated, which made the output
        indistinguishable from `image` and left no way to see what was painted.
        """
        tensor = make_tensor(RED, size=(24, 16))
        mask = np.zeros((16, 24), dtype=np.float32)
        mask[4:8, 4:8] = 1.0
        combined = Image.new("RGB", (24, 16), RED)
        for x in range(4, 8):
            for y in range(4, 8):
                combined.putpixel((x, y), (0, 0, 255))  # the brush strokes

        result = annotate_module.PhotoshopAnnotate._build_annotated(
            tensor, mask, False, combined
        )
        assert result is not tensor
        arr = (result[0].numpy() * 255).round().astype(np.uint8)
        assert tuple(arr[5, 5]) == (0, 0, 255)  # the stroke, in its real color
        assert tuple(arr[12, 20]) == RED  # untouched base elsewhere

    def test_pass_through_without_a_composite_stays_unchanged(self):
        """ComfyUI-only tier: no Photoshop document, so no strokes exist to
        combine -- the output must stay the exact same tensor object.
        """
        tensor = make_tensor(RED)
        mask = np.zeros((16, 24), dtype=np.float32)
        mask[2:5, 2:5] = 1.0
        result = annotate_module.PhotoshopAnnotate._build_annotated(tensor, mask, False, None)
        assert result is tensor

    def test_box_composite_wins_over_the_real_strokes(self):
        """box_composite selects the FORM of the annotation. The tidy box
        replaces the raw strokes rather than adding to them -- a marking blob
        plus a box around it is noisier for a box-prompt model than the box.
        """
        width, height = 40, 30
        tensor = make_tensor((0, 0, 0), size=(width, height))
        mask = np.zeros((height, width), dtype=np.float32)
        mask[5:25, 5:35] = 1.0
        combined = Image.new("RGB", (width, height), (9, 9, 9))  # would be obvious

        result = annotate_module.PhotoshopAnnotate._build_annotated(
            tensor, mask, True, combined
        )
        arr = (result[0].numpy() * 255).round().astype(np.uint8)
        assert tuple(arr[5, 5]) == (255, 0, 0)  # the box
        assert tuple(arr[15, 20]) == (0, 0, 0)  # clean image, NOT the combined view

    def test_box_drawn_at_mask_bounding_box(self):
        width, height = 40, 30
        tensor = make_tensor((0, 0, 0), size=(width, height))
        mask = np.zeros((height, width), dtype=np.float32)
        mask[5:25, 5:35] = 1.0  # bbox: x0=5, y0=5, x1=34, y1=24
        result = annotate_module.PhotoshopAnnotate._build_annotated(tensor, mask, True)
        assert result is not tensor

        result_arr = (result[0].numpy() * 255).round().astype(np.uint8)
        assert tuple(result_arr[5, 5]) == (255, 0, 0)  # top-left border pixel: red
        assert tuple(result_arr[15, 20]) == (0, 0, 0)  # deep interior: untouched (no fill)


class TestMaskPrecedence:
    """PROTOCOL.md §6d: PS mode's Instructions-layer mask > mask socket > zeros."""

    @pytest.fixture(autouse=True)
    def _require_torch(self):
        pytest.importorskip("torch")

    def test_zeros_when_pass_through_and_no_mask_socket(self, node):
        import torch

        tensor = make_tensor((10, 20, 30))
        _, mask_out, _, _ = node.execute(
            image=tensor,
            instruction="",
            mode=AnnotateMode.PASS_THROUGH,
            box_composite=False,
            timeout_seconds=60,
            unique_id="80",
            mask=None,
        )
        assert mask_out.shape == (1, 16, 24)
        assert torch.count_nonzero(mask_out).item() == 0

    def test_socket_mask_used_when_pass_through(self, node):
        import torch

        tensor = make_tensor(RED)
        socket_mask = torch.zeros((1, 16, 24))
        socket_mask[0, 3, 5] = 0.75
        _, mask_out, _, _ = node.execute(
            image=tensor,
            instruction="",
            mode=AnnotateMode.PASS_THROUGH,
            box_composite=False,
            timeout_seconds=60,
            unique_id="81",
            mask=socket_mask,
        )
        assert torch.allclose(mask_out, socket_mask)

    def test_layer_mask_wins_over_socket_mask_in_ps_mode(self, node, manager):
        node_id = "82"
        width, height = 24, 16
        source = Image.new("RGB", (width, height), (0, 0, 0))
        instructions = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        ImageDraw.Draw(instructions).point((5, 5), fill=(255, 255, 255, 255))
        make_handoff_with_layered_edit(manager, node_id, source, source, instructions)

        import torch

        socket_mask = torch.zeros((1, height, width))
        socket_mask[0, 10, 10] = 1.0  # a DIFFERENT location than the layer mask

        _, mask_out, _, _ = node.execute(
            image=tensor_from_image(source),
            instruction="",
            mode=AnnotateMode.WAIT_FIRST_SAVE,
            box_composite=False,
            timeout_seconds=60,
            unique_id=node_id,
            mask=socket_mask,
        )
        assert mask_out[0, 5, 5] > 0  # the layer-mask pixel
        assert mask_out[0, 10, 10] == 0  # the socket-only pixel is NOT used

    def test_ps_mode_ignored_when_mode_is_pass_through(self, node, manager):
        """A leftover handoff+edit must not leak into pass-through mode."""
        import torch

        node_id = "83"
        source = Image.new("RGB", (24, 16), (0, 0, 0))
        edit = source.copy()
        edit.putpixel((5, 5), (255, 255, 255))
        make_handoff_with_edit(manager, node_id, source, edit)

        _, mask_out, _, _ = node.execute(
            image=tensor_from_image(source),
            instruction="",
            mode=AnnotateMode.PASS_THROUGH,
            box_composite=False,
            timeout_seconds=60,
            unique_id=node_id,
            mask=None,
        )
        assert torch.count_nonzero(mask_out).item() == 0  # zeros tier: handoff never consulted


class TestWaitForFirstSaveBlocking:
    """PROTOCOL.md §6d (2026-07-17 update; mode renamed to
    :data:`AnnotateMode.WAIT_FIRST_SAVE`, product-owner spec 2026-07-19):
    "Wait for first save" with no consumable edit BLOCKS ``execute()`` --
    open Photoshop, then wait -- instead of the earlier fire-and-forget
    open. Mirrors ``test_nodes.py``'s ``TestWaitForFirstSaveMode``/
    ``TestExecute`` shapes. See :class:`TestRerunEverySaveMode` below for
    the NON-blocking mode's tests.
    """

    def test_blocks_until_edit_then_returns_instructions_mask(self, node, manager, monkeypatch):
        node_id = "110"
        width, height = 24, 16
        tensor = make_tensor(RED, size=(width, height))

        def _save_shortly_after_open(psd_path, override=""):
            # Ingest from a delayed background thread: the launch call
            # returning "ok" (mark_editing) must be visible before the edit
            # lands, exactly as real Photoshop usage sequences (a human
            # editing and saving takes far longer than the launch call
            # itself) -- see test_nodes.py's identical pattern/comment.
            def _do_save():
                active = manager.find_active_for_node(node_id)
                saved_psd_path = manager.psd_path(active)
                base = Image.new("RGB", (width, height), RED)
                instructions = Image.new("RGBA", (width, height), (0, 0, 0, 0))
                ImageDraw.Draw(instructions).point((5, 5), fill=(255, 255, 255, 255))
                make_layered_psd(saved_psd_path, base, instructions)
                manager.ingest_edit(active.handoff_id, base, "plugin")

            threading.Timer(0.3, _do_save).start()
            return LaunchResult(ok=True)

        monkeypatch.setattr(routes_module, "launch_photoshop", _save_shortly_after_open)

        _, mask_out, instruction_out, _ = node.execute(
            image=tensor,
            instruction="mark the sky",
            mode=AnnotateMode.WAIT_FIRST_SAVE,
            box_composite=False,
            timeout_seconds=10,
            unique_id=node_id,
            mask=None,
        )
        assert instruction_out == "mark the sky"
        assert mask_out[0, 5, 5] > 0  # the Instructions-layer pixel the background save painted

        active = manager.find_active_for_node(node_id)
        assert active is not None
        assert active.status == "edited"

    def test_open_failure_marks_error_and_interrupts_without_hanging(
        self, node, manager, monkeypatch
    ):
        """PROTOCOL.md §6d: "If the open itself fails ... log the error,
        mark the handoff error, and interrupt (don't hang)."
        """
        node_id = "111"
        tensor = make_tensor(RED)
        monkeypatch.setattr(
            routes_module,
            "launch_photoshop",
            lambda psd_path, override="": LaunchResult(ok=False, error="no Photoshop found"),
        )

        with raises_interrupt():
            node.execute(
                image=tensor,
                instruction="",
                mode=AnnotateMode.WAIT_FIRST_SAVE,
                box_composite=False,
                timeout_seconds=_SHORT_TIMEOUT,
                unique_id=node_id,
                mask=None,
            )

        # "error" is not an ACTIVE_STATUSES member, so it no longer shows up
        # as this node's active handoff -- confirm the handoff itself via
        # list_all instead.
        assert manager.find_active_for_node(node_id) is None
        matching = [h for h in manager.list_all() if h.origin_node_id == node_id]
        assert len(matching) == 1
        assert matching[0].status == "error"

    def test_timeout_interrupts_and_handoff_stays_editing(self, node, manager, launches):
        """PROTOCOL.md §6: on timeout the handoff stays `editing` (not
        error/cancelled), so a later save or re-queue resumes the same PSD.
        """
        node_id = "112"
        tensor = make_tensor(RED)
        with raises_interrupt():
            node.execute(
                image=tensor,
                instruction="",
                mode=AnnotateMode.WAIT_FIRST_SAVE,
                box_composite=False,
                timeout_seconds=_SHORT_TIMEOUT,
                unique_id=node_id,
                mask=None,
            )
        active = manager.find_active_for_node(node_id)
        assert active is not None
        assert active.status == "editing"
        assert len(launches) == 1

    def test_cancel_interrupts_promptly(self, node, manager, monkeypatch):
        """`/cpsb/cancel` (mark_cancelled) unblocks a waiting node
        immediately, without waiting out the full timeout (PROTOCOL.md §2).
        """
        node_id = "113"
        tensor = make_tensor(RED)

        def _cancel_shortly_after_open(psd_path, override=""):
            def _do_cancel():
                active = manager.find_active_for_node(node_id)
                manager.mark_cancelled(active.handoff_id)

            threading.Timer(0.3, _do_cancel).start()
            return LaunchResult(ok=True)

        monkeypatch.setattr(routes_module, "launch_photoshop", _cancel_shortly_after_open)

        start = time.monotonic()
        with raises_interrupt():
            node.execute(
                image=tensor,
                instruction="",
                mode=AnnotateMode.WAIT_FIRST_SAVE,
                box_composite=False,
                timeout_seconds=30,
                unique_id=node_id,
                mask=None,
            )
        elapsed = time.monotonic() - start
        assert elapsed < 5  # unblocked by cancellation, not the 30s timeout

        assert manager.find_active_for_node(node_id) is None  # cancelled: no longer "active"

    def test_requeue_after_timeout_reuses_and_reopens_same_handoff(
        self, node, manager, launches
    ):
        """A manual re-queue after a prior timeout resumes/refocuses the
        SAME handoff (layers intact) -- it never starts a fresh one, exactly
        like the bridge node's own "Wait for first save" re-queue behavior.
        """
        node_id = "114"
        tensor = make_tensor(RED)

        with raises_interrupt():
            node.execute(
                image=tensor,
                instruction="",
                mode=AnnotateMode.WAIT_FIRST_SAVE,
                box_composite=False,
                timeout_seconds=_SHORT_TIMEOUT,
                unique_id=node_id,
                mask=None,
            )
        first = manager.find_active_for_node(node_id)
        assert len(launches) == 1

        with raises_interrupt():
            node.execute(
                image=tensor,
                instruction="",
                mode=AnnotateMode.WAIT_FIRST_SAVE,
                box_composite=False,
                timeout_seconds=_SHORT_TIMEOUT,
                unique_id=node_id,
                mask=None,
            )
        second = manager.find_active_for_node(node_id)
        assert second.handoff_id == first.handoff_id  # reused, not a fresh handoff
        assert len(launches) == 2  # reopened

    def test_stale_handoff_from_changed_input_is_superseded_and_reopened(
        self, node, manager, launches
    ):
        node_id = "115"
        red_tensor = make_tensor(RED)
        with raises_interrupt():
            node.execute(
                image=red_tensor,
                instruction="",
                mode=AnnotateMode.WAIT_FIRST_SAVE,
                box_composite=False,
                timeout_seconds=_SHORT_TIMEOUT,
                unique_id=node_id,
                mask=None,
            )
        old = manager.find_active_for_node(node_id)

        green_tensor = make_tensor(GREEN)
        with raises_interrupt():
            node.execute(
                image=green_tensor,
                instruction="",
                mode=AnnotateMode.WAIT_FIRST_SAVE,
                box_composite=False,
                timeout_seconds=_SHORT_TIMEOUT,
                unique_id=node_id,
                mask=None,
            )
        assert manager.get(old.handoff_id).status == "superseded"
        fresh = manager.find_active_for_node(node_id)
        assert fresh.handoff_id != old.handoff_id
        assert fresh.edits == []
        assert len(launches) == 2  # reopened for the fresh handoff

    def test_pass_through_never_creates_a_handoff(self, node, manager, launches):
        tensor = make_tensor(RED)
        node.execute(
            image=tensor,
            instruction="",
            mode=AnnotateMode.PASS_THROUGH,
            box_composite=False,
            timeout_seconds=60,
            unique_id="93",
            mask=None,
        )
        assert manager.find_active_for_node("93") is None

    def test_unrecognized_mode_passes_through_without_handoff_but_warns(
        self, node, manager, launches, caplog
    ):
        # A stale/garbled mode string (e.g. a workflow saved before the
        # v0.5.30 annotate_mode->mode rename, whose old value ComfyUI cannot
        # carry to the new widget so it sits at its PASS_THROUGH default) must
        # behave like Pass-through -- input returned untouched, no handoff,
        # Photoshop never opened -- but LOUDLY. The old silent fall-through
        # read exactly like a broken "Wait for first save that does nothing".
        tensor = make_tensor(RED)
        with caplog.at_level(logging.WARNING, logger="cpsb"):
            result = node.execute(
                image=tensor,
                instruction="hello",
                mode="Open in Photoshop (mask from edits)",  # a genuine pre-v0.5.30 value
                box_composite=False,
                timeout_seconds=60,
                unique_id="94",
                mask=None,
            )
        assert result[0] is tensor  # passed through verbatim, never re-encoded
        assert result[2] == "hello"
        assert manager.find_active_for_node("94") is None  # never created/opened a handoff
        assert any("unrecognized mode" in record.message for record in caplog.records)
        assert len(launches) == 0

    def test_open_seam_invoked_with_diagnosable_log_trail(self, node, manager, launches, caplog):
        """Regression guard for the field report "the toggle didn't open
        Photoshop": proves the SAME tier-selecting launch seam the bridge
        node uses genuinely fires for a fresh "Wait for first save"
        execute(), and that every step is logged under the ``cpsb
        annotate:`` prefix so a future non-open is diagnosable from this
        node's own log trail alone.
        """
        node_id = "116"
        tensor = make_tensor(RED)
        caplog.set_level(logging.INFO, logger="cpsb")

        with raises_interrupt():
            node.execute(
                image=tensor,
                instruction="",
                mode=AnnotateMode.WAIT_FIRST_SAVE,
                box_composite=False,
                timeout_seconds=_SHORT_TIMEOUT,
                unique_id=node_id,
                mask=None,
            )

        assert len(launches) == 1  # the real launch_photoshop call actually fired

        messages = [r.message for r in caplog.records if r.name == "cpsb"]
        assert any(
            "cpsb annotate" in m and "opening Photoshop" in m and node_id in m for m in messages
        )
        assert any("cpsb annotate" in m and "launch result ok (tier 1)" in m for m in messages)
        assert any("cpsb annotate" in m and "waiting for edit" in m for m in messages)
        assert any("cpsb annotate" in m and "wait outcome 'timeout'" in m for m in messages)


class TestConsumePath:
    """"Wait for first save"'s consume-without-reopening path. See
    :class:`TestRerunEverySaveMode` for the identical check under "Re-run on
    every save".
    """

    def test_consumes_existing_edit_without_reopening(self, node, manager, launches):
        node_id = "94"
        width, height = 24, 16
        source = Image.new("RGB", (width, height), RED)
        instructions = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        ImageDraw.Draw(instructions).point((2, 2), fill=(255, 255, 255, 255))
        make_handoff_with_layered_edit(manager, node_id, source, source, instructions)

        tensor = tensor_from_image(source)
        result = node.execute(
            image=tensor,
            instruction="check this",
            mode=AnnotateMode.WAIT_FIRST_SAVE,
            box_composite=False,
            timeout_seconds=60,
            unique_id=node_id,
            mask=None,
        )
        assert len(launches) == 0  # never opened: consumed the existing edit instead
        assert result[2] == "check this"
        assert result[1][0, 2, 2] > 0


class TestRerunEverySaveMode:
    """PROTOCOL.md §6d (product-owner spec, 2026-07-19): "Re-run on every
    save" NEVER blocks -- it opens (or reuses) the handoff and returns
    immediately, relying on the frontend's auto-queue to re-invoke this node
    once the user saves. Mirrors ``test_nodes.py``'s
    ``TestRerunEverySaveMode`` shape.
    """

    def test_first_run_opens_non_blocking_and_passes_input_through(
        self, node, manager, launches
    ):
        """No hang: with no consumable edit yet, ``execute()`` returns
        immediately -- the input image and instruction straight through,
        the resolved (here: zero) mask -- while Photoshop opens in the
        background, exactly like Pass-through mode's own return shape.
        """
        node_id = "150"
        tensor = make_tensor(RED)

        start = time.monotonic()
        result = node.execute(
            image=tensor,
            instruction="mark it up",
            mode=AnnotateMode.RERUN_EVERY_SAVE,
            box_composite=False,
            timeout_seconds=1800,
            unique_id=node_id,
            mask=None,
        )
        elapsed = time.monotonic() - start
        assert elapsed < 5  # never blocked waiting for a save

        assert result[0] is tensor  # passthrough: the same tensor object
        assert result[2] == "mark it up"

        import torch

        assert torch.count_nonzero(result[1]).item() == 0  # no edit yet: zero mask

        active = manager.find_active_for_node(node_id)
        assert active is not None
        assert active.status == "editing"  # Photoshop was opened
        assert len(launches) == 1

    def test_requeue_after_save_consumes_edit_without_reopening(
        self, node, manager, launches
    ):
        """The frontend's auto-queue delivers a re-run once the handoff has
        an edit -- that re-run must CONSUME the new mask (same as "Wait for
        first save"'s consume path) withOUT relaunching Photoshop, since the
        document is already open.
        """
        node_id = "151"
        width, height = 24, 16
        tensor = make_tensor(RED, size=(width, height))

        node.execute(
            image=tensor,
            instruction="",
            mode=AnnotateMode.RERUN_EVERY_SAVE,
            box_composite=False,
            timeout_seconds=1800,
            unique_id=node_id,
            mask=None,
        )
        assert len(launches) == 1

        active = manager.find_active_for_node(node_id)
        saved_psd_path = manager.psd_path(active)
        base = Image.new("RGB", (width, height), RED)
        instructions = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        ImageDraw.Draw(instructions).point((5, 5), fill=(255, 255, 255, 255))
        make_layered_psd(saved_psd_path, base, instructions)
        manager.ingest_edit(active.handoff_id, base, "plugin")

        result = node.execute(
            image=tensor,
            instruction="",
            mode=AnnotateMode.RERUN_EVERY_SAVE,
            box_composite=False,
            timeout_seconds=1800,
            unique_id=node_id,
            mask=None,
        )
        assert len(launches) == 1  # no relaunch: consumed the existing edit instead
        assert result[1][0, 5, 5] > 0  # the newly consumed Instructions-layer pixel

    def test_reexecution_before_any_save_does_not_relaunch(self, node, manager, launches):
        """A re-run-mode passthrough execution against a handoff that is
        already open -- whether or not it has been saved yet -- must never
        reopen Photoshop. Only a genuinely new handoff does (mirrors
        ``cpsb.nodes.PhotoshopBridge``'s identical guarantee).
        """
        node_id = "152"
        tensor = make_tensor(RED)
        node.execute(
            image=tensor,
            instruction="",
            mode=AnnotateMode.RERUN_EVERY_SAVE,
            box_composite=False,
            timeout_seconds=1800,
            unique_id=node_id,
            mask=None,
        )
        assert len(launches) == 1

        result = node.execute(
            image=tensor,
            instruction="",
            mode=AnnotateMode.RERUN_EVERY_SAVE,
            box_composite=False,
            timeout_seconds=1800,
            unique_id=node_id,
            mask=None,
        )
        assert result[0] is tensor
        assert len(launches) == 1  # still just one launch

    def test_stale_handoff_from_changed_input_is_superseded_and_reopened(
        self, node, manager, launches
    ):
        node_id = "153"
        red_tensor = make_tensor(RED)
        node.execute(
            image=red_tensor,
            instruction="",
            mode=AnnotateMode.RERUN_EVERY_SAVE,
            box_composite=False,
            timeout_seconds=1800,
            unique_id=node_id,
            mask=None,
        )
        old = manager.find_active_for_node(node_id)

        green_tensor = make_tensor(GREEN)
        result = node.execute(
            image=green_tensor,
            instruction="",
            mode=AnnotateMode.RERUN_EVERY_SAVE,
            box_composite=False,
            timeout_seconds=1800,
            unique_id=node_id,
            mask=None,
        )
        assert manager.get(old.handoff_id).status == "superseded"
        fresh = manager.find_active_for_node(node_id)
        assert fresh.handoff_id != old.handoff_id
        assert fresh.edits == []
        assert len(launches) == 2  # reopened for the fresh handoff
        assert result[0] is green_tensor  # passthrough: no edit yet for the fresh handoff

    def test_open_failure_does_not_interrupt_and_still_passes_through(
        self, node, manager, monkeypatch
    ):
        """Non-blocking modes never interrupt on a failed open (mirrors
        ``cpsb.nodes.PhotoshopBridge.execute``'s own non-blocking modes,
        which don't check the launch attempt's ``ok`` at all): there is
        nothing to wait for, so the run still completes with a pass-through
        result instead of stopping the workflow -- unlike "Wait for first
        save", which interrupts (see
        ``TestWaitForFirstSaveBlocking.test_open_failure_marks_error_and_interrupts_without_hanging``).
        """
        node_id = "154"
        tensor = make_tensor(RED)
        monkeypatch.setattr(
            routes_module,
            "launch_photoshop",
            lambda psd_path, override="": LaunchResult(ok=False, error="no Photoshop found"),
        )

        result = node.execute(
            image=tensor,
            instruction="",
            mode=AnnotateMode.RERUN_EVERY_SAVE,
            box_composite=False,
            timeout_seconds=1800,
            unique_id=node_id,
            mask=None,
        )
        assert result[0] is tensor  # no interrupt: passthrough despite the failed open

        matching = [h for h in manager.list_all() if h.origin_node_id == node_id]
        assert len(matching) == 1
        assert matching[0].status == "error"  # mark_error still fired


class TestIsChanged:
    def test_stable_for_identical_inputs(self, configured):
        tensor = make_tensor(RED)
        kwargs = {
            "image": tensor,
            "instruction": "hi",
            "mode": AnnotateMode.PASS_THROUGH,
            "box_composite": False,
            "timeout_seconds": 1800,
            "unique_id": "100",
        }
        first = annotate_module.PhotoshopAnnotate.IS_CHANGED(**kwargs)
        second = annotate_module.PhotoshopAnnotate.IS_CHANGED(**kwargs)
        assert first == second

    def test_changes_when_instruction_changes(self, configured):
        tensor = make_tensor(RED)
        a = annotate_module.PhotoshopAnnotate.IS_CHANGED(
            image=tensor,
            instruction="hi",
            mode=AnnotateMode.PASS_THROUGH,
            box_composite=False,
            timeout_seconds=1800,
            unique_id="101",
        )
        b = annotate_module.PhotoshopAnnotate.IS_CHANGED(
            image=tensor,
            instruction="bye",
            mode=AnnotateMode.PASS_THROUGH,
            box_composite=False,
            timeout_seconds=1800,
            unique_id="101",
        )
        assert a != b

    def test_changes_when_mask_presence_changes(self, configured):
        import torch

        tensor = make_tensor(RED)
        without = annotate_module.PhotoshopAnnotate.IS_CHANGED(
            image=tensor,
            instruction="",
            mode=AnnotateMode.PASS_THROUGH,
            box_composite=False,
            timeout_seconds=1800,
            unique_id="102",
            mask=None,
        )
        with_mask = annotate_module.PhotoshopAnnotate.IS_CHANGED(
            image=tensor,
            instruction="",
            mode=AnnotateMode.PASS_THROUGH,
            box_composite=False,
            timeout_seconds=1800,
            unique_id="102",
            mask=torch.zeros((1, 16, 24)),
        )
        assert without != with_mask

    def test_changes_when_box_composite_changes(self, configured):
        tensor = make_tensor(RED)
        off = annotate_module.PhotoshopAnnotate.IS_CHANGED(
            image=tensor,
            instruction="",
            mode=AnnotateMode.PASS_THROUGH,
            box_composite=False,
            timeout_seconds=1800,
            unique_id="103",
        )
        on = annotate_module.PhotoshopAnnotate.IS_CHANGED(
            image=tensor,
            instruction="",
            mode=AnnotateMode.PASS_THROUGH,
            box_composite=True,
            timeout_seconds=1800,
            unique_id="103",
        )
        assert off != on

    def test_timeout_seconds_does_not_affect_hash(self, configured):
        """Bounds how long ``execute()`` waits, never what it produces --
        the same deliberate exclusion the bridge node's ``IS_CHANGED`` makes
        (PROTOCOL.md §6/§6d).
        """
        tensor = make_tensor(RED)
        short = annotate_module.PhotoshopAnnotate.IS_CHANGED(
            image=tensor,
            instruction="hi",
            mode=AnnotateMode.PASS_THROUGH,
            box_composite=False,
            timeout_seconds=10,
            unique_id="109",
        )
        long = annotate_module.PhotoshopAnnotate.IS_CHANGED(
            image=tensor,
            instruction="hi",
            mode=AnnotateMode.PASS_THROUGH,
            box_composite=False,
            timeout_seconds=86400,
            unique_id="109",
        )
        assert short == long

    def test_pass_through_never_folds_in_a_stale_handoffs_edit_hash(self, configured, manager):
        node_id = "104"
        source = Image.new("RGB", (24, 16), (0, 0, 0))
        edit = source.copy()
        edit.putpixel((1, 1), (255, 255, 255))
        make_handoff_with_edit(manager, node_id, source, edit)

        tensor = tensor_from_image(source)
        with_real_handoff = annotate_module.PhotoshopAnnotate.IS_CHANGED(
            image=tensor,
            instruction="",
            mode=AnnotateMode.PASS_THROUGH,
            box_composite=False,
            timeout_seconds=1800,
            unique_id=node_id,
        )
        # A different node id has no matching handoff at all -- if
        # pass-through mode never consults the manager, both calls must
        # hash identically.
        without_any_handoff = annotate_module.PhotoshopAnnotate.IS_CHANGED(
            image=tensor,
            instruction="",
            mode=AnnotateMode.PASS_THROUGH,
            box_composite=False,
            timeout_seconds=1800,
            unique_id="does-not-exist",
        )
        assert with_real_handoff == without_any_handoff

    def test_wait_for_save_mode_folds_in_the_latest_edit_hash(self, configured, manager):
        node_id = "105"
        source = Image.new("RGB", (24, 16), (0, 0, 0))
        meta = manager.create(
            origin_node_id=node_id,
            origin_kind="bridge_node",
            workflow_name="",
            source=SourceRef(filename="x.png", subfolder="", type="temp"),
            original_image=source,
        )
        tensor = tensor_from_image(source)
        before = annotate_module.PhotoshopAnnotate.IS_CHANGED(
            image=tensor,
            instruction="",
            mode=AnnotateMode.WAIT_FIRST_SAVE,
            box_composite=False,
            timeout_seconds=1800,
            unique_id=node_id,
        )
        edit = source.copy()
        edit.putpixel((1, 1), (255, 255, 255))
        manager.ingest_edit(meta.handoff_id, edit, "plugin")
        after = annotate_module.PhotoshopAnnotate.IS_CHANGED(
            image=tensor,
            instruction="",
            mode=AnnotateMode.WAIT_FIRST_SAVE,
            box_composite=False,
            timeout_seconds=1800,
            unique_id=node_id,
        )
        assert before != after

    def test_rerun_mode_folds_in_the_latest_edit_hash(self, configured, manager):
        """The same fold must fire for "Re-run on every save" -- this is
        what makes the frontend's auto-queued re-run after a save actually
        pick up the new mask instead of re-executing to an identical
        (therefore skipped) result.
        """
        node_id = "108"
        source = Image.new("RGB", (24, 16), (0, 0, 0))
        meta = manager.create(
            origin_node_id=node_id,
            origin_kind="bridge_node",
            workflow_name="",
            source=SourceRef(filename="x.png", subfolder="", type="temp"),
            original_image=source,
        )
        tensor = tensor_from_image(source)
        before = annotate_module.PhotoshopAnnotate.IS_CHANGED(
            image=tensor,
            instruction="",
            mode=AnnotateMode.RERUN_EVERY_SAVE,
            box_composite=False,
            timeout_seconds=1800,
            unique_id=node_id,
        )
        edit = source.copy()
        edit.putpixel((1, 1), (255, 255, 255))
        manager.ingest_edit(meta.handoff_id, edit, "plugin")
        after = annotate_module.PhotoshopAnnotate.IS_CHANGED(
            image=tensor,
            instruction="",
            mode=AnnotateMode.RERUN_EVERY_SAVE,
            box_composite=False,
            timeout_seconds=1800,
            unique_id=node_id,
        )
        assert before != after

    def test_unrecognized_mode_does_not_fold_in_edit_hash(self, configured, manager):
        # Symmetric with execute(): an unrecognized mode string consumes no
        # edit, so an arriving edit must NOT change IS_CHANGED for it -- it
        # returns its own base hash, never folding the edit in, exactly like
        # Pass-through (test_pass_through_never_folds...). The prior
        # `!= PASS_THROUGH` gate DID fold it, so an unrecognized/stale mode
        # made the node re-fire on every save yet execute() never surfaced the
        # edit -- one half of the "workflow never advances" P0.
        node_id = "109"
        source = Image.new("RGB", (24, 16), (0, 0, 0))
        meta = manager.create(
            origin_node_id=node_id,
            origin_kind="bridge_node",
            workflow_name="",
            source=SourceRef(filename="x.png", subfolder="", type="temp"),
            original_image=source,
        )
        tensor = tensor_from_image(source)
        mode = "Open in Photoshop (mask from edits)"  # a genuine pre-v0.5.30 value
        before = annotate_module.PhotoshopAnnotate.IS_CHANGED(
            image=tensor,
            instruction="",
            mode=mode,
            box_composite=False,
            timeout_seconds=1800,
            unique_id=node_id,
        )
        edit = source.copy()
        edit.putpixel((1, 1), (255, 255, 255))
        manager.ingest_edit(meta.handoff_id, edit, "plugin")
        after = annotate_module.PhotoshopAnnotate.IS_CHANGED(
            image=tensor,
            instruction="",
            mode=mode,
            box_composite=False,
            timeout_seconds=1800,
            unique_id=node_id,
        )
        assert before == after  # never folds the edit, unlike WAIT/RERUN above

    def test_unconfigured_raises_in_ps_mode(self):
        assert nodes_module._state is None
        tensor = make_tensor(RED)
        with pytest.raises(RuntimeError, match="configure"):
            annotate_module.PhotoshopAnnotate.IS_CHANGED(
                image=tensor,
                instruction="",
                mode=AnnotateMode.WAIT_FIRST_SAVE,
                box_composite=False,
                timeout_seconds=1800,
                unique_id="106",
            )

    def test_unconfigured_does_not_raise_in_pass_through_mode(self):
        """Pass-through mode never needs the manager at all."""
        assert nodes_module._state is None
        tensor = make_tensor(RED)
        result = annotate_module.PhotoshopAnnotate.IS_CHANGED(
            image=tensor,
            instruction="",
            mode=AnnotateMode.PASS_THROUGH,
            box_composite=False,
            timeout_seconds=1800,
            unique_id="107",
        )
        assert isinstance(result, str)
