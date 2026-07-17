"""Every ``/cpsb/*`` HTTP route and the plugin websocket endpoint (PROTOCOL.md §2/§3).

Routes are defined on a module-level :class:`aiohttp.web.RouteTableDef` so
this module never has to import ``server.PromptServer`` itself -- the
top-level ``__init__.py`` registers ``routes`` onto ``PromptServer.instance``'s
own aiohttp ``Application`` (verified against ComfyUI's ``server.py``:
``PromptServer.instance.app`` is a plain ``aiohttp.web.Application``, and
``Application.add_routes()`` accepts any iterable of route definitions, which
a ``RouteTableDef`` is). Shared state (the :class:`~cpsb.context.CpsbContext`,
the :class:`~cpsb.handoff.HandoffManager`, and the single plugin websocket
connection) is looked up from ``request.app`` rather than a module-level
global, so tests can exercise these handlers against a throwaway
``aiohttp.web.Application`` with a fake context -- no ComfyUI required.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web
from PIL import Image

from .context import CpsbContext
from .handoff import (
    ACTIVE_STATUSES,
    HandoffManager,
    HandoffMeta,
    HandoffNotFoundError,
    SourceRef,
)
from .launcher import launch_photoshop, tier1_status
from .psd_io import write_psd

logger = logging.getLogger("cpsb")

routes = web.RouteTableDef()

#: This backend's protocol version, exchanged in the plugin's `hello`/`hello_ack`
#: handshake (PROTOCOL.md §9). Bump alongside `pyproject.toml`'s `[project].version`.
_SERVER_VERSION = "0.1.0"

_PING_INTERVAL_SECONDS = 30
_PONG_TIMEOUT_SECONDS = 15

_VALID_SOURCE_TYPES = ("input", "output", "temp")
_VALID_ORIGIN_KINDS = ("load_image", "terminal_output", "bridge_node")
_VALID_MODES = ("new", "original", "fresh")


def _error(status: int, message: str, **extra: Any) -> web.Response:
    return web.json_response({"error": message, **extra}, status=status)


@dataclass
class PluginConnection:
    """State for the single active UXP plugin websocket connection (PROTOCOL.md §3)."""

    ws: web.WebSocketResponse
    plugin_version: str | None = None
    ps_version: str | None = None
    uxp_version: str | None = None
    local_mode: bool | None = None
    ready: bool = False
    last_pong: float = field(default_factory=time.monotonic)


class _PluginSlot:
    """Mutable holder for the single plugin connection.

    The slot object itself is installed into the app before it starts
    serving; only the slot's *contents* change afterwards. (aiohttp
    deprecates mutating an ``Application``'s state dict once started, and
    the plugin connects long after startup.)
    """

    def __init__(self) -> None:
        self.connection: PluginConnection | None = None


_APP_KEY_CONTEXT: web.AppKey[CpsbContext] = web.AppKey("cpsb_context", CpsbContext)
_APP_KEY_MANAGER: web.AppKey[HandoffManager] = web.AppKey("cpsb_manager", HandoffManager)
_APP_KEY_PLUGIN: web.AppKey[_PluginSlot] = web.AppKey("cpsb_plugin", _PluginSlot)


def install(app: web.Application, context: CpsbContext, manager: HandoffManager) -> None:
    """Attach *context* and *manager* to *app* so every route below can find them.

    Call once (from the top-level ``__init__.py`` for the real ComfyUI app, or
    from a test's throwaway ``web.Application()``) before serving any request.
    """
    app[_APP_KEY_CONTEXT] = context
    app[_APP_KEY_MANAGER] = manager
    app[_APP_KEY_PLUGIN] = _PluginSlot()


def _context(request: web.Request) -> CpsbContext:
    return request.app[_APP_KEY_CONTEXT]


def _manager(request: web.Request) -> HandoffManager:
    return request.app[_APP_KEY_MANAGER]


def _plugin(request: web.Request) -> PluginConnection | None:
    return request.app[_APP_KEY_PLUGIN].connection


def _set_plugin(app: web.Application, connection: PluginConnection | None) -> None:
    app[_APP_KEY_PLUGIN].connection = connection


def tier2_connected(app: web.Application) -> bool:
    """Whether a UXP plugin is currently connected and past its ``ready`` handshake.

    Takes the aiohttp ``Application`` directly (not a ``web.Request``) so
    non-HTTP callers -- the Photoshop Bridge node -- can ask the same
    question a route handler would.
    """
    slot = app.get(_APP_KEY_PLUGIN)
    plugin = slot.connection if slot is not None else None
    return plugin is not None and plugin.ready


# -- POST /cpsb/open ---------------------------------------------------------


def _parse_open_body(body: Any) -> tuple[dict[str, Any], str | None]:
    """Validate the ``/cpsb/open`` body. Returns ``(fields, error_message)``."""
    if not isinstance(body, dict):
        return {}, "Body must be a JSON object"
    try:
        filename = str(body["filename"])
        source_type = str(body["type"])
        origin_node_id = str(body["origin_node_id"])
        origin_kind = str(body["origin_kind"])
    except (KeyError, TypeError):
        return {}, "filename, type, origin_node_id, and origin_kind are required"

    subfolder = str(body.get("subfolder", ""))
    workflow_name = str(body.get("workflow_name") or "")
    mode = str(body.get("mode", "new"))

    if source_type not in _VALID_SOURCE_TYPES:
        return {}, f"Invalid type: {source_type!r}"
    if origin_kind not in _VALID_ORIGIN_KINDS:
        return {}, f"Invalid origin_kind: {origin_kind!r}"
    if mode not in _VALID_MODES:
        return {}, f"Invalid mode: {mode!r}"

    return (
        {
            "filename": filename,
            "subfolder": subfolder,
            "type": source_type,
            "origin_node_id": origin_node_id,
            "origin_kind": origin_kind,
            "workflow_name": workflow_name,
            "mode": mode,
        },
        None,
    )


def _resolve_source_path(
    context: CpsbContext, filename: str, subfolder: str, source_type: str
) -> Path | None:
    """Resolve ``{filename, subfolder, type}`` the way ComfyUI's own ``/view`` does.

    Rejects any path that escapes the resolved base directory (guards against
    a `..`-laden `filename`/`subfolder` in the request body).
    """
    base_dir = {
        "input": context.input_dir,
        "output": context.output_dir,
        "temp": context.temp_dir,
    }.get(source_type)
    if base_dir is None:
        return None
    base_dir = base_dir.resolve()
    candidate = (base_dir / subfolder / filename).resolve()
    try:
        candidate.relative_to(base_dir)
    except ValueError:
        return None
    return candidate


def _normalize_for_psd_write(image: Image.Image) -> Image.Image:
    """Coerce an arbitrary ComfyUI-addressable image file to RGB/RGBA for `write_psd`."""
    if image.mode in ("RGB", "RGBA"):
        return image
    if image.mode == "P":
        return image.convert("RGBA" if "transparency" in image.info else "RGB")
    return image.convert("RGB")


@routes.post("/cpsb/open")
async def open_handoff_route(request: web.Request) -> web.Response:
    try:
        raw_body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return _error(400, "Malformed JSON body")

    fields, error = _parse_open_body(raw_body)
    if error is not None:
        return _error(400, error)

    context = _context(request)
    manager = _manager(request)
    origin_node_id = fields["origin_node_id"]
    mode = fields["mode"]
    # Workflow-scoped: node ids are only unique within one workflow, so
    # workflow B's node "17" must not adopt workflow A's active handoff.
    existing = manager.find_active_for_node(origin_node_id, fields["workflow_name"])

    if mode == "new" and existing is not None:
        return web.json_response(
            {
                "error": "An edit is already in progress for this node",
                "existing_handoff_id": existing.handoff_id,
            },
            status=409,
        )

    if not tier2_connected(request.app) and not tier1_status().available:
        return _error(
            503,
            "Neither Photoshop (Tier 1) nor the Photoshop panel plugin (Tier 2) is available.",
            tier1_available=False,
            tier2_connected=False,
        )

    if mode == "original":
        if existing is None:
            return _error(404, "No active handoff for this node")
        psd_path = manager.handoff_dir(existing.handoff_id) / "source.psd"
        return await _open_and_respond(request, context, manager, existing, psd_path)

    if mode == "fresh" and existing is not None:
        manager.supersede(existing.handoff_id)

    source_path = _resolve_source_path(
        context, fields["filename"], fields["subfolder"], fields["type"]
    )
    if source_path is None or not source_path.is_file():
        return _error(404, f"Source image not found: {fields['filename']}")

    try:
        original_image = Image.open(source_path)
        original_image.load()
    except (OSError, ValueError) as exc:
        return _error(404, f"Could not read source image: {exc}")

    meta = manager.create(
        origin_node_id=origin_node_id,
        origin_kind=fields["origin_kind"],
        workflow_name=fields["workflow_name"],
        source=SourceRef(
            filename=fields["filename"], subfolder=fields["subfolder"], type=fields["type"]
        ),
        original_image=original_image,
    )
    psd_path = manager.handoff_dir(meta.handoff_id) / "source.psd"
    write_psd(psd_path, _normalize_for_psd_write(original_image))
    manager.note_source_written(meta.handoff_id)

    return await _open_and_respond(request, context, manager, meta, psd_path)


@dataclass
class OpenAttempt:
    """Outcome of one :func:`open_in_photoshop` call."""

    tier: int  # 1 or 2
    ok: bool
    error: str | None = None


async def open_in_photoshop(
    app: web.Application,
    context: CpsbContext,
    manager: HandoffManager,
    meta: HandoffMeta,
    psd_path: Path,
) -> OpenAttempt:
    """Tier-select and attempt to open *psd_path* in Photoshop (PROTOCOL.md §3/§7).

    Shared by the ``POST /cpsb/open`` route and the Photoshop Bridge node
    (:mod:`cpsb.nodes`), so both make the identical Tier 1 vs Tier 2
    decision. Takes the aiohttp ``Application`` directly rather than a
    ``web.Request`` since the bridge node has no HTTP request to hand in.

    For the Tier 1 path, ``ok`` reflects whether the OS-level launch itself
    succeeded, and the handoff's status is updated (``editing``/``error``)
    synchronously before this returns. For Tier 2, ``ok`` only means the
    ``open_handoff`` command was sent -- the plugin's own confirmation
    (``opened``/``open_failed``) arrives later, asynchronously, over the
    websocket, and updates the handoff's status at that point instead.
    """
    slot = app.get(_APP_KEY_PLUGIN)
    plugin = slot.connection if slot is not None else None
    if plugin is not None and plugin.ready:
        await plugin.ws.send_json(
            {
                "type": "open_handoff",
                "handoff_id": meta.handoff_id,
                "psd_path": str(psd_path.resolve()),
                "file_url": f"/cpsb/file/{meta.handoff_id}",
            }
        )
        return OpenAttempt(tier=2, ok=True)

    # launch_photoshop chains blocking subprocess calls (worst case several
    # seconds through the mdfind fallback) -- run it in a worker thread so
    # ComfyUI's event loop keeps serving HTTP/websocket traffic meanwhile.
    result = await asyncio.to_thread(
        launch_photoshop, psd_path, context.settings.get("photoshop_path", "")
    )
    if result.ok:
        manager.mark_editing(meta.handoff_id)
    else:
        manager.mark_error(meta.handoff_id, result.error or "Failed to launch Photoshop")
    return OpenAttempt(tier=1, ok=result.ok, error=result.error)


async def _open_and_respond(
    request: web.Request,
    context: CpsbContext,
    manager: HandoffManager,
    meta: HandoffMeta,
    psd_path: Path,
) -> web.Response:
    """Build the ``/cpsb/open`` HTTP response around :func:`open_in_photoshop`.

    The response always reports ``status: "pending"`` regardless of whether
    the open attempt itself succeeds (PROTOCOL.md §2) -- see
    :func:`open_in_photoshop` for how the definitive outcome is conveyed.
    """
    attempt = await open_in_photoshop(request.app, context, manager, meta, psd_path)
    return web.json_response(
        {"handoff_id": meta.handoff_id, "tier": attempt.tier, "status": "pending"}
    )


async def _fallback_to_tier1(
    context: CpsbContext, manager: HandoffManager, handoff_id: str
) -> None:
    """Tier 2 ``open_failed`` -> retry via Tier 1 if this server can (PROTOCOL.md §3)."""
    if not tier1_status().available:
        return
    psd_path = manager.handoff_dir(handoff_id) / "source.psd"
    if not psd_path.is_file():
        return
    # Same off-loop treatment as open_in_photoshop: this runs inside the
    # plugin websocket's message handler, squarely on the event loop.
    result = await asyncio.to_thread(
        launch_photoshop, psd_path, context.settings.get("photoshop_path", "")
    )
    if result.ok:
        manager.mark_editing(handoff_id)
    else:
        manager.mark_error(handoff_id, result.error or "Failed to launch Photoshop")


# -- POST /cpsb/upload --------------------------------------------------------


@routes.post("/cpsb/upload")
async def upload_route(request: web.Request) -> web.Response:
    manager = _manager(request)

    if request.content_type != "multipart/form-data":
        return _error(400, "multipart/form-data body required")

    handoff_id: str | None = None
    source = "manual"
    image_bytes: bytes | None = None
    reader = await request.multipart()
    async for part in reader:
        if part.name == "handoff_id":
            handoff_id = (await part.read()).decode("utf-8")
        elif part.name == "source":
            source = (await part.read()).decode("utf-8")
        elif part.name == "image":
            image_bytes = await part.read()

    if not handoff_id or image_bytes is None:
        return _error(400, "handoff_id and image are required")

    meta = manager.get(handoff_id)
    if meta is None:
        return _error(404, "Unknown handoff_id")
    if meta.status not in ACTIVE_STATUSES:
        return _error(409, f"Handoff is {meta.status}, not accepting uploads")

    try:
        image = Image.open(io.BytesIO(image_bytes))
        image.load()
    except (OSError, ValueError) as exc:
        return _error(400, f"Invalid image data: {exc}")

    logger.info("Ingesting %s-sourced upload for handoff %s", source, handoff_id)
    # Both the plugin and a manual gallery drag-and-drop deliver final,
    # already-flattened pixels with no PSD (re)compositing on our side, so
    # both map to fidelity "plugin" -- the PROTOCOL.md §1 enum value meaning
    # "authoritative pixels, not derived by us."
    edit = manager.ingest_edit(handoff_id, image, "plugin")
    if edit is None:
        # Either a duplicate of the most recent edit (idempotent -- the
        # watchdog and the plugin can both report the same save) or the
        # handoff went inactive between the check above and here; both are
        # safe to answer as "already delivered" using the last known edit.
        latest = manager.get(handoff_id)
        if latest is not None and latest.edits:
            edit = latest.edits[-1]
        else:
            return _error(409, "Handoff is no longer active")

    return web.json_response(
        {"ok": True, "filename": edit.filename, "subfolder": f"cpsb/{handoff_id}", "type": "input"}
    )


# -- GET /cpsb/file/{handoff_id} ----------------------------------------------


@routes.get("/cpsb/file/{handoff_id}")
async def file_route(request: web.Request) -> web.Response:
    manager = _manager(request)
    handoff_id = request.match_info["handoff_id"]
    meta = manager.get(handoff_id)
    if meta is None or meta.status not in ACTIVE_STATUSES:
        return _error(404, "Unknown or inactive handoff_id")
    psd_path = manager.handoff_dir(handoff_id) / "source.psd"
    if not psd_path.is_file():
        return _error(404, "source.psd not found")
    return web.Response(body=psd_path.read_bytes(), content_type="image/vnd.adobe.photoshop")


# -- POST /cpsb/cancel/{handoff_id} -------------------------------------------


@routes.post("/cpsb/cancel/{handoff_id}")
async def cancel_route(request: web.Request) -> web.Response:
    manager = _manager(request)
    handoff_id = request.match_info["handoff_id"]
    try:
        manager.mark_cancelled(handoff_id)
    except HandoffNotFoundError:
        return _error(404, "Unknown handoff_id")
    plugin = _plugin(request)
    if plugin is not None:
        await plugin.ws.send_json({"type": "handoff_cancelled", "handoff_id": handoff_id})
    return web.json_response({"ok": True})


# -- POST /cpsb/discard/{handoff_id} ------------------------------------------


@routes.post("/cpsb/discard/{handoff_id}")
async def discard_route(request: web.Request) -> web.Response:
    manager = _manager(request)
    handoff_id = request.match_info["handoff_id"]
    try:
        manager.mark_discarded(handoff_id)
    except HandoffNotFoundError:
        return _error(404, "Unknown handoff_id")
    return web.json_response({"ok": True})


# -- GET /cpsb/status ----------------------------------------------------------


@routes.get("/cpsb/status")
async def status_route(request: web.Request) -> web.Response:
    manager = _manager(request)
    tier1 = tier1_status()
    plugin = _plugin(request)
    is_tier2_connected = tier2_connected(request.app)
    return web.json_response(
        {
            "tier1_available": tier1.available,
            "tier1_reason": tier1.reason,
            "tier2_connected": is_tier2_connected,
            "ps_version": plugin.ps_version if is_tier2_connected and plugin else None,
            "handoffs": [m.to_dict() for m in manager.list_all(limit=200)],
        }
    )


# -- GET /cpsb/thumb/{handoff_id} ----------------------------------------------


@routes.get("/cpsb/thumb/{handoff_id}")
async def thumb_route(request: web.Request) -> web.Response:
    manager = _manager(request)
    handoff_id = request.match_info["handoff_id"]
    if manager.get(handoff_id) is None:
        return _error(404, "Unknown handoff_id")
    thumb_path = manager.handoff_dir(handoff_id) / "orig_thumb.png"
    if not thumb_path.is_file():
        return _error(404, "Thumbnail not found")
    return web.Response(body=thumb_path.read_bytes(), content_type="image/png")


# -- GET/POST /cpsb/settings ---------------------------------------------------


@routes.get("/cpsb/settings")
async def get_settings_route(request: web.Request) -> web.Response:
    return web.json_response(_context(request).settings.as_dict())


@routes.post("/cpsb/settings")
async def update_settings_route(request: web.Request) -> web.Response:
    try:
        partial = await request.json()
    except (json.JSONDecodeError, ValueError):
        return _error(400, "Malformed JSON body")
    if not isinstance(partial, dict):
        return _error(400, "Body must be a JSON object")
    return web.json_response(_context(request).settings.update(partial))


# -- GET /cpsb/ws (plugin websocket) -------------------------------------------


async def _wait_for_pong(connection: PluginConnection, ping_sent_at: float, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if connection.last_pong >= ping_sent_at:
            return True
        await asyncio.sleep(0.5)
    return False


async def _keepalive_loop(connection: PluginConnection) -> None:
    """Ping every 30s; close if no pong within 15s (PROTOCOL.md §3)."""
    try:
        while True:
            await asyncio.sleep(_PING_INTERVAL_SECONDS)
            ping_sent_at = time.monotonic()
            await connection.ws.send_json({"type": "ping"})
            if not await _wait_for_pong(connection, ping_sent_at, _PONG_TIMEOUT_SECONDS):
                logger.warning(
                    "cpsb plugin did not pong within %ss, closing connection",
                    _PONG_TIMEOUT_SECONDS,
                )
                await connection.ws.close(code=1000, message=b"ping timeout")
                return
    except asyncio.CancelledError:
        pass


def _emit_tier2(context: CpsbContext, *, connected: bool, ps_version: str | None) -> None:
    context.send_event("cpsb.tier2", {"connected": connected, "ps_version": ps_version})


async def _handle_plugin_message(
    context: CpsbContext,
    manager: HandoffManager,
    connection: PluginConnection,
    raw: str,
) -> None:
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("cpsb plugin sent a non-JSON frame, ignoring")
        return
    msg_type = msg.get("type") if isinstance(msg, dict) else None

    if msg_type == "hello":
        connection.plugin_version = msg.get("plugin_version")
        connection.ps_version = msg.get("ps_version")
        connection.uxp_version = msg.get("uxp_version")
        await connection.ws.send_json(
            {
                "type": "hello_ack",
                "server_version": _SERVER_VERSION,
                "input_cpsb_path": str(context.cpsb_input_dir.resolve()),
            }
        )
    elif msg_type == "ready":
        connection.local_mode = bool(msg.get("local_mode"))
        connection.ready = True
        _emit_tier2(context, connected=True, ps_version=connection.ps_version)
    elif msg_type == "opened":
        handoff_id = msg.get("handoff_id")
        if handoff_id:
            try:
                manager.mark_editing(handoff_id)
            except HandoffNotFoundError:
                logger.warning("Plugin opened unknown handoff %s", handoff_id)
    elif msg_type == "open_failed":
        handoff_id = msg.get("handoff_id")
        if handoff_id:
            try:
                manager.mark_error(handoff_id, str(msg.get("error") or "Plugin failed to open"))
            except HandoffNotFoundError:
                logger.warning("Plugin reported open_failed for unknown handoff %s", handoff_id)
            else:
                await _fallback_to_tier1(context, manager, handoff_id)
    elif msg_type == "save_detected":
        pass  # Informational only -- pixels follow via POST /cpsb/upload.
    elif msg_type == "pong":
        connection.last_pong = time.monotonic()
    else:
        logger.debug("Ignoring unknown cpsb plugin message type: %r", msg_type)


@routes.get("/cpsb/ws")
async def websocket_route(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    context = _context(request)
    manager = _manager(request)

    previous = _plugin(request)
    if previous is not None:
        await previous.ws.close(code=4000, message=b"replaced by a new connection")

    connection = PluginConnection(ws=ws)
    _set_plugin(request.app, connection)

    keepalive_task = asyncio.ensure_future(_keepalive_loop(connection))
    try:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                await _handle_plugin_message(context, manager, connection, msg.data)
            elif msg.type == aiohttp.WSMsgType.ERROR:
                logger.warning("cpsb plugin websocket error: %s", ws.exception())
    finally:
        keepalive_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await keepalive_task
        if _plugin(request) is connection:
            _set_plugin(request.app, None)
            _emit_tier2(context, connected=False, ps_version=None)

    return ws
