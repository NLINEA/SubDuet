import json
import threading
from pathlib import Path

import pytest

from paircue import cli, diagnostics
from paircue.cli import _default_setup_output, main
from paircue.config import PairCueSettings
from paircue.models import MediaItem, ProcessResult
from paircue.services import atomic
from paircue.services.readability import ReadabilityProfile
from paircue.services.subtitle_files import parse_srt
from paircue.setup_server import SetupState

SOURCE = """1
00:00:00,000 --> 00:00:02,000
Hello world

"""

TARGET = """1
00:00:00,050 --> 00:00:01,000
你好

2
00:00:01,000 --> 00:00:02,050
世界

"""


def test_pair_readability_review_is_saved_and_custom_profile_never_rewrites(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "source.srt"
    target = tmp_path / "target.srt"
    source.write_text(SOURCE.replace("Hello world", "A" * 48))
    target.write_text(TARGET)
    original = (source.read_bytes(), target.read_bytes())
    output = tmp_path / "review.srt"
    assert main(["pair", str(source), str(target), "-o", str(output)]) == 0
    notice = capsys.readouterr().out
    assert "Saved pairing for review" in notice
    assert "source line length 48 codepoints exceeds 42" in notice
    assert "partial" not in notice

    profile = tmp_path / "profile.json"
    profile.write_text(ReadabilityProfile(source_line_codepoints_max=48).model_dump_json())
    relaxed = tmp_path / "relaxed.srt"
    assert main(["pair", str(source), str(target), "-o", str(relaxed),
                 "--readability-profile", str(profile)]) == 0
    clean_notice = capsys.readouterr().out
    assert "Created" in clean_notice
    assert "Readability review needed" not in clean_notice
    assert output.read_bytes() == relaxed.read_bytes()
    assert (source.read_bytes(), target.read_bytes()) == original


def test_invalid_readability_profile_retains_output_then_explicit_retry_succeeds(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "source.srt"
    target = tmp_path / "target.srt"
    output = tmp_path / "old.srt"
    profile = tmp_path / "profile.json"
    source.write_text(SOURCE)
    target.write_text(TARGET)
    output.write_bytes(b"old output")
    profile.write_text('{"total_lines_max":0}')
    args = ["pair", str(source), str(target), "-o", str(output), "--overwrite",
            "--readability-profile", str(profile)]
    assert main(args) == 2
    assert output.read_bytes() == b"old output"
    assert "total_lines_max" in capsys.readouterr().err
    profile.write_text('{"total_lines_max":4}')
    assert main(args) == 0
    assert [c.content for c in parse_srt(output)] == ["你好\n世界\nHello world"]


class RecordingPipeline:
    def __init__(self, output: Path) -> None:
        self.output = output
        self.items: list[MediaItem] = []
        self.closed = False

    def process(self, item: MediaItem) -> ProcessResult:
        self.items.append(item)
        self.output.write_text(TARGET, encoding="utf-8")
        return ProcessResult("completed", "created learning track", (self.output,))

    def close(self) -> None:
        self.closed = True


def test_version_command_reports_packaged_version(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--version"])

    assert exc_info.value.code == 0
    assert capsys.readouterr().out.strip().startswith("subduet 0.1.0")


def test_pair_command_creates_bilingual_srt(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "movie.en.srt"
    target = tmp_path / "movie.zh-TW.srt"
    output = tmp_path / "movie.zh-TW.cc.srt"
    source.write_text(SOURCE, encoding="utf-8")
    target.write_text(TARGET, encoding="utf-8")

    result = main(["pair", str(source), str(target), "-o", str(output)])

    assert result == 0
    assert output.exists()
    assert "你好\n世界\nHello world" in output.read_text(encoding="utf-8")
    assert "100%/100% matched" in capsys.readouterr().out


@pytest.mark.parametrize("overwrite", [False, True])
def test_pair_command_will_not_overwrite_an_input(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    overwrite: bool,
) -> None:
    source = tmp_path / "movie.en.srt"
    target = tmp_path / "movie.zh-TW.srt"
    source.write_text(SOURCE, encoding="utf-8")
    target.write_text(TARGET, encoding="utf-8")

    options = ["--overwrite"] if overwrite else []
    result = main(["pair", str(source), str(target), "-o", str(source), *options])

    assert result == 2
    assert "must not overwrite" in capsys.readouterr().err
    assert source.read_text(encoding="utf-8") == SOURCE


def _pair_inputs(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "movie.en.srt"
    target = tmp_path / "movie.zh-TW.srt"
    source.write_text(SOURCE, encoding="utf-8")
    target.write_text(TARGET, encoding="utf-8")
    return source, target


def _partial_inputs(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "movie.en.srt"
    target = tmp_path / "movie.zh-TW.srt"
    source.write_text("\n\n".join(
        f"{i + 1}\n00:00:{i * 4 + 1:02},000 --> 00:00:{i * 4 + 4:02},000\nEnglish {i + 1}"
        for i in range(4)
    ) + "\n\n", encoding="utf-8")
    target.write_text("\n\n".join(
        f"{i + 1}\n00:{'01' if i == 3 else '00'}:{i * 4 + 1:02},000 --> "
        f"00:{'01' if i == 3 else '00'}:{i * 4 + 4:02},000\n中文 {i + 1}"
        for i in range(4)
    ) + "\n\n", encoding="utf-8")
    return source, target


def test_pair_retains_an_existing_output_until_explicit_overwrite(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    source, target = _pair_inputs(tmp_path)
    output = tmp_path / "movie.mul.srt"
    previous = b"previous approved output\n"
    output.write_bytes(previous)
    command = ["pair", str(source), str(target), "-o", str(output)]

    assert main(command) == 2
    assert output.read_bytes() == previous
    assert "--overwrite" in capsys.readouterr().err
    assert main([*command, "--overwrite"]) == 0
    assert "你好\n世界\nHello world" in output.read_text(encoding="utf-8")
    assert source.read_text(encoding="utf-8") == SOURCE
    assert target.read_text(encoding="utf-8") == TARGET


def test_pair_preserves_an_output_created_during_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, target = _pair_inputs(tmp_path)
    output = tmp_path / "movie.mul.srt"
    original_link = atomic.os.link

    def competing_writer(temporary: Path, destination: Path) -> None:
        destination.write_bytes(b"another writer's approved output")
        original_link(temporary, destination)

    monkeypatch.setattr(atomic.os, "link", competing_writer)
    assert main(["pair", str(source), str(target), "-o", str(output)]) == 2
    assert output.read_bytes() == b"another writer's approved output"
    assert not list(tmp_path.glob(".*.tmp"))


def test_pair_failed_publication_leaves_no_empty_output_and_can_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, target = _pair_inputs(tmp_path)
    output = tmp_path / "movie.mul.srt"
    command = ["pair", str(source), str(target), "-o", str(output)]

    def fail_publication(*args: object) -> None:
        raise PermissionError("synthetic write failure")

    with monkeypatch.context() as fault:
        fault.setattr(atomic.os, "link", fail_publication)
        assert main(command) == 2
    assert not output.exists()
    assert not list(tmp_path.glob(".*.tmp"))
    assert main(command) == 0
    assert output.is_file()


def test_pair_failed_explicit_overwrite_retains_previous_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, target = _pair_inputs(tmp_path)
    output = tmp_path / "movie.mul.srt"
    output.write_bytes(b"previous approved output")

    def fail_replacement(*args: object) -> None:
        raise PermissionError("synthetic replacement failure")

    monkeypatch.setattr(atomic.os, "replace", fail_replacement)
    assert main(["pair", str(source), str(target), "-o", str(output), "--overwrite"]) == 2
    assert output.read_bytes() == b"previous approved output"
    assert not list(tmp_path.glob(".*.tmp"))


@pytest.mark.parametrize("alias_kind", ["symlink", "hardlink"])
def test_pair_explicit_overwrite_cannot_replace_an_input_alias(
    tmp_path: Path, alias_kind: str,
) -> None:
    source, target = _pair_inputs(tmp_path)
    output = tmp_path / "alias.srt"
    if alias_kind == "symlink":
        output.symlink_to(source)
    else:
        output.hardlink_to(source)
    assert main(["pair", str(source), str(target), "-o", str(output), "--overwrite"]) == 2
    assert source.read_text(encoding="utf-8") == SOURCE


def test_pair_reports_retained_unmatched_output_cues(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    source, target = _partial_inputs(tmp_path)
    before = (source.read_bytes(), target.read_bytes())
    output = tmp_path / "movie.mul.srt"
    assert main(["pair", str(source), str(target), "-o", str(output)]) == 0

    message = capsys.readouterr().out
    assert "Saved partial pairing" in message
    assert "5 subtitle cues (75%/75% matched)" in message
    assert "Review needed" in message
    assert "Source-only output SRT cues: 4" in message
    assert "Target-only output SRT cues: 5" in message
    assert "5 bilingual cues" not in message
    cues = parse_srt(output)
    assert cues[3].content == "English 4"
    assert cues[4].content == "中文 4"
    assert before == (source.read_bytes(), target.read_bytes())


def test_pair_strict_partial_rejection_preserves_previous_result_and_can_retry(
    tmp_path: Path,
) -> None:
    source, target = _partial_inputs(tmp_path)
    output = tmp_path / "movie.mul.srt"
    output.write_bytes(b"previous approved output")
    command = ["pair", str(source), str(target), "-o", str(output), "--overwrite"]
    assert main([*command, "--min-match-ratio", "1"]) == 2
    assert output.read_bytes() == b"previous approved output"
    target.write_text(source.read_text().replace("English", "中文"), encoding="utf-8")
    assert main([*command, "--min-match-ratio", "1"]) == 0


def test_desktop_quick_pair_returns_unmatched_output_cue_numbers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, target = _partial_inputs(tmp_path)
    choices = iter((source, target))
    monkeypatch.setattr(cli, "_choose_subtitle_path", lambda role: next(choices))
    monkeypatch.setattr(cli, "_reveal_path", lambda path: None)
    result = cli._quick_pair_subtitles("target-first")
    assert result is not None
    assert not result.fully_paired
    assert result.unmatched_source_cues == (4,)
    assert result.unmatched_target_cues == (5,)
    assert "Review needed" in result.review_notice


def test_desktop_quick_pair_propagates_readability_only_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, target = _pair_inputs(tmp_path)
    source.write_text(SOURCE.replace("Hello world", "A" * 48), encoding="utf-8")
    before = (source.read_bytes(), target.read_bytes())
    choices = iter((source, target))
    monkeypatch.setattr(cli, "_choose_subtitle_path", lambda role: next(choices))
    monkeypatch.setattr(cli, "_reveal_path", lambda path: None)

    result = cli._quick_pair_subtitles("source-first")

    assert result is not None and result.fully_paired and result.needs_review
    assert result.unmatched_source_cues == result.unmatched_target_cues == ()
    assert "source line length 48 codepoints exceeds 42" in result.review_notice
    assert "partial" not in result.review_notice
    assert [cue.content for cue in parse_srt(result.output)] == ["A" * 48 + "\n你好\n世界"]
    assert before == (source.read_bytes(), target.read_bytes())


def test_desktop_quick_pair_creates_a_new_local_output_without_overwriting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "Movie.ja.srt"
    target = tmp_path / "Movie.en.srt"
    source.write_text(SOURCE, encoding="utf-8")
    target.write_text(TARGET, encoding="utf-8")
    selections = iter((source, target, source, target))
    monkeypatch.setattr(cli, "_choose_subtitle_path", lambda role: next(selections))
    revealed: list[Path] = []
    monkeypatch.setattr(cli, "_reveal_path", revealed.append)

    first = cli._quick_pair_subtitles("target-first")
    second = cli._quick_pair_subtitles("target-first")

    assert first is not None
    assert second is not None
    assert first.output == tmp_path / "Movie.mul.srt"
    assert second.output == tmp_path / "Movie.paircue-2.mul.srt"
    assert "你好\n世界\nHello world" in first.output.read_text(encoding="utf-8")
    assert source.read_text(encoding="utf-8") == SOURCE
    assert target.read_text(encoding="utf-8") == TARGET
    assert revealed == [first.output, second.output]


def test_desktop_quick_pair_removes_its_reservation_when_writing_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "Movie.ja.srt"
    target = tmp_path / "Movie.en.srt"
    source.write_text(SOURCE, encoding="utf-8")
    target.write_text(TARGET, encoding="utf-8")
    selections = iter((source, target))
    monkeypatch.setattr(cli, "_choose_subtitle_path", lambda role: next(selections))
    monkeypatch.setattr(
        cli,
        "write_srt",
        lambda path, subtitles: (_ for _ in ()).throw(PermissionError("private path")),
    )

    with pytest.raises(cli.SetupQuickPairError, match="permissions"):
        cli._quick_pair_subtitles("target-first")

    assert not (tmp_path / "Movie.mul.srt").exists()


def test_desktop_safe_demo_creates_only_project_owned_dialogue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revealed: list[Path] = []
    monkeypatch.setattr(cli, "_reveal_path", revealed.append)

    result = cli._quick_pair_demo("target-first", tmp_path)

    assert result.output == tmp_path / "SubDuet Demo.mul.srt"
    assert result.source_match_ratio == 1
    assert result.target_match_ratio == 1
    assert result.output.read_text(encoding="utf-8") == (
        "1\n"
        "00:00:01,000 --> 00:00:03,520\n"
        "¿Por dónde empezamos?\n"
        "Where should we begin?\n\n"
        "2\n"
        "00:00:04,100 --> 00:00:06,800\n"
        "Una escena a la vez.\n"
        "With one scene at a time.\n\n"
    )
    assert revealed == [result.output]


def test_setup_command_opens_packaged_private_wizard(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = main(["setup", "--no-open"])

    assert result == 0
    assert capsys.readouterr().out.strip().endswith("/paircue/setup/index.html")


def test_desktop_build_uses_the_native_private_settings_folder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("paircue.cli.sys.frozen", True, raising=False)
    monkeypatch.setattr("paircue.cli.sys.platform", "darwin")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    assert _default_setup_output() == (
        tmp_path / "Library" / "Application Support" / "PairCue" / "paircue.env"
    )


def test_desktop_folder_picker_uses_the_available_native_dialog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected = tmp_path / "Media"
    observed: list[list[str]] = []
    monkeypatch.setattr(cli.sys, "platform", "linux")
    monkeypatch.setattr(
        cli.shutil,
        "which",
        lambda name: "/usr/bin/zenity" if name == "zenity" else None,
    )

    def run_dialog(command: list[str], **kwargs: object) -> object:
        observed.append(command)
        return cli.subprocess.CompletedProcess(command, 0, stdout=f"{selected}\n", stderr="")

    monkeypatch.setattr(cli.subprocess, "run", run_dialog)

    assert cli._choose_media_directory() == selected
    assert observed == [
        [
            "/usr/bin/zenity",
            "--file-selection",
            "--directory",
            "--title=Choose your media folder for SubDuet",
        ]
    ]


def test_bare_paircue_opens_setup_and_reports_saved_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "paircue.env"
    state = SetupState(threading.Event(), output_path=output, mode="library")

    def finish_setup(*args: object, **kwargs: object) -> SetupState:
        return state

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "paircue.cli.run_setup_wizard",
        finish_setup,
    )

    result = main([])

    assert result == 0
    assert f"Saved private configuration: {output}" in capsys.readouterr().out


def test_desktop_quick_pair_can_finish_without_saving_setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "Movie.mul.srt"
    state = SetupState(threading.Event(), quick_pair_output=output)
    monkeypatch.setattr("paircue.cli.run_setup_wizard", lambda *args, **kwargs: state)

    assert main([]) == 0
    assert f"Created bilingual subtitle: {output}" in capsys.readouterr().out


def test_bare_paircue_continues_from_setup_to_native_video_picker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = tmp_path / "paircue.env"
    config.write_text(
        'PAIRCUE_PLATFORM="filesystem"\n'
        'PAIRCUE_SOURCE_LANGUAGE="ja"\n'
        'PAIRCUE_TARGET_LANGUAGE="en"\n',
        encoding="utf-8",
    )
    media = tmp_path / "Lesson.mkv"
    media.write_bytes(b"video")
    output = tmp_path / "Lesson.mul.srt"
    state = SetupState(threading.Event(), output_path=config, mode="single")
    pipeline = RecordingPipeline(output)
    picker_calls = 0

    def choose_media() -> Path:
        nonlocal picker_calls
        picker_calls += 1
        return media

    def finish_setup(
        assets: Path,
        target: Path,
        *,
        on_single_saved: object,
        on_library_saved: object,
        desktop: bool,
        connection_test: object,
        choose_folder: object,
        quick_pair: object,
        demo_pair: object,
    ) -> SetupState:
        assert callable(on_single_saved)
        assert on_library_saved is None
        assert desktop is False
        assert connection_test is None
        assert choose_folder is None
        assert quick_pair is None
        assert demo_pair is None
        on_single_saved(state)
        return state

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("paircue.cli.run_setup_wizard", finish_setup)
    monkeypatch.setattr("paircue.cli._choose_media_path", choose_media)
    monkeypatch.setattr("paircue.cli.build_pipeline", lambda settings: pipeline)
    revealed: list[Path] = []
    monkeypatch.setattr("paircue.cli._reveal_path", revealed.append)

    result = main([])

    assert result == 0
    assert picker_calls == 1
    assert pipeline.closed is True
    assert pipeline.items[0].path == media
    assert revealed == [output]
    assert state.phase == "completed"
    assert state.outputs == (output,)
    captured = capsys.readouterr().out
    assert "Choose one video" in captured
    assert f"created: {output}" in captured


def test_desktop_library_setup_starts_dashboard_before_leaving_the_wizard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "paircue.env"
    config.write_text('MEDIA_PATH="/media"\n', encoding="utf-8")
    state = SetupState(threading.Event(), output_path=config, mode="library")
    settings = PairCueSettings(
        platform="filesystem",
        media_root=tmp_path,
        state_dir=tmp_path / "state",
        api_token="x" * 40,
    )

    class FakeDesktopService:
        url = "http://127.0.0.1:9292/#token=private"

        def __init__(self, observed: PairCueSettings) -> None:
            assert observed is settings

        def start(self) -> None:
            return None

        def wait(self) -> str:
            return "stop"

    def finish_setup(
        assets: Path,
        target: Path,
        *,
        on_single_saved: object,
        on_library_saved: object,
        desktop: bool,
        connection_test: object,
        choose_folder: object,
        quick_pair: object,
        demo_pair: object,
    ) -> SetupState:
        assert desktop is True
        assert callable(on_library_saved)
        assert callable(connection_test)
        assert callable(choose_folder)
        assert callable(quick_pair)
        assert callable(demo_pair)
        on_library_saved(state)
        return state

    monkeypatch.setattr("paircue.cli._is_frozen", lambda: True)
    monkeypatch.setattr("paircue.cli.run_setup_wizard", finish_setup)
    monkeypatch.setattr("paircue.cli._desktop_library_settings", lambda path: settings)
    monkeypatch.setattr(
        "paircue.cli.check_media_source_connection",
        lambda observed: "Connected to the media folder.",
    )
    monkeypatch.setattr("paircue.cli.DesktopService", FakeDesktopService)

    assert main([]) == 0
    assert state.phase == "completed"
    assert state.action_url == FakeDesktopService.url
    assert "Connected to the media folder" in state.message


def test_learn_command_runs_one_local_video_without_a_media_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    media = tmp_path / "Japanese Film.mkv"
    media.write_bytes(b"video")
    output = tmp_path / "Japanese Film.mul.srt"
    pipeline = RecordingPipeline(output)
    observed_settings: list[PairCueSettings] = []

    def fake_build(settings: PairCueSettings) -> RecordingPipeline:
        observed_settings.append(settings)
        return pipeline

    monkeypatch.setattr("paircue.cli.build_pipeline", fake_build)
    monkeypatch.setattr("paircue.cli._choose_media_path", lambda: media)

    result = main(
        [
            "learn",
            "--from",
            "ja",
            "--to",
            "en",
            "--order",
            "source-first",
            "--title",
            "Japanese Film",
            "--year",
            "2024",
            "--audio-stream-index",
            "3",
        ]
    )

    assert result == 0
    assert pipeline.closed is True
    assert pipeline.items == [
        MediaItem("local", "movie", media, "Japanese Film", year=2024)
    ]
    assert observed_settings[0].platform == "filesystem"
    assert observed_settings[0].media_root == tmp_path
    assert observed_settings[0].source_language == "ja"
    assert observed_settings[0].target_language == "en"
    assert observed_settings[0].bilingual_order == "source-first"
    assert observed_settings[0].audio_stream_index == 3
    assert str(output) in capsys.readouterr().out


def test_doctor_json_reports_readiness_without_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    media = tmp_path / "media"
    state = tmp_path / "state"
    media.mkdir()
    state.mkdir()
    monkeypatch.setenv("PAIRCUE_PLATFORM", "filesystem")
    monkeypatch.setenv("PAIRCUE_MEDIA_ROOT", str(media))
    monkeypatch.setenv("PAIRCUE_STATE_DIR", str(state))
    monkeypatch.setenv("PAIRCUE_OPENSUBTITLES_API_KEY", "should-not-leak")
    monkeypatch.setattr(diagnostics.shutil, "which", lambda command: f"/usr/bin/{command}")

    result = main(["doctor", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload["ready"] is True
    assert "should-not-leak" not in json.dumps(payload)
    assert any(check["name"] == "FFmpeg" for check in payload["checks"])


def test_doctor_treats_video_tools_as_optional_until_transcription_is_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    media = tmp_path / "media"
    state = tmp_path / "state"
    media.mkdir()
    state.mkdir()
    monkeypatch.setenv("PAIRCUE_PLATFORM", "filesystem")
    monkeypatch.setenv("PAIRCUE_MEDIA_ROOT", str(media))
    monkeypatch.setenv("PAIRCUE_STATE_DIR", str(state))
    monkeypatch.setattr(diagnostics.shutil, "which", lambda command: None)

    result = main(["doctor", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload["ready"] is True
    tool_checks = {
        check["name"]: check["status"]
        for check in payload["checks"]
        if check["name"] in {"FFmpeg", "FFprobe"}
    }
    assert tool_checks == {"FFmpeg": "warning", "FFprobe": "warning"}


def test_doctor_requires_ffmpeg_when_transcription_is_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    media = tmp_path / "media"
    state = tmp_path / "state"
    media.mkdir()
    state.mkdir()
    monkeypatch.setenv("PAIRCUE_PLATFORM", "filesystem")
    monkeypatch.setenv("PAIRCUE_MEDIA_ROOT", str(media))
    monkeypatch.setenv("PAIRCUE_STATE_DIR", str(state))
    monkeypatch.setenv("PAIRCUE_TRANSCRIPTION_ENABLED", "true")
    monkeypatch.setenv("PAIRCUE_TRANSCRIPTION_API_KEY", "test-key")
    monkeypatch.setenv("PAIRCUE_TRANSCRIPTION_BASE_URL", "https://ai.example.com/v1")
    monkeypatch.setenv("PAIRCUE_TRANSCRIPTION_APPROVED_ORIGIN", "https://ai.example.com")
    monkeypatch.setattr(diagnostics.shutil, "which", lambda command: None)

    result = main(["doctor", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert result == 1
    assert payload["ready"] is False
    ffmpeg = next(check for check in payload["checks"] if check["name"] == "FFmpeg")
    assert ffmpeg["status"] == "error"


def test_doctor_json_redacts_invalid_configuration_input(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv(
        "PAIRCUE_TRANSCRIPTION_BASE_URL",
        "https://private-user:private-password@example.com/v1",
    )

    result = main(["doctor", "--json"])

    output = capsys.readouterr().out
    payload = json.loads(output)
    assert result == 1
    assert payload["ready"] is False
    assert "private-user" not in output
    assert "private-password" not in output
    assert "input" not in payload["configuration_errors"][0]
