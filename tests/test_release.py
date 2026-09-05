from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path

import pytest

from scripts import build_desktop
from scripts.collect_runtime_licenses import collect, collect_named


def test_desktop_build_paths_cannot_escape_the_repository(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()

    assert build_desktop._scoped(root, Path("release/stage")) == root / "release" / "stage"
    with pytest.raises(ValueError, match="must stay inside"):
        build_desktop._scoped(root, root)
    with pytest.raises(ValueError, match="must stay inside"):
        build_desktop._scoped(root, Path("../outside"))


def test_desktop_platform_labels_are_release_friendly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(build_desktop.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(build_desktop.platform, "machine", lambda: "arm64")

    assert build_desktop._labels() == ("macOS", "arm64")


def test_runtime_license_collector_includes_every_component_and_python(tmp_path: Path) -> None:
    output = tmp_path / "licenses"

    manifest = collect(output, "subduet")

    names = {str(component["name"]) for component in manifest}
    assert {"subduet", "Python"} <= names
    assert (output / "MANIFEST.json").is_file()
    for component in manifest:
        assert component["files"]


def test_build_tool_license_collector_includes_pyinstaller_copying(tmp_path: Path) -> None:
    try:
        distribution("pyinstaller")
    except PackageNotFoundError:
        pytest.skip("the optional desktop release dependencies are not installed")
    output = tmp_path / "build-tool-licenses"

    manifest = collect_named(output, ["pyinstaller", "pyinstaller-hooks-contrib"])

    names = {str(component["name"]).casefold() for component in manifest}
    assert names == {"pyinstaller", "pyinstaller-hooks-contrib"}
    assert any(path.name.endswith("COPYING.txt") for path in output.rglob("*"))
