from __future__ import annotations

import argparse
import json
import logging
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import webbrowser
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal, cast

import httpx
import srt
import uvicorn
from pydantic import ValidationError

from paircue import __version__
from paircue.api import create_core_app
from paircue.config import DownloadStationSettings, PairCueSettings
from paircue.desktop_service import DesktopService, DesktopServiceError
from paircue.diagnostics import run_diagnostics
from paircue.downloads_api import create_downloads_app
from paircue.factory import build_pipeline, build_runtime, check_media_source_connection
from paircue.models import MediaItem
from paircue.services.download_station import DownloadStationClient
from paircue.services.subtitle_files import merge_bilingual_subtitles, parse_srt, write_srt
from paircue.setup_server import (
    QuickPairResult,
    SetupConnectionError,
    SetupQuickPairError,
    SetupState,
    parse_config_values,
    run_setup_wizard,
)

MAX_QUICK_PAIR_SUBTITLE_BYTES = 16 * 1024 * 1024


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="subduet", description="cross-platform bilingual subtitle automation"
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subcommands = parser.add_subparsers(dest="command")
    subcommands.add_parser("serve", help="run the subtitle service")
    subcommands.add_parser("downloads", help="run the isolated Download Station service")
    subcommands.add_parser("generate-token", help="generate a secure API token")
    setup = subcommands.add_parser("setup", help="open the private visual setup wizard")
    setup.add_argument("--no-open", action="store_true", help="print its local path instead")
    doctor = subcommands.add_parser("doctor", help="check configuration before starting SubDuet")
    doctor.add_argument("--json", action="store_true", help="print machine-readable results")
    doctor.add_argument("--config", type=Path, help="read settings from this environment file")
    learn = subcommands.add_parser(
        "learn",
        help="create a bilingual learning track for one local video",
    )
    learn.add_argument(
        "media",
        type=Path,
        nargs="?",
        help="local movie or episode file; omit it to choose from a window",
    )
    learn.add_argument("--from", dest="source_language", help="spoken/source language tag")
    learn.add_argument("--to", dest="target_language", help="learning language tag")
    learn.add_argument(
        "--order",
        choices=("target-first", "source-first"),
        help="which language appears on the first line",
    )
    learn.add_argument("--title", help="title used for subtitle metadata fallback")
    learn.add_argument("--year", type=int, help="release year used for subtitle metadata fallback")
    learn.add_argument("--config", type=Path, help="read settings from this environment file")
    pair = subcommands.add_parser(
        "pair", help="merge two existing SRT files into one bilingual SRT"
    )
    pair.add_argument("source", type=Path, help="source-language SRT")
    pair.add_argument("target", type=Path, help="learning-language SRT")
    pair.add_argument("-o", "--output", type=Path, required=True, help="bilingual output SRT")
    pair.add_argument(
        "--order",
        choices=("target-first", "source-first"),
        default="target-first",
        help="which language appears on the first line",
    )
    pair.add_argument("--tolerance-ms", type=int, default=350)
    pair.add_argument("--min-match-ratio", type=float, default=0.7)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command is None:
        return _setup(no_open=False)
    if args.command == "generate-token":
        print(secrets.token_urlsafe(48))
        return 0
    if args.command == "pair":
        return _pair(args)
    if args.command == "setup":
        return _setup(no_open=args.no_open)
    if args.command == "doctor":
        return _doctor(as_json=args.json, config=args.config)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.command == "learn":
        return _learn(args)
    if args.command == "serve":
        settings = PairCueSettings()
        runtime = build_runtime(settings)
        app = create_core_app(settings, runtime)
        uvicorn.run(
            app,
            host=settings.api_host,
            port=settings.api_port,
            access_log=False,
            proxy_headers=False,
        )
        return 0

    download_settings = DownloadStationSettings()
    client = DownloadStationClient(
        base_url=download_settings.url,
        username=download_settings.username,
        password=download_settings.password.get_secret_value(),
        destination=download_settings.destination,
    )
    app = create_downloads_app(download_settings, client)
    uvicorn.run(
        app,
        host=download_settings.host,
        port=download_settings.port,
        access_log=False,
        proxy_headers=False,
    )
    return 0


def desktop_main() -> int:
    """Open an existing desktop library, or enter setup on the first launch."""

    output = _default_setup_output()
    try:
        if output.is_file() and _config_value(output, "MEDIA_PATH"):
            return _run_desktop_library(output, reopen_setup=True)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        # A damaged regular file can be replaced by setup. Unsafe files such as symlinks
        # remain protected by the setup server's save checks.
        return _setup(no_open=False)
    return _setup(no_open=False)


def _doctor(*, as_json: bool, config: Path | None = None) -> int:
    try:
        environment_file = _environment_file(config)
        settings = _load_settings(environment_file)
    except (OSError, ValidationError) as exc:
        if isinstance(exc, OSError):
            errors = [
                {
                    "location": "config",
                    "message": str(exc),
                    "type": "config_file_error",
                }
            ]
        else:
            errors = [
                {
                    "location": ".".join(str(part) for part in error["loc"]),
                    "message": str(error["msg"]),
                    "type": str(error["type"]),
                }
                for error in exc.errors(include_input=False, include_url=False)
            ]
        if as_json:
            print(json.dumps({"ready": False, "configuration_errors": errors}))
        else:
            print("SubDuet is not ready:", file=sys.stderr)
            for error in errors:
                print(f"[error] {error['location']}: {error['message']}", file=sys.stderr)
        return 1
    checks = run_diagnostics(settings)
    ready = not any(check.status == "error" for check in checks)
    if as_json:
        print(
            json.dumps(
                {"ready": ready, "checks": [check.as_dict() for check in checks]},
                ensure_ascii=False,
            )
        )
    else:
        for check in checks:
            print(f"[{check.status}] {check.name}: {check.detail}")
        print("SubDuet is ready." if ready else "SubDuet needs attention before it can start.")
    return 0 if ready else 1


def _setup(*, no_open: bool) -> int:
    setup_page = Path(__file__).with_name("setup") / "index.html"
    if not setup_page.is_file():
        print("SubDuet setup files are missing from this installation.", file=sys.stderr)
        return 1
    if no_open:
        print(setup_page)
        return 0
    exit_code = 0
    desktop_service: DesktopService | None = None

    def continue_with_one_video(state: SetupState) -> None:
        nonlocal exit_code
        state.update_progress("choosing", "Choose one movie or episode in the file window.")
        print("Choose one video to create your first learning track.")
        selected = _choose_media_path()
        if selected is None:
            state.update_progress(
                "cancelled",
                "No video was selected. Your setup is saved, so you can try again anytime.",
            )
            print("No video selected. Your setup is saved; run `subduet learn` whenever ready.")
            return
        state.update_progress(
            "processing",
            f"Creating bilingual subtitles for {selected.name}…",
        )
        exit_code = _learn(
            argparse.Namespace(
                media=selected,
                config=state.output_path,
                source_language=None,
                target_language=None,
                order=None,
                title=None,
                year=None,
                reveal_output=True,
                setup_state=state,
            )
        )

    def continue_with_library(state: SetupState) -> None:
        nonlocal desktop_service, exit_code
        if state.output_path is None:
            return
        state.update_progress("starting", "Checking the platform and starting your dashboard…")
        try:
            settings = _desktop_library_settings(state.output_path)
            summary = check_media_source_connection(settings)
            service = DesktopService(settings)
            service.start()
        except (OSError, ValidationError, ValueError, httpx.HTTPError, DesktopServiceError) as exc:
            exit_code = 1
            state.update_progress("failed", _desktop_start_message(exc))
            return
        desktop_service = service
        state.update_progress(
            "completed",
            f"{summary} Opening the private dashboard now.",
            action_url=service.url,
        )

    def test_library_connection(config: str) -> str:
        try:
            settings = _desktop_library_settings_from_values(
                parse_config_values(config),
                _default_setup_output().parent,
            )
            return check_media_source_connection(settings)
        except (OSError, ValidationError, ValueError, httpx.HTTPError) as exc:
            raise SetupConnectionError(_desktop_start_message(exc)) from None

    state = run_setup_wizard(
        setup_page.parent,
        _default_setup_output(),
        on_single_saved=continue_with_one_video,
        on_library_saved=continue_with_library if _is_frozen() else None,
        desktop=_is_frozen(),
        connection_test=test_library_connection if _is_frozen() else None,
        choose_folder=_choose_media_directory if _is_frozen() else None,
        quick_pair=_quick_pair_subtitles if _is_frozen() else None,
        demo_pair=_quick_pair_demo if _is_frozen() else None,
    )
    if state.quick_pair_output is not None:
        print(f"Created bilingual subtitle: {state.quick_pair_output}")
        return 0
    if state.output_path is None:
        return 1
    print(f"Saved private configuration: {state.output_path}")
    if state.backup_path is not None:
        print(f"Previous configuration backed up to: {state.backup_path}")
    if state.mode == "library" and _is_frozen():
        if desktop_service is None:
            return exit_code or 1
        try:
            action = desktop_service.wait()
        except DesktopServiceError as exc:
            print(f"SubDuet dashboard stopped: {_safe_error(exc)}", file=sys.stderr)
            return 1
        if action == "edit":
            return _setup(no_open=False)
    return exit_code


def _run_desktop_library(config: Path, *, reopen_setup: bool) -> int:
    try:
        settings = _desktop_library_settings(config)
        check_media_source_connection(settings)
        service = DesktopService(settings)
        service.start()
    except (OSError, ValidationError, ValueError, httpx.HTTPError, DesktopServiceError) as exc:
        print(
            f"SubDuet could not start the library dashboard: {_desktop_start_message(exc)}",
            file=sys.stderr,
        )
        return _setup(no_open=False) if reopen_setup else 1
    webbrowser.open(service.url)
    try:
        action = service.wait()
    except DesktopServiceError as exc:
        print(f"SubDuet dashboard stopped: {_safe_error(exc)}", file=sys.stderr)
        return 1
    if action == "edit" and reopen_setup:
        return _setup(no_open=False)
    return 0


def _desktop_start_message(error: Exception) -> str:
    if isinstance(error, httpx.HTTPStatusError):
        if error.response.status_code in {401, 403}:
            return "The server did not accept that token or API key. Check it and try again."
        return "The media server returned an error. Check its address and SubDuet access."
    if isinstance(error, (httpx.ConnectError, httpx.TimeoutException)):
        return "SubDuet could not reach the media server. Check the address and network."
    if isinstance(error, FileNotFoundError):
        return "SubDuet could not find the selected media folder. Choose it again."
    if isinstance(error, PermissionError):
        return "SubDuet does not have permission to read and write the selected media folder."
    if isinstance(error, ValidationError):
        return f"Check the required platform settings: {_safe_error(error)}"
    return _safe_error(error)


def _pair(args: argparse.Namespace) -> int:
    try:
        source = args.source.resolve(strict=True)
        target = args.target.resolve(strict=True)
        output = args.output.resolve(strict=False)
        if output in {source, target}:
            raise ValueError("output must not overwrite either input subtitle")
        if not 0 <= args.tolerance_ms <= 2_000:
            raise ValueError("tolerance must be between 0 and 2000 milliseconds")
        if not 0.5 <= args.min_match_ratio <= 1:
            raise ValueError("minimum match ratio must be between 0.5 and 1")
        merged = merge_bilingual_subtitles(
            parse_srt(source),
            parse_srt(target),
            order=args.order,
            tolerance_ms=args.tolerance_ms,
            min_match_ratio=args.min_match_ratio,
        )
        write_srt(output, merged.subtitles)
    except (OSError, ValueError) as exc:
        print(f"SubDuet could not pair these subtitles: {exc}", file=sys.stderr)
        return 2
    print(
        f"Created {output} with {len(merged.subtitles)} bilingual cues "
        f"({merged.source_match_ratio:.0%}/{merged.target_match_ratio:.0%} matched)."
    )
    return 0


def _learn(args: argparse.Namespace) -> int:
    try:
        selected_media = args.media or _choose_media_path()
        if selected_media is None:
            raise ValueError("no video was selected")
        media = selected_media.expanduser().resolve(strict=True)
        if not media.is_file():
            raise ValueError("media path is not a file")
        if args.year is not None and not 1878 <= args.year <= 2100:
            raise ValueError("year must be between 1878 and 2100")
        title = (args.title or media.stem).strip()
        if not title:
            raise ValueError("title must not be empty")
    except (OSError, ValueError) as exc:
        message = f"SubDuet could not open this video: {exc}"
        _update_guided_progress(args, "failed", message)
        print(message, file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory(prefix="paircue-learn-") as temporary_state:
        try:
            environment_file = _environment_file(args.config)
            base_settings = _load_settings(
                environment_file,
                platform="filesystem",
                media_root=media.parent,
                state_dir=Path(temporary_state),
                api_host="127.0.0.1",
                webhook_enabled=False,
            )
            settings = _load_settings(
                environment_file,
                platform="filesystem",
                media_root=media.parent,
                state_dir=Path(temporary_state),
                api_host="127.0.0.1",
                webhook_enabled=False,
                source_language=args.source_language or base_settings.source_language,
                target_language=args.target_language or base_settings.target_language,
                bilingual_order=args.order or base_settings.bilingual_order,
            )
            pipeline = build_pipeline(settings)
        except (OSError, ValidationError, ValueError) as exc:
            message = f"SubDuet configuration is not ready: {_safe_error(exc)}"
            _update_guided_progress(args, "failed", message)
            print(message, file=sys.stderr)
            return 2

        try:
            result = pipeline.process(
                MediaItem(
                    item_id="local",
                    media_type="movie",
                    path=media,
                    title=title,
                    year=args.year,
                )
            )
        finally:
            pipeline.close()

    destination = sys.stderr if result.status == "failed" else sys.stdout
    print(f"{result.status}: {result.message}", file=destination)
    for output in result.outputs:
        print(f"created: {output}")
    if (
        result.status != "failed"
        and result.outputs
        and bool(getattr(args, "reveal_output", False))
    ):
        _reveal_path(result.outputs[-1])
    guided = isinstance(getattr(args, "setup_state", None), SetupState)
    has_bilingual = any(path.name.casefold().endswith(".mul.srt") for path in result.outputs)
    if result.status == "failed":
        _update_guided_progress(args, "failed", result.message)
        return 1
    if guided and not has_bilingual:
        _update_guided_progress(
            args,
            "failed",
            "SubDuet found only one language track. Add the other language or enable translation, "
            "then reopen SubDuet.",
            result.outputs,
        )
        return 1
    _update_guided_progress(args, "completed", result.message, result.outputs)
    return 0


def _update_guided_progress(
    args: argparse.Namespace,
    phase: str,
    message: str,
    outputs: tuple[Path, ...] = (),
) -> None:
    state = getattr(args, "setup_state", None)
    if isinstance(state, SetupState):
        state.update_progress(phase, message, outputs)


def _safe_error(error: Exception) -> str:
    if isinstance(error, ValidationError):
        messages = [
            str(item["msg"])
            for item in error.errors(include_input=False, include_url=False)
        ]
        return "; ".join(messages)
    return str(error)


def _environment_file(config: Path | None) -> Path:
    if config is None:
        return Path(".env")
    resolved = config.resolve(strict=True)
    if not resolved.is_file():
        raise OSError(f"configuration path is not a file: {resolved}")
    return resolved


def _default_setup_output() -> Path:
    """Keep the original private config path so a rename never strands existing credentials."""

    if not _is_frozen():
        return Path.cwd() / "paircue.env"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "PairCue" / "paircue.env"
    if sys.platform == "win32":
        app_data = os.environ.get("APPDATA")
        root = Path(app_data) if app_data else Path.home() / "AppData" / "Roaming"
        return root / "PairCue" / "paircue.env"
    config_home = os.environ.get("XDG_CONFIG_HOME")
    root = Path(config_home) if config_home else Path.home() / ".config"
    return root / "paircue" / "paircue.env"


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _config_value(config: Path, name: str) -> str:
    return parse_config_values(_read_desktop_config(config)).get(name, "")


def _read_desktop_config(config: Path) -> str:
    if config.is_symlink() or not config.is_file() or config.stat().st_size > 1024 * 1024:
        raise OSError("desktop configuration is not a safe regular file")
    return config.read_text(encoding="utf-8")


def _desktop_library_settings(config: Path) -> PairCueSettings:
    values = parse_config_values(_read_desktop_config(config))
    return _desktop_library_settings_from_values(values, config.parent)


def _desktop_library_settings_from_values(
    values: dict[str, str],
    config_parent: Path,
) -> PairCueSettings:
    media_value = values.get("MEDIA_PATH", "")
    if not media_value:
        raise ValueError("choose the media folder before starting library automation")
    media_root = Path(media_value).expanduser().resolve(strict=True)
    if not media_root.is_dir():
        raise ValueError("the selected media location is not a folder")
    if not os.access(media_root, os.R_OK | os.W_OK):
        raise ValueError("SubDuet needs read and write access to the selected media folder")
    settings_values: dict[str, object] = {
        name.removeprefix("PAIRCUE_").casefold(): value
        for name, value in values.items()
        if name.startswith("PAIRCUE_")
    }
    settings_values.update(
        media_root=media_root,
        state_dir=config_parent / "state",
        api_host="127.0.0.1",
        api_docs_enabled=False,
        trusted_hosts="localhost,127.0.0.1",
    )
    return PairCueSettings.model_validate(settings_values)


def _load_settings(environment_file: Path, **overrides: object) -> PairCueSettings:
    """Cross the dynamic BaseSettings source boundary while retaining validated output."""

    settings_factory: Any = PairCueSettings
    return cast(PairCueSettings, settings_factory(_env_file=environment_file, **overrides))


def _choose_media_path() -> Path | None:
    """Open a native file chooser, with a drag-and-drop terminal fallback."""

    commands: list[list[str]] = []
    if sys.platform == "darwin" and Path("/usr/bin/osascript").is_file():
        commands.append(
            [
                "/usr/bin/osascript",
                "-e",
                'POSIX path of (choose file with prompt "Choose one movie or episode for SubDuet")',
            ]
        )
    elif os.name == "nt":
        powershell = shutil.which("powershell") or shutil.which("pwsh")
        if powershell:
            commands.append(
                [
                    powershell,
                    "-NoProfile",
                    "-Command",
                    "Add-Type -AssemblyName System.Windows.Forms; "
                    "$d=New-Object System.Windows.Forms.OpenFileDialog; "
                    "$d.Title='Choose one movie or episode for SubDuet'; "
                    "$d.Filter='Video files|*.mkv;*.mp4;*.m4v;*.avi;*.mov;*.webm|All files|*.*'; "
                    "if($d.ShowDialog() -eq 'OK'){Write-Output $d.FileName}",
                ]
            )
    else:
        zenity = shutil.which("zenity")
        if zenity:
            commands.append(
                [
                    zenity,
                    "--file-selection",
                    "--title=Choose one movie or episode for SubDuet",
                    "--file-filter=Video files | *.mkv *.mp4 *.m4v *.avi *.mov *.webm",
                    "--file-filter=All files | *",
                ]
            )
        kdialog = shutil.which("kdialog")
        if not zenity and kdialog:
            commands.append(
                [
                    kdialog,
                    "--getopenfilename",
                    "",
                    "Video files (*.mkv *.mp4 *.m4v *.avi *.mov *.webm)",
                    "--title",
                    "Choose one movie or episode for SubDuet",
                ]
            )

    for command in commands:
        try:
            result = subprocess.run(  # noqa: S603 - fixed platform chooser and argument array
                command,
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError:
            continue
        selected = result.stdout.strip()
        if result.returncode == 0 and selected:
            return Path(selected)
        return None

    if sys.stdin.isatty():
        selected = input("Drag one video file here, then press Return: ").strip()
        if selected:
            return Path(selected.strip("'\""))
    return None


def _choose_media_directory() -> Path | None:
    """Open the native folder chooser used by desktop library setup."""

    command: list[str] | None = None
    if sys.platform == "darwin" and Path("/usr/bin/osascript").is_file():
        command = [
            "/usr/bin/osascript",
            "-e",
            'POSIX path of (choose folder with prompt "Choose your media folder for SubDuet")',
        ]
    elif os.name == "nt":
        powershell = shutil.which("powershell") or shutil.which("pwsh")
        if powershell:
            command = [
                powershell,
                "-NoProfile",
                "-Command",
                "Add-Type -AssemblyName System.Windows.Forms; "
                "$d=New-Object System.Windows.Forms.FolderBrowserDialog; "
                "$d.Description='Choose your media folder for SubDuet'; "
                "if($d.ShowDialog() -eq 'OK'){Write-Output $d.SelectedPath}",
            ]
    else:
        zenity = shutil.which("zenity")
        kdialog = shutil.which("kdialog")
        if zenity:
            command = [
                zenity,
                "--file-selection",
                "--directory",
                "--title=Choose your media folder for SubDuet",
            ]
        elif kdialog:
            command = [
                kdialog,
                "--getexistingdirectory",
                "",
                "--title",
                "Choose your media folder for SubDuet",
            ]
    if command is None:
        return None
    try:
        result = subprocess.run(  # noqa: S603 - fixed platform chooser and argument array
            command,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    selected = result.stdout.strip()
    return Path(selected) if result.returncode == 0 and selected else None


def _choose_subtitle_path(role: Literal["source", "target"]) -> Path | None:
    """Open a native SRT chooser with role-specific wording."""

    title = (
        "Choose the spoken or source subtitle for SubDuet"
        if role == "source"
        else "Choose the learning-language subtitle for SubDuet"
    )
    command: list[str] | None = None
    if sys.platform == "darwin" and Path("/usr/bin/osascript").is_file():
        command = [
            "/usr/bin/osascript",
            "-e",
            f'POSIX path of (choose file with prompt "{title}")',
        ]
    elif os.name == "nt":
        powershell = shutil.which("powershell") or shutil.which("pwsh")
        if powershell:
            command = [
                powershell,
                "-NoProfile",
                "-Command",
                "Add-Type -AssemblyName System.Windows.Forms; "
                "$d=New-Object System.Windows.Forms.OpenFileDialog; "
                f"$d.Title='{title}'; "
                "$d.Filter='SubRip subtitles|*.srt'; "
                "if($d.ShowDialog() -eq 'OK'){Write-Output $d.FileName}",
            ]
    else:
        zenity = shutil.which("zenity")
        kdialog = shutil.which("kdialog")
        if zenity:
            command = [
                zenity,
                "--file-selection",
                f"--title={title}",
                "--file-filter=SubRip subtitles | *.srt",
            ]
        elif kdialog:
            command = [
                kdialog,
                "--getopenfilename",
                "",
                "SubRip subtitles (*.srt)",
                "--title",
                title,
            ]
    if command is None:
        return None
    try:
        result = subprocess.run(  # noqa: S603 - fixed platform chooser and argument array
            command,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    selected = result.stdout.strip()
    return Path(selected) if result.returncode == 0 and selected else None


def _quick_pair_subtitles(order: str) -> QuickPairResult | None:
    """Choose and merge two local SRT files without accounts, uploads, or configuration."""

    if order not in {"target-first", "source-first"}:
        raise SetupQuickPairError("Choose which subtitle appears first.")
    source = _choose_subtitle_path("source")
    if source is None:
        return None
    target = _choose_subtitle_path("target")
    if target is None:
        return None
    try:
        source = _checked_quick_pair_input(source)
        target = _checked_quick_pair_input(target)
        if source == target:
            raise SetupQuickPairError("Choose two different subtitle files.")
        merged = merge_bilingual_subtitles(
            parse_srt(source),
            parse_srt(target),
            order=cast(Literal["target-first", "source-first"], order),
        )
        output, reservation_inode = _reserve_quick_pair_output(source, target)
        try:
            write_srt(output, merged.subtitles)
        except Exception:
            _remove_quick_pair_reservation(output, reservation_inode)
            raise
    except SetupQuickPairError:
        raise
    except UnicodeError as exc:
        raise SetupQuickPairError("One subtitle is not a valid UTF-8 SRT file.") from exc
    except OSError as exc:
        raise SetupQuickPairError(
            "SubDuet could not read or write those subtitle files. Check their permissions."
        ) from exc
    except ValueError as exc:
        raise SetupQuickPairError(str(exc)) from None
    _reveal_path(output)
    return QuickPairResult(
        output=output,
        source_match_ratio=merged.source_match_ratio,
        target_match_ratio=merged.target_match_ratio,
    )


def _quick_pair_demo(
    order: str,
    output_directory: Path | None = None,
) -> QuickPairResult:
    """Create a tiny project-owned bilingual subtitle to prove the install works."""

    if order not in {"target-first", "source-first"}:
        raise SetupQuickPairError("Choose which subtitle appears first.")
    destination = output_directory or Path.home() / "Downloads"
    if output_directory is None and not destination.is_dir():
        destination = _default_setup_output().parent
    destination.mkdir(parents=True, exist_ok=True)
    source = [
        srt.Subtitle(
            index=1,
            start=timedelta(seconds=1),
            end=timedelta(seconds=3, milliseconds=400),
            content="Where should we begin?",
        ),
        srt.Subtitle(
            index=2,
            start=timedelta(seconds=4, milliseconds=200),
            end=timedelta(seconds=6, milliseconds=800),
            content="With one scene at a time.",
        ),
    ]
    target = [
        srt.Subtitle(
            index=1,
            start=timedelta(seconds=1, milliseconds=120),
            end=timedelta(seconds=3, milliseconds=520),
            content="¿Por dónde empezamos?",
        ),
        srt.Subtitle(
            index=2,
            start=timedelta(seconds=4, milliseconds=100),
            end=timedelta(seconds=6, milliseconds=700),
            content="Una escena a la vez.",
        ),
    ]
    merged = merge_bilingual_subtitles(
        source,
        target,
        order=cast(Literal["target-first", "source-first"], order),
    )
    output, reservation_inode = _reserve_quick_pair_output(
        destination / "SubDuet Demo.en.srt",
        destination / "SubDuet Demo.es.srt",
    )
    try:
        write_srt(output, merged.subtitles)
    except Exception:
        _remove_quick_pair_reservation(output, reservation_inode)
        raise
    _reveal_path(output)
    return QuickPairResult(
        output=output,
        source_match_ratio=merged.source_match_ratio,
        target_match_ratio=merged.target_match_ratio,
    )


def _checked_quick_pair_input(path: Path) -> Path:
    resolved = path.expanduser().resolve(strict=True)
    if not resolved.is_file() or resolved.suffix.casefold() != ".srt":
        raise SetupQuickPairError("Choose two SRT subtitle files.")
    if resolved.stat().st_size > MAX_QUICK_PAIR_SUBTITLE_BYTES:
        raise SetupQuickPairError("Each subtitle file must be 16 MB or smaller.")
    return resolved


def _reserve_quick_pair_output(source: Path, target: Path) -> tuple[Path, int]:
    """Reserve a non-destructive, media-server-safe bilingual filename."""

    source_parts = source.stem.split(".")
    target_parts = target.stem.split(".")
    common_parts: list[str] = []
    for source_part, target_part in zip(source_parts, target_parts, strict=False):
        if source_part.casefold() != target_part.casefold():
            break
        common_parts.append(target_part)
    stem = ".".join(common_parts).strip(".")
    if not stem:
        stem = target.stem
        for suffix in (".mul", ".cc"):
            if stem.casefold().endswith(suffix):
                stem = stem[: -len(suffix)]
                break
    names = (
        f"{stem}.mul.srt",
        *(f"{stem}.paircue-{index}.mul.srt" for index in range(2, 1000)),
    )
    for name in names:
        candidate = target.with_name(name)
        if candidate == target:
            continue
        try:
            descriptor = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            continue
        try:
            inode = os.fstat(descriptor).st_ino
        finally:
            os.close(descriptor)
        return candidate, inode
    raise SetupQuickPairError("Too many paired copies already exist in this folder.")


def _remove_quick_pair_reservation(path: Path, inode: int) -> None:
    try:
        stat = path.stat()
        if stat.st_ino == inode and stat.st_size == 0:
            path.unlink()
    except OSError:
        return


def _reveal_path(path: Path) -> None:
    """Reveal a completed subtitle in the native file manager when available."""

    command: list[str] | None = None
    if sys.platform == "darwin" and Path("/usr/bin/open").is_file():
        command = ["/usr/bin/open", "-R", str(path)]
    elif os.name == "nt":
        explorer = shutil.which("explorer")
        if explorer:
            command = [explorer, "/select,", str(path)]
    else:
        opener = shutil.which("xdg-open")
        if opener:
            command = [opener, str(path.parent)]
    if command is None:
        return
    try:
        subprocess.run(  # noqa: S603 - fixed platform file-manager command and argument array
            command,
            capture_output=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return
