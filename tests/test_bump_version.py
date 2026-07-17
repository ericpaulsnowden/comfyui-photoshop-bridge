"""scripts/bump_version.py: lockstep version bumping across every carrier file.

``scripts/`` has no ``__init__.py`` (it is not an importable package, and is
never installed), so the module under test is loaded from its file path --
mirroring ``test_nodes.py``'s ``TestDisplayNameMapping._load_top_level_init``,
which does the exact same thing for the repo's own top-level ``__init__.py``.

Every test here works against a throwaway fixture directory tree (never the
real repository) via ``bump_all(repo_root, ...)``'s injectable *repo_root*
parameter -- built specifically for this testability, since actually
running the script against the live repo would rewrite this project's own
version files as a side effect of running the test suite.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "bump_version.py"


def _load_bump_version_module():
    spec = importlib.util.spec_from_file_location("bump_version_under_test", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def bump_version_module():
    return _load_bump_version_module()


def _write_fake_repo(root: Path, version: str = "1.2.3") -> None:
    """A minimal directory tree with all four version-carrying files, all
    agreeing on *version*, in exactly the shape ``bump_version.py`` expects.
    """
    (root / "cpsb").mkdir(parents=True, exist_ok=True)
    (root / "cpsb" / "version.py").write_text(
        f'"""Single source of truth for this backend\'s own semver string."""\n'
        "\n"
        f'__version__ = "{version}"\n',
        encoding="utf-8",
    )

    (root / "pyproject.toml").write_text(
        "[project]\n"
        'name = "comfyui-photoshop-bridge"\n'
        f'version = "{version}"\n'
        "\n"
        "[project.urls]\n"
        'Repository = "https://example.invalid/repo"\n',
        encoding="utf-8",
    )

    (root / "photoshop_plugin").mkdir(parents=True, exist_ok=True)
    manifest = {
        "manifestVersion": 5,
        "id": "com.example.test-plugin",
        "name": "Test Plugin",
        "version": version,
        "main": "src/panel.html",
    }
    (root / "photoshop_plugin" / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )

    (root / "web" / "cpsb").mkdir(parents=True, exist_ok=True)
    (root / "web" / "cpsb" / "version.js").write_text(
        f"export const FRONTEND_VERSION = '{version}';\n", encoding="utf-8"
    )


def _read_all_versions(root: Path) -> dict[str, str]:
    return {
        "cpsb/version.py": re.search(
            r'__version__\s*=\s*"([\d.]+)"', (root / "cpsb" / "version.py").read_text()
        ).group(1),
        "pyproject.toml": re.search(
            r'(?m)^version\s*=\s*"([\d.]+)"', (root / "pyproject.toml").read_text()
        ).group(1),
        "manifest.json": json.loads(
            (root / "photoshop_plugin" / "manifest.json").read_text()
        )["version"],
        "version.js": re.search(
            r"FRONTEND_VERSION\s*=\s*'([\d.]+)'",
            (root / "web" / "cpsb" / "version.js").read_text(),
        ).group(1),
    }


class TestBumpAll:
    def test_dry_run_reports_but_writes_nothing(self, tmp_path, bump_version_module):
        _write_fake_repo(tmp_path, "1.2.3")
        before = _read_all_versions(tmp_path)

        old, new = bump_version_module.bump_all(tmp_path, "patch", dry_run=True)

        assert (old, new) == ("1.2.3", "1.2.4")
        assert _read_all_versions(tmp_path) == before  # untouched

    def test_dry_run_is_idempotent(self, tmp_path, bump_version_module):
        """Repeated dry-run calls report the identical pair every time --
        no hidden state, no side effects (module docstring's "On idempotent").
        """
        _write_fake_repo(tmp_path, "1.2.3")

        first = bump_version_module.bump_all(tmp_path, "patch", dry_run=True)
        second = bump_version_module.bump_all(tmp_path, "patch", dry_run=True)

        assert first == second == ("1.2.3", "1.2.4")

    def test_patch_bump_writes_all_four_files(self, tmp_path, bump_version_module):
        _write_fake_repo(tmp_path, "1.2.3")

        old, new = bump_version_module.bump_all(tmp_path, "patch", dry_run=False)

        assert (old, new) == ("1.2.3", "1.2.4")
        assert _read_all_versions(tmp_path) == {
            "cpsb/version.py": "1.2.4",
            "pyproject.toml": "1.2.4",
            "manifest.json": "1.2.4",
            "version.js": "1.2.4",
        }

    def test_minor_bump_resets_patch(self, tmp_path, bump_version_module):
        _write_fake_repo(tmp_path, "1.2.3")
        old, new = bump_version_module.bump_all(tmp_path, "minor", dry_run=False)
        assert (old, new) == ("1.2.3", "1.3.0")

    def test_major_bump_resets_minor_and_patch(self, tmp_path, bump_version_module):
        _write_fake_repo(tmp_path, "1.2.3")
        old, new = bump_version_module.bump_all(tmp_path, "major", dry_run=False)
        assert (old, new) == ("1.2.3", "2.0.0")

    def test_successive_real_bumps_advance_each_time(self, tmp_path, bump_version_module):
        """A real (non-dry-run) bump is deliberately NOT idempotent --
        running "patch" three times in a row must move the version forward
        three times, not settle on a fixed point (module docstring).
        """
        _write_fake_repo(tmp_path, "1.2.3")

        first = bump_version_module.bump_all(tmp_path, "patch", dry_run=False)
        second = bump_version_module.bump_all(tmp_path, "patch", dry_run=False)
        third = bump_version_module.bump_all(tmp_path, "patch", dry_run=False)

        assert first == ("1.2.3", "1.2.4")
        assert second == ("1.2.4", "1.2.5")
        assert third == ("1.2.5", "1.2.6")
        assert _read_all_versions(tmp_path)["cpsb/version.py"] == "1.2.6"

    def test_missing_file_refuses(self, tmp_path, bump_version_module):
        _write_fake_repo(tmp_path, "1.2.3")
        (tmp_path / "web" / "cpsb" / "version.js").unlink()

        with pytest.raises(SystemExit, match="does not exist"):
            bump_version_module.bump_all(tmp_path, "patch")

    def test_unparseable_file_refuses(self, tmp_path, bump_version_module):
        _write_fake_repo(tmp_path, "1.2.3")
        (tmp_path / "cpsb" / "version.py").write_text(
            "no version string here\n", encoding="utf-8"
        )

        with pytest.raises(SystemExit, match="expected exactly one version string"):
            bump_version_module.bump_all(tmp_path, "patch")

    def test_drifted_versions_refuses_and_writes_nothing(self, tmp_path, bump_version_module):
        """A prior partial/failed bump (or a hand edit) that left the four
        files disagreeing must not be silently "resolved" by picking one --
        this is exactly the "dirty parse" the whole run refuses on.
        """
        _write_fake_repo(tmp_path, "1.2.3")
        manifest_path = tmp_path / "photoshop_plugin" / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["version"] = "1.9.9"
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        before = _read_all_versions(tmp_path)

        with pytest.raises(SystemExit, match="drifted out of sync"):
            bump_version_module.bump_all(tmp_path, "patch")

        assert _read_all_versions(tmp_path) == before  # all-or-nothing: nothing written

    def test_unrelated_file_content_preserved(self, tmp_path, bump_version_module):
        """The substitution touches only the version string -- everything
        else in each file (other keys, comments, formatting) is untouched.
        """
        _write_fake_repo(tmp_path, "1.2.3")
        pyproject_before = (tmp_path / "pyproject.toml").read_text()

        bump_version_module.bump_all(tmp_path, "patch", dry_run=False)

        pyproject_after = (tmp_path / "pyproject.toml").read_text()
        assert pyproject_after == pyproject_before.replace('"1.2.3"', '"1.2.4"')
        manifest = json.loads(
            (tmp_path / "photoshop_plugin" / "manifest.json").read_text()
        )
        assert manifest["manifestVersion"] == 5  # untouched sibling key
        assert manifest["id"] == "com.example.test-plugin"


class TestMainCli:
    def test_dry_run_prints_old_arrow_new_and_writes_nothing(
        self, tmp_path, bump_version_module, monkeypatch, capsys
    ):
        _write_fake_repo(tmp_path, "1.2.3")
        monkeypatch.setattr(bump_version_module, "REPO_ROOT", tmp_path)
        before = _read_all_versions(tmp_path)

        exit_code = bump_version_module.main(["--dry-run"])

        assert exit_code == 0
        captured = capsys.readouterr()
        assert "1.2.3" in captured.out
        assert "1.2.4" in captured.out
        assert _read_all_versions(tmp_path) == before

    def test_minor_and_major_are_mutually_exclusive(self, bump_version_module):
        with pytest.raises(SystemExit):
            bump_version_module.main(["--minor", "--major"])

    def test_default_bump_is_patch(self, tmp_path, bump_version_module, monkeypatch):
        _write_fake_repo(tmp_path, "1.2.3")
        monkeypatch.setattr(bump_version_module, "REPO_ROOT", tmp_path)

        bump_version_module.main([])

        assert _read_all_versions(tmp_path)["cpsb/version.py"] == "1.2.4"

    def test_minor_flag_via_main(self, tmp_path, bump_version_module, monkeypatch):
        _write_fake_repo(tmp_path, "1.2.3")
        monkeypatch.setattr(bump_version_module, "REPO_ROOT", tmp_path)

        bump_version_module.main(["--minor"])

        assert _read_all_versions(tmp_path)["cpsb/version.py"] == "1.3.0"


class TestSubprocessInvocation:
    """Exit-criteria smoke test: run the script with ``--dry-run`` (and once
    for real) via ``subprocess``, proving it works as a genuine standalone
    CLI tool -- not just an importable module. Copies the script into an
    isolated fake repo so ``REPO_ROOT`` (derived from the script's own file
    location) resolves to throwaway fixture files, never the real repo.
    """

    @staticmethod
    def _copy_script_into(root: Path) -> Path:
        scripts_dir = root / "scripts"
        scripts_dir.mkdir()
        copied = scripts_dir / "bump_version.py"
        copied.write_text(SCRIPT_PATH.read_text(encoding="utf-8"), encoding="utf-8")
        return copied

    def test_dry_run_via_subprocess(self, tmp_path):
        _write_fake_repo(tmp_path, "2.5.9")
        copied_script = self._copy_script_into(tmp_path)
        before = _read_all_versions(tmp_path)

        result = subprocess.run(
            [sys.executable, str(copied_script), "--dry-run"],
            capture_output=True,
            text=True,
            timeout=30,
        )

        assert result.returncode == 0, result.stderr
        assert "2.5.9 -> 2.5.10" in result.stdout
        assert _read_all_versions(tmp_path) == before  # dry-run: nothing written

    def test_real_bump_via_subprocess(self, tmp_path):
        _write_fake_repo(tmp_path, "0.9.0")
        copied_script = self._copy_script_into(tmp_path)

        result = subprocess.run(
            [sys.executable, str(copied_script), "--minor"],
            capture_output=True,
            text=True,
            timeout=30,
        )

        assert result.returncode == 0, result.stderr
        assert "0.9.0 -> 0.10.0" in result.stdout
        assert _read_all_versions(tmp_path) == {
            "cpsb/version.py": "0.10.0",
            "pyproject.toml": "0.10.0",
            "manifest.json": "0.10.0",
            "version.js": "0.10.0",
        }

    def test_refusal_via_subprocess_exits_nonzero(self, tmp_path):
        _write_fake_repo(tmp_path, "1.0.0")
        (tmp_path / "cpsb" / "version.py").write_text("nothing to see here\n", encoding="utf-8")
        copied_script = self._copy_script_into(tmp_path)

        result = subprocess.run(
            [sys.executable, str(copied_script), "--dry-run"],
            capture_output=True,
            text=True,
            timeout=30,
        )

        assert result.returncode != 0
        assert "refusing to bump" in result.stderr
