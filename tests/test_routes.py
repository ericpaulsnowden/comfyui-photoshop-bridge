"""Every /cpsb/* route and the plugin websocket, via aiohttp's test client.

No ComfyUI: the routes are mounted on a throwaway ``web.Application`` with
the fake context from ``conftest.py``, and Photoshop launching is
monkeypatched (``cpsb.routes`` imported ``launch_photoshop``/``tier1_status``
into its own namespace, so that is where the patches land).
"""

from __future__ import annotations

import asyncio
import io
import time

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from PIL import Image

import cpsb.routes as routes_module
from cpsb.context import CpsbContext
from cpsb.handoff import HandoffManager
from cpsb.launcher import LaunchResult, Tier1Status

SOURCE_FILENAME = "ComfyUI_00042_.png"


def png_bytes(color: tuple[int, int, int], size: tuple[int, int] = (24, 16)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


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
            "subfolder": f"cpsb/{handoff_id}",
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
