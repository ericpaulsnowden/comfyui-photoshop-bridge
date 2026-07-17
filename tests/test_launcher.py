"""Launcher: platform discovery/launch logic with everything external faked.

No real process is ever launched: ``subprocess.run``/``Popen`` and
``platform.system``/``release`` are monkeypatched, and Windows registry
access goes through the launcher's injectable ``winreg_module`` parameter.
"""

from __future__ import annotations

import os
import subprocess
import types
from pathlib import Path

import pytest

from cpsb import launcher


class FakeCompleted:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class RunRecorder:
    """Scriptable ``subprocess.run`` replacement keyed on argv[0:2]."""

    def __init__(self, script) -> None:
        self.script = script
        self.calls: list[list[str]] = []

    def __call__(self, cmd, **kwargs) -> FakeCompleted:
        self.calls.append(list(cmd))
        return self.script(cmd)


class PopenRecorder:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, cmd, **kwargs) -> types.SimpleNamespace:
        self.calls.append(list(cmd))
        return types.SimpleNamespace(pid=4242)


@pytest.fixture
def psd_path(tmp_path) -> Path:
    path = tmp_path / "source.psd"
    path.write_bytes(b"8BPS")
    return path


class TestOverride:
    def test_override_used_verbatim(self, monkeypatch, psd_path):
        popen = PopenRecorder()
        monkeypatch.setattr(subprocess, "Popen", popen)
        result = launcher.launch_photoshop(psd_path, "/custom/Photoshop")
        assert result.ok
        assert popen.calls == [["/custom/Photoshop", str(psd_path)]]

    def test_override_failure_reported(self, monkeypatch, psd_path):
        def exploding_popen(cmd, **kwargs):
            raise OSError("no such file")

        monkeypatch.setattr(subprocess, "Popen", exploding_popen)
        result = launcher.launch_photoshop(psd_path, "/missing/Photoshop")
        assert not result.ok
        assert "no such file" in result.error


class TestMacOS:
    @pytest.fixture(autouse=True)
    def darwin(self, monkeypatch):
        monkeypatch.setattr(launcher.platform, "system", lambda: "Darwin")

    def test_bundle_id_open_success(self, monkeypatch, psd_path):
        run = RunRecorder(lambda cmd: FakeCompleted(0))
        monkeypatch.setattr(subprocess, "run", run)
        result = launcher.launch_photoshop(psd_path)
        assert result.ok
        assert run.calls == [["open", "-b", "com.adobe.Photoshop", str(psd_path)]]

    def test_mdfind_fallback_picks_newest_year(self, monkeypatch, psd_path):
        apps = (
            "/Applications/Adobe Photoshop 2024/Adobe Photoshop 2024.app\n"
            "/Applications/Adobe Photoshop 2026/Adobe Photoshop 2026.app\n"
        )

        def script(cmd):
            if cmd[:2] == ["open", "-b"]:
                return FakeCompleted(1, stderr="Unable to find application")
            if cmd[0] == "mdfind":
                return FakeCompleted(0, stdout=apps)
            if cmd[:2] == ["open", "-a"]:
                return FakeCompleted(0)
            raise AssertionError(f"unexpected command {cmd}")

        run = RunRecorder(script)
        monkeypatch.setattr(subprocess, "run", run)
        result = launcher.launch_photoshop(psd_path)
        assert result.ok
        open_a_call = run.calls[-1]
        assert open_a_call[:2] == ["open", "-a"]
        assert "2026" in open_a_call[2]

    def test_nothing_found_reports_error(self, monkeypatch, psd_path):
        def script(cmd):
            if cmd[:2] == ["open", "-b"]:
                return FakeCompleted(1)
            if cmd[0] == "mdfind":
                return FakeCompleted(0, stdout="")
            raise AssertionError(f"unexpected command {cmd}")

        monkeypatch.setattr(subprocess, "run", RunRecorder(script))
        result = launcher.launch_photoshop(psd_path)
        assert not result.ok
        assert "Photoshop not found" in result.error


class FakeWinreg:
    """Minimal winreg stand-in: nested dicts for keys, tuples for values."""

    HKEY_LOCAL_MACHINE = "HKLM"

    def __init__(self, tree: dict) -> None:
        self.tree = tree

    class _Key:
        def __init__(self, node: dict) -> None:
            self.node = node

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

    def OpenKey(self, parent, path: str):  # winreg API casing
        node = self.tree if parent == self.HKEY_LOCAL_MACHINE else parent.node
        for part in path.split("\\"):
            if not isinstance(node, dict) or part not in node:
                raise OSError(f"key not found: {path}")
            node = node[part]
        return self._Key(node)

    def QueryValueEx(self, key, value_name: str):  # winreg API casing
        values = key.node.get("__values__", {})
        if value_name not in values:
            raise OSError(f"value not found: {value_name}")
        return values[value_name], 1

    def EnumKey(self, key, index: int):  # winreg API casing
        names = [name for name in key.node if name != "__values__"]
        if index >= len(names):
            raise OSError("no more subkeys")
        return names[index]


class TestWindows:
    def test_app_paths_key_preferred(self, monkeypatch, psd_path):
        winreg = FakeWinreg(
            {
                "SOFTWARE": {
                    "Microsoft": {
                        "Windows": {
                            "CurrentVersion": {
                                "App Paths": {
                                    "Photoshop.exe": {
                                        "__values__": {"": r"C:\PS\Photoshop.exe"}
                                    }
                                }
                            }
                        }
                    }
                }
            }
        )
        popen = PopenRecorder()
        monkeypatch.setattr(subprocess, "Popen", popen)
        result = launcher._launch_windows(psd_path, winreg)
        assert result.ok
        assert popen.calls == [[r"C:\PS\Photoshop.exe", str(psd_path)]]

    def test_adobe_key_enumeration_picks_numerically_newest(self, monkeypatch, psd_path):
        winreg = FakeWinreg(
            {
                "SOFTWARE": {
                    "Adobe": {
                        "Photoshop": {
                            "90.0": {"__values__": {"ApplicationPath": r"C:\Adobe\PS90"}},
                            "140.0": {"__values__": {"ApplicationPath": r"C:\Adobe\PS140"}},
                        }
                    }
                }
            }
        )
        popen = PopenRecorder()
        monkeypatch.setattr(subprocess, "Popen", popen)
        result = launcher._launch_windows(psd_path, winreg)
        assert result.ok
        # Numeric ordering: 140.0 beats 90.0 (string sort would invert this).
        assert popen.calls[0][0] == str(Path(r"C:\Adobe\PS140") / "Photoshop.exe")

    def test_startfile_last_resort_carries_warning(self, monkeypatch, psd_path):
        winreg = FakeWinreg({})
        opened: list[str] = []
        monkeypatch.setattr(os, "startfile", opened.append, raising=False)
        result = launcher._launch_windows(psd_path, winreg)
        assert result.ok
        assert result.warning is not None
        assert "file association" in result.warning
        assert opened == [str(psd_path)]

    def test_no_startfile_available_reports_not_found(self, monkeypatch, psd_path):
        winreg = FakeWinreg({})
        monkeypatch.delattr(os, "startfile", raising=False)
        result = launcher._launch_windows(psd_path, winreg)
        assert not result.ok
        assert "Photoshop not found" in result.error


class TestTier1Gating:
    def test_macos_always_available(self, monkeypatch):
        monkeypatch.setattr(launcher.platform, "system", lambda: "Darwin")
        status = launcher.tier1_status()
        assert status.available
        assert status.reason is None

    def test_windows_always_available(self, monkeypatch):
        monkeypatch.setattr(launcher.platform, "system", lambda: "Windows")
        assert launcher.tier1_status().available

    def test_docker_detected(self, monkeypatch, tmp_path):
        sentinel = tmp_path / "dockerenv"
        sentinel.touch()
        monkeypatch.setattr(launcher.platform, "system", lambda: "Linux")
        monkeypatch.setattr(launcher.platform, "release", lambda: "6.1.0-generic")
        monkeypatch.setattr(launcher, "_DOCKER_SENTINEL", sentinel)
        status = launcher.tier1_status()
        assert not status.available
        assert status.reason == "docker"

    def test_wsl_detected(self, monkeypatch, tmp_path):
        monkeypatch.setattr(launcher.platform, "system", lambda: "Linux")
        monkeypatch.setattr(
            launcher.platform, "release", lambda: "5.15.90.1-microsoft-standard-WSL2"
        )
        monkeypatch.setattr(launcher, "_DOCKER_SENTINEL", tmp_path / "absent")
        status = launcher.tier1_status()
        assert not status.available
        assert status.reason == "wsl"

    def test_headless_linux_detected(self, monkeypatch, tmp_path):
        monkeypatch.setattr(launcher.platform, "system", lambda: "Linux")
        monkeypatch.setattr(launcher.platform, "release", lambda: "6.1.0-generic")
        monkeypatch.setattr(launcher, "_DOCKER_SENTINEL", tmp_path / "absent")
        monkeypatch.delenv("DISPLAY", raising=False)
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        status = launcher.tier1_status()
        assert not status.available
        assert status.reason == "headless-server"

    def test_linux_with_display_available(self, monkeypatch, tmp_path):
        monkeypatch.setattr(launcher.platform, "system", lambda: "Linux")
        monkeypatch.setattr(launcher.platform, "release", lambda: "6.1.0-generic")
        monkeypatch.setattr(launcher, "_DOCKER_SENTINEL", tmp_path / "absent")
        monkeypatch.setenv("DISPLAY", ":0")
        assert launcher.tier1_status().available

    def test_unsupported_platform_errors(self, monkeypatch, tmp_path):
        monkeypatch.setattr(launcher.platform, "system", lambda: "Linux")
        result = launcher.launch_photoshop(tmp_path / "x.psd")
        assert not result.ok
        assert "Photoshop not found" in result.error
