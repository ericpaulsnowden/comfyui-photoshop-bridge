"""ComfyUI entry point for comfyui-photoshop-bridge.

This is the only file in the pack that touches ComfyUI's own modules
(``server``, ``folder_paths``). It builds the real
:class:`~cpsb.context.CpsbContext`, wires up the handoff manager, watcher,
HTTP routes, and the Photoshop Bridge / Load PSD nodes, and exposes the
standard ``NODE_CLASS_MAPPINGS`` / ``WEB_DIRECTORY`` module attributes
ComfyUI's loader looks for. Everything under ``cpsb/`` stays importable (and
tested) without ComfyUI -- see ``cpsb/context.py``.
"""

import logging

try:
    from .cpsb import actions as _cpsb_actions
    from .cpsb import annotate as _cpsb_annotate
    from .cpsb import compose_psd as _cpsb_compose_psd
    from .cpsb import live as _cpsb_live
    from .cpsb import load_psd as _cpsb_load_psd
    from .cpsb import nodes as _cpsb_nodes
except ImportError:
    # Imported without package context (e.g. pytest's rootdir Package setup,
    # or tooling that loads node-pack entry files flat). ComfyUI itself always
    # loads this file as a package, taking the relative-import branch above.
    from cpsb import actions as _cpsb_actions
    from cpsb import annotate as _cpsb_annotate
    from cpsb import compose_psd as _cpsb_compose_psd
    from cpsb import live as _cpsb_live
    from cpsb import load_psd as _cpsb_load_psd
    from cpsb import nodes as _cpsb_nodes

logger = logging.getLogger("cpsb")

# Display names only (PROTOCOL.md §6/§6b). The class ids "PhotoshopBridge"
# and "PhotoshopLoadPSD" below MUST NOT change: saved workflows reference
# nodes by this id, and renaming either would silently break every workflow
# that already has that node in it. "PhotoshopLoadPSD" (not the shorter
# "LoadPSD") specifically to avoid colliding with other packs' same-named
# node (PROTOCOL.md §6b).
NODE_CLASS_MAPPINGS = {
    "PhotoshopBridge": _cpsb_nodes.PhotoshopBridge,
    "PhotoshopLoadPSD": _cpsb_load_psd.PhotoshopLoadPSD,
    "PhotoshopComposePSD": _cpsb_compose_psd.PhotoshopComposePSD,
    "PhotoshopAnnotate": _cpsb_annotate.PhotoshopAnnotate,
    "PhotoshopAction": _cpsb_actions.PhotoshopAction,
    "PhotoshopLiveCanvas": _cpsb_live.PhotoshopLiveCanvas,
    "PhotoshopLivePrompt": _cpsb_live.PhotoshopLivePrompt,
    "PhotoshopLiveCreativity": _cpsb_live.PhotoshopLiveCreativity,
    "PhotoshopLivePreview": _cpsb_live.PhotoshopLivePreview,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "PhotoshopBridge": "Edit in Photoshop",
    "PhotoshopLoadPSD": "Load PSD",
    "PhotoshopComposePSD": "Compose Layers to PSD",
    "PhotoshopAnnotate": "Annotate for Edit",
    "PhotoshopAction": "Run Photoshop Action",
    "PhotoshopLiveCanvas": "Photoshop Live Canvas",
    "PhotoshopLivePrompt": "Photoshop Live Prompt",
    "PhotoshopLiveCreativity": "Photoshop Live Creativity",
    "PhotoshopLivePreview": "Photoshop Live Preview",
}

# ComfyUI checks os.path.isdir() on this itself (nodes.py load_custom_node),
# so a missing/not-yet-built web/ folder is tolerated, never a crash.
WEB_DIRECTORY = "./web"

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]

try:
    import folder_paths  # ComfyUI-only module
    from server import PromptServer  # ComfyUI-only module

    _COMFY_AVAILABLE = True
except ImportError:  # Running outside ComfyUI (tests, tooling): skip all wiring.
    _COMFY_AVAILABLE = False
    logger.info("cpsb: ComfyUI not detected; backend wiring skipped")


def _wire_into_comfyui() -> None:
    """Build the real context and attach everything to the running PromptServer.

    Only ever called when ComfyUI is present (see the guard at the bottom of
    this file), which also guarantees this module was loaded as a package, so
    the relative imports below are safe here even though the module itself
    tolerates flat imports.
    """
    from pathlib import Path

    from .cpsb import routes as cpsb_routes
    from .cpsb.context import CpsbContext, load_settings
    from .cpsb.handoff import HandoffManager
    from .cpsb.watcher import CpsbWatcher

    server = PromptServer.instance
    user_dir = Path(folder_paths.get_user_directory())

    def _send_event(event: str, payload: dict) -> None:
        # send_sync is thread-safe (call_soon_threadsafe internally), so the
        # watcher's background threads can emit events directly through it.
        server.send_sync(event, payload)

    context = CpsbContext(
        input_dir=Path(folder_paths.get_input_directory()),
        output_dir=Path(folder_paths.get_output_directory()),
        temp_dir=Path(folder_paths.get_temp_directory()),
        user_dir=user_dir,
        send_event=_send_event,
        settings=load_settings(user_dir),
    )
    manager = HandoffManager(context)

    watcher = CpsbWatcher(context, manager)
    try:
        watcher.start()
    except OSError as exc:
        # E.g. inotify watch limits. Tier 1 save detection degrades, but the
        # routes, node, and Tier 2 all still work -- keep the pack alive.
        logger.warning("cpsb: could not start the input/cpsb watcher: %s", exc)

    # Custom nodes load before PromptServer.add_routes()/run, so adding to
    # the app's router here lands our routes on ComfyUI's own port. We must
    # register both the bare and /api-prefixed paths ourselves, because the
    # frontend's api.fetchApi always calls /api/cpsb/... and registering our
    # own RouteTableDef directly skips ComfyUI's /api mirroring (see
    # cpsb.routes.add_routes_to_app). `watcher` is passed through so the
    # open/cancel/discard handlers can maintain its PROTOCOL.md §6b
    # edit_in_place watch set (cpsb.routes.install's own docstring).
    cpsb_routes.install(server.app, context, manager, watcher)
    cpsb_routes.add_routes_to_app(server.app)

    _cpsb_nodes.configure(context, manager, server.app, server.loop)

    logger.info("cpsb: Photoshop bridge ready (watching %s)", context.cpsb_input_dir)


if _COMFY_AVAILABLE:
    _wire_into_comfyui()
