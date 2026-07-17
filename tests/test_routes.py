"""Every /cpsb/* route and the plugin websocket, via aiohttp's test client.

No ComfyUI: the routes are mounted on a throwaway ``web.Application`` with
the fake context from ``conftest.py``, and Photoshop launching is
monkeypatched (``cpsb.routes`` imported ``launch_photoshop``/``tier1_status``
into its own namespace, so that is where the patches land).
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import platform
import threading
import time
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from PIL import Image
from psd_tools import PSDImage

import cpsb.routes as routes_module
from cpsb.context import DEFAULT_MANAGED_FOLDER_NAME, CpsbContext
from cpsb.handoff import HandoffManager, WaitOutcome, compute_source_hash
from cpsb.launcher import LaunchResult, Tier1Status
from cpsb.psd_io import write_psd
from cpsb.version import __version__ as CPSB_VERSION

SOURCE_FILENAME = "ComfyUI_00042_.png"


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


async def wait_until(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("Condition not met in time")


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

    async def test_open_answers_on_api_prefixed_path(
        self, api_client, source_image, launches
    ):
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
        assert launches.calls[0][0].endswith(f"{handoff_id}/source.psd")
        # The blocking launch must have run off the event loop (to_thread).
        assert launches.on_event_loop == [False]
        assert manager.get(handoff_id).status == "editing"
        folder = context.cpsb_input_dir / handoff_id
        assert (folder / "source.psd").is_file()
        assert (folder / "orig_thumb.png").is_file()
        assert (folder / "meta.json").is_file()

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

        other_workflow = await client.post(
            "/cpsb/open", json=open_body(workflow_name="workflow-b")
        )
        assert other_workflow.status == 200
        assert (await other_workflow.json())["handoff_id"] != (await first.json())["handoff_id"]

        # Same workflow again -> the 409 conflict is still enforced.
        same_workflow = await client.post(
            "/cpsb/open", json=open_body(workflow_name="workflow-a")
        )
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
    the handoff's ``source.psd`` is a verbatim byte-for-byte copy of the
    user's own file (never a re-encoded flatten), and ``source_hash`` is the
    sha256 of those raw bytes rather than a PNG-encoding hash.
    """

    async def test_copies_bytes_verbatim(self, client, context, manager, launches, tmp_path):
        original = psd_bytes(tmp_path, color=(11, 22, 33))
        (context.input_dir / "sample.psd").write_bytes(original)

        response = await client.post("/cpsb/open", json=open_body_psd())

        assert response.status == 200
        handoff_id = (await response.json())["handoff_id"]
        copied = (context.cpsb_input_dir / handoff_id / "source.psd").read_bytes()
        assert copied == original
        assert manager.get(handoff_id).source_hash == hashlib.sha256(original).hexdigest()

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
        copied_path = context.cpsb_input_dir / handoff_id / "source.psd"
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
        copied = (context.cpsb_input_dir / handoff_id / "source.psd").read_bytes()
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
        assert manager.get(second_body["handoff_id"]).source_hash == hashlib.sha256(
            new_bytes
        ).hexdigest()

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
        response = await client.post(
            "/cpsb/open", json=open_body_psd(filename="never-created.psd")
        )
        assert response.status == 404  # got past body validation to file resolution


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
        response = await client.post(
            "/cpsb/upload", data=upload_form(handoff_id, b"not a png")
        )
        assert response.status == 400

    async def test_sibling_output_for_terminal_origin(self, client, context, source_image):
        response = await client.post(
            "/cpsb/open", json=open_body(origin_kind="terminal_output")
        )
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
            await (
                await client.post("/cpsb/open", json=open_body(origin_node_id="18"))
            ).json()
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
    async def handshake(self, ws, context: CpsbContext) -> dict:
        await ws.send_json(
            {"type": "hello", "plugin_version": "0.1.0", "ps_version": "26.5", "uxp_version": "8.1"}
        )
        ack = await ws.receive_json(timeout=5)
        assert ack["type"] == "hello_ack"
        assert ack["input_cpsb_path"] == str(context.cpsb_input_dir.resolve())
        await ws.send_json({"type": "ready", "local_mode": True})
        return ack

    async def test_handshake_marks_tier2_connected(self, client, context, events, launches):
        async with client.ws_connect("/cpsb/ws") as ws:
            await self.handshake(ws, context)
            await wait_until(
                lambda: any(p.get("connected") for p in events.of_type("cpsb.tier2"))
            )
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
            assert command["psd_path"].endswith("source.psd")
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
            await wait_until(
                lambda: any(p.get("connected") for p in events.of_type("cpsb.tier2"))
            )
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
