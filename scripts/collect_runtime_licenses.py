#!/usr/bin/env python3
"""Collect complete runtime license files for a desktop release archive."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import sysconfig
from importlib.metadata import Distribution, distribution
from pathlib import Path

from packaging.utils import canonicalize_name

from scripts.check_runtime_licenses import _runtime_graph

LICENSE_FILE = re.compile(
    r"(?:^|/)(?:licen[cs]e|copying|notice|copyright)(?:[^/]*)$",
    re.IGNORECASE,
)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--distribution", default="subduet")
    parser.add_argument("--only", nargs="+", help="collect these named distributions instead")
    return parser.parse_args()


def _safe_filename(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._") or "LICENSE"


def _python_license() -> Path:
    candidates = [
        Path(sysconfig.get_paths()["stdlib"]) / "LICENSE.txt",
        Path(sys.base_prefix) / "LICENSE.txt",
        Path(sys.base_prefix) / "LICENSE",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise RuntimeError("the bundled Python installation has no discoverable license file")


def _copy_distribution(item: Distribution, output: Path) -> dict[str, object]:
    name = str(item.metadata["Name"])
    component = output / f"{_safe_filename(canonicalize_name(name))}-{item.version}"
    component.mkdir()
    copied: list[str] = []
    for entry in item.files or ():
        relative = str(entry).replace("\\", "/")
        if not LICENSE_FILE.search(relative):
            continue
        source = Path(item.locate_file(entry))
        if not source.is_file():
            continue
        destination_name = _safe_filename(relative.replace("/", "__"))
        destination = component / destination_name
        if destination.exists():
            destination = component / f"{len(copied) + 1}-{destination_name}"
        shutil.copy2(source, destination)
        copied.append(destination.name)
    if not copied:
        raise RuntimeError(f"distribution has no packaged license file: {name}")
    return {"name": name, "version": item.version, "files": copied}


def _write_manifest(output: Path, manifest: list[dict[str, object]]) -> None:
    (output / "MANIFEST.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def collect_named(output: Path, names: list[str]) -> list[dict[str, object]]:
    output.mkdir(parents=True, exist_ok=False)
    manifest = [_copy_distribution(distribution(name), output) for name in names]
    _write_manifest(output, manifest)
    return manifest


def collect(output: Path, distribution_name: str) -> list[dict[str, object]]:
    output.mkdir(parents=True, exist_ok=False)
    manifest = [
        _copy_distribution(item, output) for item in _runtime_graph(distribution_name)
    ]

    python_source = _python_license()
    python_directory = output / f"python-{sys.version_info.major}.{sys.version_info.minor}"
    python_directory.mkdir()
    shutil.copy2(python_source, python_directory / "LICENSE.txt")
    manifest.append(
        {
            "name": "Python",
            "version": sys.version.split()[0],
            "files": ["LICENSE.txt"],
        }
    )
    _write_manifest(output, manifest)
    return manifest


def main() -> int:
    arguments = _arguments()
    if arguments.only:
        manifest = collect_named(arguments.output, arguments.only)
        kind = "build-tool"
    else:
        manifest = collect(arguments.output, arguments.distribution)
        kind = "runtime"
    print(f"Collected licenses for {len(manifest)} {kind} components in {arguments.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
