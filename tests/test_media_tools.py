import subprocess
from datetime import timedelta
from pathlib import Path

import numpy as np
import pytest
import srt

from paircue.services import media_tools
from paircue.services.audio_tracks import AudioTrackError
from paircue.services.media_tools import (
    EmbeddedSubtitleExtractor,
    SubtitleSynchronizer,
    _best_offset_windows,
    _smooth_activity,
    _subtitle_activity,
)


def test_embedded_extraction_gracefully_skips_when_ffprobe_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    media_path = tmp_path / "movie.mkv"
    media_path.write_bytes(b"media")
    monkeypatch.setattr(media_tools.shutil, "which", lambda command: None)

    assert EmbeddedSubtitleExtractor().extract(media_path, {"en"}) == ()


def test_best_offset_finds_subtitle_delay() -> None:
    subtitle = np.zeros(200, dtype=np.bool_)
    subtitle[[10, 11, 12, 40, 41, 90, 91, 92, 140, 170]] = True
    audio = np.zeros(200, dtype=np.bool_)
    audio[subtitle.nonzero()[0] + 17] = True

    match = _best_offset_windows(audio, subtitle, max_offset_windows=30)

    assert match is not None
    assert match.offset_windows == 17
    assert match.confidence == 1.0


def test_best_offset_finds_subtitle_advance() -> None:
    subtitle = np.zeros(200, dtype=np.bool_)
    subtitle[[20, 21, 50, 80, 81, 130, 160]] = True
    audio = np.zeros(200, dtype=np.bool_)
    audio[subtitle.nonzero()[0] - 12] = True

    match = _best_offset_windows(audio, subtitle, max_offset_windows=20)

    assert match is not None
    assert match.offset_windows == -12


def test_best_offset_returns_none_without_activity() -> None:
    assert (
        _best_offset_windows(
            np.zeros(50, dtype=np.bool_),
            np.zeros(50, dtype=np.bool_),
            max_offset_windows=10,
        )
        is None
    )


def test_random_overlap_does_not_receive_high_confidence() -> None:
    random = np.random.default_rng(42)
    audio = random.random(2_000) < 0.35
    subtitle = random.random(2_000) < 0.25

    match = _best_offset_windows(audio, subtitle, max_offset_windows=100)

    assert match is None or match.confidence < 0.24


def test_subtitle_activity_marks_cue_windows() -> None:
    cues = [
        srt.Subtitle(
            index=1,
            start=timedelta(milliseconds=200),
            end=timedelta(milliseconds=500),
            content="Hello",
        )
    ]

    activity = _subtitle_activity(cues, window_ms=100, minimum_windows=8)

    assert activity.tolist() == [False, False, True, True, True, False, False, False]


def test_smooth_activity_fills_short_gap_and_removes_edge_spike() -> None:
    activity = np.array(
        [True, False, False, False, True, True, False, False, True, True],
        dtype=np.bool_,
    )

    assert _smooth_activity(activity).tolist() == [
        False,
        False,
        False,
        False,
        True,
        True,
        True,
        True,
        True,
        True,
    ]


def test_shift_cues_is_atomic_and_preserves_content(tmp_path: Path) -> None:
    path = tmp_path / "movie.en.srt"
    cue = srt.Subtitle(
        index=1,
        start=timedelta(seconds=1),
        end=timedelta(seconds=2),
        content="Original dialogue",
    )
    path.write_text(srt.compose([cue]), encoding="utf-8")
    synchronizer = SubtitleSynchronizer(window_ms=100)

    assert synchronizer._shift_cues(path, [cue], 15)

    shifted = list(srt.parse(path.read_text(encoding="utf-8")))
    assert shifted[0].start == timedelta(seconds=2.5)
    assert shifted[0].end == timedelta(seconds=3.5)
    assert shifted[0].content == "Original dialogue"
    assert not list(tmp_path.glob(".*.srt"))


def test_sync_applies_only_the_confident_offset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    media_path = tmp_path / "movie.mkv"
    media_path.write_bytes(b"media")
    subtitle_path = tmp_path / "movie.en.srt"
    starts = [10, 22, 49, 83, 121, 169]
    cues = [
        srt.Subtitle(
            index=index,
            start=timedelta(milliseconds=start * 100),
            end=timedelta(milliseconds=(start + 1) * 100),
            content=f"Cue {index}",
        )
        for index, start in enumerate(starts, 1)
    ]
    subtitle_path.write_text(srt.compose(cues), encoding="utf-8")
    subtitle_activity = _subtitle_activity(cues, window_ms=100, minimum_windows=220)
    audio_activity = np.zeros(220, dtype=np.bool_)
    audio_activity[np.flatnonzero(subtitle_activity) + 15] = True

    command: list[str] = []

    def fake_run(arguments: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        command.extend(arguments)
        Path(arguments[-1]).write_bytes(b"temporary pcm" * 10)
        return subprocess.CompletedProcess(arguments, 0, "", "")

    synchronizer = SubtitleSynchronizer(window_ms=100)
    monkeypatch.setattr(media_tools, "_required_binary", lambda _: "ffmpeg")
    monkeypatch.setattr(media_tools.subprocess, "run", fake_run)
    monkeypatch.setattr(synchronizer, "_audio_activity", lambda _: audio_activity)
    monkeypatch.setattr(media_tools, "select_audio_stream", lambda *_: 3)

    assert synchronizer.sync(media_path, subtitle_path)

    shifted = list(srt.parse(subtitle_path.read_text(encoding="utf-8")))
    assert shifted[0].start == cues[0].start + timedelta(seconds=1.5)
    assert not list(tmp_path.glob("*.paircue-sync.wav"))
    assert command[command.index("-protocol_whitelist") + 1] == "file,crypto,data"
    assert command[command.index("-map") + 1] == "0:3"


def test_ambiguous_audio_keeps_original_subtitle_timing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    subtitle = tmp_path / "movie.en.srt"
    original = "1\n00:00:01,000 --> 00:00:02,000\nUnchanged (not removed)\n\n"
    subtitle.write_text(original, encoding="utf-8")

    def ambiguous(*args: object) -> int:
        raise AudioTrackError("ambiguous")

    def no_decode(*args: object, **kwargs: object) -> None:
        raise AssertionError("ambiguous audio must not be decoded")

    monkeypatch.setattr(media_tools, "select_audio_stream", ambiguous)
    monkeypatch.setattr(media_tools.subprocess, "run", no_decode)
    assert not SubtitleSynchronizer().sync(tmp_path / "movie.mkv", subtitle)
    assert subtitle.read_text(encoding="utf-8") == original
    assert not list(tmp_path.glob("*.paircue-sync.wav"))
