"""Every path into ``deliverEdit`` keeps the save-before-send dirty check ON.

The behaviour itself is pinned by ``tests/js/send_save.test.mjs`` (run through
``tests/test_plugin_js.py``). This is the plain source scan that needs no Node:
a new caller of ``deliverEdit`` that forgot ``afterNativeSave`` is safe by
default, and only the save listener may opt out (docs/PROTOCOL.md §6d).
"""

from __future__ import annotations

import re
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent / "photoshop_plugin"

# ---- every path into deliverEdit: one fix site, one deliberate opt-out -----

_CALL = re.compile(r"\bdeliverEdit\(([^)]*)\)")


def _deliver_edit_calls() -> dict[str, list[str]]:
    """``{file: [argument text of each deliverEdit(...) call]}``, comments skipped."""
    calls: dict[str, list[str]] = {}
    for path in sorted(PLUGIN_DIR.glob("*.js")):
        if path.name == "handoffs.js":
            continue  # the definition (and its doc comments) live there
        for line in path.read_text(encoding="utf-8").splitlines():
            code = line.split("//", 1)[0]
            stripped = code.strip()
            if stripped.startswith(("*", "/*")):
                continue
            calls.setdefault(path.name, []).extend(m.group(1) for m in _CALL.finditer(code))
    return {name: found for name, found in calls.items() if found}


def test_only_the_save_listener_opts_out_of_save_before_send() -> None:
    """The panel button, the Send command and runAction go through deliverEdit with the
    dirty check ON; only the save listener (a save Photoshop just finished) passes
    ``afterNativeSave: true``. A new caller that forgot the option is safe by default."""
    calls = _deliver_edit_calls()
    assert set(calls) == {"index.js", "panel.js", "runAction.js", "saveListener.js"}
    for name, arguments in calls.items():
        if name == "saveListener.js":
            assert arguments == ["record.handoffId, { afterNativeSave: true }"]
        else:
            assert all("afterNativeSave" not in argument for argument in arguments), name
