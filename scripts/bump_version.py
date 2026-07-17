#!/usr/bin/env python3
"""Bump comfyui-photoshop-bridge's version across every file that carries it.

Usage::

    python scripts/bump_version.py               # bump the patch version
    python scripts/bump_version.py --minor        # bump minor, reset patch
    python scripts/bump_version.py --major        # bump major, reset minor+patch
    python scripts/bump_version.py --dry-run       # print what would change, write nothing

Stdlib-only (no third-party dependencies, including no TOML parser -- this
project's own ``requires-python = ">=3.10"`` predates :mod:`tomllib`, which
only ships from 3.11, so ``pyproject.toml``'s version is located with a
regex rather than a real TOML parse). Safe to run outside this package's own
venv; only needs a Python 3.10+ interpreter.

Targets (PROTOCOL.md §9 -- backend, frontend, and the Photoshop UXP plugin
each carry their own semver string, kept in lockstep by this script even
though the runtime protocol itself only requires them to be *compatible*,
not identical):

* ``cpsb/version.py``               -- ``__version__ = "X.Y.Z"``      (backend)
* ``pyproject.toml``                -- ``[project]`` ``version = "X.Y.Z"``
* ``photoshop_plugin/manifest.json``-- ``"version": "X.Y.Z"``         (UXP plugin)
* ``web/cpsb/version.js``           -- ``FRONTEND_VERSION = 'X.Y.Z'`` (frontend)

Every target is located with a small, anchored regex and is required to
match EXACTLY ONCE; if any file is missing, unreadable, or its version
string can't be found unambiguously, the whole run refuses and writes
nothing to any file ("dirty parse") -- a partially-applied bump (three
files moved, one left behind) is worse than no bump at all, since it
silently desynchronizes the very single-source-of-truth this script exists
to maintain. The four current versions are also required to already agree
with each other before bumping, for the same reason: if they've drifted
(e.g. a previous bump crashed partway, or someone hand-edited one file),
this refuses rather than guessing which one is "right".

On "idempotent": ``--dry-run`` is genuinely idempotent -- it only reads, so
running it any number of times in a row reports the identical (old, new)
pair every time, with zero side effects. An actual bump is not, and should
not be, idempotent in that same sense: running ``--patch`` twice in a row is
*supposed* to move 0.2.0 -> 0.2.1 -> 0.2.2, not silently no-op the second
time -- that is simply what "bump" means. What IS guaranteed on every real
run is that all four files move together and land on the exact same new
version, every time, by construction.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import NamedTuple

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Bare X.Y.Z, reused inside every file-specific pattern below.
_VERSION = r"\d+\.\d+\.\d+"


class _Target(NamedTuple):
    """One version-carrying file: its path, a label for messages, and the
    regex that locates the version string inside it.

    Every pattern below captures exactly three groups -- ``(prefix)
    (version)(suffix)`` -- so :func:`_write_new_version` can substitute
    group 2 alone and leave the surrounding file content (quoting style,
    key name, trailing punctuation) completely untouched.
    """

    path: Path
    pattern: re.Pattern[str]
    label: str


def _targets(repo_root: Path) -> list[_Target]:
    return [
        _Target(
            repo_root / "cpsb" / "version.py",
            re.compile(rf'(__version__\s*=\s*")({_VERSION})(")'),
            "cpsb/version.py",
        ),
        _Target(
            repo_root / "pyproject.toml",
            # Deliberately no trailing `\s*$`: `\s` matches newlines too, so
            # a trailing anchor outside the three capture groups would get
            # silently swallowed by subn()'s whole-match replacement below
            # (verified the hard way -- an earlier version of this pattern
            # ate the blank line after `version = "..."` on every write).
            # `(?m)^` alone is already precise enough: it only matches a
            # `version = "X.Y.Z"` that starts its own line.
            re.compile(rf'(?m)^(version\s*=\s*")({_VERSION})(")'),
            "pyproject.toml",
        ),
        _Target(
            repo_root / "photoshop_plugin" / "manifest.json",
            re.compile(rf'("version"\s*:\s*")({_VERSION})(")'),
            "photoshop_plugin/manifest.json",
        ),
        _Target(
            repo_root / "web" / "cpsb" / "version.js",
            re.compile(rf"(FRONTEND_VERSION\s*=\s*')({_VERSION})(')"),
            "web/cpsb/version.js",
        ),
    ]


def _bump(version: str, part: str) -> str:
    """*version* with *part* ("major"|"minor"|"patch") bumped, semver-style.

    Bumping "major" resets minor and patch to 0; bumping "minor" resets
    patch to 0; "patch" alone leaves the other two untouched.
    """
    major, minor, patch = (int(piece) for piece in version.split("."))
    if part == "major":
        major, minor, patch = major + 1, 0, 0
    elif part == "minor":
        minor, patch = minor + 1, 0
    else:
        patch += 1
    return f"{major}.{minor}.{patch}"


def _read_current_version(target: _Target) -> str:
    """The version string *target* currently holds.

    Raises:
        SystemExit: *target* is missing, or its pattern matches zero or
            more than once in the file -- refuses to guess through a
            "dirty parse" (a hand-edited or unexpected file layout is
            exactly the case where silently picking the first/last match
            could bump the wrong string, or leave the file inconsistent).
    """
    if not target.path.is_file():
        raise SystemExit(f"refusing to bump: {target.label} does not exist")
    text = target.path.read_text(encoding="utf-8")
    matches = list(target.pattern.finditer(text))
    if len(matches) != 1:
        raise SystemExit(
            f"refusing to bump: expected exactly one version string in "
            f"{target.label}, found {len(matches)}"
        )
    return matches[0].group(2)


def _write_new_version(target: _Target, new_version: str) -> None:
    text = target.path.read_text(encoding="utf-8")
    new_text, count = target.pattern.subn(
        lambda m: m.group(1) + new_version + m.group(3), text
    )
    if count != 1:
        raise SystemExit(
            f"refusing to bump: expected exactly one version string in "
            f"{target.label}, found {count} while writing"
        )
    target.path.write_text(new_text, encoding="utf-8")


def bump_all(repo_root: Path, part: str, dry_run: bool = False) -> tuple[str, str]:
    """Bump every version-carrying file under *repo_root* in lockstep.

    Args:
        repo_root: The repository root (parent of ``cpsb/``, ``web/``,
            etc.) -- injectable so tests can point this at a throwaway copy
            instead of the real repository.
        part: "major", "minor", or "patch".
        dry_run: When True, computes and returns the ``(old, new)`` version
            pair without writing any file.

    Returns:
        ``(old_version, new_version)``.

    Raises:
        SystemExit: any target fails to parse (see
            :func:`_read_current_version`), or the targets don't all
            currently agree on the same version.
    """
    targets = _targets(repo_root)
    current_versions = {target.label: _read_current_version(target) for target in targets}
    distinct = set(current_versions.values())
    if len(distinct) != 1:
        details = ", ".join(f"{label}={version}" for label, version in current_versions.items())
        raise SystemExit(f"refusing to bump: versions have drifted out of sync ({details})")

    old_version = distinct.pop()
    new_version = _bump(old_version, part)

    if not dry_run:
        for target in targets:
            _write_new_version(target, new_version)

    return old_version, new_version


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Bump comfyui-photoshop-bridge's version across every file that carries it."
    )
    bump_part = parser.add_mutually_exclusive_group()
    bump_part.add_argument(
        "--minor",
        action="store_const",
        dest="part",
        const="minor",
        help="Bump the minor version (resets patch to 0). Default: bump the patch version.",
    )
    bump_part.add_argument(
        "--major",
        action="store_const",
        dest="part",
        const="major",
        help="Bump the major version (resets minor and patch to 0).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would change without writing any file.",
    )
    parser.set_defaults(part="patch")
    args = parser.parse_args(argv)

    old_version, new_version = bump_all(REPO_ROOT, args.part, dry_run=args.dry_run)

    verb = "Would bump" if args.dry_run else "Bumped"
    print(f"{verb} {old_version} -> {new_version} ({args.part})")
    for target in _targets(REPO_ROOT):
        print(f"  {target.label}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
