from __future__ import annotations

import json
import logging
import math
import os
import shutil
import subprocess
import tempfile
import wave
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import numpy as np
import numpy.typing as npt
import srt

from paircue.languages import language_matches, observed_language_tag
from paircue.services.audio_tracks import select_audio_stream

log = logging.getLogger(__name__)


def _required_binary(name: str) -> str:
    binary = shutil.which(name)
    if binary is None:
        raise FileNotFoundError(f"required executable is unavailable: {name}")
    return binary


TEXT_SUBTITLE_CODECS = {"subrip", "srt", "ass", "ssa", "webvtt", "mov_text"}
LOCAL_MEDIA_PROTOCOLS = "file,crypto,data"


def ensure_media_path(path: Path, media_root: Path) -> Path:
    resolved_root = media_root.resolve(strict=True)
    resolved = path.resolve(strict=True)
    if not resolved.is_file() or not resolved.is_relative_to(resolved_root):
        raise ValueError("media path is not a file inside MEDIA_ROOT")
    return resolved


class EmbeddedSubtitleExtractor:
    def extract(
        self,
        media_path: Path,
        languages: set[str] | None = None,
    ) -> tuple[Path, ...]:
        try:
            probe = subprocess.run(  # noqa: S603 - fixed executable and argv; no shell
                [
                    _required_binary("ffprobe"),
                    "-v",
                    "error",
                    "-print_format",
                    "json",
                    "-show_streams",
                    "-select_streams",
                    "s",
                    "-protocol_whitelist",
                    LOCAL_MEDIA_PROTOCOLS,
                    str(media_path),
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            log.info("FFprobe is unavailable; continuing with external subtitle files")
            return ()
        if probe.returncode != 0:
            log.debug("ffprobe found no readable subtitle tracks for %s", media_path.name)
            return ()
        try:
            streams = json.loads(probe.stdout).get("streams", [])
        except (AttributeError, json.JSONDecodeError):
            log.warning("ffprobe returned an unreadable subtitle stream list")
            return ()
        outputs: list[Path] = []
        for subtitle_index, stream in enumerate(streams):
            codec = str(stream.get("codec_name") or "").lower()
            if codec not in TEXT_SUBTITLE_CODECS:
                continue
            language = str(stream.get("tags", {}).get("language") or "").lower()
            mapped = observed_language_tag(language)
            if languages is not None:
                mapped = next(
                    (requested for requested in languages if language_matches(language, requested)),
                    None,
                )
            if mapped is None:
                continue
            target = media_path.parent / f"{media_path.stem}.{mapped}.srt"
            if target.exists():
                continue
            temporary = self._temporary_srt(target)
            try:
                result = subprocess.run(  # noqa: S603 - fixed executable and argv; no shell
                    [
                        _required_binary("ffmpeg"),
                        "-v",
                        "error",
                        "-y",
                        "-protocol_whitelist",
                        LOCAL_MEDIA_PROTOCOLS,
                        "-i",
                        str(media_path),
                        "-map",
                        f"0:s:{subtitle_index}",
                        "-c:s",
                        "srt",
                        str(temporary),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=90,
                    check=False,
                )
                if result.returncode == 0 and temporary.stat().st_size > 10:
                    os.replace(temporary, target)
                    outputs.append(target)
            finally:
                temporary.unlink(missing_ok=True)
        return tuple(outputs)

    @staticmethod
    def _temporary_srt(target: Path) -> Path:
        with tempfile.NamedTemporaryFile(
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".srt",
            delete=False,
        ) as handle:
            path = Path(handle.name)
        path.unlink(missing_ok=True)
        return path


class SubtitleSynchronizer:
    """Synchronize SRT cues against media audio without a third-party sync engine.

    FFmpeg is used only to decode the user's media into temporary PCM audio. SubDuet's
    own activity detection, cross-correlation, confidence gate, and cue shifting live
    in this class and the helpers below.
    """

    def __init__(
        self,
        *,
        max_offset_seconds: int = 120,
        min_confidence: float = 0.24,
        sample_rate: int = 8_000,
        window_ms: int = 100,
        source_language: str = "en",
        audio_stream_index: int | None = None,
    ) -> None:
        self.max_offset_seconds = max_offset_seconds
        self.min_confidence = min_confidence
        self.sample_rate = sample_rate
        self.window_ms = window_ms
        self.source_language = source_language
        self.audio_stream_index = audio_stream_index

    def sync(self, media_path: Path, subtitle_path: Path) -> bool:
        audio_path = self._temporary_audio(media_path)
        try:
            audio_index = select_audio_stream(
                media_path, self.source_language, self.audio_stream_index,
            )
            result = subprocess.run(  # noqa: S603 - fixed executable and argv; no shell
                [
                    _required_binary("ffmpeg"),
                    "-v",
                    "error",
                    "-y",
                    "-protocol_whitelist",
                    LOCAL_MEDIA_PROTOCOLS,
                    "-i",
                    str(media_path),
                    "-map",
                    f"0:{audio_index}",
                    "-vn",
                    "-ac",
                    "1",
                    "-ar",
                    str(self.sample_rate),
                    "-c:a",
                    "pcm_s16le",
                    str(audio_path),
                ],
                capture_output=True,
                text=True,
                timeout=360,
                check=False,
            )
            if result.returncode != 0 or not audio_path.exists() or audio_path.stat().st_size < 44:
                return False
            cues = list(srt.parse(subtitle_path.read_text(encoding="utf-8-sig")))
            if not cues:
                return False
            audio_activity = self._audio_activity(audio_path)
            subtitle_activity = _subtitle_activity(
                cues,
                window_ms=self.window_ms,
                minimum_windows=len(audio_activity),
            )
            match = _best_offset_windows(
                audio_activity,
                subtitle_activity,
                max_offset_windows=math.ceil(
                    self.max_offset_seconds * 1_000 / self.window_ms
                ),
            )
            if match is None or match.confidence < self.min_confidence:
                log.info("subtitle sync confidence is too low for %s", subtitle_path.name)
                return False
            if match.offset_windows == 0:
                return True
            return self._shift_cues(subtitle_path, cues, match.offset_windows)
        except (
            FileNotFoundError,
            OSError,
            ValueError,
            srt.SRTParseError,
            subprocess.TimeoutExpired,
            wave.Error,
        ):
            log.warning("subtitle synchronization is unavailable; keeping original timing")
            return False
        finally:
            audio_path.unlink(missing_ok=True)

    def _audio_activity(self, audio_path: Path) -> npt.NDArray[np.bool_]:
        samples_per_window = self.sample_rate * self.window_ms // 1_000
        if samples_per_window <= 0:
            raise ValueError("audio activity window must contain at least one sample")
        energies: list[npt.NDArray[np.float64]] = []
        remainder: npt.NDArray[np.int16] = np.empty(0, dtype=np.int16)
        with wave.open(str(audio_path), "rb") as audio:
            if audio.getnchannels() != 1 or audio.getsampwidth() != 2:
                raise ValueError("decoded audio must be mono 16-bit PCM")
            if audio.getframerate() != self.sample_rate:
                raise ValueError("decoded audio has an unexpected sample rate")
            while frames := audio.readframes(samples_per_window * 1_024):
                samples: npt.NDArray[np.int16] = np.frombuffer(frames, dtype="<i2")
                if remainder.size:
                    samples = np.asarray(
                        np.concatenate((remainder, samples)), dtype=np.int16
                    )
                window_count = samples.size // samples_per_window
                if window_count:
                    boundary = window_count * samples_per_window
                    windows = samples[:boundary].reshape(window_count, samples_per_window)
                    absolute = np.abs(windows.astype(np.float32))
                    energies.append(np.asarray(np.mean(absolute, axis=1), dtype=np.float64))
                    remainder = np.asarray(samples[boundary:].copy(), dtype=np.int16)
                else:
                    remainder = np.asarray(samples.copy(), dtype=np.int16)
        if not energies:
            return np.zeros(0, dtype=np.bool_)
        energy = np.concatenate(energies)
        log_energy = 20.0 * np.log10(energy + 1.0)
        quiet, active = np.percentile(log_energy, [20, 80])
        threshold = quiet + max(5.0, (active - quiet) * 0.42)
        threshold = min(threshold, active)
        activity = log_energy >= threshold
        return _smooth_activity(activity)

    def _shift_cues(
        self,
        subtitle_path: Path,
        cues: Sequence[srt.Subtitle],
        offset_windows: int,
    ) -> bool:
        offset = timedelta(milliseconds=offset_windows * self.window_ms)
        shifted: list[srt.Subtitle] = []
        for cue in cues:
            shifted_end = cue.end + offset
            if shifted_end <= timedelta(0):
                continue
            start = max(timedelta(0), cue.start + offset)
            end = max(start + timedelta(milliseconds=1), shifted_end)
            shifted.append(
                srt.Subtitle(
                    index=cue.index,
                    start=start,
                    end=end,
                    content=cue.content,
                    proprietary=cue.proprietary,
                )
            )
        temporary = EmbeddedSubtitleExtractor._temporary_srt(subtitle_path)
        try:
            temporary.write_text(srt.compose(shifted), encoding="utf-8")
            if temporary.stat().st_size <= 10:
                return False
            os.replace(temporary, subtitle_path)
            return True
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _temporary_audio(media_path: Path) -> Path:
        with tempfile.NamedTemporaryFile(
            dir=media_path.parent,
            prefix=f".{media_path.name}.",
            suffix=".paircue-sync.wav",
            delete=False,
        ) as handle:
            return Path(handle.name)


@dataclass(frozen=True)
class _SyncMatch:
    offset_windows: int
    confidence: float


def _subtitle_activity(
    cues: Sequence[srt.Subtitle],
    *,
    window_ms: int,
    minimum_windows: int,
) -> npt.NDArray[np.bool_]:
    latest_ms = max((cue.end.total_seconds() * 1_000 for cue in cues), default=0.0)
    size = max(minimum_windows, math.ceil(latest_ms / window_ms) + 1)
    changes = np.zeros(size + 1, dtype=np.int32)
    for cue in cues:
        start = max(0, math.floor(cue.start.total_seconds() * 1_000 / window_ms))
        end = min(size, math.ceil(cue.end.total_seconds() * 1_000 / window_ms))
        if end <= start:
            continue
        changes[start] += 1
        changes[end] -= 1
    return np.cumsum(changes[:-1]) > 0


def _smooth_activity(activity: npt.NDArray[np.bool_]) -> npt.NDArray[np.bool_]:
    """Fill sub-300ms gaps and remove isolated 100ms spikes."""
    if activity.size < 3:
        return activity.astype(np.bool_, copy=True)
    smoothed = activity.astype(np.bool_, copy=True)
    _replace_short_runs(smoothed, value=True, maximum_length=1, replacement=False)
    _replace_short_runs(smoothed, value=False, maximum_length=2, replacement=True)
    return smoothed


def _replace_short_runs(
    values: npt.NDArray[np.bool_],
    *,
    value: bool,
    maximum_length: int,
    replacement: bool,
) -> None:
    start = 0
    while start < values.size:
        if bool(values[start]) != value:
            start += 1
            continue
        end = start + 1
        while end < values.size and bool(values[end]) == value:
            end += 1
        bounded = start > 0 and end < values.size
        if end - start <= maximum_length and (value or bounded):
            values[start:end] = replacement
        start = end


def _best_offset_windows(
    audio_activity: npt.NDArray[np.bool_],
    subtitle_activity: npt.NDArray[np.bool_],
    *,
    max_offset_windows: int,
) -> _SyncMatch | None:
    """Find the subtitle delay that best overlaps detected audio activity."""
    audio = np.asarray(audio_activity, dtype=np.float64)
    subtitle = np.asarray(subtitle_activity, dtype=np.float64)
    if audio.size == 0 or subtitle.size == 0 or not audio.any() or not subtitle.any():
        return None

    correlation_size = audio.size + subtitle.size - 1
    fft_size = 1 << (correlation_size - 1).bit_length()
    correlation = np.fft.irfft(
        np.fft.rfft(audio, fft_size) * np.fft.rfft(subtitle[::-1], fft_size),
        fft_size,
    )[:correlation_size]
    lags = np.arange(-(subtitle.size - 1), audio.size)
    allowed = np.flatnonzero(np.abs(lags) <= max_offset_windows)
    if allowed.size == 0:
        return None

    audio_prefix = np.concatenate(([0.0], np.cumsum(audio)))
    subtitle_prefix = np.concatenate(([0.0], np.cumsum(subtitle)))
    scores = np.zeros(allowed.size, dtype=np.float64)
    total_subtitle_activity = float(subtitle.sum())
    for position, correlation_index in enumerate(allowed):
        lag = int(lags[correlation_index])
        audio_start = max(0, lag)
        subtitle_start = max(0, -lag)
        overlap = min(audio.size - audio_start, subtitle.size - subtitle_start)
        if overlap <= 0:
            continue
        audio_count = audio_prefix[audio_start + overlap] - audio_prefix[audio_start]
        subtitle_count = (
            subtitle_prefix[subtitle_start + overlap] - subtitle_prefix[subtitle_start]
        )
        audio_inactive = overlap - audio_count
        subtitle_inactive = overlap - subtitle_count
        if min(audio_count, subtitle_count, audio_inactive, subtitle_inactive) <= 0:
            continue
        coverage = subtitle_count / total_subtitle_activity
        intersection = correlation[correlation_index]
        numerator = intersection * overlap - audio_count * subtitle_count
        denominator = math.sqrt(
            audio_count * audio_inactive * subtitle_count * subtitle_inactive
        )
        phi = numerator / denominator
        scores[position] = float(max(0.0, phi) * min(1.0, coverage / 0.8))

    best_position = int(np.argmax(scores))
    best_score = float(scores[best_position])
    if best_score <= 0:
        return None
    exclusion = max(1, math.ceil(1_000 / 100))
    competitors = scores.copy()
    competitors[
        max(0, best_position - exclusion) : min(scores.size, best_position + exclusion + 1)
    ] = 0
    runner_up = float(np.max(competitors)) if competitors.size else 0.0
    separation_factor = min(1.0, max(0.0, best_score - runner_up) / 0.1)
    confidence = best_score * separation_factor
    return _SyncMatch(
        offset_windows=int(lags[allowed[best_position]]),
        confidence=confidence,
    )
