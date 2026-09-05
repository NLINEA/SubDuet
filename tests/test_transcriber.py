import csv
import subprocess
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
import srt

from paircue.services.transcriber import (
    AudioChunk,
    OpenAICompatibleTranscriber,
    TranscriptionConfig,
    TranscriptionError,
)


def _transcriber(
    tmp_path: Path,
    handler: httpx.MockTransport,
    *,
    max_attempts: int = 1,
) -> OpenAICompatibleTranscriber:
    return OpenAICompatibleTranscriber(
        TranscriptionConfig(
            base_url="https://api.openai.com/v1",
            api_key="secret-key",
            model="whisper-1",
            max_attempts=max_attempts,
        ),
        temporary_root=tmp_path / "work",
        client=httpx.Client(transport=handler),
    )


def test_transcription_request_uses_timestamped_multipart_contract(tmp_path: Path) -> None:
    chunk = tmp_path / "chunk.flac"
    chunk.write_bytes(b"FLAC audio")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/audio/transcriptions"
        assert request.headers["Authorization"] == "Bearer secret-key"
        assert request.headers["User-Agent"].startswith("SubDuet/")
        assert b'name="model"\r\n\r\nwhisper-1' in request.content
        assert b'name="language"\r\n\r\nja' in request.content
        assert b'name="timestamp_granularities[]"\r\n\r\nsegment' in request.content
        return httpx.Response(
            200,
            json={
                "segments": [
                    {"start": 1.25, "end": 2.5, "text": "  hello   world  "},
                ]
            },
        )

    transcriber = _transcriber(tmp_path, httpx.MockTransport(handler))

    cues = transcriber._transcribe_chunk(AudioChunk(chunk, 300, 10), "ja-JP")

    assert cues[0].start == timedelta(seconds=301.25)
    assert cues[0].end == timedelta(seconds=302.5)
    assert cues[0].content == "hello world"


def test_audio_segmentation_rejects_non_local_ffmpeg_protocols(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transcriber = _transcriber(tmp_path, httpx.MockTransport(lambda _: httpx.Response(500)))
    media = tmp_path / "movie.mkv"
    media.write_bytes(b"media")
    output_directory = tmp_path / "segments"
    output_directory.mkdir()
    command: list[str] = []

    def fake_run(arguments: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        command.extend(arguments)
        chunk = output_directory / "chunk-00000.flac"
        chunk.write_bytes(b"audio")
        with (output_directory / "chunks.csv").open("w", newline="", encoding="utf-8") as file:
            csv.writer(file).writerow([chunk.name, "0", "1"])
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr("paircue.services.transcriber.shutil.which", lambda _: "ffmpeg")
    monkeypatch.setattr("paircue.services.transcriber.subprocess.run", fake_run)

    chunks = transcriber._segment_audio(media, output_directory)

    assert len(chunks) == 1
    assert command[command.index("-protocol_whitelist") + 1] == "file,crypto,data"


def test_transcribe_combines_chunks_and_writes_only_complete_srt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={"segments": [{"start": 1.0, "end": 2.0, "text": f"Chunk {calls}"}]},
        )

    transcriber = _transcriber(tmp_path, httpx.MockTransport(handler))

    def fake_segments(_: Path, directory: Path) -> tuple[AudioChunk, ...]:
        first = directory / "chunk-00000.flac"
        second = directory / "chunk-00001.flac"
        first.write_bytes(b"first")
        second.write_bytes(b"second")
        return AudioChunk(first, 0, 300), AudioChunk(second, 300, 300)

    monkeypatch.setattr(transcriber, "_segment_audio", fake_segments)
    output = tmp_path / "movie.en.srt"

    assert transcriber.transcribe(tmp_path / "movie.mkv", output, "en") == output

    cues = list(srt.parse(output.read_text(encoding="utf-8")))
    assert [cue.content for cue in cues] == ["Chunk 1", "Chunk 2"]
    assert cues[1].start == timedelta(seconds=301)
    assert not list((tmp_path / "work").iterdir())


@pytest.mark.parametrize(
    "payload",
    [
        {"segments": "not-a-list"},
        {"segments": [{"start": -1, "end": 2, "text": "bad"}]},
        {"segments": [{"start": 2, "end": 1, "text": "bad"}]},
        {"segments": [{"start": 0, "end": 1, "text": 42}]},
    ],
)
def test_invalid_timestamp_response_is_rejected(payload: object) -> None:
    with pytest.raises(TranscriptionError):
        OpenAICompatibleTranscriber._parse_segments(payload, 0, 10)


def test_partial_transcription_never_replaces_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                200,
                json={"segments": [{"start": 0, "end": 1, "text": "valid"}]},
            )
        return httpx.Response(200, json={"segments": "invalid"})

    transcriber = _transcriber(tmp_path, httpx.MockTransport(handler))

    def fake_segments(_: Path, directory: Path) -> tuple[AudioChunk, ...]:
        first = directory / "chunk-00000.flac"
        second = directory / "chunk-00001.flac"
        first.write_bytes(b"first")
        second.write_bytes(b"second")
        return AudioChunk(first, 0, 300), AudioChunk(second, 300, 300)

    monkeypatch.setattr(transcriber, "_segment_audio", fake_segments)
    output = tmp_path / "movie.en.srt"

    with pytest.raises(TranscriptionError):
        transcriber.transcribe(tmp_path / "movie.mkv", output, "en")

    assert not output.exists()


def test_transcription_response_is_bounded_while_streaming(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chunk = tmp_path / "chunk.flac"
    chunk.write_bytes(b"FLAC audio")
    monkeypatch.setattr("paircue.services.transcriber.MAX_RESPONSE_BYTES", 20)

    transcriber = _transcriber(
        tmp_path,
        httpx.MockTransport(lambda _: httpx.Response(200, content=b"x" * 21)),
    )

    with pytest.raises(TranscriptionError, match="response exceeds"):
        transcriber._transcribe_chunk(AudioChunk(chunk, 0, 10), "en")
