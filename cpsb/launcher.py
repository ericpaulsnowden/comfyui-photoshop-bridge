"""Photoshop discovery and OS-level launch for Tier 1 (PROTOCOL.md §7).

Two independent questions live here:

* :func:`tier1_status` -- can this *server process's environment* even
  attempt an OS-level launch at all (it cannot from inside a headless
  Linux container, WSL, or a display-less session)?
* :func:`launch_photoshop` -- given that it can, actually find and launch
  (or focus) Photoshop for a specific PSD path.

Windows registry access goes through an injectable ``winreg``-like module
parameter so the discovery logic is unit-testable on any platform with a
fake registry, never a real one.
"""

from __future__ import annotations

import logging
import os
import platform
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("cpsb")

_SUBPROCESS_TIMEOUT_SECONDS = 15
_YEAR_RE = re.compile(r"(\d{4})")

#: Presence of this file identifies a Docker container (module-level so tests
#: can point it at a temp path instead of the real ``/.dockerenv``).
_DOCKER_SENTINEL = Path("/.dockerenv")

_NOT_FOUND_MESSAGE = (
    "Photoshop not found. Install Photoshop, set the executable path in "
    "settings, or open the file manually."
)


@dataclass
class Tier1Status:
    """Whether this server process's environment permits an OS-level launch."""

    available: bool
    reason: str | None = None  # "headless-server" | "docker" | "wsl" | None


@dataclass
class LaunchResult:
    """Outcome of one attempt to launch or focus Photoshop."""

    ok: bool
    warning: str | None = None
    error: str | None = None


def tier1_status() -> Tier1Status:
    """Environment gating for Tier 1 (PROTOCOL.md §7).

    This does not check whether Photoshop is actually installed -- only
    :func:`launch_photoshop` (an active attempt) can determine that. This
    only rules out server environments where no OS-level launch is even
    conceivable: a Docker container, WSL, or a headless (display-less)
    Linux session. macOS and Windows are always considered available here.
    """
    system = platform.system()
    if system != "Linux":
        return Tier1Status(available=True)
    if _DOCKER_SENTINEL.exists():
        return Tier1Status(available=False, reason="docker")
    if "microsoft" in platform.release().lower():
        return Tier1Status(available=False, reason="wsl")
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return Tier1Status(available=False, reason="headless-server")
    return Tier1Status(available=True)


def launch_photoshop(psd_path: Path, photoshop_path_override: str = "") -> LaunchResult:
    """Launch (or focus) Photoshop on *psd_path*.

    Args:
        psd_path: Absolute path to the handoff's ``source.psd``.
        photoshop_path_override: The ``photoshop_path`` setting. Used
            verbatim when non-empty, taking priority over platform
            discovery (PROTOCOL.md §7: "settings override first").
    """
    if photoshop_path_override:
        return _popen(photoshop_path_override, psd_path)

    system = platform.system()
    if system == "Darwin":
        return _launch_macos(psd_path)
    if system == "Windows":
        return _launch_windows(psd_path)
    return LaunchResult(ok=False, error=_NOT_FOUND_MESSAGE)


def _popen(executable: str, psd_path: Path) -> LaunchResult:
    try:
        subprocess.Popen([executable, str(psd_path)])
    except OSError as exc:
        return LaunchResult(ok=False, error=f"Failed to launch '{executable}': {exc}")
    return LaunchResult(ok=True)


# -- macOS ------------------------------------------------------------------


def _pick_newest_macos_app(app_paths: list[str]) -> str | None:
    """Prefer the highest year/version found in the app bundle path."""
    candidates = [p for p in app_paths if p.strip()]
    if not candidates:
        return None

    def year_key(app_path: str) -> int:
        match = _YEAR_RE.search(app_path)
        return int(match.group(1)) if match else 0

    return max(candidates, key=year_key)


def _launch_macos(psd_path: Path) -> LaunchResult:
    try:
        result = subprocess.run(
            ["open", "-b", "com.adobe.Photoshop", str(psd_path)],
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return LaunchResult(ok=False, error=f"Failed to run 'open': {exc}")
    if result.returncode == 0:
        return LaunchResult(ok=True)

    logger.info(
        "'open -b com.adobe.Photoshop' failed (%s); falling back to mdfind",
        result.stderr.strip(),
    )
    try:
        mdfind_result = subprocess.run(
            ["mdfind", "kMDItemCFBundleIdentifier == 'com.adobe.Photoshop'"],
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return LaunchResult(ok=False, error=f"{_NOT_FOUND_MESSAGE} (mdfind failed: {exc})")

    app_path = _pick_newest_macos_app(mdfind_result.stdout.splitlines())
    if app_path is None:
        return LaunchResult(ok=False, error=_NOT_FOUND_MESSAGE)

    try:
        result = subprocess.run(
            ["open", "-a", app_path, str(psd_path)],
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return LaunchResult(ok=False, error=f"Failed to launch '{app_path}': {exc}")
    if result.returncode == 0:
        return LaunchResult(ok=True)
    return LaunchResult(ok=False, error=result.stderr.strip() or f"'open -a {app_path}' failed")


# -- Windows ------------------------------------------------------------------


def _find_windows_app_path(winreg_module: Any) -> str | None:
    """Read the default value of the ``App Paths\\Photoshop.exe`` key."""
    key_path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\Photoshop.exe"
    try:
        with winreg_module.OpenKey(winreg_module.HKEY_LOCAL_MACHINE, key_path) as key:
            value, _ = winreg_module.QueryValueEx(key, "")
            return value or None
    except OSError:
        return None


def _version_sort_key(version: str) -> tuple[float, str]:
    """Numeric-first sort key for Adobe registry version keys like ``"140.0"``.

    A plain string sort would rank ``"90.0"`` above ``"140.0"``.
    """
    try:
        return (float(version), version)
    except ValueError:
        return (0.0, version)


def _find_windows_adobe_key(winreg_module: Any) -> str | None:
    """Enumerate ``SOFTWARE\\Adobe\\Photoshop\\<ver>`` for ``ApplicationPath``, newest first."""
    base_path = r"SOFTWARE\Adobe\Photoshop"
    candidates: list[tuple[str, str]] = []
    try:
        with winreg_module.OpenKey(winreg_module.HKEY_LOCAL_MACHINE, base_path) as base_key:
            index = 0
            while True:
                try:
                    version = winreg_module.EnumKey(base_key, index)
                except OSError:
                    break
                index += 1
                try:
                    with winreg_module.OpenKey(base_key, version) as version_key:
                        app_path, _ = winreg_module.QueryValueEx(version_key, "ApplicationPath")
                except OSError:
                    continue
                candidates.append((version, str(Path(app_path) / "Photoshop.exe")))
    except OSError:
        return None
    if not candidates:
        return None
    candidates.sort(key=lambda item: _version_sort_key(item[0]), reverse=True)
    return candidates[0][1]


def _launch_windows(psd_path: Path, winreg_module: Any | None = None) -> LaunchResult:
    if winreg_module is None:
        try:
            import winreg as winreg_module  # type: ignore[no-redef]
        except ImportError:
            return LaunchResult(ok=False, error="winreg is only available on Windows")

    exe = _find_windows_app_path(winreg_module) or _find_windows_adobe_key(winreg_module)
    if exe is not None:
        try:
            subprocess.Popen([exe, str(psd_path)])
            return LaunchResult(ok=True)
        except OSError as exc:
            logger.warning("Failed to launch discovered Photoshop at %s: %s", exe, exc)

    return _start_file_fallback(psd_path)


def _start_file_fallback(psd_path: Path) -> LaunchResult:
    """Last resort: the OS's own ``.psd`` file association, per PROTOCOL.md §7."""
    startfile = getattr(os, "startfile", None)
    if startfile is None:
        return LaunchResult(ok=False, error=_NOT_FOUND_MESSAGE)
    try:
        startfile(str(psd_path))
    except OSError as exc:
        return LaunchResult(ok=False, error=f"{_NOT_FOUND_MESSAGE} (os.startfile failed: {exc})")
    return LaunchResult(
        ok=True,
        warning=(
            "Opened via the .psd file association -- if that is not Photoshop, "
            "set the Photoshop executable path in settings."
        ),
    )
