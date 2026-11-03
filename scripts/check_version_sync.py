#!/usr/bin/env python3
"""Fail when this pack's version strings have drifted apart.

Usage::

    python scripts/check_version_sync.py

CI runs this on every push and pull request (.github/workflows/ci.yml).

The version is carried by several files (the backend ``version.py``,
``pyproject.toml``, the frontend ``version.js`` and, for the Photoshop and
Premiere bridges, the UXP plugin ``manifest.json``). ``scripts/bump_version.py``
is the one sanctioned way to change them and refuses to start from a drifted
tree; this is the read-only counterpart for CI. It reuses bump_version's own
target list, so the two can never disagree about which files carry the
version -- add a file to ``bump_version._targets`` and it is checked here too.

Exit status: 0 when every file holds the same X.Y.Z, 1 when they disagree or a
file is missing or ambiguous. Stdlib-only, ASCII-only output (a Windows CI
console is not UTF-8), and path-separator-neutral.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent


def _load_bump_version():
    """Import scripts/bump_version.py by path (``scripts/`` is not a package)."""
    spec = importlib.util.spec_from_file_location("bump_version", SCRIPTS_DIR / "bump_version.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def collect_versions(repo_root: Path) -> tuple[dict[str, str], list[str]]:
    """Read the version out of every carrier file under *repo_root*.

    Returns ``(versions, problems)``: ``versions`` maps each file's label to
    the X.Y.Z found in it; ``problems`` lists every file that is missing or
    whose version string cannot be located exactly once.
    """
    versions: dict[str, str] = {}
    problems: list[str] = []
    for target in _load_bump_version()._targets(repo_root):
        if not target.path.is_file():
            problems.append(f"{target.label}: file not found")
            continue
        text = target.path.read_text(encoding="utf-8")
        matches = list(target.pattern.finditer(text))
        if len(matches) != 1:
            problems.append(
                f"{target.label}: expected exactly one version string, found {len(matches)}"
            )
            continue
        versions[target.label] = matches[0].group(2)
    return versions, problems


def main() -> int:
    versions, problems = collect_versions(REPO_ROOT)

    width = max((len(label) for label in versions), default=0)
    for label, version in versions.items():
        print(f"  {label:<{width}}  {version}")

    distinct = sorted(set(versions.values()))
    if len(distinct) > 1:
        problems.append("version strings disagree: " + ", ".join(distinct))

    if problems:
        print("")
        for problem in problems:
            print(f"FAIL: {problem}")
        print(
            "Edit the lagging file(s) by hand so every version matches, then use\n"
            "scripts/bump_version.py for all future bumps (it moves them together)."
        )
        return 1

    print(f"OK: all {len(versions)} version strings agree on {distinct[0]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
