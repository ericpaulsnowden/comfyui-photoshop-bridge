"""CpsbContext / SettingsStore: sanitize_managed_name and the managed-folder
property (PROTOCOL.md §1/§2).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from cpsb.context import (
    DEFAULT_MANAGED_FOLDER_NAME,
    CpsbContext,
    SettingsStore,
    sanitize_managed_name,
)


class TestSanitizeManagedName:
    """The managed-folder name becomes a directory under ``input/`` and is
    echoed into image subfolders the frontend fetches, so anything that
    isn't a single, benign path component must fall back to the default
    (PROTOCOL.md §2).
    """

    @pytest.mark.parametrize(
        "raw",
        [
            "../x",  # parent-reference traversal
            "a/b",  # embedded separator
            "a\\b",  # embedded backslash, unconditionally rejected
            "",  # empty
            "  ",  # whitespace-only
            ".",  # current-dir reference
            "..",  # parent-dir reference
            "/etc/passwd",  # absolute path
            "..\\..\\windows",  # traversal via backslash
        ],
        ids=[
            "parent-ref-traversal",
            "forward-slash",
            "backslash",
            "empty",
            "whitespace-only",
            "dot",
            "dotdot",
            "absolute-path",
            "backslash-traversal",
        ],
    )
    def test_hostile_strings_fall_back_to_default(self, raw: str) -> None:
        assert sanitize_managed_name(raw) == DEFAULT_MANAGED_FOLDER_NAME

    @pytest.mark.parametrize(
        "raw",
        [None, 123, 1.5, True, ["photoshop"], {"name": "photoshop"}],
        ids=["none", "int", "float", "bool", "list", "dict"],
    )
    def test_non_string_types_fall_back_to_default(self, raw: object) -> None:
        assert sanitize_managed_name(raw) == DEFAULT_MANAGED_FOLDER_NAME

    def test_os_sep_variant_falls_back_to_default(self) -> None:
        # Exercises the actual os.sep check on whatever platform the suite
        # runs on, distinct from the hardcoded "/" and "\\" literal checks
        # above (defense in depth: both are checked unconditionally,
        # regardless of the host OS's own separator).
        assert sanitize_managed_name(f"weird{os.sep}name") == DEFAULT_MANAGED_FOLDER_NAME

    def test_altsep_rejected_when_platform_has_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # os.altsep is None on POSIX; simulate a Windows-style alternate
        # separator so the os.altsep branch is exercised on every platform.
        monkeypatch.setattr(os, "altsep", "/")
        assert sanitize_managed_name("a/b") == DEFAULT_MANAGED_FOLDER_NAME

    def test_valid_name_passes_through_unchanged(self) -> None:
        assert sanitize_managed_name("my-photoshop-folder") == "my-photoshop-folder"

    def test_surrounding_whitespace_is_stripped(self) -> None:
        assert sanitize_managed_name("  photoshop  ") == "photoshop"

    def test_default_itself_passes_through(self) -> None:
        assert sanitize_managed_name(DEFAULT_MANAGED_FOLDER_NAME) == DEFAULT_MANAGED_FOLDER_NAME


@pytest.fixture
def context(tmp_path: Path) -> CpsbContext:
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    return CpsbContext(
        input_dir=input_dir,
        output_dir=tmp_path / "output",
        temp_dir=tmp_path / "temp",
        user_dir=tmp_path / "user",
        send_event=lambda event, payload: None,
        settings=SettingsStore(tmp_path / "user" / "cpsb.json"),
    )


class TestManagedFolderNameProperty:
    """``CpsbContext.managed_folder_name`` re-sanitizes on every access, so a
    live settings change (or a hand-edited ``cpsb.json``) can never smuggle
    a path separator into ``cpsb_input_dir``.
    """

    def test_defaults_when_unset(self, context: CpsbContext) -> None:
        assert context.managed_folder_name == DEFAULT_MANAGED_FOLDER_NAME
        assert context.cpsb_input_dir == context.input_dir / DEFAULT_MANAGED_FOLDER_NAME

    def test_reflects_a_valid_setting(self, context: CpsbContext) -> None:
        context.settings.update({"managed_folder_name": "custom-folder"})
        assert context.managed_folder_name == "custom-folder"
        assert context.cpsb_input_dir == context.input_dir / "custom-folder"

    def test_hostile_stored_value_still_sanitized_on_read(self, context: CpsbContext) -> None:
        # Bypasses sanitize_managed_name entirely, simulating a hand-edited
        # cpsb.json rather than a value that went through POST /cpsb/settings.
        context.settings.update({"managed_folder_name": "../../etc"})
        assert context.managed_folder_name == DEFAULT_MANAGED_FOLDER_NAME
        assert context.cpsb_input_dir == context.input_dir / DEFAULT_MANAGED_FOLDER_NAME

    def test_picks_up_a_live_change_without_restart(self, context: CpsbContext) -> None:
        assert context.managed_folder_name == DEFAULT_MANAGED_FOLDER_NAME
        context.settings.update({"managed_folder_name": "renamed"})
        assert context.managed_folder_name == "renamed"
