from __future__ import annotations

import csv
import json
import logging
import math
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, Protocol, cast

import httpx
import srt

from paircue import __version__
from paircue.ai_connections import validate_ai_connection
from paircue.services.atomic import atomic_write_bytes
from paircue.services.audio_tracks import AudioTrackError, select_audio_stream
from paircue.services.provider_privacy import (
    ProviderResponseTooLargeError,
    private_provider_diagnostics,
    safe_provider_failure,
)

log = logging.getLogger(__name__)

MAX_AUDIO_CHUNK_BYTES = 24 * 1024 * 1024
MAX_RESPONSE_BYTES = 10 * 1024 * 1024
MAX_SEGMENTS_PER_CHUNK = 20_000
LOCAL_MEDIA_PROTOCOLS = "file,crypto,data"


class TranscriptionError(RuntimeError):
    pass


class Transcriber(Protocol):
    def transcribe(self, media_path: Path, output_path: Path, language: str) -> Path: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class AudioChunk:
    path: Path
    offset_seconds: float
    duration_seconds: float


@dataclass(frozen=True, slots=True)
class TranscriptionConfig:
    base_url: str
    api_key: str = field(repr=False)
    model: str
    timeout_seconds: float = 300
    max_attempts: int = 3
    chunk_seconds: int = 300
    prompt: str = ""
    provider: str = "custom"
    approved_origin: str = ""
    audio_stream_index: int | None = None


class OpenAICompatibleTranscriber:
    """Generate an SRT through the documented multipart transcription API.

    Media segmentation and response validation are SubDuet code. The configured service can be
    OpenAI or a user-operated endpoint that implements the same transcription contract.
    """

    def __init__(
        self,
        config: TranscriptionConfig,
        *,
        temporary_root: Path,
        client: httpx.Client | None = None,
    ) -> None:
        self.config = config
        self.base_url = validate_ai_connection(
            config.base_url, config.approved_origin, config.provider
        )
        self.temporary_root = temporary_root
        self.temporary_root.mkdir(parents=True, exist_ok=True)
        self._owns_client = client is None
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(config.timeout_seconds),
            follow_redirects=False,
        )

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def transcribe(self, media_path: Path, output_path: Path, language: str) -> Path:
        if output_path.exists():
            return output_path
        with tempfile.TemporaryDirectory(
            dir=self.temporary_root,
            prefix="paircue-transcribe-",
        ) as directory:
            chunks = self._segment_audio(media_path, Path(directory), language)
            cues: list[srt.Subtitle] = []
            for chunk in chunks:
                cues.extend(self._transcribe_chunk(chunk, language))
        if not cues:
            raise TranscriptionError("transcription returned no timestamped dialogue")
        rendered = cast(str, srt.compose(cues, reindex=True))
        atomic_write_bytes(output_path, rendered.encode("utf-8"))
        return output_path

    def _segment_audio(
        self, media_path: Path, directory: Path, language: str,
    ) -> tuple[AudioChunk, ...]:
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            raise TranscriptionError("FFmpeg is required for subtitle generation")
        try:
            audio_index = select_audio_stream(media_path, language, self.config.audio_stream_index)
        except AudioTrackError as exc:
            raise TranscriptionError(str(exc)) from None
        pattern = directory / "chunk-%05d.flac"
        segment_list = directory / "chunks.csv"
        result = subprocess.run(  # noqa: S603 - resolved executable and fixed argument array
            [
                ffmpeg,
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
                "16000",
                "-c:a",
                "flac",
                "-f",
                "segment",
                "-segment_time",
                str(self.config.chunk_seconds),
                "-reset_timestamps",
                "1",
                "-segment_list",
                str(segment_list),
                "-segment_list_type",
                "csv",
                str(pattern),
            ],
            capture_output=True,
            text=True,
            timeout=3_600,
            check=False,
        )
        if result.returncode != 0:
            detail = result.stderr.strip()[-500:] or "audio stream could not be decoded"
            raise TranscriptionError(f"FFmpeg audio segmentation failed: {detail}")

        chunks: list[AudioChunk] = []
        try:
            rows = list(csv.reader(segment_list.read_text(encoding="utf-8").splitlines()))
        except OSError as exc:
            raise TranscriptionError("FFmpeg returned no audio segment list") from exc
        if not rows or len(rows) > 10_000:
            raise TranscriptionError("FFmpeg returned an invalid number of audio chunks")
        for index, row in enumerate(rows):
            if len(row) != 3 or row[0] != f"chunk-{index:05d}.flac":
                raise TranscriptionError("FFmpeg returned an invalid audio segment list")
            path = directory / row[0]
            size = path.stat().st_size
            if size <= 0 or size > MAX_AUDIO_CHUNK_BYTES:
                raise TranscriptionError(f"generated audio chunk has an invalid size: {size}")
            try:
                start, end = float(row[1]), float(row[2])
            except ValueError as exc:
                raise TranscriptionError("FFmpeg returned invalid audio segment timing") from exc
            if (
                not math.isfinite(start)
                or not math.isfinite(end)
                or start < 0
                or end <= start
                or end - start > self.config.chunk_seconds + 5
            ):
                raise TranscriptionError("FFmpeg returned invalid audio segment timing")
            chunks.append(AudioChunk(path, start, end - start))
        if not chunks:
            raise TranscriptionError("media contains no readable audio stream")
        return tuple(chunks)

    def _transcribe_chunk(
        self,
        chunk: AudioChunk,
        language: str,
    ) -> list[srt.Subtitle]:
        if chunk.path.stat().st_size > MAX_AUDIO_CHUNK_BYTES:
            raise TranscriptionError("audio chunk exceeds the upload size limit")
        data = {
            "model": self.config.model,
            "language": language.split("-", 1)[0].casefold(),
            "response_format": "verbose_json",
            "timestamp_granularities[]": "segment",
            "temperature": "0",
        }
        if self.config.prompt:
            data["prompt"] = self.config.prompt
        headers = {
            "Accept": "application/json",
            "User-Agent": f"SubDuet/{__version__}",
        }
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"

        last_error = "transcription provider failed"
        for attempt in range(1, self.config.max_attempts + 1):
            try:
                with (
                    private_provider_diagnostics(),
                    chunk.path.open("rb") as audio,
                    self.client.stream(
                        "POST",
                        f"{self.base_url}/audio/transcriptions",
                        headers=headers,
                        data=data,
                        files={"file": (chunk.path.name, audio, "audio/flac")},
                        follow_redirects=False,
                    ) as response,
                ):
                    response.raise_for_status()
                    content = bytearray()
                    for part in response.iter_bytes():
                        content.extend(part)
                        if len(content) > MAX_RESPONSE_BYTES:
                            raise ProviderResponseTooLargeError
                return self._parse_segments(
                    json.loads(content),
                    chunk.offset_seconds,
                    chunk.duration_seconds,
                )
            except (
                OSError,
                httpx.HTTPError,
                TypeError,
                ValueError,
                OverflowError,
                TranscriptionError,
            ) as exc:
                last_error = safe_provider_failure(exc)
                if attempt < self.config.max_attempts:
                    time.sleep(min(2**attempt, 10))
        raise TranscriptionError(last_error) from None

    @staticmethod
    def _parse_segments(
        payload: Any,
        offset_seconds: float,
        duration_seconds: float,
    ) -> list[srt.Subtitle]:
        if not isinstance(payload, dict):
            raise TranscriptionError("transcription response is not an object")
        rows = payload.get("segments")
        if not isinstance(rows, list) or len(rows) > MAX_SEGMENTS_PER_CHUNK:
            raise TranscriptionError("transcription response has an invalid segments array")
        cues: list[srt.Subtitle] = []
        for row in rows:
            if not isinstance(row, dict):
                raise TranscriptionError("transcription segment is not an object")
            start = OpenAICompatibleTranscriber._finite_number(row.get("start"), "start")
            end = OpenAICompatibleTranscriber._finite_number(row.get("end"), "end")
            text = row.get("text")
            if start < 0 or end <= start or end > duration_seconds + 5:
                raise TranscriptionError("transcription segment has invalid timing")
            if not isinstance(text, str):
                raise TranscriptionError("transcription segment has invalid text")
            normalized = " ".join(text.split()).strip()
            if not normalized:
                continue
            cues.append(
                srt.Subtitle(
                    index=len(cues) + 1,
                    start=timedelta(seconds=offset_seconds + start),
                    end=timedelta(seconds=offset_seconds + end),
                    content=normalized,
                )
            )
        return cues

    @staticmethod
    def _finite_number(value: Any, field: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TranscriptionError(f"transcription segment has invalid {field}")
        number = float(value)
        if not math.isfinite(number):
            raise TranscriptionError(f"transcription segment has invalid {field}")
        return number
