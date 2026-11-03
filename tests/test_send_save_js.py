"""Manual Send must never deliver stale bytes (review item 6, docs/PROTOCOL.md §6d).

The bug: for an Annotate (``wants_layered_psd``) handoff, ``deliverEdit`` in
``photoshop_plugin/handoffs.js`` read the sandbox PSD straight off disk without
saving the open document first, so strokes painted since the last Cmd/Ctrl+S
never left Photoshop while the panel reported "sent". The fix saves an unsaved
document (inside ``executeAsModal``) before a MANUAL Send reads it, suppresses
the save listener's echo of that save so one Send delivers once, and refuses to
send anything when the save fails.

The plugin is plain CommonJS that only runs inside UXP, so the behavior is
pinned by loading the REAL ``handoffs.js`` / ``saveListener.js`` / ``log.js``
into a Node ``vm`` with fakes for Photoshop, UXP storage, the websocket
connection, the exporter and the uploader (``tests/js/plugin_vm.js``). Every
scenario lives in ``tests/js/send_save_scenarios.js`` and is reported here as
its own pytest case. The call-site test is a plain source scan.

What this can NOT prove (only real Photoshop can): that ``Document.saved``
flips on a painted stroke and back on a save, that ``Document.save()`` shows no
dialog, and when Photoshop delivers the ``save`` notification relative to the
save promise -- see the manual steps in the PR notes / PROTOCOL.md §6d.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO_ROOT / "photoshop_plugin"
SCENARIOS_JS = REPO_ROOT / "tests" / "js" / "send_save_scenarios.js"

NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node (JS runtime) not installed")


def _scenario_names() -> list[str]:
    """Scenario names, discovered from the JS module so the two cannot drift."""
    if NODE is None:
        return ["node_not_installed"]
    result = subprocess.run(
        [
            NODE,
            "-e",
            "process.stdout.write(JSON.stringify("
            f"Object.keys(require({json.dumps(str(SCENARIOS_JS))}).scenarios)))",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return json.loads(result.stdout)


SCENARIO_NAMES = _scenario_names()


@pytest.fixture(scope="module")
def scenario_results() -> dict[str, dict]:
    """Runs every scenario once under Node and returns ``{name: {ok, error?}}``."""
    result = subprocess.run(
        [NODE, str(SCENARIOS_JS)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, f"node exited {result.returncode}:\n{result.stderr}"
    return json.loads(result.stdout)


@pytest.mark.parametrize("name", SCENARIO_NAMES)
def test_scenario(scenario_results: dict[str, dict], name: str) -> None:
    outcome = scenario_results[name]
    assert outcome["ok"], outcome.get("error", "scenario failed")


def test_every_scenario_ran(scenario_results: dict[str, dict]) -> None:
    assert sorted(scenario_results) == sorted(SCENARIO_NAMES)


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
