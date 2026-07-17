"""Single source of truth for this backend's own semver string (PROTOCOL.md §9).

Backend, frontend JS (``web/cpsb/version.js``), and the Photoshop UXP plugin
(``photoshop_plugin/manifest.json``) each carry their own version string,
exchanged where relevant (the plugin's ``hello``/``hello_ack`` handshake,
PROTOCOL.md §3; ``GET /cpsb/status``'s ``server_version``, PROTOCOL.md §2).
``cpsb.routes`` imports :data:`__version__` from here rather than defining
its own literal, so there is exactly one place this package's release number
lives. ``scripts/bump_version.py`` rewrites the string below (plus
``pyproject.toml``, the plugin manifest, and the frontend's own
``version.js``) in lockstep; keep this file to the single assignment below
so that script's regex can find and replace it reliably.
"""

from __future__ import annotations

__version__ = "0.5.7"
