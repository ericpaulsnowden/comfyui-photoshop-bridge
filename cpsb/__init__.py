"""Backend package for comfyui-photoshop-bridge.

Everything here depends only on the injected :class:`~cpsb.context.CpsbContext`
-- never on ComfyUI's ``server`` or ``folder_paths`` modules directly -- so
the package imports and tests cleanly outside ComfyUI. The real wiring lives
in the node pack's top-level ``__init__.py``.

Submodules with heavier optional dependencies (:mod:`cpsb.routes` needs
aiohttp, :mod:`cpsb.watcher` needs watchdog, :mod:`cpsb.nodes` needs both)
are imported explicitly by their consumers rather than re-exported here.
"""

from .context import DEFAULT_SETTINGS, CpsbContext, SettingsStore, load_settings
from .handoff import (
    ACTIVE_STATUSES,
    EditRecord,
    HandoffManager,
    HandoffMeta,
    HandoffNotFoundError,
    SiblingOutput,
    SourceRef,
    WaitOutcome,
    compute_source_hash,
)
from .launcher import LaunchResult, Tier1Status, launch_photoshop, tier1_status
from .psd_io import normalize_to_rgb8, read_edited_psd, write_psd

__version__ = "0.1.0"

__all__ = [
    "ACTIVE_STATUSES",
    "DEFAULT_SETTINGS",
    "CpsbContext",
    "EditRecord",
    "HandoffManager",
    "HandoffMeta",
    "HandoffNotFoundError",
    "LaunchResult",
    "SettingsStore",
    "SiblingOutput",
    "SourceRef",
    "Tier1Status",
    "WaitOutcome",
    "__version__",
    "compute_source_hash",
    "launch_photoshop",
    "load_settings",
    "normalize_to_rgb8",
    "read_edited_psd",
    "tier1_status",
    "write_psd",
]
