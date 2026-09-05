from __future__ import annotations

import os
import shutil
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from paircue.config import PairCueSettings

CheckStatus = Literal["ok", "warning", "error"]


@dataclass(frozen=True, slots=True)
class DiagnosticCheck:
    name: str
    status: CheckStatus
    detail: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


def run_diagnostics(settings: PairCueSettings) -> list[DiagnosticCheck]:
    ffmpeg_required = settings.transcription_enabled
    checks = [
        DiagnosticCheck("Python", "ok", sys.version.split()[0]),
        _directory_check("Media library", settings.media_root, require_write=True),
        _directory_check("State storage", settings.state_dir, require_write=True),
        _executable_check("FFmpeg", "ffmpeg", required=ffmpeg_required),
        _executable_check("FFprobe", "ffprobe", required=False),
        DiagnosticCheck("Platform", "ok", settings.platform),
    ]

    if settings.subtitle_download_enabled and settings.opensubtitles_api_key.get_secret_value():
        checks.append(DiagnosticCheck("Subtitle search", "ok", "OpenSubtitles API configured"))
    elif settings.subtitle_download_enabled:
        checks.append(
            DiagnosticCheck(
                "Subtitle search",
                "warning",
                "enabled but no OpenSubtitles API key; automatic download is inactive",
            )
        )
    else:
        checks.append(DiagnosticCheck("Subtitle search", "warning", "disabled"))

    if settings.transcription_enabled:
        host = urlparse(settings.transcription_base_url).hostname or "configured endpoint"
        checks.append(
            DiagnosticCheck(
                "Subtitle generation",
                "ok",
                f"{settings.transcription_model} via {host}; audio will leave SubDuet",
            )
        )
    else:
        checks.append(
            DiagnosticCheck(
                "Subtitle generation",
                "warning",
                "disabled; media with no source subtitle cannot be transcribed",
            )
        )

    if settings.translation_enabled:
        host = urlparse(settings.translation_base_url).hostname or "configured endpoint"
        final_check = (
            "; AI final quality check enabled"
            if settings.translation_final_check_enabled
            else "; AI final quality check disabled"
        )
        checks.append(
            DiagnosticCheck(
                "Bilingual translation",
                "ok",
                f"{settings.translation_model} via {host}{final_check}",
            )
        )
    else:
        checks.append(
            DiagnosticCheck(
                "Bilingual translation",
                "warning",
                "disabled; both language tracks must already exist",
            )
        )
    return checks


def _directory_check(name: str, path: Path, *, require_write: bool) -> DiagnosticCheck:
    resolved = path.expanduser()
    if resolved.exists():
        if not resolved.is_dir():
            return DiagnosticCheck(name, "error", f"not a directory: {resolved}")
        required = os.R_OK | (os.W_OK if require_write else 0)
        if not os.access(resolved, required):
            return DiagnosticCheck(name, "error", f"insufficient access: {resolved}")
        return DiagnosticCheck(name, "ok", str(resolved.resolve()))

    parent = _nearest_existing_parent(resolved)
    if parent is None or not os.access(parent, os.W_OK):
        return DiagnosticCheck(name, "error", f"missing and cannot be created: {resolved}")
    return DiagnosticCheck(name, "warning", f"will be created under {parent}")


def _nearest_existing_parent(path: Path) -> Path | None:
    candidate = path
    while candidate != candidate.parent:
        candidate = candidate.parent
        if candidate.exists():
            return candidate
    return candidate if candidate.exists() else None


def _executable_check(name: str, command: str, *, required: bool) -> DiagnosticCheck:
    executable = shutil.which(command)
    if executable is None:
        status: CheckStatus = "error" if required else "warning"
        detail = f"{command} is not on PATH"
        if not required:
            detail += "; features that do not inspect media remain available"
        return DiagnosticCheck(name, status, detail)
    return DiagnosticCheck(name, "ok", executable)
