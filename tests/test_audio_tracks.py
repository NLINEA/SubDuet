import shutil
import subprocess
from pathlib import Path

import httpx
import numpy as np
import pytest

from paircue.services.audio_tracks import (
    AudioTrack,
    AudioTrackError,
    choose_audio_stream,
    read_audio_tracks,
    select_audio_stream,
)
from paircue.services.transcriber import (
    OpenAICompatibleTranscriber,
    TranscriptionConfig,
    TranscriptionError,
)


def test_language_beats_stream_order_and_default() -> None:
    tracks = (AudioTrack(1, "ja", default=True), AudioTrack(3, "en"))
    assert choose_audio_stream(tracks, "en-US") == 3
    assert choose_audio_stream(tracks, "ja") == 1


def test_commentary_is_not_automatically_selected() -> None:
    tracks = (AudioTrack(0, "en", default=True, commentary=True), AudioTrack(2, "en"))
    assert choose_audio_stream(tracks, "en") == 2
    assert choose_audio_stream(tracks, "en", override=0) == 0


def test_regional_audio_and_unambiguous_defaults() -> None:
    tracks = (AudioTrack(0, "pt-BR"), AudioTrack(1, "pt-PT", default=True))
    assert choose_audio_stream(tracks, "pt-BR") == 0
    assert choose_audio_stream(tracks, "pt") == 1
    with pytest.raises(AudioTrackError):
        choose_audio_stream((AudioTrack(0, "zh-TW"),), "zh-HK")


@pytest.mark.parametrize("tracks", [
    (AudioTrack(0, "en"), AudioTrack(1, "en")),
    (AudioTrack(0, None, default=True), AudioTrack(1, None)),
    (AudioTrack(0, "ja", default=True),),
])
def test_ambiguous_or_wrong_language_is_not_guessed(tracks: tuple[AudioTrack, ...]) -> None:
    with pytest.raises(AudioTrackError, match="No audio was uploaded"):
        choose_audio_stream(tracks, "en")
    assert choose_audio_stream(tracks, "en", override=0) == 0
    with pytest.raises(AudioTrackError, match="does not exist"):
        choose_audio_stream(tracks, "en", override=99)


def test_single_unlabelled_stream_can_be_used_without_guessing_between_tracks() -> None:
    assert choose_audio_stream((AudioTrack(2, None),), "ko") == 2


def test_ambiguous_audio_stops_before_transcription_network_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "paircue.services.audio_tracks.read_audio_tracks",
        lambda _: (AudioTrack(0, "en"), AudioTrack(1, "en")),
    )
    monkeypatch.setattr("paircue.services.transcriber.shutil.which", lambda _: "ffmpeg")
    requests = []
    transcriber = OpenAICompatibleTranscriber(
        TranscriptionConfig(
            "https://ai.example/v1", "test-only-key", "model",
            approved_origin="https://ai.example",
        ),
        temporary_root=tmp_path / "work",
        client=httpx.Client(transport=httpx.MockTransport(
            lambda request: requests.append(request) or httpx.Response(500)
        )),
    )
    output = tmp_path / "result.srt"
    try:
        with pytest.raises(TranscriptionError, match="audio-stream-index"):
            transcriber.transcribe(tmp_path / "movie.mkv", output, "en")
        assert not requests
        assert not output.exists()
        assert not list((tmp_path / "work").iterdir())
    finally:
        transcriber.client.close()


def test_real_ffmpeg_decodes_the_requested_second_language(tmp_path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None or shutil.which("ffprobe") is None:
        pytest.skip("FFmpeg and FFprobe are required for the synthetic multi-track regression")
    media = tmp_path / "two-language-tones.mkv"
    subprocess.run(  # noqa: S603 - fixed synthetic sources; no user media or shell
        [ffmpeg, "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
         "-f", "lavfi", "-i", "sine=frequency=880:duration=1", "-map", "0:a", "-map", "1:a",
         "-c:a", "pcm_s16le", "-metadata:s:a:0", "language=jpn",
         "-metadata:s:a:1", "language=eng", str(media)],
        check=True, capture_output=True, timeout=30,
    )
    assert [track.language for track in read_audio_tracks(media)] == ["ja", "en"]
    assert select_audio_stream(media, "en") == 1
    transcriber = OpenAICompatibleTranscriber(
        TranscriptionConfig(
            "http://127.0.0.1:9000/v1", "", "model",
            approved_origin="http://127.0.0.1:9000",
        ), temporary_root=tmp_path / "work",
    )
    segments = tmp_path / "segments"
    segments.mkdir()
    try:
        chunk, = transcriber._segment_audio(media, segments, "en")
        pcm = subprocess.run(  # noqa: S603 - generated FLAC, fixed PCM decoder arguments
            [ffmpeg, "-v", "error", "-i", str(chunk.path), "-f", "s16le", "-"],
            check=True, capture_output=True, timeout=30,
        ).stdout
        samples = np.frombuffer(pcm, dtype="<i2")
        frequencies = np.fft.rfftfreq(len(samples), 1 / 16000)
        dominant = frequencies[np.argmax(np.abs(np.fft.rfft(samples)))]
        # The second (English-labelled) track, not first-track 440 Hz.
        assert abs(dominant - 880) < 5
    finally:
        transcriber.close()
