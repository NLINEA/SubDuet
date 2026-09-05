#!/usr/bin/env python3
"""Fail without echoing values when tracked or packaged files look sensitive."""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from collections.abc import Iterable, Iterator
from pathlib import Path, PurePosixPath

GIT = shutil.which("git")
SKIPPED_DIRECTORIES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "release",
}
SKIPPED_FILES = {".coverage"}
SENSITIVE_SUFFIXES = {".key", ".p12", ".pfx"}
SENSITIVE_NAMES = re.compile(
    r"^(?:\.env(?:\..+)?|(?:paircue|subduet|downloads)\.env(?:\..+)?|credentials.*\.json)$",
    re.IGNORECASE,
)
ALLOWED_EXAMPLE_NAMES = {
    ".env.example", "downloads.env.example", "paircue.env.example", "subduet.env.example",
}
PRIVATE_CONTEXT = (
    (
        "private macOS path",
        re.compile(
            rb"/Users/[A-Za-z0-9._-]+/(?:Desktop|Documents|Downloads|Library|Movies|Music|"
            rb"Pictures|Projects|workspace)/"
        ),
    ),
    ("private Windows path", re.compile(rb"[A-Za-z]:\\Users\\[A-Za-z0-9._ -]+\\")),
    (
        "private Codex context",
        re.compile(
            rb"(?:\." + rb"codex|\." + rb"chatgpt-projects|rollout_" + rb"summaries)"
        ),
    ),
    (
        "private conversation context",
        re.compile(rb"(?:oai-mem-" + rb"citation|in-app-browser-" + rb"context)"),
    ),
)
SECRET_PATTERNS = (
    ("private key", re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
    ("OpenAI-style API key", re.compile(rb"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b")),
    (
        "GitHub access token",
        re.compile(rb"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    ),
    ("Google API key", re.compile(rb"\bAIza[0-9A-Za-z_-]{20,}\b")),
    ("AWS access key", re.compile(rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("Slack token", re.compile(rb"\bxox[baprs]-[A-Za-z0-9-]{20,}\b")),
)
ASSIGNMENT = re.compile(
    rb"(?m)^[ \t]*(?:export[ \t]+)?[A-Z0-9_]*(?:API_KEY|TOKEN|SECRET|PASSWORD)"
    rb"[A-Z0-9_]*[ \t]*=[ \t]*([^\r\n#]+)"
)
PLACEHOLDER_WORDS = (
    b"${",
    b"change-me",
    b"changeme",
    b"dummy",
    b"example",
    b"generate-",
    b"generated-",
    b"placeholder",
    b"redacted",
    b"replace-me",
    b"test-only",
    b"your-",
)


def _git(*arguments: str) -> bytes:
    if GIT is None:
        raise RuntimeError("git is unavailable")
    result = subprocess.run(  # noqa: S603 - fixed git executable and argument array
        [GIT, *arguments],
        check=True,
        capture_output=True,
    )
    return result.stdout


def _tracked_files() -> Iterator[tuple[str, bytes]]:
    for raw_path in _git("ls-files", "-z").split(b"\0"):
        if not raw_path:
            continue
        path = Path(raw_path.decode("utf-8", errors="surrogateescape"))
        if path.is_file():
            yield path.as_posix(), path.read_bytes()


def _history_files() -> Iterator[tuple[str, bytes]]:
    seen: set[str] = set()
    for line in _git("rev-list", "--objects", "--all").decode("utf-8").splitlines():
        object_id, separator, path = line.partition(" ")
        if not separator or object_id in seen:
            continue
        seen.add(object_id)
        object_type = _git("cat-file", "-t", object_id).strip()
        if object_type == b"blob":
            yield path, _git("cat-file", "-p", object_id)


def _path_files(root: Path) -> Iterator[tuple[str, bytes]]:
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        if path.name in SKIPPED_FILES:
            continue
        if any(part in SKIPPED_DIRECTORIES for part in path.relative_to(root).parts):
            continue
        yield path.relative_to(root).as_posix(), path.read_bytes()


def _sensitive_filename(path: str) -> bool:
    name = PurePosixPath(path).name
    if name in ALLOWED_EXAMPLE_NAMES:
        return False
    return bool(SENSITIVE_NAMES.fullmatch(name)) or PurePosixPath(path).suffix.casefold() in {
        suffix.casefold() for suffix in SENSITIVE_SUFFIXES
    }


def scan_files(files: Iterable[tuple[str, bytes]]) -> list[tuple[str, str]]:
    findings: set[tuple[str, str]] = set()
    for path, content in files:
        if _sensitive_filename(path):
            findings.add((path, "sensitive filename"))
        for label, pattern in (*SECRET_PATTERNS, *PRIVATE_CONTEXT):
            if pattern.search(content):
                findings.add((path, label))
        for match in ASSIGNMENT.finditer(content):
            value = match.group(1).strip().strip(b"\"'").lower()
            if len(value) < 16 or not value or any(word in value for word in PLACEHOLDER_WORDS):
                continue
            findings.add((path, "credential-like environment value"))
    return sorted(findings)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--history", action="store_true", help="also scan every Git blob")
    parser.add_argument("--path", type=Path, help="scan a build or release directory instead")
    return parser.parse_args()


def main() -> int:
    arguments = _arguments()
    if arguments.path is not None:
        files: Iterable[tuple[str, bytes]] = _path_files(arguments.path)
        scope = str(arguments.path)
    else:
        current = list(_tracked_files())
        files = [*current, *list(_history_files())] if arguments.history else current
        scope = "tracked files and Git history" if arguments.history else "tracked files"
    findings = scan_files(files)
    if findings:
        print("Potential private data found; values are intentionally hidden:", file=sys.stderr)
        for path, label in findings:
            print(f"- {path}: {label}", file=sys.stderr)
        return 1
    print(f"Secret and private-context check passed for {scope}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
