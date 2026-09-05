import json
import threading
from pathlib import Path

import pytest

from paircue import cli, diagnostics
from paircue.cli import _default_setup_output, main
from paircue.config import PairCueSettings
from paircue.models import MediaItem, ProcessResult
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


def test_pair_command_will_not_overwrite_an_input(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "movie.en.srt"
    target = tmp_path / "movie.zh-TW.srt"
    source.write_text(SOURCE, encoding="utf-8")
    target.write_text(TARGET, encoding="utf-8")

    result = main(["pair", str(source), str(target), "-o", str(source)])

    assert result == 2
    assert "must not overwrite" in capsys.readouterr().err


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
