"""Upgrade compatibility and credential boundaries for the public rename."""

import subprocess
import sys
from importlib.metadata import distribution
from pathlib import Path

import pytest

import subduet
from paircue import __version__, cli
from scripts.check_secrets import scan_files


@pytest.mark.parametrize("module", ["subduet", "paircue"])
def test_new_and_legacy_module_entry_points_report_the_same_version(module: str) -> None:
    result = subprocess.run(  # noqa: S603 - fixed interpreter and parameterized local modules
        [sys.executable, "-m", module, "--version"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.stdout.strip() == f"subduet {__version__}"


def test_installed_distribution_retains_both_console_commands() -> None:
    assert subduet.__version__ == __version__
    commands = {
        item.name: item.load()
        for item in distribution("subduet").entry_points
        if item.group == "console_scripts"
    }
    assert commands["subduet"] is cli.main
    assert commands["paircue"] is cli.main


@pytest.mark.parametrize("platform", ["darwin", "win32", "linux"])
def test_renamed_desktop_reuses_existing_private_config_location(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, platform: str
) -> None:
    monkeypatch.setattr(cli, "_is_frozen", lambda: True)
    monkeypatch.setattr(cli.sys, "platform", platform)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    expected = {
        "darwin": tmp_path / "Library" / "Application Support" / "PairCue" / "paircue.env",
        "win32": tmp_path / "Roaming" / "PairCue" / "paircue.env",
        "linux": tmp_path / "config" / "paircue" / "paircue.env",
    }
    assert cli._default_setup_output() == expected[platform]


@pytest.mark.parametrize("name", ["paircue.env", "subduet.env", "subduet.env.backup-1"])
def test_private_config_is_rejected_even_without_a_recognizable_key(name: str) -> None:
    assert (name, "sensitive filename") in scan_files([(name, b"SHORT_KEY=example\n")])


def test_public_configuration_examples_still_pass_the_secret_scanner() -> None:
    files = [(name, b"SERVICE_API_KEY=your-api-key\n") for name in (
        "paircue.env.example", "subduet.env.example", "downloads.env.example", ".env.example",
    )]
    assert scan_files(files) == []
