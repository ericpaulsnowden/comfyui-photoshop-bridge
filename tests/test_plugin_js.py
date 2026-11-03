"""Frontend + Photoshop-plugin JavaScript tests, run under Node's own test runner.

The JS under ``web/cpsb`` (ES modules) and ``photoshop_plugin`` (CommonJS) is
loaded UNMODIFIED into a ``vm`` context with stubbed imports (see
``tests/js/harness.mjs``) and driven the way ComfyUI / Photoshop drive it. Each
``tests/js/*.test.mjs`` file is one pytest case here; on failure the Node
runner's output (which names the failing JS test and its assertion) is the
assertion message.

Needs ``node`` >= 20 on PATH (global ``CustomEvent``, stable ``vm`` modules
behind ``--experimental-vm-modules``); skipped otherwise.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

JS_DIR = Path(__file__).resolve().parent / "js"
NODE = shutil.which("node")


def _node_major() -> int:
    if NODE is None:
        return 0
    try:
        out = subprocess.run(
            [NODE, "--version"], capture_output=True, text=True, timeout=30, check=False
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return 0
    match = re.match(r"v(\d+)\.", out.strip())
    return int(match.group(1)) if match else 0


pytestmark = pytest.mark.skipif(_node_major() < 20, reason="needs node >= 20 on PATH")


@pytest.mark.parametrize(
    "script",
    sorted(JS_DIR.glob("*.test.mjs")),
    ids=lambda path: path.name,
)
def test_js_suite(script: Path) -> None:
    env = {**os.environ, "NODE_NO_WARNINGS": "1"}
    env.pop("CPSB_JS_ROOT", None)  # always test THIS checkout
    result = subprocess.run(
        [NODE, "--experimental-vm-modules", str(script)],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        check=False,
    )
    assert result.returncode == 0, f"{script.name} failed:\n{result.stdout}\n{result.stderr}"
