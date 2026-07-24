"""Every /cpsb/* route and the plugin websocket, via aiohttp's test client.

No ComfyUI: the routes are mounted on a throwaway ``web.Application`` with
the fake context from ``conftest.py``, and Photoshop launching is
monkeypatched (``cpsb.routes`` imported ``launch_photoshop``/``tier1_status``
into its own namespace, so that is where the patches land).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import os
import platform
import threading
import time
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from PIL import Image, ImageDraw
from psd_tools import PSDImage

import cpsb.annotate as annotate_module
import cpsb.routes as routes_module
from cpsb.context import DEFAULT_MANAGED_FOLDER_NAME, CpsbContext
from cpsb.handoff import HandoffManager, SourceRef, WaitOutcome, compute_source_hash
from cpsb.launcher import LaunchResult, Tier1Status
from cpsb.psd_io import write_psd
from cpsb.version import __version__ as CPSB_VERSION
from cpsb.watcher import CpsbWatcher

SOURCE_FILENAME = "ComfyUI_00042_.png"
#: The managed PSD copy's derived name for `SOURCE_FILENAME` (product-owner
#: requirement 2026-07-18: named after the origin file, not the literal
#: "source.psd" every handoff used to get -- cpsb.handoff._derive_psd_filename
#: strips the extension and appends ".psd", and every char in this stem is
#: already allowed, so nothing is sanitized away).
SOURCE_PSD_FILENAME = "ComfyUI_00042_.psd"


def png_bytes(color: tuple[int, int, int], size: tuple[int, int] = (24, 16)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def psd_bytes(
    tmp_path: Path, color: tuple[int, int, int] = (10, 20, 30), size: tuple[int, int] = (16, 16)
) -> bytes:
    """Real, minimal PSD bytes via `write_psd` (a scratch file, read back and discarded)."""
    scratch = tmp_path / f"scratch_{hashlib.sha1(repr((color, size)).encode()).hexdigest()}.psd"
    write_psd(scratch, Image.new("RGB", size, color))
    return scratch.read_bytes()


def layered_psd_bytes(
    tmp_path: Path,
    base_color: tuple[int, int, int] = (9, 9, 9),
    size: tuple[int, int] = (16, 16),
    paint_box: tuple[int, int, int, int] | None = (2, 2, 6, 6),
) -> bytes:
    """A LAYERED PSD (a base pixel layer + a painted "Instructions" layer),
    as bytes -- the remote-upload analogue of `psd_bytes` above (PROTOCOL.md
    §6d, remote Tier-2 layered annotate).

    Built directly with psd-tools' own construction API (mirrors
    `tests/test_annotate.py`'s `make_layered_psd` fixture helper, and the
    SAME API `cpsb.annotate._write_instructions_psd` itself uses), not by
    calling that node function -- an independent stand-in for "what
    Photoshop saved and the plugin uploaded," not a round trip through this
    project's own write path.
    """
    key = hashlib.sha1(repr((base_color, size, paint_box)).encode()).hexdigest()
    scratch = tmp_path / f"layered_{key}.psd"
    width, height = size
    psd = PSDImage.new(mode="RGB", size=(width, height), depth=8)
    psd.create_pixel_layer(
        Image.new("RGB", size, base_color), name="Image", top=0, left=0, opacity=255
    )
    instructions = Image.new("RGBA", size, (0, 0, 0, 0))
    if paint_box is not None:
        ImageDraw.Draw(instructions).rectangle(paint_box, fill=(255, 255, 255, 255))
    psd.create_pixel_layer(
        instructions, name=annotate_module.INSTRUCTIONS_LAYER_NAME, top=0, left=0, opacity=255
    )
    psd.save(scratch)
    return scratch.read_bytes()


class LaunchRecorder:
    """Stands in for ``launch_photoshop``; records calls, returns a canned result.

    Also records whether each call executed on a thread with a running
    event loop -- the routes must dispatch the (blocking) launch through
    ``asyncio.to_thread``, never directly on aiohttp's loop.
    """

    def __init__(self, result: LaunchResult | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.on_event_loop: list[bool] = []
        self.result = result or LaunchResult(ok=True)

    def __call__(self, psd_path, override="") -> LaunchResult:
        try:
            asyncio.get_running_loop()
            self.on_event_loop.append(True)
        except RuntimeError:
            self.on_event_loop.append(False)
        self.calls.append((str(psd_path), override))
        return self.result


@pytest.fixture
def manager(context: CpsbContext) -> HandoffManager:
    return HandoffManager(context)


@pytest.fixture
def launches(monkeypatch) -> LaunchRecorder:
    recorder = LaunchRecorder()
    monkeypatch.setattr(routes_module, "launch_photoshop", recorder)
    monkeypatch.setattr(routes_module, "tier1_status", lambda: Tier1Status(available=True))
    return recorder


@pytest.fixture
async def client(context: CpsbContext, manager: HandoffManager, launches: LaunchRecorder):
    app = web.Application()
    app.add_routes(routes_module.routes)
    routes_module.install(app, context, manager)
    test_client = TestClient(TestServer(app))
    await test_client.start_server()
    yield test_client
    await test_client.close()


@pytest.fixture
def source_image(context: CpsbContext) -> str:
    (context.output_dir / SOURCE_FILENAME).write_bytes(png_bytes((10, 20, 30)))
    return SOURCE_FILENAME


def open_body(**overrides) -> dict:
    body = {
        "filename": SOURCE_FILENAME,
        "subfolder": "",
        "type": "output",
        "origin_node_id": "17",
        "origin_kind": "load_image",
        "workflow_name": "wf",
        "mode": "new",
    }
    body.update(overrides)
    return body


def open_body_psd(**overrides) -> dict:
    """Like `open_body`, but for a psd-native (`origin_kind: "load_psd"`) open
    (PROTOCOL.md §2/§6b) -- `type: "input"` since that's where the Load PSD
    node's combo lists files from.
    """
    body = {
        "filename": "sample.psd",
        "subfolder": "",
        "type": "input",
        "origin_node_id": "5",
        "origin_kind": "load_psd",
        "workflow_name": "wf",
        "mode": "new",
    }
    body.update(overrides)
    return body


def upload_form(handoff_id: str, image: bytes, source: str = "plugin") -> aiohttp.FormData:
    form = aiohttp.FormData()
    form.add_field("handoff_id", handoff_id)
    form.add_field("source", source)
    form.add_field("image", image, filename="edit.png", content_type="image/png")
    return form


def b64_chunks(data: bytes, chunk_chars: int = 700_000) -> list[str]:
    """Mirrors `cpsb.routes._split_b64`'s chunking scheme (PROTOCOL.md §3):
    base64-encode the WHOLE payload once, then slice that string into
    fixed-size pieces. A small `chunk_chars` forces a real multi-chunk
    transfer in tests without needing a multi-hundred-KB fixture.
    """
    encoded = base64.b64encode(data).decode("ascii")
    if not encoded:
        return [""]
    return [encoded[i : i + chunk_chars] for i in range(0, len(encoded), chunk_chars)]


async def wait_until(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("Condition not met in time")


@pytest.fixture
async def watcher_client(context: CpsbContext, manager: HandoffManager, launches: LaunchRecorder):
    """Like ``client``, but wires a REAL, started :class:`CpsbWatcher` into
    ``routes.install`` -- needed for PROTOCOL.md §6b ``edit_in_place`` tests
    that exercise the watch_original/unwatch_original hooks (every other
    fixture's unset watcher makes those a silent no-op by design, see
    ``routes.install``'s own docstring). The watcher instance is attached as
    ``.cpsb_watcher`` on the returned client for tests that need to poll it
    directly.
    """
    context.settings.update({"debounce_ms": 200})
    watcher = CpsbWatcher(context, manager)
    watcher.start()
    app = web.Application()
    app.add_routes(routes_module.routes)
    routes_module.install(app, context, manager, watcher)
    test_client = TestClient(TestServer(app))
    await test_client.start_server()
    test_client.cpsb_watcher = watcher
    yield test_client
    await test_client.close()
    watcher.stop()


@pytest.fixture
async def api_client(context: CpsbContext, manager: HandoffManager, launches: LaunchRecorder):
    """A client whose routes are mounted via ``add_routes_to_app`` -- i.e. under
    both the bare and ``/api``-prefixed paths, exactly as ``__init__.py`` wires
    them into the real ComfyUI server -- so the ``/api`` mirroring is exercised.
    """
    app = web.Application()
    routes_module.add_routes_to_app(app)
    routes_module.install(app, context, manager)
    test_client = TestClient(TestServer(app))
    await test_client.start_server()
    yield test_client
    await test_client.close()


class TestApiPrefix:
    """Regression guard: the frontend only ever calls the ``/api``-prefixed
    form (``api.fetchApi``), so every route must answer there, not just on the
    bare path. A prior version registered bare paths only, so every frontend
    POST came back 405 from ComfyUI's static handler.
    """

    async def test_open_answers_on_api_prefixed_path(self, api_client, source_image, launches):
        response = await api_client.post("/api/cpsb/open", json=open_body())
        assert response.status == 200

    async def test_open_still_answers_on_bare_path(self, api_client, source_image, launches):
        response = await api_client.post("/cpsb/open", json=open_body())
        assert response.status == 200

    async def test_status_answers_on_both_paths(self, api_client):
        assert (await api_client.get("/cpsb/status")).status == 200
        assert (await api_client.get("/api/cpsb/status")).status == 200

    async def test_api_prefixed_open_rejects_wrong_method(self, api_client):
        # GET on the POST-only route must be 405 (route exists, method wrong) --
        # proving the path is genuinely registered, not falling through to a
        # 404. This is the exact status the bare-only bug produced for POSTs.
        assert (await api_client.get("/api/cpsb/open")).status == 405


class TestOpen:
    async def test_happy_path_creates_and_launches(
        self, client, manager, context, source_image, launches
    ):
        response = await client.post("/cpsb/open", json=open_body())
        assert response.status == 200
        data = await response.json()
        assert data["tier"] == 1
        assert data["status"] == "pending"
        handoff_id = data["handoff_id"]

        # Tier 1 launch attempted with the handoff PSD, then marked editing.
        assert len(launches.calls) == 1
        assert launches.calls[0][0].endswith(f"{handoff_id}/{SOURCE_PSD_FILENAME}")
        # The blocking launch must have run off the event loop (to_thread).
        assert launches.on_event_loop == [False]
        assert manager.get(handoff_id).status == "editing"
        assert manager.get(handoff_id).psd_filename == SOURCE_PSD_FILENAME
        folder = context.cpsb_input_dir / handoff_id
        assert (folder / SOURCE_PSD_FILENAME).is_file()
        assert (folder / "orig_thumb.png").is_file()
        assert (folder / "meta.json").is_file()

    async def test_managed_filename_derives_from_the_source_and_is_launched_on(
        self, client, manager, context, launches
    ):
        """Product-owner requirement 2026-07-18, end-to-end: opening a handoff
        from "Eric-Headshot.jpg" names the managed copy "Eric-Headshot.psd"
        (not the literal "source.psd" every handoff used to get), and
        Photoshop is launched on THAT exact path.
        """
        (context.output_dir / "Eric-Headshot.jpg").write_bytes(png_bytes((10, 20, 30)))

        response = await client.post("/cpsb/open", json=open_body(filename="Eric-Headshot.jpg"))

        assert response.status == 200
        handoff_id = (await response.json())["handoff_id"]
        meta = manager.get(handoff_id)
        assert meta.psd_filename == "Eric-Headshot.psd"
        expected_path = context.cpsb_input_dir / handoff_id / "Eric-Headshot.psd"
        assert manager.psd_path(meta) == expected_path
        assert expected_path.is_file()
        assert launches.calls[0][0] == str(expected_path)

    async def test_file_route_serves_the_derived_filename(
        self, client, manager, context, launches
    ):
        """GET /cpsb/file/{id} serves whatever file the derived name
        actually resolved to -- exercised here with a non-default name so a
        stale hardcoded "source.psd" assumption anywhere would show up as a
        404 rather than passing by accident.
        """
        (context.output_dir / "Eric-Headshot.jpg").write_bytes(png_bytes((1, 2, 3)))
        response = await client.post("/cpsb/open", json=open_body(filename="Eric-Headshot.jpg"))
        handoff_id = (await response.json())["handoff_id"]

        file_response = await client.get(f"/cpsb/file/{handoff_id}")

        assert file_response.status == 200
        expected = (context.cpsb_input_dir / handoff_id / "Eric-Headshot.psd").read_bytes()
        assert await file_response.read() == expected

    async def test_launch_failure_marks_error(self, client, manager, source_image, launches):
        launches.result = LaunchResult(ok=False, error="Photoshop not found")
        response = await client.post("/cpsb/open", json=open_body())
        assert response.status == 200  # contract: response shape is fixed
        handoff_id = (await response.json())["handoff_id"]
        meta = manager.get(handoff_id)
        assert meta.status == "error"
        assert meta.error == "Photoshop not found"

    async def test_missing_source_404(self, client, launches):
        response = await client.post("/cpsb/open", json=open_body(filename="nope.png"))
        assert response.status == 404
        assert "error" in await response.json()

    async def test_path_traversal_rejected(self, client, context, launches):
        (context.input_dir / "secret.png").write_bytes(png_bytes((1, 1, 1)))
        response = await client.post(
            "/cpsb/open", json=open_body(filename="../secret.png", type="output")
        )
        assert response.status == 404

    async def test_malformed_body_400(self, client, launches):
        response = await client.post("/cpsb/open", data=b"not json")
        assert response.status == 400
        response = await client.post("/cpsb/open", json={"filename": "x.png"})
        assert response.status == 400
        response = await client.post("/cpsb/open", json=open_body(mode="banana"))
        assert response.status == 400
        response = await client.post("/cpsb/open", json=open_body(origin_kind="banana"))
        assert response.status == 400
        response = await client.post("/cpsb/open", json=open_body(trigger_policy="banana"))
        assert response.status == 400

    async def test_trigger_policy_defaults_to_rerun(self, client, manager, source_image, launches):
        """Product-owner requirement 2026-07-18: omitting `trigger_policy`
        entirely (every existing caller of `/cpsb/open`) must keep today's
        exact behavior.
        """
        response = await client.post("/cpsb/open", json=open_body())
        handoff_id = (await response.json())["handoff_id"]
        assert manager.get(handoff_id).trigger_policy == "Re-run workflow"

    async def test_trigger_policy_persisted_when_provided(
        self, client, manager, context, source_image, launches
    ):
        response = await client.post(
            "/cpsb/open", json=open_body(trigger_policy="Ignore (do nothing)")
        )
        handoff_id = (await response.json())["handoff_id"]
        assert manager.get(handoff_id).trigger_policy == "Ignore (do nothing)"
        stored = json.loads(
            (context.cpsb_input_dir / handoff_id / "meta.json").read_text()
        )
        assert stored["trigger_policy"] == "Ignore (do nothing)"

    async def test_existing_handoff_conflict_and_modes(
        self, client, manager, source_image, launches
    ):
        first = await (await client.post("/cpsb/open", json=open_body())).json()

        # mode:"new" with an active handoff -> 409 + existing_handoff_id.
        conflict = await client.post("/cpsb/open", json=open_body())
        assert conflict.status == 409
        conflict_body = await conflict.json()
        assert conflict_body["existing_handoff_id"] == first["handoff_id"]

        # mode:"original" -> same handoff re-opened, no new folder.
        original = await (await client.post("/cpsb/open", json=open_body(mode="original"))).json()
        assert original["handoff_id"] == first["handoff_id"]

        # mode:"fresh" -> old superseded, brand-new handoff.
        fresh = await (await client.post("/cpsb/open", json=open_body(mode="fresh"))).json()
        assert fresh["handoff_id"] != first["handoff_id"]
        assert manager.get(first["handoff_id"]).status == "superseded"
        assert manager.get(fresh["handoff_id"]).status == "editing"

    async def test_mode_original_without_handoff_404(self, client, source_image, launches):
        response = await client.post("/cpsb/open", json=open_body(mode="original"))
        assert response.status == 404

    async def test_same_node_id_across_workflows_no_conflict(
        self, client, manager, source_image, launches
    ):
        """Workflow B's node "17" must not 409 against workflow A's handoff."""
        first = await client.post("/cpsb/open", json=open_body(workflow_name="workflow-a"))
        assert first.status == 200

        other_workflow = await client.post("/cpsb/open", json=open_body(workflow_name="workflow-b"))
        assert other_workflow.status == 200
        assert (await other_workflow.json())["handoff_id"] != (await first.json())["handoff_id"]

        # Same workflow again -> the 409 conflict is still enforced.
        same_workflow = await client.post("/cpsb/open", json=open_body(workflow_name="workflow-a"))
        assert same_workflow.status == 409
        body = await same_workflow.json()
        assert body["existing_handoff_id"] == (await first.json())["handoff_id"]

    async def test_unavailable_both_tiers_503(self, client, source_image, launches, monkeypatch):
        monkeypatch.setattr(
            routes_module,
            "tier1_status",
            lambda: Tier1Status(available=False, reason="headless-server"),
        )
        response = await client.post("/cpsb/open", json=open_body())
        assert response.status == 503
        body = await response.json()
        assert body["tier1_available"] is False
        assert body["tier2_connected"] is False


class TestAutoSupersedeOnChangedSource:
    """PROTOCOL.md §6's bridge-node rule, mirrored at ``POST /cpsb/open``:
    ``mode:"new"`` against an active handoff only 409s when the incoming
    image is genuinely the SAME one. If upstream regenerated the image
    under the same filename (a fixed-name SaveImage/PreviewImage, or a
    counter that happened to repeat), the stale handoff is auto-superseded
    and the request proceeds as a fresh 200.
    """

    async def test_same_image_reopen_still_conflicts(self, client, manager, source_image, launches):
        first = await (await client.post("/cpsb/open", json=open_body())).json()

        conflict = await client.post("/cpsb/open", json=open_body())

        assert conflict.status == 409
        body = await conflict.json()
        assert body["existing_handoff_id"] == first["handoff_id"]
        assert manager.get(first["handoff_id"]).status == "editing"  # untouched

    async def test_changed_image_supersedes_and_proceeds(
        self, client, context, manager, source_image, launches
    ):
        first = await (await client.post("/cpsb/open", json=open_body())).json()
        assert manager.get(first["handoff_id"]).status == "editing"

        # Upstream re-generated the image under the SAME filename.
        (context.output_dir / SOURCE_FILENAME).write_bytes(png_bytes((1, 2, 3)))

        second = await client.post("/cpsb/open", json=open_body())

        assert second.status == 200
        second_body = await second.json()
        assert second_body["handoff_id"] != first["handoff_id"]
        assert manager.get(first["handoff_id"]).status == "superseded"
        assert manager.get(second_body["handoff_id"]).status == "editing"
        with Image.open(context.output_dir / SOURCE_FILENAME) as new_source:
            expected_hash = compute_source_hash(new_source)
        assert manager.get(second_body["handoff_id"]).source_hash == expected_hash

    async def test_changed_image_missing_source_404s_before_superseding(
        self, client, context, manager, source_image, launches
    ):
        """Resolving/hashing the new image happens BEFORE any supersede, so
        a bad follow-up request can't destroy the still-valid old handoff.
        """
        first = await (await client.post("/cpsb/open", json=open_body())).json()

        # The file that would need to be re-read to decide same-vs-changed
        # has vanished.
        (context.output_dir / SOURCE_FILENAME).unlink()

        response = await client.post("/cpsb/open", json=open_body())

        assert response.status == 404
        assert manager.get(first["handoff_id"]).status == "editing"  # NOT superseded

    async def test_legacy_handoff_without_source_hash_still_conflicts(
        self, client, context, manager, source_image, launches
    ):
        """A pre-source_hash handoff (``None``) is treated as matching, even
        when the image actually changed -- the same documented legacy-
        tolerance choice as the bridge node (PROTOCOL.md §1).
        """
        first = await (await client.post("/cpsb/open", json=open_body())).json()
        with manager._lock:
            manager._handoffs[first["handoff_id"]].source_hash = None

        (context.output_dir / SOURCE_FILENAME).write_bytes(png_bytes((9, 9, 9)))
        conflict = await client.post("/cpsb/open", json=open_body())

        assert conflict.status == 409
        assert (await conflict.json())["existing_handoff_id"] == first["handoff_id"]
        assert manager.get(first["handoff_id"]).status == "editing"  # not superseded

    async def test_mode_fresh_unaffected_by_hash_comparison(
        self, client, manager, source_image, launches
    ):
        """mode:"fresh" is an explicit, unconditional supersede -- it must
        not be gated on the image having changed.
        """
        first = await (await client.post("/cpsb/open", json=open_body())).json()

        fresh = await client.post("/cpsb/open", json=open_body(mode="fresh"))

        assert fresh.status == 200
        fresh_body = await fresh.json()
        assert fresh_body["handoff_id"] != first["handoff_id"]
        assert manager.get(first["handoff_id"]).status == "superseded"


class TestOpenPsdNative:
    """``POST /cpsb/open`` with ``origin_kind: "load_psd"`` (PROTOCOL.md §2/§6b):
    the handoff's managed PSD copy is a verbatim byte-for-byte copy of the
    user's own file (never a re-encoded flatten), and ``source_hash`` is the
    sha256 of those raw bytes rather than a PNG-encoding hash. Every source
    filename used here already ends in ``.psd`` with a clean stem, so the
    derived ``psd_filename`` is identical to the origin's own name.
    """

    async def test_copies_bytes_verbatim(self, client, context, manager, launches, tmp_path):
        original = psd_bytes(tmp_path, color=(11, 22, 33))
        (context.input_dir / "sample.psd").write_bytes(original)

        response = await client.post("/cpsb/open", json=open_body_psd())

        assert response.status == 200
        handoff_id = (await response.json())["handoff_id"]
        meta = manager.get(handoff_id)
        assert meta.psd_filename == "sample.psd"  # derived from "sample.psd"'s own stem
        copied = manager.psd_path(meta).read_bytes()
        assert copied == original
        assert meta.source_hash == hashlib.sha256(original).hexdigest()

    async def test_preserves_real_layers_not_flattened(
        self, client, context, manager, launches, tmp_path
    ):
        """The headline product win (research-psd-loading.md §5): a
        genuinely layered PSD survives the round trip un-flattened, unlike
        every other origin (which always calls `write_psd`, layer-less by
        construction).
        """
        psd = PSDImage.new("RGB", (20, 20), color=1.0, depth=8)
        psd.create_pixel_layer(Image.new("RGB", (10, 10), (255, 0, 0)), name="Red", top=2, left=2)
        scratch = tmp_path / "layered.psd"
        psd.save(scratch)
        original = scratch.read_bytes()
        (context.input_dir / "layered.psd").write_bytes(original)

        response = await client.post("/cpsb/open", json=open_body_psd(filename="layered.psd"))

        assert response.status == 200
        handoff_id = (await response.json())["handoff_id"]
        meta = manager.get(handoff_id)
        assert meta.psd_filename == "layered.psd"  # derived from "layered.psd"'s own stem
        copied_path = manager.psd_path(meta)
        assert copied_path.read_bytes() == original
        assert len(list(PSDImage.open(copied_path))) == 1  # the "Red" layer, intact

    async def test_rejects_non_psd_extension(self, client, context, launches):
        (context.input_dir / "photo.png").write_bytes(png_bytes((1, 2, 3)))

        response = await client.post("/cpsb/open", json=open_body_psd(filename="photo.png"))

        assert response.status == 400

    async def test_missing_file_404(self, client, context, launches):
        response = await client.post("/cpsb/open", json=open_body_psd(filename="ghost.psd"))
        assert response.status == 404

    async def test_thumbnail_written_from_the_flatten(
        self, client, context, manager, launches, tmp_path
    ):
        original = psd_bytes(tmp_path, color=(40, 80, 120))
        (context.input_dir / "sample.psd").write_bytes(original)

        response = await client.post("/cpsb/open", json=open_body_psd())

        handoff_id = (await response.json())["handoff_id"]
        thumb_path = context.cpsb_input_dir / handoff_id / "orig_thumb.png"
        assert thumb_path.is_file()
        with Image.open(thumb_path) as thumb:
            assert thumb.getpixel((0, 0))[:3] == (40, 80, 120)

    async def test_flatten_failure_still_succeeds_with_placeholder_thumbnail(
        self, client, context, manager, launches
    ):
        """PROTOCOL.md §2: "never fail the open for a thumbnail" -- a PSD
        with a valid signature but corrupt body still opens successfully,
        with a placeholder thumbnail standing in for the failed flatten.
        """
        corrupt = b"8BPS" + b"\x00" * 4
        (context.input_dir / "corrupt.psd").write_bytes(corrupt)

        response = await client.post("/cpsb/open", json=open_body_psd(filename="corrupt.psd"))

        assert response.status == 200
        handoff_id = (await response.json())["handoff_id"]
        thumb_path = context.cpsb_input_dir / handoff_id / "orig_thumb.png"
        assert thumb_path.is_file()
        with Image.open(thumb_path) as thumb:
            thumb.load()  # a real, decodable PNG -- not a stub/empty file
        # The raw bytes are still copied verbatim regardless of the flatten failure.
        copied = manager.psd_path(manager.get(handoff_id)).read_bytes()
        assert copied == corrupt

    async def test_source_hash_is_raw_bytes_sha_not_png_encoding(
        self, client, context, manager, launches, tmp_path
    ):
        original = psd_bytes(tmp_path, color=(7, 7, 7))
        (context.input_dir / "sample.psd").write_bytes(original)

        response = await client.post("/cpsb/open", json=open_body_psd())

        handoff_id = (await response.json())["handoff_id"]
        assert manager.get(handoff_id).source_hash == hashlib.sha256(original).hexdigest()
        # Not the flattened-thumbnail PNG-encoding hash the non-psd-native
        # path uses (`compute_source_hash`) -- confirms the raw-bytes scheme
        # is genuinely in effect, not coincidentally equal to it.
        with Image.open(context.cpsb_input_dir / handoff_id / "orig_thumb.png") as thumb:
            assert manager.get(handoff_id).source_hash != compute_source_hash(thumb)

    async def test_same_bytes_reopen_still_conflicts(self, client, context, launches, tmp_path):
        (context.input_dir / "sample.psd").write_bytes(psd_bytes(tmp_path))
        first = await (await client.post("/cpsb/open", json=open_body_psd())).json()

        conflict = await client.post("/cpsb/open", json=open_body_psd())

        assert conflict.status == 409
        assert (await conflict.json())["existing_handoff_id"] == first["handoff_id"]

    async def test_changed_bytes_auto_supersedes_and_proceeds(
        self, client, context, manager, launches, tmp_path
    ):
        """Auto-supersede on a changed source (PROTOCOL.md §6), mirrored for
        psd-native sources: same shape as
        ``TestAutoSupersedeOnChangedSource.test_changed_image_supersedes_and_proceeds``,
        but keyed on raw PSD bytes instead of a decoded-pixel PNG hash.
        """
        (context.input_dir / "sample.psd").write_bytes(psd_bytes(tmp_path, color=(1, 1, 1)))
        first = await (await client.post("/cpsb/open", json=open_body_psd())).json()
        assert manager.get(first["handoff_id"]).status == "editing"

        new_bytes = psd_bytes(tmp_path, color=(2, 2, 2))
        (context.input_dir / "sample.psd").write_bytes(new_bytes)

        second = await client.post("/cpsb/open", json=open_body_psd())

        assert second.status == 200
        second_body = await second.json()
        assert second_body["handoff_id"] != first["handoff_id"]
        assert manager.get(first["handoff_id"]).status == "superseded"
        assert (
            manager.get(second_body["handoff_id"]).source_hash
            == hashlib.sha256(new_bytes).hexdigest()
        )

    async def test_mode_original_reopens_the_same_copied_file(
        self, client, context, manager, launches, tmp_path
    ):
        (context.input_dir / "sample.psd").write_bytes(psd_bytes(tmp_path))
        first = await (await client.post("/cpsb/open", json=open_body_psd())).json()

        reopened = await client.post("/cpsb/open", json=open_body_psd(mode="original"))

        assert reopened.status == 200
        assert (await reopened.json())["handoff_id"] == first["handoff_id"]

    async def test_origin_kind_accepted_by_body_validation(self, client, context, launches):
        """`load_psd` must be a recognized `origin_kind` -- not rejected by
        the same 400 that an unrecognized value gets.
        """
        response = await client.post("/cpsb/open", json=open_body_psd(filename="never-created.psd"))
        assert response.status == 404  # got past body validation to file resolution


class TestPsdPreview:
    """``GET /cpsb/psd_preview`` -- not part of PROTOCOL.md, a pure frontend
    nicety for the Load PSD node (``web/cpsb/loadpsd.js``): flattens the
    selected ``.psd``/``.psb`` into a cached, ComfyUI-``/view``-addressable
    temp PNG, the same way ``psd_io.read_edited_psd`` flattens for every
    other read of a PSD in this package -- no Photoshop plugin involved.
    """

    async def test_happy_path_returns_temp_png_of_the_right_size(
        self, client, context, tmp_path
    ):
        (context.input_dir / "sample.psd").write_bytes(
            psd_bytes(tmp_path, color=(50, 60, 70), size=(24, 12))
        )

        response = await client.get(
            "/cpsb/psd_preview", params={"filename": "sample.psd", "subfolder": "", "type": "input"}
        )

        assert response.status == 200
        data = await response.json()
        assert data["type"] == "temp"
        assert data["subfolder"] == "cpsb"
        assert data["filename"].startswith("psdpreview_")
        assert data["filename"].endswith(".png")
        png_path = context.temp_dir / "cpsb" / data["filename"]
        assert png_path.is_file()
        with Image.open(png_path) as preview:
            preview.load()
            assert preview.size == (24, 12)
            assert preview.getpixel((0, 0))[:3] == (50, 60, 70)

    async def test_tiff_previews_via_raster_decode(self, client, context):
        """A .tif/.tiff previews too, through raster_io.decode_to_rgb8 -- the
        SAME decoder the Load PSD node's execute() uses. Owner report
        2026-07-22: selecting a TIFF used to 400 here, so the on-node preview
        silently never refreshed (a downstream Preview node still worked).
        """
        Image.new("RGB", (20, 10), (12, 34, 56)).save(
            context.input_dir / "photo.tif", format="TIFF"
        )

        response = await client.get(
            "/cpsb/psd_preview", params={"filename": "photo.tif", "type": "input"}
        )

        assert response.status == 200
        data = await response.json()
        assert data["type"] == "temp"
        assert data["filename"].startswith("psdpreview_")
        png_path = context.temp_dir / "cpsb" / data["filename"]
        with Image.open(png_path) as preview:
            preview.load()
            assert preview.size == (20, 10)
            assert preview.getpixel((0, 0))[:3] == (12, 34, 56)

    async def test_default_subfolder_and_type_are_empty_and_input(self, client, context, tmp_path):
        """``subfolder``/``type`` are optional query params -- default to
        ``""``/``"input"`` (unlike ComfyUI's own ``/view``, which defaults
        ``type`` to ``"output"``; PSDs the Load PSD combo can select always
        live in the input directory).
        """
        (context.input_dir / "sample.psd").write_bytes(psd_bytes(tmp_path))

        response = await client.get("/cpsb/psd_preview", params={"filename": "sample.psd"})

        assert response.status == 200
        data = await response.json()
        assert data["filename"].startswith("psdpreview_")

    async def test_missing_filename_400(self, client, context):
        response = await client.get("/cpsb/psd_preview", params={})
        assert response.status == 400

    async def test_invalid_type_400(self, client, context, tmp_path):
        (context.input_dir / "sample.psd").write_bytes(psd_bytes(tmp_path))
        response = await client.get(
            "/cpsb/psd_preview", params={"filename": "sample.psd", "type": "banana"}
        )
        assert response.status == 400

    async def test_rejects_non_psd_extension(self, client, context):
        (context.input_dir / "photo.png").write_bytes(png_bytes((1, 2, 3)))

        response = await client.get("/cpsb/psd_preview", params={"filename": "photo.png"})

        assert response.status == 400

    async def test_missing_file_404(self, client, context):
        response = await client.get("/cpsb/psd_preview", params={"filename": "ghost.psd"})
        assert response.status == 404

    async def test_path_traversal_rejected(self, client, context, tmp_path):
        (context.input_dir / "secret.psd").write_bytes(psd_bytes(tmp_path))

        response = await client.get(
            "/cpsb/psd_preview", params={"filename": "../secret.psd", "type": "input"}
        )

        assert response.status == 404

    async def test_cache_hit_does_not_reflatten_on_second_call(
        self, client, context, tmp_path, monkeypatch
    ):
        (context.input_dir / "sample.psd").write_bytes(psd_bytes(tmp_path, color=(9, 9, 9)))

        first = await client.get("/cpsb/psd_preview", params={"filename": "sample.psd"})
        assert first.status == 200
        first_data = await first.json()
        png_path = context.temp_dir / "cpsb" / first_data["filename"]
        mtime_after_first = png_path.stat().st_mtime_ns

        # A cache hit must not call read_edited_psd (re-flatten) at all --
        # not just "produce the same bytes."
        def _fail_if_called(*_args, **_kwargs):
            raise AssertionError("cache hit re-flattened the PSD")

        monkeypatch.setattr(routes_module, "read_edited_psd", _fail_if_called)

        second = await client.get("/cpsb/psd_preview", params={"filename": "sample.psd"})

        assert second.status == 200
        second_data = await second.json()
        assert second_data == first_data
        assert png_path.stat().st_mtime_ns == mtime_after_first

    async def test_flatten_failure_returns_200_without_preview(self, client, context):
        corrupt = b"8BPS" + b"\x00" * 4
        (context.input_dir / "corrupt.psd").write_bytes(corrupt)

        response = await client.get("/cpsb/psd_preview", params={"filename": "corrupt.psd"})

        assert response.status == 200
        data = await response.json()
        assert data["filename"] is None
        assert data["subfolder"] is None
        assert data["type"] == "temp"
        # Nothing left behind in the preview cache for a failed flatten.
        assert not (context.temp_dir / "cpsb").exists() or not list(
            (context.temp_dir / "cpsb").glob("*.png")
        )


class TestFsList:
    """``GET /cpsb/fs/list`` -- the server-backed directory-browser dialog for
    ``PhotoshopComposePSD.existing_psd_path`` (`cpsb/routes.py`'s own "GET
    /cpsb/fs/list" section header has the full design rationale, including why
    this route is deliberately left UNGATED by the `/cpsb/open` client-locality
    428 confirm). Not part of PROTOCOL.md -- a pure frontend nicety, like
    `/cpsb/psd_preview` above. Route/response shape standardized 2026-07-19
    across cpsb/cprb/epsnodes (../STANDARD-fs-browse.md) -- migrated from the
    old `/cpsb/browse` (`path` param, full-path objects, no `ROOTS` sentinel).
    """

    async def test_roots_listing_has_no_parent_and_includes_default_dir_and_home(
        self, client, context
    ):
        response = await client.get("/cpsb/fs/list", params={"dir": "ROOTS"})

        assert response.status == 200
        data = await response.json()
        assert data["dir"] == "ROOTS"
        assert data["parent"] is None
        assert data["sep"] == os.sep
        assert data["files"] == []
        assert data["truncated"] is False

        by_name = {entry["name"]: entry["path"] for entry in data["dirs"]}
        assert by_name["ComfyUI Input"] == str(context.input_dir.resolve())
        assert by_name["Home"] == str(Path.home().resolve())

    async def test_empty_dir_param_lists_the_pack_default_directory(self, client, context):
        """Empty/omitted ``dir`` means "the pack's own default directory"
        (STANDARD-fs-browse.md) -- NOT the roots listing, which now requires
        the explicit ``ROOTS`` sentinel.
        """
        response = await client.get("/cpsb/fs/list", params={"dir": ""})

        assert response.status == 200
        data = await response.json()
        assert data["dir"] == str(context.input_dir.resolve())

    async def test_missing_dir_param_lists_the_pack_default_directory(self, client, context):
        response = await client.get("/cpsb/fs/list")

        assert response.status == 200
        data = await response.json()
        assert data["dir"] == str(context.input_dir.resolve())

    async def test_lists_directory_filtered_and_sorted(self, client, tmp_path):
        target = tmp_path / "browse_target"
        target.mkdir()
        (target / "Zeta").mkdir()
        (target / "alpha").mkdir()
        (target / ".hidden_dir").mkdir()
        (target / "cover.psd").write_bytes(b"not real psd bytes, extension-only test")
        (target / "image.PSB").write_bytes(b"psb")
        (target / "notes.txt").write_bytes(b"not a psd")
        (target / ".hidden.psd").write_bytes(b"hidden psd")

        response = await client.get("/cpsb/fs/list", params={"dir": str(target)})

        assert response.status == 200
        data = await response.json()
        assert data["dir"] == str(target.resolve())
        assert data["parent"] == str(target.parent.resolve())
        assert data["sep"] == os.sep
        assert data["truncated"] is False

        # Names-only entries (STANDARD-fs-browse.md): the client joins with
        # `dir` + `sep`, so a regular listing's dirs/files carry no `path`.
        assert data["dirs"] == [{"name": "alpha"}, {"name": "Zeta"}]  # case-insensitive sort

        file_names = [entry["name"] for entry in data["files"]]
        assert file_names == ["cover.psd", "image.PSB"]  # case-insensitive sort + extension
        for file_entry in data["files"]:
            assert "path" not in file_entry
            assert file_entry["size"] > 0
            assert isinstance(file_entry["mtime"], float)

    async def test_ext_param_narrows_the_default_allowlist(self, client, tmp_path):
        target = tmp_path / "ext_target"
        target.mkdir()
        (target / "a.psd").write_bytes(b"psd")
        (target / "b.psb").write_bytes(b"psb")

        response = await client.get(
            "/cpsb/fs/list", params={"dir": str(target), "ext": ".psd"}
        )

        assert response.status == 200
        data = await response.json()
        assert [entry["name"] for entry in data["files"]] == ["a.psd"]

    async def test_400_on_a_file_path(self, client, tmp_path):
        a_file = tmp_path / "not_a_directory.psd"
        a_file.write_bytes(b"psd")

        response = await client.get("/cpsb/fs/list", params={"dir": str(a_file)})

        assert response.status == 400
        data = await response.json()
        assert "error" in data

    async def test_400_on_a_missing_dir(self, client, tmp_path):
        response = await client.get(
            "/cpsb/fs/list", params={"dir": str(tmp_path / "does_not_exist")}
        )

        assert response.status == 400

    async def test_400_on_a_relative_dir(self, client):
        response = await client.get("/cpsb/fs/list", params={"dir": "not/absolute"})

        assert response.status == 400

    async def test_400_on_garbage_path(self, client):
        response = await client.get("/cpsb/fs/list", params={"dir": "bad\x00path"})

        assert response.status == 400

    async def test_truncates_over_500_entries(self, client, tmp_path):
        big = tmp_path / "big"
        big.mkdir()
        for index in range(501):
            (big / f"{index:04d}.psd").touch()

        response = await client.get("/cpsb/fs/list", params={"dir": str(big)})

        assert response.status == 200
        data = await response.json()
        assert data["truncated"] is True
        assert len(data["files"]) == 500

    async def test_parent_is_null_at_a_filesystem_root(self, client):
        response = await client.get("/cpsb/fs/list", params={"dir": "/"})

        assert response.status == 200
        data = await response.json()
        assert data["dir"] == "/"
        assert data["parent"] is None

    async def test_locality_flag_defaults_to_ungated_for_a_simulated_remote_caller(
        self, client, monkeypatch
    ):
        """STANDARD-fs-browse.md's locality policy: cpsb's `FS_LIST_LOCAL_ONLY`
        defaults to `False` (unlike cprb/epsnodes) -- a simulated non-loopback
        caller (`is_request_local` patched False, the same seam
        `TestClientLocalityGate` uses for `/cpsb/open`) must still get a 200,
        never a 403.
        """
        monkeypatch.setattr(routes_module, "is_request_local", lambda request: False)

        response = await client.get("/cpsb/fs/list", params={"dir": "ROOTS"})

        assert response.status == 200


class TestOpenPsdEditInPlace:
    """PROTOCOL.md §6b "Edit-original option": ``edit_in_place: true`` on a
    ``load_psd`` open skips the managed PSD copy entirely and makes the
    handoff's edit target the user's own original file.
    """

    async def test_default_false_keeps_copy_behavior(
        self, client, context, manager, launches, tmp_path
    ):
        """Regression pin: omitting `edit_in_place` must be byte-identical
        to every pre-existing TestOpenPsdNative assertion.
        """
        original = psd_bytes(tmp_path, color=(11, 22, 33))
        (context.input_dir / "sample.psd").write_bytes(original)

        response = await client.post("/cpsb/open", json=open_body_psd())

        assert response.status == 200
        handoff_id = (await response.json())["handoff_id"]
        meta = manager.get(handoff_id)
        assert meta.edit_in_place is False
        assert meta.original_path is None
        assert manager.psd_path(meta).read_bytes() == original
        assert launches.calls[0][0].endswith(f"{handoff_id}/{meta.psd_filename}")

    async def test_edit_in_place_skips_the_copy(self, client, context, manager, launches, tmp_path):
        original_file = context.input_dir / "sample.psd"
        original_file.write_bytes(psd_bytes(tmp_path, color=(44, 55, 66)))

        response = await client.post("/cpsb/open", json=open_body_psd(edit_in_place=True))

        assert response.status == 200
        handoff_id = (await response.json())["handoff_id"]
        meta = manager.get(handoff_id)
        assert not manager.psd_path(meta).exists()
        assert meta.edit_in_place is True
        assert meta.original_path == str(original_file.resolve())

    async def test_edit_in_place_launches_photoshop_on_the_original_path(
        self, client, context, launches, tmp_path
    ):
        original_file = context.input_dir / "sample.psd"
        original_file.write_bytes(psd_bytes(tmp_path))

        response = await client.post("/cpsb/open", json=open_body_psd(edit_in_place=True))

        assert response.status == 200
        assert launches.calls[0][0] == str(original_file.resolve())

    async def test_edit_in_place_still_writes_orig_thumb(
        self, client, context, manager, launches, tmp_path
    ):
        (context.input_dir / "sample.psd").write_bytes(psd_bytes(tmp_path, color=(1, 2, 3)))

        response = await client.post("/cpsb/open", json=open_body_psd(edit_in_place=True))

        handoff_id = (await response.json())["handoff_id"]
        thumb_path = context.cpsb_input_dir / handoff_id / "orig_thumb.png"
        assert thumb_path.is_file()
        with Image.open(thumb_path) as thumb:
            assert thumb.getpixel((0, 0))[:3] == (1, 2, 3)

    async def test_edit_in_place_never_writes_the_original_file(
        self, client, context, launches, tmp_path
    ):
        """No write of ours ever touches the original -- there is nothing
        for the watcher to suppress as "our own write" on this path.
        """
        original_file = context.input_dir / "sample.psd"
        original_bytes = psd_bytes(tmp_path, color=(9, 9, 9))
        original_file.write_bytes(original_bytes)

        response = await client.post("/cpsb/open", json=open_body_psd(edit_in_place=True))

        assert response.status == 200
        assert original_file.read_bytes() == original_bytes  # byte-for-byte untouched

    async def test_edit_in_place_rejects_non_psd_extension(self, client, context, launches):
        (context.input_dir / "photo.png").write_bytes(png_bytes((1, 2, 3)))

        response = await client.post(
            "/cpsb/open", json=open_body_psd(filename="photo.png", edit_in_place=True)
        )

        assert response.status == 400

    async def test_edit_in_place_missing_file_404(self, client, context, launches):
        response = await client.post(
            "/cpsb/open", json=open_body_psd(filename="ghost.psd", edit_in_place=True)
        )
        assert response.status == 404

    async def test_edit_in_place_ignored_for_non_load_psd_origin(
        self, client, source_image, manager, launches
    ):
        """`edit_in_place` only means something for `origin_kind: load_psd`
        -- a flat image has no PSD-native "original" to point at.
        """
        response = await client.post("/cpsb/open", json=open_body(edit_in_place=True))

        assert response.status == 200
        handoff_id = (await response.json())["handoff_id"]
        meta = manager.get(handoff_id)
        assert meta.edit_in_place is False
        assert meta.original_path is None

    async def test_mode_original_reopens_the_original_file_not_a_copy(
        self, client, context, manager, launches, tmp_path
    ):
        original_file = context.input_dir / "sample.psd"
        original_file.write_bytes(psd_bytes(tmp_path))
        first = await (
            await client.post("/cpsb/open", json=open_body_psd(edit_in_place=True))
        ).json()

        reopened = await client.post("/cpsb/open", json=open_body_psd(mode="original"))

        assert reopened.status == 200
        assert (await reopened.json())["handoff_id"] == first["handoff_id"]
        assert launches.calls[-1][0] == str(original_file.resolve())

    async def test_file_route_serves_the_original_for_edit_in_place(
        self, client, context, tmp_path
    ):
        """GET /cpsb/file/{id} (Tier 2 remote-mode download) must serve the
        ORIGINAL bytes for an edit_in_place handoff -- there is no managed
        PSD copy to fall back to.
        """
        original = psd_bytes(tmp_path, color=(70, 80, 90))
        (context.input_dir / "sample.psd").write_bytes(original)
        handoff_id = (
            await (await client.post("/cpsb/open", json=open_body_psd(edit_in_place=True))).json()
        )["handoff_id"]

        response = await client.get(f"/cpsb/file/{handoff_id}")

        assert response.status == 200
        assert await response.read() == original

    async def test_fresh_mode_unwatches_the_superseded_handoff(
        self, watcher_client, context, manager, launches, tmp_path
    ):
        """Superseding an edit_in_place handoff must stop watching its
        original file -- a later save of that (now-detached) file must not
        land on the wrong (superseded) handoff.
        """
        original_file = context.input_dir / "sample.psd"
        original_file.write_bytes(psd_bytes(tmp_path, color=(5, 5, 5)))
        first = await (
            await watcher_client.post("/cpsb/open", json=open_body_psd(edit_in_place=True))
        ).json()
        first_id = first["handoff_id"]
        assert watcher_client.cpsb_watcher._original_by_handoff.get(first_id) is not None

        fresh = await watcher_client.post("/cpsb/open", json=open_body_psd(mode="fresh"))
        assert fresh.status == 200

        assert first_id not in watcher_client.cpsb_watcher._original_by_handoff


class TestOpenPsdEditInPlaceWatcherIntegration:
    """End-to-end: opening an edit_in_place handoff registers a live
    filesystem watch, saving the ORIGINAL file ingests an edit into the
    MANAGED folder, and cancel/discard stop that watch (PROTOCOL.md §6b).
    """

    SETTLE_TIMEOUT = 15.0

    async def _wait_for_edit_count(self, manager, handoff_id: str, count: int) -> None:
        deadline = time.monotonic() + self.SETTLE_TIMEOUT
        while time.monotonic() < deadline:
            meta = manager.get(handoff_id)
            if meta is not None and len(meta.edits) >= count:
                return
            await asyncio.sleep(0.05)
        meta = manager.get(handoff_id)
        raise AssertionError(
            f"Timed out waiting for {count} edit(s); have {len(meta.edits) if meta else 0}"
        )

    async def test_saving_the_original_ingests_an_edit_into_managed_storage(
        self, watcher_client, context, manager, launches, tmp_path
    ):
        original_file = context.input_dir / "sample.psd"
        original_file.write_bytes(psd_bytes(tmp_path, color=(1, 1, 1)))

        response = await watcher_client.post("/cpsb/open", json=open_body_psd(edit_in_place=True))
        handoff_id = (await response.json())["handoff_id"]
        await asyncio.sleep(0.3)  # let the initial open settle before the "save"

        # Simulate the user's plain Cmd+S: Photoshop rewrites the ORIGINAL
        # file in place, at its ORIGINAL path -- never a managed-folder path.
        write_psd(original_file, Image.new("RGB", (16, 16), (200, 0, 0)))

        await self._wait_for_edit_count(manager, handoff_id, 1)

        refreshed = manager.get(handoff_id)
        assert refreshed.status == "edited"
        assert len(refreshed.edits) == 1
        # The edit lands in MANAGED storage, addressable exactly like any
        # other handoff's edit -- downstream/gallery consumption unchanged.
        edit_path = context.cpsb_input_dir / handoff_id / refreshed.edits[0].filename
        assert edit_path.is_file()
        with Image.open(edit_path) as edit_image:
            assert edit_image.getpixel((0, 0)) == (200, 0, 0)
        # And the original itself is exactly what Photoshop "saved" -- this
        # package never rewrites it.
        with Image.open(original_file) as saved:
            assert saved.getpixel((0, 0))[:3] == (200, 0, 0)

    async def test_cancel_stops_the_watch_further_saves_are_not_ingested(
        self, watcher_client, context, manager, launches, tmp_path
    ):
        original_file = context.input_dir / "sample.psd"
        original_file.write_bytes(psd_bytes(tmp_path, color=(1, 1, 1)))
        handoff_id = (
            await (
                await watcher_client.post("/cpsb/open", json=open_body_psd(edit_in_place=True))
            ).json()
        )["handoff_id"]
        await asyncio.sleep(0.3)

        cancel_response = await watcher_client.post(f"/cpsb/cancel/{handoff_id}")
        assert cancel_response.status == 200

        write_psd(original_file, Image.new("RGB", (16, 16), (0, 250, 0)))
        await asyncio.sleep(0.3 + 0.2 * 4)  # generous settle window, nothing should arrive

        refreshed = manager.get(handoff_id)
        assert refreshed.status == "cancelled"
        assert refreshed.edits == []


class TestTriggerPolicyWatcherGate:
    """Product-owner requirement 2026-07-18: the `trigger_policy` gate must
    apply to the AUTOMATIC Tier 1 watcher path too, not just the HTTP/
    websocket upload routes -- this is the exact "close the PSD without
    saving [into the graph]" workflow the request called out, and it never
    goes through either upload route at all.
    """

    async def test_ignore_policy_settled_save_is_not_ingested(
        self, watcher_client, context, manager, launches
    ):
        (context.output_dir / SOURCE_FILENAME).write_bytes(png_bytes((1, 2, 3)))
        response = await watcher_client.post(
            "/cpsb/open", json=open_body(trigger_policy="Ignore (do nothing)")
        )
        assert response.status == 200
        handoff_id = (await response.json())["handoff_id"]
        assert manager.get(handoff_id).trigger_policy == "Ignore (do nothing)"
        await asyncio.sleep(0.3)  # let the initial open settle before the "save"

        # Simulate Photoshop overwriting the MANAGED copy in place (a plain
        # Cmd+S), exactly like a real edit would arrive.
        source_path = manager.psd_path(manager.get(handoff_id))
        write_psd(source_path, Image.new("RGB", (16, 16), (250, 0, 0)))

        # Generous settle window (debounce + a margin) -- nothing should
        # ever arrive, so this is a "still true after waiting" assertion,
        # not a wait_until.
        await asyncio.sleep(0.3 + 0.2 * 4)

        refreshed = manager.get(handoff_id)
        assert refreshed.status == "editing"  # never moved to "edited"
        assert refreshed.edits == []

    async def test_update_only_policy_settled_save_is_ingested(
        self, watcher_client, context, manager, launches
    ):
        """Contrast case, proving the gate is policy-specific rather than
        having broken the watcher entirely.
        """
        (context.output_dir / SOURCE_FILENAME).write_bytes(png_bytes((1, 2, 3)))
        response = await watcher_client.post(
            "/cpsb/open", json=open_body(trigger_policy="Update only (don't re-run)")
        )
        handoff_id = (await response.json())["handoff_id"]
        await asyncio.sleep(0.3)

        source_path = manager.psd_path(manager.get(handoff_id))
        write_psd(source_path, Image.new("RGB", (16, 16), (0, 250, 0)))

        await wait_until(lambda: len(manager.get(handoff_id).edits) >= 1)

        refreshed = manager.get(handoff_id)
        assert refreshed.status == "edited"
        edit_path = context.cpsb_input_dir / handoff_id / refreshed.edits[0].filename
        with Image.open(edit_path) as edited:
            assert edited.getpixel((0, 0))[:3] == (0, 250, 0)


async def _connect_tier2_plugin(ws, context: CpsbContext, local_mode: bool = True) -> None:
    """Minimal hello/ready handshake to register a ready Tier 2 plugin (PROTOCOL.md §3).

    A slimmer, standalone duplicate of ``TestPluginWebsocket.handshake`` --
    kept separate rather than shared so this module's other test classes
    don't couple to that class's own helper. *local_mode* controls the
    ``ready`` message's own field (PROTOCOL.md §2, amended: this is what
    decides whether the connected plugin bypasses the client-locality gate).
    """
    await ws.send_json(
        {"type": "hello", "plugin_version": "0.1.0", "ps_version": "26.5", "uxp_version": "8.1"}
    )
    await ws.receive_json(timeout=5)  # hello_ack
    await ws.send_json({"type": "ready", "local_mode": local_mode})


class TestClientLocalityGate:
    """PROTOCOL.md §2/§7: ``POST /cpsb/open`` gates a Tier 1 launch behind a
    428 confirm when the requesting client isn't on this machine.

    ``cpsb.routes`` imports ``is_request_local`` into its own namespace
    (``from .locality import is_request_local``), so that -- not
    ``cpsb.locality`` -- is where these tests monkeypatch it.

    PROTOCOL.md §2 was amended after this gate's first release: a connected
    Tier 2 plugin bypasses the gate ONLY in REMOTE mode (``local_mode:
    false`` -- the plugin runs on a different machine than the server,
    almost certainly the browser's own). A LOCAL-mode plugin (``local_mode:
    true``) sits on the SERVER's own machine, same as the Tier 1 OS-launch
    path, so it is gated exactly like Tier 1 -- see
    ``TestTier2LocalMode``/``TestTier2RemoteMode`` below for that split.
    """

    async def test_local_client_no_gate(self, client, source_image, launches):
        """aiohttp's ``TestClient`` always connects from 127.0.0.1 -- the
        loopback fast path -- so every existing ``/cpsb/open`` test (none
        of which patch ``is_request_local``) must keep passing unmodified,
        and a fresh request here must never see a 428.
        """
        response = await client.post("/cpsb/open", json=open_body())
        assert response.status == 200

    async def test_remote_client_gated_with_no_side_effects(
        self, client, manager, context, source_image, launches, monkeypatch
    ):
        monkeypatch.setattr(routes_module, "is_request_local", lambda request: False)

        response = await client.post("/cpsb/open", json=open_body())

        assert response.status == 428
        body = await response.json()
        assert body["reason"] == "client_remote"
        assert body["server_name"] == platform.node()
        assert "error" in body
        # No side effects whatsoever: no handoff recorded, no folder on disk.
        assert manager.list_all() == []
        assert list(context.cpsb_input_dir.glob("*")) == []

    async def test_remote_client_gate_does_not_supersede_existing(
        self, client, context, manager, source_image, launches, monkeypatch
    ):
        first = await (await client.post("/cpsb/open", json=open_body())).json()
        assert manager.get(first["handoff_id"]).status == "editing"

        # Upstream re-generated the image under the SAME filename -- without
        # the gate this auto-supersedes the old handoff and proceeds once
        # the open is actually attempted (TestAutoSupersedeOnChangedSource).
        (context.output_dir / SOURCE_FILENAME).write_bytes(png_bytes((1, 2, 3)))

        monkeypatch.setattr(routes_module, "is_request_local", lambda request: False)
        # The 428 gate must intercept before that pending supersede is
        # applied, exactly like it already defers past the 503 check.
        response = await client.post("/cpsb/open", json=open_body())

        assert response.status == 428
        assert manager.get(first["handoff_id"]).status == "editing"  # NOT superseded

    async def test_remote_client_with_client_remote_ok_proceeds(
        self, client, source_image, launches, monkeypatch
    ):
        monkeypatch.setattr(routes_module, "is_request_local", lambda request: False)

        response = await client.post("/cpsb/open", json=open_body(client_remote_ok=True))

        assert response.status == 200
        data = await response.json()
        assert data["tier"] == 1
        assert data["status"] == "pending"

    async def test_mode_original_gated_the_same(
        self, client, manager, source_image, launches, monkeypatch
    ):
        """mode:"original" is still a Tier 1 launch on the server -- the
        gate must apply to it exactly as it does to a fresh open.
        """
        first = await (await client.post("/cpsb/open", json=open_body())).json()

        monkeypatch.setattr(routes_module, "is_request_local", lambda request: False)
        response = await client.post("/cpsb/open", json=open_body(mode="original"))

        assert response.status == 428
        body = await response.json()
        assert body["reason"] == "client_remote"
        assert manager.get(first["handoff_id"]).status == "editing"  # untouched

        # And the acknowledgement flag lets the exact same re-open through.
        acknowledged = await client.post(
            "/cpsb/open", json=open_body(mode="original", client_remote_ok=True)
        )
        assert acknowledged.status == 200
        assert (await acknowledged.json())["handoff_id"] == first["handoff_id"]


class TestTier2LocalMode:
    """PROTOCOL.md §2 (amended): a Tier 2 plugin in LOCAL mode sits on the
    SERVER's own machine, so it does NOT bypass the client-locality gate --
    it is gated exactly like a Tier 1 OS-launch would be.
    """

    async def test_remote_client_gated_428(
        self, client, context, source_image, launches, monkeypatch
    ):
        monkeypatch.setattr(routes_module, "is_request_local", lambda request: False)
        async with client.ws_connect("/cpsb/ws") as ws:
            await _connect_tier2_plugin(ws, context, local_mode=True)
            await wait_until(lambda: routes_module.tier2_connected(client.app))

            response = await client.post("/cpsb/open", json=open_body())

            assert response.status == 428
            body = await response.json()
            assert body["reason"] == "client_remote"
            assert body["server_name"] == platform.node()

    async def test_remote_client_with_client_remote_ok_proceeds_as_tier2(
        self, client, context, source_image, launches, monkeypatch
    ):
        monkeypatch.setattr(routes_module, "is_request_local", lambda request: False)
        async with client.ws_connect("/cpsb/ws") as ws:
            await _connect_tier2_plugin(ws, context, local_mode=True)
            await wait_until(lambda: routes_module.tier2_connected(client.app))

            response = await client.post("/cpsb/open", json=open_body(client_remote_ok=True))

            assert response.status == 200
            data = await response.json()
            assert data["tier"] == 2
            assert data["status"] == "pending"

    async def test_local_client_no_gate(self, client, context, source_image, launches):
        """The default aiohttp TestClient connects from loopback (local) --
        a local-mode plugin's own machine, so no confirm is needed at all.
        """
        async with client.ws_connect("/cpsb/ws") as ws:
            await _connect_tier2_plugin(ws, context, local_mode=True)
            await wait_until(lambda: routes_module.tier2_connected(client.app))

            response = await client.post("/cpsb/open", json=open_body())

            assert response.status == 200
            data = await response.json()
            assert data["tier"] == 2
            assert data["status"] == "pending"


class TestTier2RemoteMode:
    """PROTOCOL.md §2 (amended): a Tier 2 plugin in REMOTE mode DOES bypass
    the client-locality gate -- the document opens wherever the plugin
    runs, which is where the user chose to install it, almost certainly the
    requesting browser's own machine.
    """

    async def test_remote_client_bypasses_gate_without_flag(
        self, client, context, source_image, launches, monkeypatch
    ):
        monkeypatch.setattr(routes_module, "is_request_local", lambda request: False)
        async with client.ws_connect("/cpsb/ws") as ws:
            await _connect_tier2_plugin(ws, context, local_mode=False)
            await wait_until(lambda: routes_module.tier2_connected(client.app))

            response = await client.post("/cpsb/open", json=open_body())

            assert response.status == 200
            data = await response.json()
            assert data["tier"] == 2
            assert data["status"] == "pending"


class TestTier2BypassesLocalityGateHelper:
    """Direct unit coverage of ``_tier2_bypasses_locality_gate`` (PROTOCOL.md
    §2, amended), isolating the "no plugin at all" / "plugin not ready yet"
    branches the route-level Tier2LocalMode/RemoteMode tests above don't
    exercise directly (a connected-but-not-ready plugin never reaches those
    routes' assertions, since ``tier2_connected`` -- and therefore whether
    Tier 2 is even selected -- also requires ``ready``).
    """

    def _app(self, context: CpsbContext, manager: HandoffManager) -> web.Application:
        app = web.Application()
        routes_module.install(app, context, manager)
        return app

    def test_no_plugin_connected_does_not_bypass(self, context, manager):
        app = self._app(context, manager)
        assert routes_module._tier2_bypasses_locality_gate(app) is False

    def test_connected_but_not_ready_does_not_bypass(self, context, manager):
        app = self._app(context, manager)
        connection = routes_module.PluginConnection(ws=object(), local_mode=False, ready=False)
        app[routes_module._APP_KEY_PLUGIN].connection = connection
        assert routes_module._tier2_bypasses_locality_gate(app) is False

    def test_ready_local_mode_does_not_bypass(self, context, manager):
        app = self._app(context, manager)
        connection = routes_module.PluginConnection(ws=object(), local_mode=True, ready=True)
        app[routes_module._APP_KEY_PLUGIN].connection = connection
        assert routes_module._tier2_bypasses_locality_gate(app) is False

    def test_ready_remote_mode_bypasses(self, context, manager):
        app = self._app(context, manager)
        connection = routes_module.PluginConnection(ws=object(), local_mode=False, ready=True)
        app[routes_module._APP_KEY_PLUGIN].connection = connection
        assert routes_module._tier2_bypasses_locality_gate(app) is True


class TestUpload:
    async def create_handoff(self, client) -> str:
        response = await client.post("/cpsb/open", json=open_body())
        return (await response.json())["handoff_id"]

    async def test_upload_ingests_edit(self, client, manager, context, source_image):
        handoff_id = await self.create_handoff(client)
        response = await client.post(
            "/cpsb/upload", data=upload_form(handoff_id, png_bytes((200, 0, 0)))
        )
        assert response.status == 200
        data = await response.json()
        assert data == {
            "ok": True,
            "filename": "edit_001.png",
            "subfolder": f"{DEFAULT_MANAGED_FOLDER_NAME}/{handoff_id}",
            "type": "input",
        }
        meta = manager.get(handoff_id)
        assert meta.status == "edited"
        assert meta.edits[0].fidelity == "plugin"

    async def test_duplicate_upload_is_idempotent(self, client, manager, source_image):
        handoff_id = await self.create_handoff(client)
        payload = png_bytes((200, 0, 0))
        first = await client.post("/cpsb/upload", data=upload_form(handoff_id, payload))
        second = await client.post("/cpsb/upload", data=upload_form(handoff_id, payload))
        assert first.status == 200
        assert second.status == 200
        assert (await second.json())["filename"] == "edit_001.png"
        assert len(manager.get(handoff_id).edits) == 1

    async def test_unknown_handoff_404(self, client, launches):
        response = await client.post(
            "/cpsb/upload", data=upload_form("deadbeef", png_bytes((1, 1, 1)))
        )
        assert response.status == 404

    async def test_inactive_handoff_409(self, client, manager, source_image):
        handoff_id = await self.create_handoff(client)
        await client.post(f"/cpsb/cancel/{handoff_id}")
        response = await client.post(
            "/cpsb/upload", data=upload_form(handoff_id, png_bytes((1, 1, 1)))
        )
        assert response.status == 409

    async def test_missing_fields_400(self, client, launches):
        form = aiohttp.FormData()
        form.add_field("handoff_id", "abc")
        response = await client.post("/cpsb/upload", data=form)
        assert response.status == 400

    async def test_invalid_image_400(self, client, source_image):
        handoff_id = await self.create_handoff(client)
        response = await client.post("/cpsb/upload", data=upload_form(handoff_id, b"not a png"))
        assert response.status == 400

    async def test_sibling_output_for_terminal_origin(self, client, context, source_image):
        response = await client.post("/cpsb/open", json=open_body(origin_kind="terminal_output"))
        handoff_id = (await response.json())["handoff_id"]
        await client.post("/cpsb/upload", data=upload_form(handoff_id, png_bytes((5, 5, 5))))
        assert (context.output_dir / "ComfyUI_00042__ps1.png").is_file()

    async def test_upload_subfolder_reflects_creation_time_setting(
        self, client, context, source_image
    ):
        """The subfolder in the upload response must name the folder the
        handoff actually lives in, not whatever managed_folder_name
        currently is -- exactly the ``handoff_dir``/``managed_dir_for``
        contract exercised for the ``cpsb.updated`` event in
        test_handoff.py::TestManagedFolderSwitch, but for this second,
        independent literal at the /cpsb/upload route.
        """
        context.settings.update({"managed_folder_name": "folder-a"})
        handoff_id = await self.create_handoff(client)

        context.settings.update({"managed_folder_name": "folder-b"})
        response = await client.post(
            "/cpsb/upload", data=upload_form(handoff_id, png_bytes((7, 7, 7)))
        )

        assert response.status == 200
        data = await response.json()
        assert data["subfolder"] == f"folder-a/{handoff_id}"
        assert (context.input_dir / "folder-a" / handoff_id / data["filename"]).is_file()


class TestTriggerPolicyGate:
    """Product-owner requirement 2026-07-18: a handoff's `trigger_policy`
    ("Ignore (do nothing)" in particular) must be enforced SERVER-SIDE at
    every ingest call site -- not just suggested to a frontend that might
    not even have a browser tab open (the plugin can upload with none at
    all). Covers the HTTP upload route here; the plugin websocket's
    `upload_edit` is covered in `TestPluginWebsocketFileTransfer`, and the
    Tier 1 watcher's settled-save path in `TestTriggerPolicyWatcherGate`
    below.
    """

    async def open_with_policy(self, client, trigger_policy: str) -> str:
        response = await client.post("/cpsb/open", json=open_body(trigger_policy=trigger_policy))
        assert response.status == 200
        return (await response.json())["handoff_id"]

    async def test_ignore_policy_upload_does_not_append_an_edit(
        self, client, manager, source_image
    ):
        handoff_id = await self.open_with_policy(client, "Ignore (do nothing)")

        response = await client.post(
            "/cpsb/upload", data=upload_form(handoff_id, png_bytes((200, 0, 0)))
        )

        # Success-shaped, never an error: the uploader (plugin or gallery
        # drag-and-drop) did nothing wrong.
        assert response.status == 200
        data = await response.json()
        assert data["ok"] is True

        refreshed = manager.get(handoff_id)
        assert refreshed.edits == []
        assert refreshed.status == "editing"  # never moved to "edited"

    async def test_update_only_policy_upload_appends_an_edit(self, client, manager, source_image):
        handoff_id = await self.open_with_policy(client, "Update only (don't re-run)")

        response = await client.post(
            "/cpsb/upload", data=upload_form(handoff_id, png_bytes((0, 200, 0)))
        )

        assert response.status == 200
        data = await response.json()
        assert data["ok"] is True
        assert data["filename"] == "edit_001.png"

        refreshed = manager.get(handoff_id)
        assert len(refreshed.edits) == 1
        assert refreshed.status == "edited"

    async def test_rerun_policy_upload_appends_an_edit(self, client, manager, source_image):
        """Explicit "Re-run workflow" behaves exactly like today's default."""
        handoff_id = await self.open_with_policy(client, "Re-run workflow")

        response = await client.post(
            "/cpsb/upload", data=upload_form(handoff_id, png_bytes((0, 0, 200)))
        )

        assert response.status == 200
        assert len(manager.get(handoff_id).edits) == 1

    async def test_no_trigger_policy_key_at_all_upload_appends_an_edit(
        self, client, manager, source_image
    ):
        """A handoff opened before this feature existed has NO
        `trigger_policy` at all in its in-memory/on-disk state (simulating
        an upgrade from an older version) -- it must keep ingesting exactly
        as it always has.
        """
        handoff_id = await self.create_handoff_default(client)
        meta = manager.get(handoff_id)
        assert meta.trigger_policy == "Re-run workflow"  # the safe default

        response = await client.post(
            "/cpsb/upload", data=upload_form(handoff_id, png_bytes((1, 1, 1)))
        )

        assert response.status == 200
        assert len(manager.get(handoff_id).edits) == 1

    async def create_handoff_default(self, client) -> str:
        response = await client.post("/cpsb/open", json=open_body())
        return (await response.json())["handoff_id"]


class TestFileAndThumb:
    async def test_file_serves_psd(self, client, source_image):
        handoff_id = (await (await client.post("/cpsb/open", json=open_body())).json())[
            "handoff_id"
        ]
        response = await client.get(f"/cpsb/file/{handoff_id}")
        assert response.status == 200
        assert response.content_type == "image/vnd.adobe.photoshop"
        body = await response.read()
        assert body.startswith(b"8BPS")  # PSD magic

    async def test_file_404_for_unknown_or_inactive(self, client, source_image):
        assert (await client.get("/cpsb/file/deadbeef")).status == 404
        handoff_id = (await (await client.post("/cpsb/open", json=open_body())).json())[
            "handoff_id"
        ]
        await client.post(f"/cpsb/cancel/{handoff_id}")
        assert (await client.get(f"/cpsb/file/{handoff_id}")).status == 404

    async def test_thumb_serves_png(self, client, source_image):
        handoff_id = (await (await client.post("/cpsb/open", json=open_body())).json())[
            "handoff_id"
        ]
        response = await client.get(f"/cpsb/thumb/{handoff_id}")
        assert response.status == 200
        assert response.content_type == "image/png"
        assert (await response.read()).startswith(b"\x89PNG")

    async def test_thumb_404_unknown(self, client, launches):
        assert (await client.get("/cpsb/thumb/deadbeef")).status == 404


class TestCancelAndDiscard:
    async def test_cancel_transitions_and_404s(self, client, manager, source_image):
        handoff_id = (await (await client.post("/cpsb/open", json=open_body())).json())[
            "handoff_id"
        ]
        response = await client.post(f"/cpsb/cancel/{handoff_id}")
        assert response.status == 200
        assert (await response.json()) == {"ok": True}
        assert manager.get(handoff_id).status == "cancelled"
        assert (await client.post("/cpsb/cancel/deadbeef")).status == 404

    async def test_discard_transitions_and_404s(self, client, manager, source_image):
        handoff_id = (await (await client.post("/cpsb/open", json=open_body())).json())[
            "handoff_id"
        ]
        response = await client.post(f"/cpsb/discard/{handoff_id}")
        assert response.status == 200
        assert manager.get(handoff_id).status == "discarded"
        assert (await client.post("/cpsb/discard/deadbeef")).status == 404

    async def test_cancel_is_idempotent_200_when_already_terminal(
        self, client, manager, source_image
    ):
        """PROTOCOL.md §2: cancelling an already-terminal handoff returns
        200 and is a no-op -- cancel must be safe to mash (double-click,
        the gallery and the node badge both firing it, etc.).
        """
        handoff_id = (await (await client.post("/cpsb/open", json=open_body())).json())[
            "handoff_id"
        ]
        first = await client.post(f"/cpsb/cancel/{handoff_id}")
        assert first.status == 200

        second = await client.post(f"/cpsb/cancel/{handoff_id}")
        third = await client.post(f"/cpsb/cancel/{handoff_id}")

        assert second.status == 200
        assert (await second.json()) == {"ok": True}
        assert third.status == 200
        assert manager.get(handoff_id).status == "cancelled"

    async def test_cancel_after_discard_is_idempotent_not_a_revival(
        self, client, manager, source_image
    ):
        handoff_id = (await (await client.post("/cpsb/open", json=open_body())).json())[
            "handoff_id"
        ]
        await client.post(f"/cpsb/discard/{handoff_id}")

        response = await client.post(f"/cpsb/cancel/{handoff_id}")

        assert response.status == 200
        assert manager.get(handoff_id).status == "discarded"  # NOT overwritten to cancelled

    async def test_cancel_unknown_id_still_404s_even_after_a_real_cancel(
        self, client, manager, source_image
    ):
        handoff_id = (await (await client.post("/cpsb/open", json=open_body())).json())[
            "handoff_id"
        ]
        await client.post(f"/cpsb/cancel/{handoff_id}")

        response = await client.post("/cpsb/cancel/deadbeef")

        assert response.status == 404


class TestStatus:
    async def test_status_shape_and_ordering(self, client, source_image, launches):
        first = (await (await client.post("/cpsb/open", json=open_body())).json())["handoff_id"]
        second = (
            await (await client.post("/cpsb/open", json=open_body(origin_node_id="18"))).json()
        )["handoff_id"]

        response = await client.get("/cpsb/status")
        assert response.status == 200
        data = await response.json()
        assert data["server_version"] == routes_module._SERVER_VERSION
        # Single source of truth (PROTOCOL.md §9): routes.py must not carry
        # its own literal, only re-export cpsb.version.__version__.
        assert data["server_version"] == CPSB_VERSION
        assert data["tier1_available"] is True
        assert data["tier1_reason"] is None
        assert data["tier2_connected"] is False
        assert data["ps_version"] is None
        ids = [h["handoff_id"] for h in data["handoffs"]]
        assert ids == [second, first]  # newest first


class TestSettings:
    async def test_get_returns_defaults(self, client, launches):
        response = await client.get("/cpsb/settings")
        assert await response.json() == {
            "photoshop_path": "",
            "debounce_ms": 800,
            "cleanup_days": 14,
            "sibling_outputs": True,
            "managed_folder_name": DEFAULT_MANAGED_FOLDER_NAME,
        }

    async def test_post_merges_and_persists(self, client, context, launches):
        response = await client.post("/cpsb/settings", json={"debounce_ms": 500})
        data = await response.json()
        assert data["debounce_ms"] == 500
        assert data["cleanup_days"] == 14  # untouched keys survive the merge
        assert context.settings.get("debounce_ms") == 500
        assert (context.user_dir / "cpsb.json").is_file()

    async def test_post_malformed_400(self, client, launches):
        assert (await client.post("/cpsb/settings", data=b"nope")).status == 400
        assert (await client.post("/cpsb/settings", json=[1, 2])).status == 400


class TestPluginWebsocket:
    async def handshake(self, ws, context: CpsbContext, local_mode: bool = True) -> dict:
        await ws.send_json(
            {"type": "hello", "plugin_version": "0.1.0", "ps_version": "26.5", "uxp_version": "8.1"}
        )
        ack = await ws.receive_json(timeout=5)
        assert ack["type"] == "hello_ack"
        assert ack["input_cpsb_path"] == str(context.cpsb_input_dir.resolve())
        await ws.send_json({"type": "ready", "local_mode": local_mode})
        return ack

    async def test_handshake_marks_tier2_connected(self, client, context, events, launches):
        async with client.ws_connect("/cpsb/ws") as ws:
            await self.handshake(ws, context)
            await wait_until(lambda: any(p.get("connected") for p in events.of_type("cpsb.tier2")))
            status = await (await client.get("/cpsb/status")).json()
            assert status["tier2_connected"] is True
            assert status["ps_version"] == "26.5"

    async def test_open_dispatches_over_websocket(
        self, client, manager, context, source_image, launches
    ):
        async with client.ws_connect("/cpsb/ws") as ws:
            await self.handshake(ws, context)
            await wait_until(lambda: routes_module.tier2_connected(client.app))

            response = await client.post("/cpsb/open", json=open_body())
            data = await response.json()
            assert data["tier"] == 2

            command = await ws.receive_json(timeout=5)
            assert command["type"] == "open_handoff"
            assert command["handoff_id"] == data["handoff_id"]
            assert command["psd_path"].endswith(SOURCE_PSD_FILENAME)
            assert command["file_url"] == f"/cpsb/file/{data['handoff_id']}"
            # Tier 2: no OS launch, and status stays pending until `opened`.
            assert launches.calls == []
            assert manager.get(data["handoff_id"]).status == "pending"

            await ws.send_json(
                {"type": "opened", "handoff_id": data["handoff_id"], "document_id": 7}
            )
            await wait_until(lambda: manager.get(data["handoff_id"]).status == "editing")

    async def test_open_failed_marks_error(
        self, client, manager, context, source_image, launches, monkeypatch
    ):
        monkeypatch.setattr(
            routes_module,
            "tier1_status",
            lambda: Tier1Status(available=False, reason="headless-server"),
        )
        async with client.ws_connect("/cpsb/ws") as ws:
            await self.handshake(ws, context)
            await wait_until(lambda: routes_module.tier2_connected(client.app))

            data = await (await client.post("/cpsb/open", json=open_body())).json()
            await ws.receive_json(timeout=5)  # open_handoff command
            await ws.send_json(
                {"type": "open_failed", "handoff_id": data["handoff_id"], "error": "boom"}
            )
            await wait_until(lambda: manager.get(data["handoff_id"]).status == "error")
            assert manager.get(data["handoff_id"]).error == "boom"

    async def test_open_failed_local_mode_falls_back_to_tier1(
        self, client, manager, context, source_image, launches, monkeypatch
    ):
        """A LOCAL-mode plugin that fails to open falls back to a Tier 1 OS-open
        (same machine as the server, so the launch lands on the right screen)."""
        monkeypatch.setattr(
            routes_module, "tier1_status", lambda: Tier1Status(available=True)
        )
        async with client.ws_connect("/cpsb/ws") as ws:
            await self.handshake(ws, context, local_mode=True)
            await wait_until(lambda: routes_module.tier2_connected(client.app))
            data = await (await client.post("/cpsb/open", json=open_body())).json()
            await ws.receive_json(timeout=5)  # open_handoff command
            await ws.send_json(
                {"type": "open_failed", "handoff_id": data["handoff_id"], "error": "boom"}
            )
            # The Tier 1 fallback launch fires and drives status to editing.
            await wait_until(lambda: manager.get(data["handoff_id"]).status == "editing")
            assert len(launches.calls) == 1

    async def test_open_failed_remote_mode_does_not_fall_back(
        self, client, manager, context, source_image, launches, monkeypatch
    ):
        """A REMOTE-mode plugin that fails to open must NOT fall back to a
        server-side Tier 1 launch — that would open Photoshop on the server
        (the wrong machine). The handoff stays in `error` with the plugin's
        message, and no OS launch happens."""
        monkeypatch.setattr(
            routes_module, "tier1_status", lambda: Tier1Status(available=True)
        )
        async with client.ws_connect("/cpsb/ws") as ws:
            await self.handshake(ws, context, local_mode=False)
            await wait_until(lambda: routes_module.tier2_connected(client.app))
            data = await (await client.post("/cpsb/open", json=open_body())).json()
            await ws.receive_json(timeout=5)  # open_handoff command
            await ws.send_json(
                {"type": "open_failed", "handoff_id": data["handoff_id"], "error": "boom"}
            )
            await wait_until(lambda: manager.get(data["handoff_id"]).status == "error")
            assert manager.get(data["handoff_id"]).error == "boom"
            # No server-side launch — a remote plugin's failure stays an error.
            assert launches.calls == []

    async def test_opened_sets_plugin_doc_open_true(
        self, client, manager, context, source_image, launches
    ):
        """Gallery overhaul (2026-07-22): `opened` now ALSO records the
        plugin's own ground truth that the document is open, alongside the
        existing `status -> editing` transition."""
        async with client.ws_connect("/cpsb/ws") as ws:
            await self.handshake(ws, context)
            await wait_until(lambda: routes_module.tier2_connected(client.app))

            data = await (await client.post("/cpsb/open", json=open_body())).json()
            handoff_id = data["handoff_id"]
            await ws.receive_json(timeout=5)  # open_handoff command
            assert manager.get(handoff_id).plugin_doc_open is None

            await ws.send_json({"type": "opened", "handoff_id": handoff_id, "document_id": 7})
            await wait_until(lambda: manager.get(handoff_id).status == "editing")
            assert manager.get(handoff_id).plugin_doc_open is True

    async def test_document_closed_sets_plugin_doc_open_false(
        self, client, manager, context, source_image, launches
    ):
        """The new `document_closed` message records the document closing
        WITHOUT touching `status` — the handoff stays `editing` (still
        reachable by Re-open/Cancel), only the display layer's derived
        "closed" pseudo-status changes (web/cpsb/state.js)."""
        async with client.ws_connect("/cpsb/ws") as ws:
            await self.handshake(ws, context)
            await wait_until(lambda: routes_module.tier2_connected(client.app))

            data = await (await client.post("/cpsb/open", json=open_body())).json()
            handoff_id = data["handoff_id"]
            await ws.receive_json(timeout=5)  # open_handoff command
            await ws.send_json({"type": "opened", "handoff_id": handoff_id, "document_id": 7})
            await wait_until(lambda: manager.get(handoff_id).plugin_doc_open is True)

            await ws.send_json({"type": "document_closed", "handoff_id": handoff_id})
            await wait_until(lambda: manager.get(handoff_id).plugin_doc_open is False)
            assert manager.get(handoff_id).status == "editing"  # unchanged

    async def test_document_closed_unknown_handoff_does_not_crash_connection(
        self, client, manager, context, source_image, launches
    ):
        async with client.ws_connect("/cpsb/ws") as ws:
            await self.handshake(ws, context)
            await wait_until(lambda: routes_module.tier2_connected(client.app))

            await ws.send_json({"type": "document_closed", "handoff_id": "deadbeef"})

            # The connection/message loop must keep working afterward — a
            # real subsequent round trip still succeeds.
            data = await (await client.post("/cpsb/open", json=open_body())).json()
            handoff_id = data["handoff_id"]
            await ws.receive_json(timeout=5)  # open_handoff command
            await ws.send_json({"type": "opened", "handoff_id": handoff_id, "document_id": 7})
            await wait_until(lambda: manager.get(handoff_id).plugin_doc_open is True)

    def _make_bare_handoff(self, manager: HandoffManager, node_id: str) -> str:
        """A bare ``bridge_node`` handoff (no PSD on disk) -- enough for the
        `run_action`/`action_ok`/`action_error` tests below, which never
        reach `open_in_photoshop` (that's `cpsb.actions.PhotoshopAction`'s
        own job, covered in ``tests/test_actions.py``) -- only the raw
        websocket message plumbing ``cpsb.routes`` itself owns.
        """
        meta = manager.create(
            origin_node_id=node_id,
            origin_kind="bridge_node",
            workflow_name="",
            source=SourceRef(filename=f"action_{node_id}.png", subfolder="", type="temp"),
            original_image=Image.new("RGB", (8, 8), (1, 2, 3)),
        )
        return meta.handoff_id

    async def test_send_run_action_dispatches_over_websocket(
        self, client, manager, context, launches
    ):
        """``cpsb.actions.PhotoshopAction``'s new server->plugin message (not
        yet in PROTOCOL.md §3 -- see ``cpsb/actions.py``'s own docstring).
        """
        async with client.ws_connect("/cpsb/ws") as ws:
            await self.handshake(ws, context)
            await wait_until(lambda: routes_module.tier2_connected(client.app))

            handoff_id = self._make_bare_handoff(manager, "9")
            sent = await routes_module.send_run_action(
                client.app, handoff_id, "My Action", "My Set"
            )
            assert sent is True

            command = await ws.receive_json(timeout=5)
            assert command == {
                "type": "run_action",
                "handoff_id": handoff_id,
                "action_name": "My Action",
                "action_set": "My Set",
            }

    async def test_send_run_action_false_when_no_plugin_connected(self, client, manager):
        sent = await routes_module.send_run_action(client.app, "deadbeef", "Action", "Set")
        assert sent is False

    async def test_action_error_marks_handoff_error_and_unblocks_wait(
        self, client, manager, context, launches
    ):
        """A bad Action/Set name (or a delivery failure after a successful
        play, ``photoshop_plugin/runAction.js``) surfaces as `action_error`
        -- the SAME `manager.mark_error` transition `open_failed` uses, so a
        blocking ``wait_for_edit`` unblocks with ERROR immediately instead of
        spinning for the full timeout.
        """
        async with client.ws_connect("/cpsb/ws") as ws:
            await self.handshake(ws, context)
            await wait_until(lambda: routes_module.tier2_connected(client.app))

            handoff_id = self._make_bare_handoff(manager, "9")
            manager.mark_editing(handoff_id)

            waiter = asyncio.ensure_future(
                asyncio.to_thread(manager.wait_for_edit, handoff_id, 5.0)
            )
            await wait_until(lambda: handoff_id in manager._waiters)

            await ws.send_json(
                {
                    "type": "action_error",
                    "handoff_id": handoff_id,
                    "error": 'Action "Bogus" not found in set "Bogus Set"',
                }
            )

            outcome = await waiter
            assert outcome == WaitOutcome.ERROR
            refreshed = manager.get(handoff_id)
            assert refreshed.status == "error"
            assert refreshed.error == 'Action "Bogus" not found in set "Bogus Set"'

    async def test_action_error_for_unknown_handoff_does_not_crash(self, client, context, launches):
        async with client.ws_connect("/cpsb/ws") as ws:
            await self.handshake(ws, context)
            await wait_until(lambda: routes_module.tier2_connected(client.app))

            await ws.send_json({"type": "action_error", "handoff_id": "deadbeef", "error": "boom"})
            # No crash / connection stays alive -- proven by a further round trip.
            await ws.send_json({"type": "pong"})
            await asyncio.sleep(0.05)
            assert routes_module.tier2_connected(client.app)

    async def test_action_ok_is_informational_only(self, client, manager, context, launches):
        """`action_ok` never touches handoff state on its own -- the actual
        unblock signal is the edit that lands via the ordinary upload path,
        which `runAction.js`'s `deliverEdit` always runs BEFORE sending this
        (``cpsb/actions.py``'s own docstring).
        """
        async with client.ws_connect("/cpsb/ws") as ws:
            await self.handshake(ws, context)
            await wait_until(lambda: routes_module.tier2_connected(client.app))

            handoff_id = self._make_bare_handoff(manager, "9")
            await ws.send_json({"type": "action_ok", "handoff_id": handoff_id})
            await ws.send_json({"type": "pong"})
            await asyncio.sleep(0.05)
            assert routes_module.tier2_connected(client.app)
            assert manager.get(handoff_id).status == "pending"

    async def test_cancel_notifies_plugin(self, client, manager, context, source_image, launches):
        async with client.ws_connect("/cpsb/ws") as ws:
            await self.handshake(ws, context)
            await wait_until(lambda: routes_module.tier2_connected(client.app))
            data = await (await client.post("/cpsb/open", json=open_body())).json()
            await ws.receive_json(timeout=5)  # open_handoff

            await client.post(f"/cpsb/cancel/{data['handoff_id']}")
            notification = await ws.receive_json(timeout=5)
            assert notification == {
                "type": "handoff_cancelled",
                "handoff_id": data["handoff_id"],
            }

    async def test_full_cancel_unwind(
        self, client, manager, context, events, source_image, launches
    ):
        """PROTOCOL.md §2: cancel is the authoritative unstick for a handoff.
        One /cpsb/cancel call must, together: emit exactly one cpsb.status
        event, unblock a blocked wait_for_edit() with CANCELLED, notify a
        connected plugin exactly once, and leave the handoff immune to a
        late upload landing afterward (the upload/watcher-vs-cancel race
        PROTOCOL.md §2 calls out) -- status stays cancelled, no edit
        recorded. (The watcher-settle side of that race is already covered
        end-to-end by test_watcher.py::TestIgnoredFiles::
        test_save_for_inactive_handoff_ignored; this test adds the upload,
        blocking-wait, and websocket angles combined.)
        """
        async with client.ws_connect("/cpsb/ws") as ws:
            await self.handshake(ws, context)
            await wait_until(lambda: routes_module.tier2_connected(client.app))

            data = await (await client.post("/cpsb/open", json=open_body())).json()
            handoff_id = data["handoff_id"]
            await ws.receive_json(timeout=5)  # open_handoff command

            # A bridge-node-style blocking wait, registered before cancel.
            outcomes: list[str] = []

            def waiter() -> None:
                outcomes.append(manager.wait_for_edit(handoff_id, 10, poll_interval=0.02))

            wait_thread = threading.Thread(target=waiter)
            wait_thread.start()
            await asyncio.sleep(0.1)  # let the waiter register

            status_events_before = len(events.of_type("cpsb.status"))

            response = await client.post(f"/cpsb/cancel/{handoff_id}")
            assert response.status == 200
            assert (await response.json()) == {"ok": True}

            # The connected plugin is notified exactly once.
            notification = await ws.receive_json(timeout=5)
            assert notification == {"type": "handoff_cancelled", "handoff_id": handoff_id}

            # The blocked waiter unblocks with CANCELLED, not a timeout.
            wait_thread.join(timeout=5)
            assert not wait_thread.is_alive()
            assert outcomes == [WaitOutcome.CANCELLED]

            # Exactly one new cpsb.status event, and it says "cancelled".
            status_events_after = events.of_type("cpsb.status")
            assert len(status_events_after) == status_events_before + 1
            assert status_events_after[-1]["status"] == "cancelled"
            assert status_events_after[-1]["handoff_id"] == handoff_id

            # A late upload arriving after cancel is rejected outright and
            # records no edit -- the handoff must not be revived.
            upload_response = await client.post(
                "/cpsb/upload", data=upload_form(handoff_id, png_bytes((250, 10, 10)))
            )
            assert upload_response.status == 409
            refreshed = manager.get(handoff_id)
            assert refreshed.status == "cancelled"
            assert refreshed.edits == []

            # Mashing cancel again afterward stays a clean no-op.
            second_cancel = await client.post(f"/cpsb/cancel/{handoff_id}")
            assert second_cancel.status == 200
            assert len(events.of_type("cpsb.status")) == len(status_events_after)

    async def test_new_plugin_replaces_old_with_close_4000(self, client, context, launches):
        ws1 = await client.ws_connect("/cpsb/ws")
        await self.handshake(ws1, context)
        await wait_until(lambda: routes_module.tier2_connected(client.app))

        ws2 = await client.ws_connect("/cpsb/ws")
        closed = await ws1.receive(timeout=5)
        assert closed.type == aiohttp.WSMsgType.CLOSE
        assert closed.data == 4000

        await self.handshake(ws2, context)
        await wait_until(lambda: routes_module.tier2_connected(client.app))
        await ws2.close()
        await ws1.close()

    async def test_disconnect_emits_tier2_event(self, client, context, events, launches):
        async with client.ws_connect("/cpsb/ws") as ws:
            await self.handshake(ws, context)
            await wait_until(lambda: any(p.get("connected") for p in events.of_type("cpsb.tier2")))
        await wait_until(
            lambda: any(p.get("connected") is False for p in events.of_type("cpsb.tier2"))
        )
        status = await (await client.get("/cpsb/status")).json()
        assert status["tier2_connected"] is False

    async def test_unknown_message_types_ignored(self, client, context, launches):
        async with client.ws_connect("/cpsb/ws") as ws:
            await self.handshake(ws, context)
            await ws.send_json({"type": "from_the_future", "x": 1})
            await ws.send_json({"type": "pong"})
            await ws.send_str("not json at all")
            # Connection survives all three.
            await wait_until(lambda: routes_module.tier2_connected(client.app))

    async def test_open_handoff_command_carries_wants_layered_psd_flag(
        self, client, manager, context, source_image, launches
    ):
        """PROTOCOL.md §6d (remote Tier-2 layered annotate): `open_handoff`
        echoes `HandoffMeta.wants_layered_psd` verbatim -- the signal a
        REMOTE-mode plugin uses (`handoffs.js`) to pick the layered-PSD
        upload transport over the flat-PNG one at save time. `False` (the
        default) for an ordinary handoff opened the normal way; `True` for
        one created the way `cpsb.annotate._create_handoff` does.
        """
        async with client.ws_connect("/cpsb/ws") as ws:
            await self.handshake(ws, context, local_mode=False)
            await wait_until(lambda: routes_module.tier2_connected(client.app))

            # Ordinary handoff: defaults to False.
            data = await (await client.post("/cpsb/open", json=open_body())).json()
            command = await ws.receive_json(timeout=5)
            assert command["type"] == "open_handoff"
            assert command["handoff_id"] == data["handoff_id"]
            assert command["wants_layered_psd"] is False

            # An annotate-style handoff, created the way
            # cpsb.annotate._create_handoff does (wants_layered_psd=True),
            # then opened through the same tier-selecting seam.
            meta = manager.create(
                origin_node_id="9",
                origin_kind="bridge_node",
                workflow_name="",
                source=SourceRef(filename="annotate_9.png", subfolder="", type="temp"),
                original_image=Image.new("RGB", (8, 8), (1, 2, 3)),
                wants_layered_psd=True,
            )
            assert manager.get(meta.handoff_id).wants_layered_psd is True
            psd_path = manager.psd_path(meta)
            write_psd(psd_path, Image.new("RGB", (8, 8), (1, 2, 3)))
            manager.note_source_written(meta.handoff_id)

            attempt = await routes_module.open_in_photoshop(
                client.app, context, manager, meta, psd_path
            )
            assert attempt.ok is True
            command = await ws.receive_json(timeout=5)
            assert command["type"] == "open_handoff"
            assert command["handoff_id"] == meta.handoff_id
            assert command["wants_layered_psd"] is True


class TestPluginWebsocketFileTransfer:
    """PROTOCOL.md §3 REMOTE-mode file transfer: `request_file`/`file_chunk`/
    `file_error` (download, replacing a `fetch` of `GET /cpsb/file/<id>`) and
    `upload_edit`/`upload_ok`/`upload_error` (upload, replacing a `fetch` POST
    to `/cpsb/upload`) -- both riding the plugin websocket instead of HTTP,
    since UXP blocks cleartext `http://` to a non-localhost host but not
    `ws://`. Connects the plugin in REMOTE mode (`local_mode=False`)
    throughout, since that's the only mode either message type is meant for.
    """

    async def test_request_file_streams_chunks_matching_the_source(
        self, client, context, manager, source_image, launches
    ):
        async with client.ws_connect("/cpsb/ws") as ws:
            await _connect_tier2_plugin(ws, context, local_mode=False)
            await wait_until(lambda: routes_module.tier2_connected(client.app))

            data = await (await client.post("/cpsb/open", json=open_body())).json()
            handoff_id = data["handoff_id"]
            await ws.receive_json(timeout=5)  # open_handoff command

            expected = manager.psd_path(manager.get(handoff_id)).read_bytes()

            await ws.send_json({"type": "request_file", "handoff_id": handoff_id})

            chunks: dict[int, str] = {}
            total = None
            while total is None or len(chunks) < total:
                msg = await ws.receive_json(timeout=5)
                assert msg["type"] == "file_chunk"
                assert msg["handoff_id"] == handoff_id
                total = msg["total"]
                chunks[msg["seq"]] = msg["data_b64"]

            reassembled = base64.b64decode("".join(chunks[i] for i in range(total)))
            assert reassembled == expected
            assert reassembled.startswith(b"8BPS")  # PSD magic

    async def test_request_file_unknown_handoff_gets_file_error(self, client, context, launches):
        async with client.ws_connect("/cpsb/ws") as ws:
            await _connect_tier2_plugin(ws, context, local_mode=False)
            await wait_until(lambda: routes_module.tier2_connected(client.app))

            await ws.send_json({"type": "request_file", "handoff_id": "deadbeef"})

            msg = await ws.receive_json(timeout=5)
            assert msg == {
                "type": "file_error",
                "handoff_id": "deadbeef",
                "error": "Unknown or inactive handoff_id",
            }

    async def test_request_file_inactive_handoff_gets_file_error(
        self, client, context, manager, source_image, launches
    ):
        async with client.ws_connect("/cpsb/ws") as ws:
            await _connect_tier2_plugin(ws, context, local_mode=False)
            await wait_until(lambda: routes_module.tier2_connected(client.app))

            data = await (await client.post("/cpsb/open", json=open_body())).json()
            handoff_id = data["handoff_id"]
            await ws.receive_json(timeout=5)  # open_handoff command

            cancel_response = await client.post(f"/cpsb/cancel/{handoff_id}")
            assert cancel_response.status == 200
            await ws.receive_json(timeout=5)  # handoff_cancelled notification

            await ws.send_json({"type": "request_file", "handoff_id": handoff_id})

            msg = await ws.receive_json(timeout=5)
            assert msg == {
                "type": "file_error",
                "handoff_id": handoff_id,
                "error": "Unknown or inactive handoff_id",
            }

    async def test_upload_edit_chunks_reassemble_and_ingest(
        self, client, context, manager, source_image, launches
    ):
        async with client.ws_connect("/cpsb/ws") as ws:
            await _connect_tier2_plugin(ws, context, local_mode=False)
            await wait_until(lambda: routes_module.tier2_connected(client.app))

            data = await (await client.post("/cpsb/open", json=open_body())).json()
            handoff_id = data["handoff_id"]
            await ws.receive_json(timeout=5)  # open_handoff command

            payload = png_bytes((222, 33, 44))
            # A tiny chunk_chars forces a genuine multi-frame transfer rather
            # than one chunk holding the whole payload.
            chunks = b64_chunks(payload, chunk_chars=16)
            assert len(chunks) > 1
            total = len(chunks)
            for seq, chunk in enumerate(chunks):
                await ws.send_json(
                    {
                        "type": "upload_edit",
                        "handoff_id": handoff_id,
                        "seq": seq,
                        "total": total,
                        "data_b64": chunk,
                        "fidelity": "plugin",
                    }
                )

            ack = await ws.receive_json(timeout=5)
            assert ack == {"type": "upload_ok", "handoff_id": handoff_id}

            meta = manager.get(handoff_id)
            assert meta.status == "edited"
            assert len(meta.edits) == 1
            assert meta.edits[0].fidelity == "plugin"
            edit_path = context.cpsb_input_dir / handoff_id / meta.edits[0].filename
            with Image.open(edit_path) as edited:
                assert edited.getpixel((0, 0))[:3] == (222, 33, 44)

    async def test_upload_edit_ignore_policy_does_not_ingest(
        self, client, context, manager, source_image, launches
    ):
        """Product-owner requirement 2026-07-18: a "Ignore (do nothing)"
        handoff must not ingest a plugin websocket upload -- this is the
        Tier 2 "Send back now" path the request specifically called out
        ("The same issues occur with it auto running the workflow").
        """
        async with client.ws_connect("/cpsb/ws") as ws:
            await _connect_tier2_plugin(ws, context, local_mode=False)
            await wait_until(lambda: routes_module.tier2_connected(client.app))

            data = await (
                await client.post(
                    "/cpsb/open", json=open_body(trigger_policy="Ignore (do nothing)")
                )
            ).json()
            handoff_id = data["handoff_id"]
            await ws.receive_json(timeout=5)  # open_handoff command
            # Plugin confirms the document actually opened (PROTOCOL.md §3),
            # same as a real "Send back now" round trip would see.
            await ws.send_json({"type": "opened", "handoff_id": handoff_id})
            await wait_until(lambda: manager.get(handoff_id).status == "editing")

            payload = png_bytes((5, 5, 5))
            await ws.send_json(
                {
                    "type": "upload_edit",
                    "handoff_id": handoff_id,
                    "seq": 0,
                    "total": 1,
                    "data_b64": base64.b64encode(payload).decode("ascii"),
                    "fidelity": "plugin",
                }
            )

            # A normal upload_ok ack -- the plugin did nothing wrong -- never
            # an upload_error.
            ack = await ws.receive_json(timeout=5)
            assert ack == {"type": "upload_ok", "handoff_id": handoff_id}

            meta = manager.get(handoff_id)
            assert meta.edits == []
            assert meta.status == "editing"  # never moved to "edited"

    async def test_upload_edit_unknown_handoff_gets_upload_error(self, client, context, launches):
        async with client.ws_connect("/cpsb/ws") as ws:
            await _connect_tier2_plugin(ws, context, local_mode=False)
            await wait_until(lambda: routes_module.tier2_connected(client.app))

            payload = png_bytes((1, 2, 3))
            await ws.send_json(
                {
                    "type": "upload_edit",
                    "handoff_id": "deadbeef",
                    "seq": 0,
                    "total": 1,
                    "data_b64": base64.b64encode(payload).decode("ascii"),
                    "fidelity": "plugin",
                }
            )

            msg = await ws.receive_json(timeout=5)
            assert msg == {
                "type": "upload_error",
                "handoff_id": "deadbeef",
                "error": "Unknown handoff_id",
                "reason": "unknown_handoff",
            }

    async def test_upload_edit_inactive_handoff_gets_upload_error(
        self, client, context, manager, source_image, launches
    ):
        async with client.ws_connect("/cpsb/ws") as ws:
            await _connect_tier2_plugin(ws, context, local_mode=False)
            await wait_until(lambda: routes_module.tier2_connected(client.app))

            data = await (await client.post("/cpsb/open", json=open_body())).json()
            handoff_id = data["handoff_id"]
            await ws.receive_json(timeout=5)  # open_handoff command
            await client.post(f"/cpsb/cancel/{handoff_id}")
            await ws.receive_json(timeout=5)  # handoff_cancelled notification

            payload = png_bytes((5, 5, 5))
            await ws.send_json(
                {
                    "type": "upload_edit",
                    "handoff_id": handoff_id,
                    "seq": 0,
                    "total": 1,
                    "data_b64": base64.b64encode(payload).decode("ascii"),
                    "fidelity": "plugin",
                }
            )

            msg = await ws.receive_json(timeout=5)
            assert msg["type"] == "upload_error"
            assert msg["handoff_id"] == handoff_id
            assert msg["reason"] == "inactive"
            assert manager.get(handoff_id).status == "cancelled"
            assert manager.get(handoff_id).edits == []

    async def test_upload_edit_invalid_image_data_gets_upload_error(
        self, client, context, manager, source_image, launches
    ):
        async with client.ws_connect("/cpsb/ws") as ws:
            await _connect_tier2_plugin(ws, context, local_mode=False)
            await wait_until(lambda: routes_module.tier2_connected(client.app))

            data = await (await client.post("/cpsb/open", json=open_body())).json()
            handoff_id = data["handoff_id"]
            await ws.receive_json(timeout=5)  # open_handoff command

            garbage = b"not a png"
            await ws.send_json(
                {
                    "type": "upload_edit",
                    "handoff_id": handoff_id,
                    "seq": 0,
                    "total": 1,
                    "data_b64": base64.b64encode(garbage).decode("ascii"),
                    "fidelity": "plugin",
                }
            )

            msg = await ws.receive_json(timeout=5)
            assert msg["type"] == "upload_error"
            assert msg["handoff_id"] == handoff_id
            assert msg["reason"] == "invalid_image"
            meta = manager.get(handoff_id)
            assert meta.status != "edited"
            assert meta.edits == []

    async def test_upload_edit_unknown_kind_gets_upload_error(
        self, client, context, manager, launches
    ):
        """An unrecognized `kind` (neither `"png"` nor `"psd"`) is treated the
        same as any other malformed chunk -- rejected outright, never
        silently guessed at.
        """
        async with client.ws_connect("/cpsb/ws") as ws:
            await _connect_tier2_plugin(ws, context, local_mode=False)
            await wait_until(lambda: routes_module.tier2_connected(client.app))

            await ws.send_json(
                {
                    "type": "upload_edit",
                    "handoff_id": "deadbeef",
                    "seq": 0,
                    "total": 1,
                    "data_b64": base64.b64encode(b"whatever").decode("ascii"),
                    "fidelity": "plugin",
                    "kind": "xml",
                }
            )
            msg = await ws.receive_json(timeout=5)
            assert msg == {
                "type": "upload_error",
                "handoff_id": "deadbeef",
                "error": "Malformed upload_edit chunk",
                "reason": "malformed",
            }

    async def test_upload_edit_explicit_png_kind_still_decodes_as_image(
        self, client, context, manager, source_image, launches
    ):
        """`kind` defaults to (and, sent explicitly, still means) the
        original flat-PNG transport -- a non-annotate handoff's remote
        upload is byte-for-byte unchanged by the new `kind` field's mere
        existence.
        """
        async with client.ws_connect("/cpsb/ws") as ws:
            await _connect_tier2_plugin(ws, context, local_mode=False)
            await wait_until(lambda: routes_module.tier2_connected(client.app))

            data = await (await client.post("/cpsb/open", json=open_body())).json()
            handoff_id = data["handoff_id"]
            await ws.receive_json(timeout=5)  # open_handoff command

            payload = png_bytes((77, 88, 99))
            await ws.send_json(
                {
                    "type": "upload_edit",
                    "handoff_id": handoff_id,
                    "seq": 0,
                    "total": 1,
                    "data_b64": base64.b64encode(payload).decode("ascii"),
                    "fidelity": "plugin",
                    "kind": "png",
                }
            )
            ack = await ws.receive_json(timeout=5)
            assert ack == {"type": "upload_ok", "handoff_id": handoff_id}

            meta = manager.get(handoff_id)
            assert meta.status == "edited"
            edit_path = context.cpsb_input_dir / handoff_id / meta.edits[0].filename
            with Image.open(edit_path) as edited:
                assert edited.getpixel((0, 0))[:3] == (77, 88, 99)

    async def test_upload_edit_psd_kind_writes_layered_copy_and_ingests(
        self, client, context, manager, launches, tmp_path
    ):
        """The layered-PSD remote-upload transport (PROTOCOL.md §6d): a
        `kind: "psd"` `upload_edit` writes the reassembled bytes to the
        handoff's own managed PSD copy path -- the SAME path
        `cpsb.annotate._read_ps_saved_psd` re-reads once `meta.edits` is
        non-empty -- rather than decoding them as a flat PNG, so the real
        "Instructions" layer (and whatever the user painted on it) survives
        the round trip.
        """
        async with client.ws_connect("/cpsb/ws") as ws:
            await _connect_tier2_plugin(ws, context, local_mode=False)
            await wait_until(lambda: routes_module.tier2_connected(client.app))

            meta = manager.create(
                origin_node_id="41",
                origin_kind="bridge_node",
                workflow_name="",
                source=SourceRef(filename="annotate_41.png", subfolder="", type="temp"),
                original_image=Image.new("RGB", (16, 16), (9, 9, 9)),
                wants_layered_psd=True,
            )
            handoff_id = meta.handoff_id

            payload = layered_psd_bytes(tmp_path, base_color=(9, 9, 9))
            # A tiny chunk_chars forces a genuine multi-frame transfer.
            chunks = b64_chunks(payload, chunk_chars=64)
            assert len(chunks) > 1
            total = len(chunks)
            for seq, chunk in enumerate(chunks):
                await ws.send_json(
                    {
                        "type": "upload_edit",
                        "handoff_id": handoff_id,
                        "seq": seq,
                        "total": total,
                        "data_b64": chunk,
                        "fidelity": "plugin",
                        "kind": "psd",
                    }
                )

            ack = await ws.receive_json(timeout=5)
            assert ack == {"type": "upload_ok", "handoff_id": handoff_id}

            refreshed = manager.get(handoff_id)
            assert refreshed.status == "edited"
            assert len(refreshed.edits) == 1
            assert refreshed.edits[0].fidelity in ("composite", "recomposite")

            # The managed copy on disk is now the REAL layered upload, byte
            # for byte -- not a flattened re-encoding of it.
            psd_path = manager.psd_path(refreshed)
            assert psd_path.read_bytes() == payload

            # And the annotate read path finds the real Instructions layer.
            image, mask, _combined = annotate_module._read_ps_saved_psd(psd_path)
            assert mask is not None
            assert mask[3:6, 3:6].min() > 0.9  # the painted region
            assert mask[0, 0] == 0  # untouched corner
            assert image.size == (16, 16)

    async def test_upload_edit_malformed_psd_kind_gets_upload_error_and_recovers(
        self, client, context, manager, launches, tmp_path
    ):
        """Malformed `kind: "psd"` bytes must not crash the plugin
        websocket's message loop (a genuinely corrupt or truncated transfer
        is possible on a real cross-machine link), and must never touch the
        handoff's managed copy on disk -- `_ingest_psd_upload` validates
        BEFORE writing anything, so a follow-up GOOD upload for the same
        handoff still succeeds normally afterward.
        """
        async with client.ws_connect("/cpsb/ws") as ws:
            await _connect_tier2_plugin(ws, context, local_mode=False)
            await wait_until(lambda: routes_module.tier2_connected(client.app))

            meta = manager.create(
                origin_node_id="42",
                origin_kind="bridge_node",
                workflow_name="",
                source=SourceRef(filename="annotate_42.png", subfolder="", type="temp"),
                original_image=Image.new("RGB", (8, 8), (1, 1, 1)),
                wants_layered_psd=True,
            )
            handoff_id = meta.handoff_id

            garbage = b"not a real psd at all, just garbage bytes"
            await ws.send_json(
                {
                    "type": "upload_edit",
                    "handoff_id": handoff_id,
                    "seq": 0,
                    "total": 1,
                    "data_b64": base64.b64encode(garbage).decode("ascii"),
                    "fidelity": "plugin",
                    "kind": "psd",
                }
            )
            msg = await ws.receive_json(timeout=5)
            assert msg["type"] == "upload_error"
            assert msg["handoff_id"] == handoff_id
            assert msg["reason"] == "invalid_image"
            refreshed = manager.get(handoff_id)
            assert refreshed.status != "edited"
            assert refreshed.edits == []
            assert not manager.psd_path(meta).is_file()  # never touched

            # The connection survives, and a good upload right after works.
            good_payload = layered_psd_bytes(tmp_path, base_color=(1, 1, 1))
            await ws.send_json(
                {
                    "type": "upload_edit",
                    "handoff_id": handoff_id,
                    "seq": 0,
                    "total": 1,
                    "data_b64": base64.b64encode(good_payload).decode("ascii"),
                    "fidelity": "plugin",
                    "kind": "psd",
                }
            )
            ack = await ws.receive_json(timeout=5)
            assert ack == {"type": "upload_ok", "handoff_id": handoff_id}
            assert manager.get(handoff_id).status == "edited"

    async def test_upload_edit_psd_kind_for_non_annotate_handoff_falls_back_to_flatten(
        self, client, context, manager, source_image, launches, tmp_path
    ):
        """Defensive graceful-fallback case: a `kind: "psd"` upload for a
        handoff that never asked for layered treatment
        (`wants_layered_psd=False`) still ingests cleanly -- write it,
        flatten it, record the flattened composite as the edit -- rather
        than erroring out, since the write+flatten step in
        `_ingest_psd_upload` never actually inspects that flag.
        """
        async with client.ws_connect("/cpsb/ws") as ws:
            await _connect_tier2_plugin(ws, context, local_mode=False)
            await wait_until(lambda: routes_module.tier2_connected(client.app))

            data = await (await client.post("/cpsb/open", json=open_body())).json()
            handoff_id = data["handoff_id"]
            await ws.receive_json(timeout=5)  # open_handoff command
            assert manager.get(handoff_id).wants_layered_psd is False

            payload = layered_psd_bytes(tmp_path, base_color=(4, 5, 6), paint_box=None)
            await ws.send_json(
                {
                    "type": "upload_edit",
                    "handoff_id": handoff_id,
                    "seq": 0,
                    "total": 1,
                    "data_b64": base64.b64encode(payload).decode("ascii"),
                    "fidelity": "plugin",
                    "kind": "psd",
                }
            )
            ack = await ws.receive_json(timeout=5)
            assert ack == {"type": "upload_ok", "handoff_id": handoff_id}
            refreshed = manager.get(handoff_id)
            assert refreshed.status == "edited"
            assert len(refreshed.edits) == 1


class TestManualPush:
    """`manual_push` (2026-07-23) -- "send a layer/document to ComfyUI": the
    reverse of every other round trip, initiated by the plugin with NO
    ComfyUI node or existing handoff behind it at all. Chunking mirrors
    `upload_edit` exactly (see TestPluginWebsocketFileTransfer above) but
    there is no prior `POST /cpsb/open` -- the whole point is this message
    alone creates a brand-new handoff/gallery card.
    """

    async def test_creates_a_new_handoff_with_no_prior_open(self, client, context, manager):
        async with client.ws_connect("/cpsb/ws") as ws:
            await _connect_tier2_plugin(ws, context, local_mode=False)
            await wait_until(lambda: routes_module.tier2_connected(client.app))

            payload = png_bytes((10, 20, 30))
            chunks = b64_chunks(payload, chunk_chars=16)
            assert len(chunks) > 1
            for seq, chunk in enumerate(chunks):
                await ws.send_json(
                    {
                        "type": "manual_push",
                        "push_id": "push-1",
                        "seq": seq,
                        "total": len(chunks),
                        "data_b64": chunk,
                        "title": "Background (layer)",
                    }
                )

            ack = await ws.receive_json(timeout=5)
            assert ack["type"] == "manual_push_ok"
            assert ack["push_id"] == "push-1"
            handoff_id = ack["handoff_id"]
            assert handoff_id

            meta = manager.get(handoff_id)
            assert meta is not None
            assert meta.origin_kind == "manual_send"
            assert meta.status == "edited"
            assert meta.source.filename == "Background (layer)"
            assert len(meta.edits) == 1
            edit_path = context.cpsb_input_dir / handoff_id / meta.edits[0].filename
            with Image.open(edit_path) as edited:
                assert edited.getpixel((0, 0))[:3] == (10, 20, 30)

    async def test_writes_a_normal_reopenable_managed_psd(self, client, context, manager):
        """Unlike a bare bridge_node handoff, a push writes a real managed
        PSD copy -- so Open/Re-open works on a pushed card via the exact
        same code path every other `edited` handoff already uses, no new
        gallery capability-gating needed."""
        async with client.ws_connect("/cpsb/ws") as ws:
            await _connect_tier2_plugin(ws, context, local_mode=False)
            await wait_until(lambda: routes_module.tier2_connected(client.app))

            payload = png_bytes((1, 2, 3))
            await ws.send_json(
                {
                    "type": "manual_push",
                    "push_id": "push-2",
                    "seq": 0,
                    "total": 1,
                    "data_b64": base64.b64encode(payload).decode("ascii"),
                    "title": "art.psd (whole document)",
                }
            )
            ack = await ws.receive_json(timeout=5)
            handoff_id = ack["handoff_id"]
            meta = manager.get(handoff_id)
            assert manager.psd_path(meta).is_file()

    async def test_origin_node_id_never_matches_a_real_graph_node(self, client, context, manager):
        """The synthesized origin_node_id must be inert everywhere a real
        node id is looked up -- find_active_for_node for an unrelated node
        must not somehow match it."""
        async with client.ws_connect("/cpsb/ws") as ws:
            await _connect_tier2_plugin(ws, context, local_mode=False)
            await wait_until(lambda: routes_module.tier2_connected(client.app))

            await ws.send_json(
                {
                    "type": "manual_push",
                    "push_id": "push-3",
                    "seq": 0,
                    "total": 1,
                    "data_b64": base64.b64encode(png_bytes((5, 5, 5))).decode("ascii"),
                    "title": "x",
                }
            )
            await ws.receive_json(timeout=5)

            assert manager.find_active_for_node("17") is None
            assert manager.find_active_for_node("") is None

    async def test_default_title_when_missing(self, client, context, manager):
        async with client.ws_connect("/cpsb/ws") as ws:
            await _connect_tier2_plugin(ws, context, local_mode=False)
            await wait_until(lambda: routes_module.tier2_connected(client.app))

            await ws.send_json(
                {
                    "type": "manual_push",
                    "push_id": "push-4",
                    "seq": 0,
                    "total": 1,
                    "data_b64": base64.b64encode(png_bytes((7, 7, 7))).decode("ascii"),
                }
            )
            ack = await ws.receive_json(timeout=5)
            meta = manager.get(ack["handoff_id"])
            assert meta.source.filename == "Sent from Photoshop"

    async def test_malformed_chunk_gets_manual_push_error(self, client, context, manager):
        async with client.ws_connect("/cpsb/ws") as ws:
            await _connect_tier2_plugin(ws, context, local_mode=False)
            await wait_until(lambda: routes_module.tier2_connected(client.app))

            await ws.send_json({"type": "manual_push", "push_id": "push-5", "seq": 0})
            msg = await ws.receive_json(timeout=5)
            assert msg == {
                "type": "manual_push_error",
                "push_id": "push-5",
                "error": "Malformed manual_push chunk",
            }

    async def test_invalid_image_gets_manual_push_error_and_no_handoff(
        self, client, context, manager
    ):
        async with client.ws_connect("/cpsb/ws") as ws:
            await _connect_tier2_plugin(ws, context, local_mode=False)
            await wait_until(lambda: routes_module.tier2_connected(client.app))

            before = len(manager.list_all())
            await ws.send_json(
                {
                    "type": "manual_push",
                    "push_id": "push-6",
                    "seq": 0,
                    "total": 1,
                    "data_b64": base64.b64encode(b"not an image").decode("ascii"),
                    "title": "x",
                }
            )
            msg = await ws.receive_json(timeout=5)
            assert msg["type"] == "manual_push_error"
            assert msg["push_id"] == "push-6"
            assert len(manager.list_all()) == before

    async def test_pushed_card_reopens_via_open_route(self, client, context, manager):
        """The gallery's Open button on a pushed card POSTs /cpsb/open with
        `origin_kind: "manual_send"` and `mode: "original"` — the whole
        reason the push handler writes a real managed PSD. Regression: the
        route's _VALID_ORIGIN_KINDS allowlist originally omitted
        "manual_send", so this exact request 400'd ("Invalid origin_kind")
        and the button was dead on arrival."""
        async with client.ws_connect("/cpsb/ws") as ws:
            await _connect_tier2_plugin(ws, context, local_mode=False)
            await wait_until(lambda: routes_module.tier2_connected(client.app))

            await ws.send_json(
                {
                    "type": "manual_push",
                    "push_id": "push-8",
                    "seq": 0,
                    "total": 1,
                    "data_b64": base64.b64encode(png_bytes((30, 60, 90))).decode("ascii"),
                    "title": "Sky (layer)",
                }
            )
            ack = await ws.receive_json(timeout=5)
            meta = manager.get(ack["handoff_id"])

            # Exactly what gallery.js's openBodyFromMeta sends for mode
            # "original" on this card.
            response = await client.post(
                "/cpsb/open",
                json={
                    "filename": meta.source.filename,
                    "subfolder": meta.source.subfolder,
                    "type": meta.source.type,
                    "origin_node_id": meta.origin_node_id,
                    "origin_kind": "manual_send",
                    "workflow_name": meta.workflow_name,
                    "mode": "original",
                },
            )
            assert response.status == 200
            data = await response.json()
            assert data["handoff_id"] == meta.handoff_id
            assert data["tier"] == 2
            # And the plugin actually receives the open command for the
            # managed PSD the push wrote.
            command = await ws.receive_json(timeout=5)
            assert command["type"] == "open_handoff"
            assert command["handoff_id"] == meta.handoff_id

    async def test_palette_mode_png_is_normalized_not_a_crash(self, client, context, manager):
        """write_psd is a bare PSDImage.frompil, which RAISES for palette-mode
        ("P") images — and an exception escaping this handler doesn't fail one
        push, it tears down the whole plugin websocket. Regression for the
        missing _normalize_for_psd_write call (the /cpsb/open route always had
        it; the push handler originally didn't)."""
        buffer = io.BytesIO()
        Image.new("RGB", (16, 16), (200, 40, 40)).convert("P").save(buffer, format="PNG")
        async with client.ws_connect("/cpsb/ws") as ws:
            await _connect_tier2_plugin(ws, context, local_mode=False)
            await wait_until(lambda: routes_module.tier2_connected(client.app))

            await ws.send_json(
                {
                    "type": "manual_push",
                    "push_id": "push-p",
                    "seq": 0,
                    "total": 1,
                    "data_b64": base64.b64encode(buffer.getvalue()).decode("ascii"),
                    "title": "indexed.png",
                }
            )
            ack = await ws.receive_json(timeout=5)
            assert ack["type"] == "manual_push_ok"
            meta = manager.get(ack["handoff_id"])
            assert meta.status == "edited"
            assert manager.psd_path(meta).is_file()

    async def test_decompression_bomb_replies_error_and_keeps_socket_alive(
        self, client, context, manager, monkeypatch
    ):
        """PIL's DecompressionBombError subclasses Exception directly — the
        original narrow (OSError, ValueError) catch missed it, and the raise
        escaped the message loop and killed the Tier-2 socket. Reproduced
        for real by lowering Pillow's pixel limit instead of shipping a
        180-megapixel fixture."""
        async with client.ws_connect("/cpsb/ws") as ws:
            await _connect_tier2_plugin(ws, context, local_mode=False)
            await wait_until(lambda: routes_module.tier2_connected(client.app))

            monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 10)  # 24x16 > 2*10 -> bomb
            await ws.send_json(
                {
                    "type": "manual_push",
                    "push_id": "push-bomb",
                    "seq": 0,
                    "total": 1,
                    "data_b64": base64.b64encode(png_bytes((1, 1, 1))).decode("ascii"),
                    "title": "huge.png",
                }
            )
            msg = await ws.receive_json(timeout=5)
            assert msg["type"] == "manual_push_error"
            assert msg["push_id"] == "push-bomb"

            # The socket survived: a normal push right after still works.
            monkeypatch.undo()
            await ws.send_json(
                {
                    "type": "manual_push",
                    "push_id": "push-after-bomb",
                    "seq": 0,
                    "total": 1,
                    "data_b64": base64.b64encode(png_bytes((2, 2, 2))).decode("ascii"),
                    "title": "ok.png",
                }
            )
            ack = await ws.receive_json(timeout=5)
            assert ack["type"] == "manual_push_ok"

    async def test_ingest_failure_marks_handoff_error_and_keeps_socket_alive(
        self, client, context, manager, monkeypatch
    ):
        """A failure AFTER manager.create() (write_psd/ingest) must reply
        manual_push_error and mark the half-created handoff `error` (visible,
        removable, purgeable) — never escape the message loop (socket
        teardown) or strand an unexplained never-purgeable `pending` card."""

        def boom(*_args, **_kwargs):
            raise RuntimeError("simulated write failure")

        async with client.ws_connect("/cpsb/ws") as ws:
            await _connect_tier2_plugin(ws, context, local_mode=False)
            await wait_until(lambda: routes_module.tier2_connected(client.app))

            monkeypatch.setattr(routes_module, "write_psd", boom)
            await ws.send_json(
                {
                    "type": "manual_push",
                    "push_id": "push-boom",
                    "seq": 0,
                    "total": 1,
                    "data_b64": base64.b64encode(png_bytes((3, 3, 3))).decode("ascii"),
                    "title": "doomed.png",
                }
            )
            msg = await ws.receive_json(timeout=5)
            assert msg["type"] == "manual_push_error"
            assert msg["push_id"] == "push-boom"
            orphans = [m for m in manager.list_all() if m.origin_node_id == "ps-push:push-boom"]
            assert len(orphans) == 1
            assert orphans[0].status == "error"  # terminal, visible, purgeable

            # Socket alive and clean state: the next push succeeds.
            monkeypatch.undo()
            await ws.send_json(
                {
                    "type": "manual_push",
                    "push_id": "push-after-boom",
                    "seq": 0,
                    "total": 1,
                    "data_b64": base64.b64encode(png_bytes((4, 4, 4))).decode("ascii"),
                    "title": "fine.png",
                }
            )
            ack = await ws.receive_json(timeout=5)
            assert ack["type"] == "manual_push_ok"

    async def test_chunks_after_error_do_not_recreate_a_buffer(self, client, context, manager):
        """The sender bursts every chunk before any reply can reach it, so
        the chunks FOLLOWING a mid-stream error used to setdefault() a fresh
        _PendingPush that could never complete — a multi-MB leak held until
        the socket closed. The rejected push_id is now tombstoned."""
        async with client.ws_connect("/cpsb/ws") as ws:
            await _connect_tier2_plugin(ws, context, local_mode=False)
            await wait_until(lambda: routes_module.tier2_connected(client.app))

            # Chunk 0 malformed (missing total) -> error + tombstone.
            await ws.send_json({"type": "manual_push", "push_id": "push-leak", "seq": 0})
            msg = await ws.receive_json(timeout=5)
            assert msg["type"] == "manual_push_error"

            # The rest of the burst arrives anyway.
            for seq in (1, 2):
                await ws.send_json(
                    {
                        "type": "manual_push",
                        "push_id": "push-leak",
                        "seq": seq,
                        "total": 3,
                        "data_b64": "aGk=",
                        "title": "x",
                    }
                )

            # Give the server a beat to process, then a fresh push proving the
            # loop is healthy — and whose ok-reply ORDERING also proves the
            # trailing chunks above were handled (dropped) first.
            await ws.send_json(
                {
                    "type": "manual_push",
                    "push_id": "push-clean",
                    "seq": 0,
                    "total": 1,
                    "data_b64": base64.b64encode(png_bytes((5, 5, 5))).decode("ascii"),
                    "title": "clean.png",
                }
            )
            ack = await ws.receive_json(timeout=5)
            assert ack["type"] == "manual_push_ok"

            slot = client.app[routes_module._APP_KEY_PLUGIN]
            assert "push-leak" not in slot.connection.pending_pushes  # no recreated buffer
            assert "push-leak" in slot.connection.rejected_push_ids

    async def test_appears_in_status_route(self, client, context, manager):
        async with client.ws_connect("/cpsb/ws") as ws:
            await _connect_tier2_plugin(ws, context, local_mode=False)
            await wait_until(lambda: routes_module.tier2_connected(client.app))

            await ws.send_json(
                {
                    "type": "manual_push",
                    "push_id": "push-7",
                    "seq": 0,
                    "total": 1,
                    "data_b64": base64.b64encode(png_bytes((9, 9, 9))).decode("ascii"),
                    "title": "pushed.png",
                }
            )
            ack = await ws.receive_json(timeout=5)

            status = await (await client.get("/cpsb/status")).json()
            ids = [h["handoff_id"] for h in status["handoffs"]]
            assert ack["handoff_id"] in ids


def jpeg_bytes(color: tuple[int, int, int], size: tuple[int, int] = (24, 16)) -> bytes:
    """A real JPEG payload -- the live-frame wire format (PROTOCOL.md §3)."""
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


class TestLiveFrame:
    """`live_frame` (realtime drawing M1, docs/roadmap/realtime-drawing.md):
    the plugin's save-free canvas stream. Keep-latest, fire-and-forget, and
    EPHEMERAL -- one in-memory slot on the connection, no handoff, no disk,
    no gallery entry, ever.
    """

    async def test_frame_stored_and_cpsb_live_emitted(self, client, context, manager, events):
        async with client.ws_connect("/cpsb/ws") as ws:
            await _connect_tier2_plugin(ws, context, local_mode=False)
            await wait_until(lambda: routes_module.tier2_connected(client.app))

            payload = jpeg_bytes((10, 20, 30))
            await ws.send_json(
                {
                    "type": "live_frame",
                    "seq": 1,
                    "data_b64": base64.b64encode(payload).decode("ascii"),
                    "doc_title": "sketch.psd",
                }
            )
            await wait_until(lambda: routes_module.get_live_frame(client.app) is not None)

            frame = routes_module.get_live_frame(client.app)
            assert frame is not None
            jpeg, seq, title = frame
            assert jpeg == payload
            assert seq == 1
            assert title == "sketch.psd"
            live_events = events.of_type("cpsb.live")
            assert live_events and live_events[-1] == {"seq": 1, "doc_title": "sketch.psd"}

    async def test_keep_latest_replaces_and_seq_is_server_side(self, client, context, manager):
        """The slot holds ONE frame, and seq is the SERVER's monotonic
        counter -- the plugin's own seq is ignored, so a plugin
        restart/reconnect can never replay a lower number and stall
        IS_CHANGED."""
        async with client.ws_connect("/cpsb/ws") as ws:
            await _connect_tier2_plugin(ws, context, local_mode=False)
            await wait_until(lambda: routes_module.tier2_connected(client.app))

            first = jpeg_bytes((1, 1, 1))
            second = jpeg_bytes((2, 2, 2))
            for plugin_seq, payload in ((99, first), (1, second)):  # plugin seq goes BACKWARD
                await ws.send_json(
                    {
                        "type": "live_frame",
                        "seq": plugin_seq,
                        "data_b64": base64.b64encode(payload).decode("ascii"),
                        "doc_title": "sketch.psd",
                    }
                )
            await wait_until(
                lambda: (routes_module.get_live_frame(client.app) or (None, 0, None))[1] == 2
            )
            jpeg, seq, _title = routes_module.get_live_frame(client.app)
            assert jpeg == second  # latest replaced earlier
            assert seq == 2  # server-side counter, strictly increasing

    async def test_frames_never_touch_disk_or_handoffs(self, client, context, manager):
        """The roadmap's design commitment, asserted: a live frame creates no
        handoff and writes nothing under the managed folder."""
        async with client.ws_connect("/cpsb/ws") as ws:
            await _connect_tier2_plugin(ws, context, local_mode=False)
            await wait_until(lambda: routes_module.tier2_connected(client.app))

            before_files = sorted(context.cpsb_input_dir.rglob("*"))
            await ws.send_json(
                {
                    "type": "live_frame",
                    "seq": 1,
                    "data_b64": base64.b64encode(jpeg_bytes((5, 5, 5))).decode("ascii"),
                    "doc_title": "x",
                }
            )
            await wait_until(lambda: routes_module.get_live_frame(client.app) is not None)
            assert manager.list_all() == []
            assert sorted(context.cpsb_input_dir.rglob("*")) == before_files

    async def test_invalid_frames_dropped_socket_alive(self, client, context, manager):
        """Bad base64 / non-JPEG bytes are dropped with a log line -- never an
        error reply (fire-and-forget protocol), never a socket teardown, and
        never a slot update."""
        async with client.ws_connect("/cpsb/ws") as ws:
            await _connect_tier2_plugin(ws, context, local_mode=False)
            await wait_until(lambda: routes_module.tier2_connected(client.app))

            await ws.send_json(
                {"type": "live_frame", "seq": 1, "data_b64": "!!!not-base64!!!", "doc_title": "x"}
            )
            await ws.send_json(
                {
                    "type": "live_frame",
                    "seq": 2,
                    "data_b64": base64.b64encode(b"not a jpeg").decode("ascii"),
                    "doc_title": "x",
                }
            )
            # Socket still healthy: a valid frame right after lands as seq 1
            # (the two bad ones never bumped the counter).
            await ws.send_json(
                {
                    "type": "live_frame",
                    "seq": 3,
                    "data_b64": base64.b64encode(jpeg_bytes((7, 7, 7))).decode("ascii"),
                    "doc_title": "x",
                }
            )
            await wait_until(lambda: routes_module.get_live_frame(client.app) is not None)
            _jpeg, seq, _title = routes_module.get_live_frame(client.app)
            assert seq == 1

    async def test_get_live_frame_none_without_plugin_or_frame(self, client, context, manager):
        assert routes_module.get_live_frame(client.app) is None  # no plugin at all
        async with client.ws_connect("/cpsb/ws") as ws:
            await _connect_tier2_plugin(ws, context, local_mode=False)
            await wait_until(lambda: routes_module.tier2_connected(client.app))
            assert routes_module.get_live_frame(client.app) is None  # ready, no frame yet


class TestLivePrompt:
    """`live_prompt` (realtime drawing prompt control): the panel's prompt
    field, streamed to one keep-latest slot. Empty text clears the slot so the
    `PhotoshopLivePrompt` node falls back to its own widget.
    """

    async def test_prompt_stored_and_event_emitted(self, client, context, manager, events):
        async with client.ws_connect("/cpsb/ws") as ws:
            await _connect_tier2_plugin(ws, context, local_mode=False)
            await wait_until(lambda: routes_module.tier2_connected(client.app))

            await ws.send_json({"type": "live_prompt", "text": "a red origami bird"})
            await wait_until(
                lambda: routes_module.get_live_prompt(client.app) == "a red origami bird"
            )
            prompt_events = events.of_type("cpsb.liveprompt")
            assert prompt_events and prompt_events[-1] == {"has_prompt": True}

    async def test_prompt_is_stripped_and_kept_latest(self, client, context, manager):
        async with client.ws_connect("/cpsb/ws") as ws:
            await _connect_tier2_plugin(ws, context, local_mode=False)
            await wait_until(lambda: routes_module.tier2_connected(client.app))

            await ws.send_json({"type": "live_prompt", "text": "  first  "})
            await ws.send_json({"type": "live_prompt", "text": "  second  "})
            await wait_until(lambda: routes_module.get_live_prompt(client.app) == "second")

    async def test_empty_text_clears_slot_and_event_says_so(
        self, client, context, manager, events
    ):
        async with client.ws_connect("/cpsb/ws") as ws:
            await _connect_tier2_plugin(ws, context, local_mode=False)
            await wait_until(lambda: routes_module.tier2_connected(client.app))

            await ws.send_json({"type": "live_prompt", "text": "temporary"})
            await wait_until(lambda: routes_module.get_live_prompt(client.app) == "temporary")
            await ws.send_json({"type": "live_prompt", "text": "   "})  # whitespace clears
            await wait_until(lambda: routes_module.get_live_prompt(client.app) is None)
            prompt_events = events.of_type("cpsb.liveprompt")
            assert prompt_events[-1] == {"has_prompt": False}

    async def test_get_live_prompt_none_without_plugin(self, client, context, manager):
        assert routes_module.get_live_prompt(client.app) is None  # no plugin at all
        async with client.ws_connect("/cpsb/ws") as ws:
            await _connect_tier2_plugin(ws, context, local_mode=False)
            await wait_until(lambda: routes_module.tier2_connected(client.app))
            assert routes_module.get_live_prompt(client.app) is None  # ready, no prompt yet


class TestLiveCreativity:
    """`live_creativity` (realtime creativity slider): the preview panel's
    slider, streamed to one keep-latest slot, clamped to 0.0..1.0."""

    async def test_value_stored_and_event_emitted(self, client, context, manager, events):
        async with client.ws_connect("/cpsb/ws") as ws:
            await _connect_tier2_plugin(ws, context, local_mode=False)
            await wait_until(lambda: routes_module.tier2_connected(client.app))

            await ws.send_json({"type": "live_creativity", "value": 0.75})
            await wait_until(lambda: routes_module.get_live_creativity(client.app) == 0.75)
            evs = events.of_type("cpsb.livecreativity")
            assert evs and evs[-1] == {"creativity": 0.75}

    async def test_value_is_clamped(self, client, context, manager):
        async with client.ws_connect("/cpsb/ws") as ws:
            await _connect_tier2_plugin(ws, context, local_mode=False)
            await wait_until(lambda: routes_module.tier2_connected(client.app))

            await ws.send_json({"type": "live_creativity", "value": 9.0})
            await wait_until(lambda: routes_module.get_live_creativity(client.app) == 1.0)
            await ws.send_json({"type": "live_creativity", "value": -3.0})
            await wait_until(lambda: routes_module.get_live_creativity(client.app) == 0.0)

    async def test_non_numeric_value_dropped(self, client, context, manager):
        async with client.ws_connect("/cpsb/ws") as ws:
            await _connect_tier2_plugin(ws, context, local_mode=False)
            await wait_until(lambda: routes_module.tier2_connected(client.app))

            await ws.send_json({"type": "live_creativity", "value": 0.5})
            await wait_until(lambda: routes_module.get_live_creativity(client.app) == 0.5)
            await ws.send_json({"type": "live_creativity", "value": "not-a-number"})
            # Bad value dropped: the slot keeps the last good value.
            await ws.send_json({"type": "live_creativity", "value": 0.6})
            await wait_until(lambda: routes_module.get_live_creativity(client.app) == 0.6)

    async def test_get_live_creativity_none_without_plugin(self, client, context, manager):
        assert routes_module.get_live_creativity(client.app) is None
        async with client.ws_connect("/cpsb/ws") as ws:
            await _connect_tier2_plugin(ws, context, local_mode=False)
            await wait_until(lambda: routes_module.tier2_connected(client.app))
            assert routes_module.get_live_creativity(client.app) is None  # ready, untouched
