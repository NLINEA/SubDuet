#!/usr/bin/env python3
"""Build and stage a self-contained SubDuet desktop release for the current OS."""

from __future__ import annotations

import platform
import shutil
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path


def _scoped(root: Path, candidate: Path) -> Path:
    resolved = (root / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
    if resolved == root or not resolved.is_relative_to(root):
        raise ValueError(f"desktop build path must stay inside the repository: {resolved}")
    return resolved


def _labels() -> tuple[str, str]:
    system = {"Darwin": "macOS", "Windows": "windows", "Linux": "linux"}.get(
        platform.system()
    )
    if system is None:
        raise RuntimeError(f"unsupported desktop build system: {platform.system()}")
    machine = platform.machine().casefold()
    architecture = "arm64" if machine in {"arm64", "aarch64"} else "x64"
    return system, architecture


def _built_payload(dist_dir: Path) -> tuple[Path, Path]:
    if sys.platform == "darwin":
        payload = dist_dir / "SubDuet.app"
        executable = payload / "Contents" / "MacOS" / "SubDuet"
    elif sys.platform == "win32":
        payload = dist_dir / "SubDuet.exe"
        executable = payload
    else:
        payload = dist_dir / "SubDuet"
        executable = payload
    if not payload.exists() or not executable.is_file():
        raise RuntimeError(f"PyInstaller did not create the expected desktop payload: {payload}")
    return payload, executable


def _run(command: list[str], root: Path) -> None:
    subprocess.run(  # noqa: S603 - every build command is assembled from repository paths
        command,
        cwd=root,
        check=True,
        timeout=180,
    )


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    # Release paths are deliberately fixed. Accepting path arguments here would make destructive
    # cleanup and the packaged-executable smoke test depend on untrusted command-line input.
    dist_dir = _scoped(root, Path("dist/desktop"))
    work_dir = _scoped(root, Path("build/desktop"))
    stage_parent = _scoped(root, Path("release/stage"))
    system, architecture = _labels()
    stage = stage_parent / f"SubDuet-{system}-{architecture}"

    try:
        import PyInstaller.__main__
    except ImportError as exc:
        raise RuntimeError("install SubDuet's release dependencies before building") from exc

    for target in (dist_dir, work_dir, stage_parent):
        if target.exists():
            shutil.rmtree(target)
    dist_dir.mkdir(parents=True)
    work_dir.mkdir(parents=True)
    stage.mkdir(parents=True)

    options = [
        str(root / "src" / "paircue" / "desktop.py"),
        "--name=SubDuet",
        "--windowed",
        "--noupx",
        "--noconfirm",
        "--clean",
        f"--paths={root / 'src'}",
        "--collect-data=paircue",
        f"--distpath={dist_dir}",
        f"--workpath={work_dir}",
        f"--specpath={work_dir}",
    ]
    if sys.platform == "darwin":
        # Keep the installed app identity stable across the public rename.
        options.extend(("--onedir", "--osx-bundle-identifier=io.paircue.desktop"))
    else:
        options.append("--onefile")
    PyInstaller.__main__.run(options)

    payload, executable = _built_payload(dist_dir)
    _run([str(executable), "setup", "--no-open"], root)
    _run([str(executable), "--version"], root)
    if executable.stat().st_size < 1_000_000:
        raise RuntimeError("desktop executable is unexpectedly small")
    if payload.is_dir():
        required_assets = (
            "paircue/setup/index.html",
            "paircue/setup/favicon.svg",
            "paircue/dashboard/index.html",
        )
        packaged_paths = {path.as_posix() for path in payload.rglob("*") if path.is_file()}
        for asset in required_assets:
            if not any(path.endswith(asset) for path in packaged_paths):
                raise RuntimeError(f"desktop app is missing packaged asset: {asset}")

    destination = stage / payload.name
    if payload.is_dir():
        shutil.copytree(payload, destination, symlinks=True)
    else:
        shutil.copy2(payload, destination)
    for document in ("LICENSE", "THIRD_PARTY_NOTICES.md", "DESKTOP_README.md"):
        shutil.copy2(root / document, stage / document)

    _run(
        [
            sys.executable,
            "-m",
            "scripts.collect_runtime_licenses",
            str(stage / "THIRD_PARTY_LICENSES"),
        ],
        root,
    )
    _run(
        [
            sys.executable,
            "-m",
            "scripts.collect_runtime_licenses",
            str(stage / "BUILD_TOOL_LICENSES"),
            "--only",
            "pyinstaller",
            "pyinstaller-hooks-contrib",
        ],
        root,
    )
    _run(
        [
            sys.executable,
            "-m",
            "scripts.check_runtime_licenses",
            "--sbom",
            str(stage / "subduet-sbom.cdx.json"),
        ],
        root,
    )
    (stage / "BUILD-INFO.txt").write_text(
        f"SubDuet {version('subduet')} desktop beta\nPlatform: {system} {architecture}\n"
        f"Python: {sys.version.split()[0]}\nFFmpeg bundled: no\n",
        encoding="utf-8",
    )
    print(stage)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
