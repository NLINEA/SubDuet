"""Select the intended spoken track before decoding or uploading any audio."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from paircue.languages import observed_language_tag


class AudioTrackError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AudioTrack:
    index: int
    language: str | None
    default: bool = False
    commentary: bool = False


def read_audio_tracks(media_path: Path) -> tuple[AudioTrack, ...]:
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        raise AudioTrackError("FFprobe is required to choose the spoken audio track safely")
    try:
        result = subprocess.run(  # noqa: S603 - resolved binary and fixed argv; no shell
            [
                ffprobe, "-v", "error", "-print_format", "json", "-select_streams", "a",
                "-show_entries",
                "stream=index:stream_tags=language,title:stream_disposition=default,comment,visual_impaired",
                "-protocol_whitelist", "file,crypto,data", str(media_path),
            ],
            capture_output=True, text=True, timeout=30, check=False,
        )
        if result.returncode or len(result.stdout) > 1024 * 1024:
            raise ValueError
        streams = json.loads(result.stdout)["streams"]
        if not isinstance(streams, list) or not 1 <= len(streams) <= 128:
            raise ValueError
        tracks: list[AudioTrack] = []
        for stream in streams:
            index = stream["index"]
            if type(index) is not int or not 0 <= index <= 65535:
                raise ValueError
            tags = stream.get("tags") or {}
            disposition = stream.get("disposition") or {}
            language = observed_language_tag(str(tags.get("language", ""))[:80])
            if language in {"und", "mul", "zxx"}:
                language = None
            title = str(tags.get("title", ""))[:256].casefold()
            tracks.append(AudioTrack(
                index, language,
                default=disposition.get("default") == 1,
                commentary=(
                    disposition.get("comment") == 1 or disposition.get("visual_impaired") == 1
                    or "commentary" in title or "audio description" in title
                ),
            ))
        if len({track.index for track in tracks}) != len(tracks):
            raise ValueError
        return tuple(tracks)
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError, AttributeError):
        raise AudioTrackError(
            "Could not read a reliable audio-track list; no audio was uploaded"
        ) from None


def choose_audio_stream(
    tracks: tuple[AudioTrack, ...], language: str, override: int | None = None,
) -> int:
    if override is not None:
        if type(override) is int and any(track.index == override for track in tracks):
            return override
        raise AudioTrackError("The selected audio stream does not exist in this video")
    requested = observed_language_tag(language)
    if requested is None:
        raise AudioTrackError("Choose a valid spoken language before processing audio")
    usable = [track for track in tracks if not track.commentary]
    candidates = [track for track in usable if track.language == requested]
    if not candidates:
        # A generic tag can match a regional tag, but two explicit regions are not interchangeable.
        candidates = [track for track in usable if track.language and (
            track.language.split("-", 1)[0] == requested.split("-", 1)[0]
            and ("-" not in track.language or "-" not in requested)
        )]
    if len(candidates) == 1:
        return candidates[0].index
    defaults = [track for track in candidates if track.default]
    if len(defaults) == 1:
        return defaults[0].index
    if len(tracks) == 1 and len(usable) == 1 and usable[0].language is None:
        return usable[0].index
    choices = ", ".join(f"{track.index} ({track.language or 'unlabelled'})" for track in tracks)
    raise AudioTrackError(
        "The spoken audio track is ambiguous or does not match the selected language. "
        "Correct the spoken language, or use --audio-stream-index for this video "
        f"(PAIRCUE_AUDIO_STREAM_INDEX in a config). Available streams: {choices}. "
        "No audio was uploaded."
    )


def select_audio_stream(media_path: Path, language: str, override: int | None = None) -> int:
    return choose_audio_stream(read_audio_tracks(media_path), language, override)
