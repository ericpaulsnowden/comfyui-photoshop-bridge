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
import hashlib
import io
import json
import logging
import platform
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
    compute_source_hash,
)
from .launcher import launch_photoshop, tier1_status
from .locality import is_request_local
from .psd_io import read_edited_psd, write_psd
from .version import __version__
from .watcher import CpsbWatcher

logger = logging.getLogger("cpsb")

routes = web.RouteTableDef()

#: This backend's protocol version, exchanged in the plugin's `hello`/`hello_ack`
#: handshake (PROTOCOL.md §9) and reported at `GET /cpsb/status` (PROTOCOL.md
#: §2). Single source of truth is `cpsb.version`, which `scripts/bump_version.py`
#: rewrites alongside `pyproject.toml`'s `[project].version`.
_SERVER_VERSION = __version__

_PING_INTERVAL_SECONDS = 30
_PONG_TIMEOUT_SECONDS = 15

_VALID_SOURCE_TYPES = ("input", "output", "temp")
_VALID_ORIGIN_KINDS = ("load_image", "terminal_output", "bridge_node", "load_psd")
_VALID_MODES = ("new", "original", "fresh")

#: Extensions accepted for a psd-native (`origin_kind: "load_psd"`) source
#: (PROTOCOL.md §2/§6b), case-insensitive.
_PSD_NATIVE_EXTENSIONS = (".psd", ".psb")

#: Fallback size for the psd-native placeholder thumbnail (PROTOCOL.md §2:
#: "if flatten fails, a neutral placeholder thumb -- never fail the open for
#: a thumbnail"). Matches `HandoffManager._write_thumbnail`'s own 256px cap
#: exactly so a placeholder is never itself the one thumbnail that needed
#: downsizing.
_PLACEHOLDER_THUMBNAIL_SIZE = (256, 256)
_PLACEHOLDER_THUMBNAIL_COLOR = (128, 128, 128)


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
_APP_KEY_WATCHER: web.AppKey[CpsbWatcher | None] = web.AppKey("cpsb_watcher", CpsbWatcher)


def install(
    app: web.Application,
    context: CpsbContext,
    manager: HandoffManager,
    watcher: CpsbWatcher | None = None,
) -> None:
    """Attach *context*, *manager*, and *watcher* to *app* so every route below can find them.

    Call once (from the top-level ``__init__.py`` for the real ComfyUI app, or
    from a test's throwaway ``web.Application()``) before serving any request.

    Args:
        app: The aiohttp application every ``/cpsb/*`` route is registered on.
        context: The active backend context.
        manager: The handoff manager.
        watcher: The Tier 1 save-detection watcher (PROTOCOL.md §6b), used
            only for the ``edit_in_place`` open/close hooks
            (:func:`open_handoff_route`, :func:`cancel_route`,
            :func:`discard_route`). Optional and defaults to ``None`` so
            every existing caller (real ``__init__.py`` wiring aside) and
            every pre-existing test that never exercises ``edit_in_place``
            keeps working unchanged; those hooks are simple no-ops when
            unset.
    """
    app[_APP_KEY_CONTEXT] = context
    app[_APP_KEY_MANAGER] = manager
    app[_APP_KEY_PLUGIN] = _PluginSlot()
    app[_APP_KEY_WATCHER] = watcher


def add_routes_to_app(app: web.Application) -> None:
    """Register every ``/cpsb/*`` route on *app* under BOTH the bare path and
    the ``/api``-prefixed path.

    ComfyUI's own ``PromptServer.add_routes()`` duplicates its route table
    under ``/api`` (``server.py``: it builds ``/api`` + path for every
    ``RouteDef`` in ``self.routes`` and adds both tables to the app), and the
    frontend's ``api.fetchApi`` always calls the ``/api``-prefixed form. A
    custom node that registers its own ``RouteTableDef`` directly with
    ``app.add_routes()`` gets only the bare paths, so every frontend call
    (``/api/cpsb/open`` etc.) lands on ComfyUI's static handler and comes back
    ``405 Method Not Allowed`` for anything but GET.

    Replicating the duplication here -- rather than appending our routes to
    ``PromptServer.routes`` and letting ComfyUI mirror them -- keeps
    registration independent of whether ComfyUI's ``add_routes()`` runs before
    or after custom nodes load, and avoids double-registering the bare paths.
    """
    api_routes = web.RouteTableDef()
    for route in routes:
        if isinstance(route, web.RouteDef):
            api_routes.route(route.method, "/api" + route.path, **route.kwargs)(route.handler)
    app.add_routes(api_routes)
    app.add_routes(routes)


def _context(request: web.Request) -> CpsbContext:
    return request.app[_APP_KEY_CONTEXT]


def _manager(request: web.Request) -> HandoffManager:
    return request.app[_APP_KEY_MANAGER]


def _watcher(request: web.Request) -> CpsbWatcher | None:
    """The Tier 1 watcher, or ``None`` when :func:`install` didn't get one.

    Every call site below treats ``None`` as "nothing to do" -- see
    :func:`install`'s docstring for why that's the right default rather
    than a hard requirement.
    """
    return request.app[_APP_KEY_WATCHER]


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


def _tier2_bypasses_locality_gate(app: web.Application) -> bool:
    """Whether a connected Tier 2 plugin bypasses the client-locality gate.

    PROTOCOL.md §2 (amended): a connected plugin bypasses the gate ONLY when
    it is in REMOTE mode -- the document then opens wherever the plugin
    runs, which is where the user chose to install it, almost certainly the
    same machine as the requesting browser. A plugin in LOCAL mode sits on
    the SERVER's own machine (``local_mode`` means the shared
    ``input_cpsb_path`` from ``hello_ack`` exists on the plugin's local
    filesystem, PROTOCOL.md §3) -- the same machine the Tier 1 OS-launch
    path would use -- so for a remote browser the document would still land
    on a screen that browser can't see: the gate still applies exactly as
    it does to Tier 1.

    ``tier2_connected()`` requires ``plugin.ready``, and the plugin's
    ``ready`` message handler (``_handle_plugin_message``) always sets
    ``local_mode`` in the very same branch that flips ``ready`` to `True`
    (one message, two fields, set together) -- so a connected, ready
    plugin's ``local_mode`` is never ``None`` here; the assert below
    documents and enforces that invariant rather than silently trusting it.
    """
    slot = app.get(_APP_KEY_PLUGIN)
    plugin = slot.connection if slot is not None else None
    if plugin is None or not plugin.ready:
        return False
    assert plugin.local_mode is not None, (
        "a ready Tier 2 plugin always has local_mode set (see _handle_plugin_message)"
    )
    return plugin.local_mode is False


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
    # Acknowledges the client-locality gate (PROTOCOL.md §2/§7): the
    # frontend sets this once the user has confirmed opening Photoshop on
    # a machine other than the one they're browsing from.
    client_remote_ok = bool(body.get("client_remote_ok", False))
    # PROTOCOL.md §6b "Edit-original option": only meaningful for
    # `origin_kind: "load_psd"` (menu.js only ever sends it for a
    # PhotoshopLoadPSD node's own widget value) -- parsed unconditionally
    # here, gated to that origin_kind by the route handler below, so a
    # stray/misapplied flag on any other origin is simply ignored rather
    # than rejected.
    edit_in_place = bool(body.get("edit_in_place", False))

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
            "client_remote_ok": client_remote_ok,
            "edit_in_place": edit_in_place,
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


def _open_source_image(
    context: CpsbContext, fields: dict[str, Any]
) -> tuple[Image.Image | None, web.Response | None]:
    """Resolve and decode the request's source image.

    Returns ``(image, None)`` on success or ``(None, response)`` with a
    ready-to-return 404 on failure. Factored out so both the early
    same-vs-changed-image hash check and the later handoff-creation step
    below can resolve the same image without duplicating the path-
    resolution/decode-error handling.
    """
    source_path = _resolve_source_path(
        context, fields["filename"], fields["subfolder"], fields["type"]
    )
    if source_path is None or not source_path.is_file():
        return None, _error(404, f"Source image not found: {fields['filename']}")
    try:
        image = Image.open(source_path)
        image.load()
    except (OSError, ValueError) as exc:
        return None, _error(404, f"Could not read source image: {exc}")
    return image, None


@dataclass
class ResolvedSource:
    """Result of resolving a ``POST /cpsb/open`` request's source file.

    ``thumbnail_image`` is what ``HandoffManager.create`` downsizes into
    ``orig_thumb.png`` (PROTOCOL.md §1) -- always present on success, for
    every origin kind. ``raw_psd_bytes`` is populated only for a psd-native
    source (``origin_kind: "load_psd"``, PROTOCOL.md §2): when set, the
    route copies it verbatim as ``source.psd`` instead of calling
    :func:`cpsb.psd_io.write_psd` on the decoded pixels, so the handoff
    keeps the user's actual layers rather than flattening them away.
    ``original_path`` mirrors ``raw_psd_bytes`` (populated for the same
    psd-native case, alongside it) -- the resolved absolute path the bytes
    were read from, reused verbatim as the edit target when the PROTOCOL.md
    §6b ``edit_in_place`` option is requested, so it never needs re-resolving.
    """

    thumbnail_image: Image.Image
    source_hash: str
    raw_psd_bytes: bytes | None = None
    original_path: Path | None = None


def _placeholder_thumbnail_source() -> Image.Image:
    """A small neutral image standing in for a psd-native thumbnail source.

    Used only when the psd-native flatten below fails (PROTOCOL.md §2:
    "if flatten fails, a neutral placeholder thumb -- never fail the open
    for a thumbnail") -- the raw bytes that actually become ``source.psd``
    are unaffected either way.
    """
    return Image.new("RGB", _PLACEHOLDER_THUMBNAIL_SIZE, _PLACEHOLDER_THUMBNAIL_COLOR)


def _open_psd_native_source(source_path: Path) -> tuple[bytes, Image.Image]:
    """Raw bytes + a best-effort thumbnail source for a psd-native open.

    Reuses :func:`cpsb.psd_io.read_edited_psd` for the flatten (PROTOCOL.md
    §2: "orig_thumb from the flatten (reuse psd_io)") -- the identical
    embedded-composite-then-recomposite logic already used for the Tier 1
    edit-ingest path (:mod:`cpsb.watcher`), just applied here to the file
    being copied in rather than one coming back from Photoshop. A flatten
    failure never fails the open itself: it falls back to a neutral
    placeholder image so ``HandoffManager.create``'s thumbnail step always
    has something to downsize, while the raw bytes -- the only thing that
    actually becomes ``source.psd`` -- are read and returned regardless of
    whether the flatten succeeded.
    """
    raw_bytes = source_path.read_bytes()
    try:
        image, _fidelity = read_edited_psd(source_path)
    except Exception:
        # Deliberately broad: an unreadable or exotic PSD must not turn
        # into a failed open over a thumbnail (PROTOCOL.md §2).
        logger.warning(
            "%s: could not flatten for a thumbnail; using a placeholder",
            source_path,
            exc_info=True,
        )
        image = _placeholder_thumbnail_source()
    return raw_bytes, image


def _resolve_psd_native_source(
    context: CpsbContext, fields: dict[str, Any]
) -> tuple[ResolvedSource | None, web.Response | None]:
    """:class:`ResolvedSource` for an ``origin_kind: "load_psd"`` open (PROTOCOL.md §2).

    Returns ``(resolved, None)`` on success or ``(None, response)`` with a
    ready-to-return error: **404** if the file doesn't exist, **400** if it
    exists but isn't a ``.psd``/``.psb`` (checked by extension, matching
    :class:`~cpsb.load_psd.PhotoshopLoadPSD`'s own combo filter -- neither
    this route nor that node sniffs file content).
    """
    source_path = _resolve_source_path(
        context, fields["filename"], fields["subfolder"], fields["type"]
    )
    if source_path is None or not source_path.is_file():
        return None, _error(404, f"Source PSD not found: {fields['filename']}")
    if source_path.suffix.lower() not in _PSD_NATIVE_EXTENSIONS:
        return None, _error(400, f"Not a .psd/.psb file: {fields['filename']}")

    raw_bytes, thumbnail_image = _open_psd_native_source(source_path)
    source_hash = hashlib.sha256(raw_bytes).hexdigest()
    return (
        ResolvedSource(
            thumbnail_image=thumbnail_image,
            source_hash=source_hash,
            raw_psd_bytes=raw_bytes,
            original_path=source_path,
        ),
        None,
    )


def _resolve_source(
    context: CpsbContext, fields: dict[str, Any]
) -> tuple[ResolvedSource | None, web.Response | None]:
    """:class:`ResolvedSource` for any ``origin_kind`` (PROTOCOL.md §1/§2/§6b).

    Dispatches to the psd-native path (raw-bytes identity, verbatim copy)
    for ``origin_kind: "load_psd"``; every other origin keeps the existing
    behavior -- decode via Pillow, hash the normalized PNG encoding
    (:func:`cpsb.handoff.compute_source_hash`), later re-encoded as a flat
    PSD by :func:`cpsb.psd_io.write_psd`. A single entry point here is what
    lets the 409-vs-auto-supersede comparison and the final handoff-creation
    step (both in :func:`open_handoff_route`) stay origin-agnostic.
    """
    if fields["origin_kind"] == "load_psd":
        return _resolve_psd_native_source(context, fields)
    image, error_response = _open_source_image(context, fields)
    if error_response is not None:
        return None, error_response
    return ResolvedSource(thumbnail_image=image, source_hash=compute_source_hash(image)), None


def _psd_path_for_handoff(manager: HandoffManager, meta: HandoffMeta) -> Path:
    """The path Photoshop should open (and the watcher should watch) for *meta*.

    PROTOCOL.md §6b: an ``edit_in_place`` handoff's edit target IS the
    user's own file (``meta.original_path``) -- it never has a managed
    ``source.psd`` copy to fall back to. Every other handoff keeps opening
    the managed copy exactly as before this option existed.
    """
    if meta.edit_in_place:
        assert meta.original_path is not None, (
            "an edit_in_place handoff always records original_path (see "
            "open_handoff_route's creation branch, the only place edit_in_place is set)"
        )
        return Path(meta.original_path)
    return manager.handoff_dir(meta.handoff_id) / "source.psd"


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

    resolved: ResolvedSource | None = None
    supersede_existing = False

    if mode == "new" and existing is not None:
        # The incoming source must be resolved and hashed BEFORE deciding
        # 409 vs. proceed: a re-open of the SAME source is the genuine
        # conflict the "Edit Original / Start Fresh" chooser exists for,
        # but upstream regenerating the image under an unchanged filename
        # (the common case for counter-based or fixed-name SaveImage/
        # PreviewImage nodes) must not block on a stale handoff the user
        # would just dismiss via "Start Fresh" anyway -- auto-supersede
        # instead and proceed as new (mirrors the bridge node's identical
        # source_hash rule, PROTOCOL.md §6). A legacy handoff with no
        # recorded source_hash is treated as matching -- same documented
        # choice as the bridge node. `_resolve_source` picks the raw-bytes
        # (psd-native) vs. decoded-PNG hash scheme per `origin_kind`
        # (PROTOCOL.md §1/§2), so this comparison is correct either way.
        resolved, error_response = _resolve_source(context, fields)
        if error_response is not None:
            return error_response
        if existing.source_hash is None or existing.source_hash == resolved.source_hash:
            return web.json_response(
                {
                    "error": "An edit is already in progress for this node",
                    "existing_handoff_id": existing.handoff_id,
                },
                status=409,
            )
        # Actually superseding is deferred until after the tier-availability
        # check below, so a 503 never leaves the node with no active
        # handoff at all (mirrors mode:"fresh"'s existing ordering).
        supersede_existing = True

    tier2 = tier2_connected(request.app)
    if not tier2 and not tier1_status().available:
        return _error(
            503,
            "Neither Photoshop (Tier 1) nor the Photoshop panel plugin (Tier 2) is available.",
            tier1_available=False,
            tier2_connected=False,
        )

    # Client-locality gate (PROTOCOL.md §2/§7, amended). Relevant whenever
    # this request would open on THIS machine -- the Tier 1 path always, and
    # the Tier 2 path too unless the connected plugin is in REMOTE mode (see
    # _tier2_bypasses_locality_gate). Placed here, after the 409/503 checks
    # above have settled that an open will truly be attempted, but BEFORE
    # any handoff is created, any file is written, or the pending supersede
    # (explicit `mode:"fresh"`, or the auto-supersede-on-changed-source
    # decided above) is actually applied below -- a 428 must leave no side
    # effects.
    if (
        not _tier2_bypasses_locality_gate(request.app)
        and not fields["client_remote_ok"]
        and not is_request_local(request)
    ):
        server_name = platform.node()
        return _error(
            428,
            f"Photoshop will open on {server_name}, not this computer",
            reason="client_remote",
            server_name=server_name,
        )

    if mode == "original":
        if existing is None:
            return _error(404, "No active handoff for this node")
        psd_path = _psd_path_for_handoff(manager, existing)
        return await _open_and_respond(request, context, manager, existing, psd_path)

    if supersede_existing or (mode == "fresh" and existing is not None):
        manager.supersede(existing.handoff_id)
        # PROTOCOL.md §6b: an edit_in_place handoff being replaced here must
        # stop being watched -- it's a no-op (and harmless) for every other
        # handoff, since unwatch_original() no-ops for an id that was never
        # registered in the first place.
        watcher = _watcher(request)
        if watcher is not None:
            watcher.unwatch_original(existing.handoff_id)

    if resolved is None:
        resolved, error_response = _resolve_source(context, fields)
        if error_response is not None:
            return error_response

    # PROTOCOL.md §6b "Edit-original option": only a load_psd open honors
    # the flag -- `_resolve_source` guarantees `resolved.original_path` is
    # set whenever origin_kind is load_psd, so this never reads a stale/
    # unset value.
    edit_in_place = fields["edit_in_place"] and fields["origin_kind"] == "load_psd"
    original_path = str(resolved.original_path.resolve()) if edit_in_place else None

    meta = manager.create(
        origin_node_id=origin_node_id,
        origin_kind=fields["origin_kind"],
        workflow_name=fields["workflow_name"],
        source=SourceRef(
            filename=fields["filename"], subfolder=fields["subfolder"], type=fields["type"]
        ),
        original_image=resolved.thumbnail_image,
        source_hash=resolved.source_hash,
        edit_in_place=edit_in_place,
        original_path=original_path,
    )
    if edit_in_place:
        # The user's own file IS the edit target -- never copied, never
        # written to by this package at all (PROTOCOL.md §6b). orig_thumb.png
        # was still written by manager.create() above, from the §4 flatten
        # of that same file, so the gallery keeps working unchanged.
        psd_path = _psd_path_for_handoff(manager, meta)
        watcher = _watcher(request)
        if watcher is not None:
            watcher.watch_original(meta.handoff_id, psd_path)
    else:
        psd_path = manager.handoff_dir(meta.handoff_id) / "source.psd"
        if resolved.raw_psd_bytes is not None:
            # psd-native (PROTOCOL.md §2): copy the user's own layered file
            # verbatim -- never write_psd/frompil, which would flatten it.
            psd_path.parent.mkdir(parents=True, exist_ok=True)
            psd_path.write_bytes(resolved.raw_psd_bytes)
        else:
            write_psd(psd_path, _normalize_for_psd_write(resolved.thumbnail_image))
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

    # meta was fetched before ingest_edit(), but managed_dir is set once at
    # creation and never changes afterward, so it is still authoritative.
    subfolder = f"{manager.managed_dir_for(meta)}/{handoff_id}"
    return web.json_response(
        {"ok": True, "filename": edit.filename, "subfolder": subfolder, "type": "input"}
    )


# -- GET /cpsb/file/{handoff_id} ----------------------------------------------


@routes.get("/cpsb/file/{handoff_id}")
async def file_route(request: web.Request) -> web.Response:
    manager = _manager(request)
    handoff_id = request.match_info["handoff_id"]
    meta = manager.get(handoff_id)
    if meta is None or meta.status not in ACTIVE_STATUSES:
        return _error(404, "Unknown or inactive handoff_id")
    # PROTOCOL.md §6b: an edit_in_place handoff has no source.psd copy at
    # all -- Tier 2 remote-mode download must serve the user's own file.
    psd_path = _psd_path_for_handoff(manager, meta)
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
    # PROTOCOL.md §6b: stop watching an edit_in_place handoff's original file
    # once cancelled -- a no-op for every other handoff (never registered).
    watcher = _watcher(request)
    if watcher is not None:
        watcher.unwatch_original(handoff_id)
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
    # PROTOCOL.md §6b: same unwatch as cancel_route -- see its comment.
    watcher = _watcher(request)
    if watcher is not None:
        watcher.unwatch_original(handoff_id)
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
            "server_version": _SERVER_VERSION,
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
