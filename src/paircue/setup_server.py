from __future__ import annotations

import hmac
import json
import logging
import re
import secrets
import shutil
import threading
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlparse

from paircue.config import PairCueSettings
from paircue.services.atomic import atomic_write_bytes

log = logging.getLogger(__name__)

MAX_CONFIG_BYTES = 64 * 1024
MAX_EXISTING_CONFIG_BYTES = 1024 * 1024
ASSETS: dict[str, tuple[str, str]] = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
    "/setup.css": ("setup.css", "text/css; charset=utf-8"),
    "/setup.js": ("setup.js", "text/javascript; charset=utf-8"),
}


@dataclass(slots=True)
class SetupState:
    saved: threading.Event
    output_path: Path | None = None
    backup_path: Path | None = None
    mode: str = ""
    phase: str = "setup"
    message: str = "Finish the setup in your browser."
    outputs: tuple[Path, ...] = ()
    action_url: str = ""
    delivered: threading.Event = field(default_factory=threading.Event)
    quick_pair_completed: threading.Event = field(default_factory=threading.Event)
    quick_pair_output: Path | None = None
    _progress_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def update_progress(
        self,
        phase: str,
        message: str,
        outputs: tuple[Path, ...] = (),
        *,
        action_url: str = "",
    ) -> None:
        with self._progress_lock:
            self.phase = phase
            self.message = message
            self.outputs = outputs
            self.action_url = action_url

    def progress_payload(self) -> dict[str, object]:
        with self._progress_lock:
            return {
                "phase": self.phase,
                "message": self.message,
                "outputs": [path.name for path in self.outputs],
                "action_url": self.action_url,
                "terminal": self.phase in {"completed", "failed", "cancelled"},
            }


class SetupConnectionError(ValueError):
    """A secret-safe connection failure that may be shown in the setup page."""


class SetupQuickPairError(ValueError):
    """A safe local subtitle-pairing failure that may be shown in the setup page."""


@dataclass(frozen=True, slots=True)
class QuickPairResult:
    output: Path
    source_match_ratio: float
    target_match_ratio: float


class SetupHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        assets_root: Path,
        output_path: Path,
        *,
        desktop: bool = False,
        connection_test: Callable[[str], str] | None = None,
        choose_folder: Callable[[], Path | None] | None = None,
        quick_pair: Callable[[str], QuickPairResult | None] | None = None,
        demo_pair: Callable[[str], QuickPairResult] | None = None,
    ) -> None:
        super().__init__(("127.0.0.1", 0), SetupRequestHandler)
        self.assets_root = assets_root
        self.output_path = output_path
        self.token = secrets.token_urlsafe(32)
        self.desktop = desktop
        self.connection_test = connection_test
        self.choose_folder = choose_folder
        self.quick_pair = quick_pair
        self.demo_pair = demo_pair
        self.state = SetupState(threading.Event())
        self.save_lock = threading.Lock()
        self.quick_pair_lock = threading.Lock()

    @property
    def origin(self) -> str:
        host, port = cast(tuple[str, int], self.server_address)
        return f"http://{host}:{port}"


class SetupRequestHandler(BaseHTTPRequestHandler):
    server: SetupHTTPServer

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if not self._trusted_host():
            self.send_error(HTTPStatus.BAD_REQUEST)
            return
        path = urlparse(self.path).path
        if path == "/progress":
            if not self._authorized():
                self.send_error(HTTPStatus.FORBIDDEN)
                return
            payload = self.server.state.progress_payload()
            self._json_response(HTTPStatus.OK, payload)
            if bool(payload["terminal"]):
                self.server.state.delivered.set()
            return
        if path == "/readiness":
            ffmpeg = shutil.which("ffmpeg") is not None
            ffprobe = shutil.which("ffprobe") is not None
            self._json_response(
                HTTPStatus.OK,
                {
                    "ready": ffmpeg and ffprobe,
                    "ffmpeg": ffmpeg,
                    "ffprobe": ffprobe,
                },
            )
            return
        if path == "/context":
            self._json_response(HTTPStatus.OK, {"desktop": self.server.desktop})
            return
        asset = ASSETS.get(path)
        if asset is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        filename, content_type = asset
        try:
            content = (self.server.assets_root / filename).read_bytes()
        except OSError:
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self.send_response(HTTPStatus.OK)
        self._security_headers(content_type, len(content))
        self.end_headers()
        self.wfile.write(content)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urlparse(self.path)
        if (
            parsed.path
            not in {
                "/config",
                "/test-platform",
                "/choose-folder",
                "/quick-pair",
                "/demo-pair",
            }
            or not self._trusted_host()
            or not self._authorized()
            or self.headers.get("Origin") != self.server.origin
        ):
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if parsed.path == "/choose-folder":
            if self.server.choose_folder is None:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            try:
                selected = self.server.choose_folder()
            except OSError:
                self._json_response(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"selected": False, "path": ""},
                )
                return
            self._json_response(
                HTTPStatus.OK,
                {
                    "selected": selected is not None,
                    "path": str(selected) if selected is not None else "",
                },
            )
            return
        if parsed.path in {"/quick-pair", "/demo-pair"}:
            pair = self.server.quick_pair if parsed.path == "/quick-pair" else self.server.demo_pair
            if pair is None:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            order = parse_qs(parsed.query).get("order", [""])[0]
            if order not in {"target-first", "source-first"}:
                self._json_response(
                    HTTPStatus.BAD_REQUEST,
                    {"completed": False, "message": "Choose which subtitle appears first."},
                )
                return
            if not self.server.quick_pair_lock.acquire(blocking=False):
                self._json_response(
                    HTTPStatus.CONFLICT,
                    {"completed": False, "message": "Another subtitle action is already open."},
                )
                return
            try:
                try:
                    result = pair(order)
                finally:
                    self.server.quick_pair_lock.release()
            except SetupQuickPairError as exc:
                self._json_response(
                    HTTPStatus.BAD_REQUEST,
                    {"completed": False, "message": str(exc)},
                )
                return
            except Exception as exc:
                log.warning("desktop Quick Pair failed (%s)", type(exc).__name__)
                message = (
                    "SubDuet could not create the safe demo. Check folder permissions."
                    if parsed.path == "/demo-pair"
                    else "SubDuet could not pair those subtitle files."
                )
                self._json_response(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"completed": False, "message": message},
                )
                return
            if result is None:
                self._json_response(
                    HTTPStatus.OK,
                    {"completed": False, "message": "No subtitle files were changed."},
                )
                return
            self._json_response(
                HTTPStatus.OK,
                {
                    "completed": True,
                    "filename": result.output.name,
                    "message": (
                        "Created a bilingual subtitle "
                        f"({result.source_match_ratio:.0%}/"
                        f"{result.target_match_ratio:.0%} matched)."
                    ),
                },
            )
            self.server.state.quick_pair_output = result.output
            self.server.state.quick_pair_completed.set()
            return
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            self.send_error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_CONFIG_BYTES:
            self.send_error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return
        try:
            payload: Any = json.loads(self.rfile.read(length))
            config = payload.get("config") if isinstance(payload, dict) else None
            mode = payload.get("mode") if isinstance(payload, dict) else None
            if not isinstance(config, str) or not config or "\0" in config:
                raise ValueError("invalid configuration")
            if mode not in {"single", "library"}:
                raise ValueError("invalid setup mode")
            encoded = config.encode("utf-8")
            if len(encoded) > MAX_CONFIG_BYTES:
                raise ValueError("configuration is too large")
            _validate_config(config, mode=mode)
            if parsed.path == "/test-platform":
                if mode != "library" or self.server.connection_test is None:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                message = self.server.connection_test(config)
                self._json_response(HTTPStatus.OK, {"ok": True, "message": message})
                return
            with self.server.save_lock:
                if self.server.state.saved.is_set():
                    self._json_response(
                        HTTPStatus.CONFLICT,
                        {"saved": False, "message": "SubDuet Setup was already saved."},
                    )
                    return
                output, backup = self._save_config(config)
                self.server.state.output_path = output
                self.server.state.backup_path = backup
                self.server.state.mode = mode
                self.server.state.update_progress(
                    "saved",
                    "Your private setup is saved on this device.",
                )
                self._json_response(
                    HTTPStatus.OK,
                    {
                        "saved": True,
                        "filename": output.name,
                        "location": str(output.parent),
                        "backup": backup.name if backup is not None else "",
                    },
                )
                self.server.state.saved.set()
        except SetupConnectionError as exc:
            self._json_response(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "message": str(exc)},
            )
            return
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            log.warning("visual setup could not save configuration (%s)", type(exc).__name__)
            self._json_response(
                HTTPStatus.BAD_REQUEST,
                {"saved": False, "message": "SubDuet could not save this configuration."},
            )
            return

    def _save_config(self, config: str) -> tuple[Path, Path | None]:
        output = self.server.output_path
        output.parent.mkdir(parents=True, exist_ok=True)
        backup: Path | None = None
        if output.exists():
            if (
                output.is_symlink()
                or not output.is_file()
                or output.stat().st_size > MAX_EXISTING_CONFIG_BYTES
            ):
                raise OSError("existing configuration cannot be backed up safely")
            timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
            backup = output.with_name(f"{output.name}.backup-{timestamp}")
            shutil.copy2(output, backup)
            backup.chmod(0o600)
        atomic_write_bytes(output, config.encode("utf-8"), mode=0o600)
        return output, backup

    def _trusted_host(self) -> bool:
        hostname = self.headers.get("Host", "").partition(":")[0]
        return hostname in {"127.0.0.1", "localhost"}

    def _authorized(self) -> bool:
        scheme, separator, supplied = self.headers.get("Authorization", "").partition(" ")
        return (
            separator == " "
            and scheme.casefold() == "bearer"
            and hmac.compare_digest(supplied, self.server.token)
        )

    def _json_response(self, status: HTTPStatus, payload: dict[str, object]) -> None:
        content = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self._security_headers("application/json; charset=utf-8", len(content))
        self.end_headers()
        self.wfile.write(content)

    def _security_headers(self, content_type: str, length: int) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
        )

    def log_message(self, format: str, *args: object) -> None:
        return


def run_setup_wizard(
    assets_root: Path,
    output_path: Path,
    *,
    on_single_saved: Callable[[SetupState], None] | None = None,
    on_library_saved: Callable[[SetupState], None] | None = None,
    desktop: bool = False,
    connection_test: Callable[[str], str] | None = None,
    choose_folder: Callable[[], Path | None] | None = None,
    quick_pair: Callable[[str], QuickPairResult | None] | None = None,
    demo_pair: Callable[[str], QuickPairResult] | None = None,
) -> SetupState:
    server = SetupHTTPServer(
        assets_root,
        output_path,
        desktop=desktop,
        connection_test=connection_test,
        choose_folder=choose_folder,
        quick_pair=quick_pair,
        demo_pair=demo_pair,
    )
    thread = threading.Thread(target=server.serve_forever, name="paircue-setup", daemon=True)
    thread.start()
    url = f"{server.origin}/#token={server.token}"
    if webbrowser.open(url):
        print("SubDuet Setup opened. Finish the three short steps in your browser.")
    else:
        print(f"Open this private local address in a browser: {url}")
    print("Waiting for you to save the setup. Press Ctrl+C to cancel.")
    try:
        while not server.state.saved.wait(0.25):
            if server.state.quick_pair_completed.is_set():
                break
        if server.state.quick_pair_completed.is_set():
            return server.state
        callback = on_single_saved if server.state.mode == "single" else on_library_saved
        if server.state.output_path is not None and callback is not None:
            try:
                callback(server.state)
            except Exception as exc:
                log.error("guided setup continuation failed (%s)", type(exc).__name__)
                server.state.update_progress(
                    "failed",
                    "SubDuet could not finish setup. Check the settings and try again.",
                )
            if server.state.phase not in {"completed", "failed", "cancelled"}:
                server.state.update_progress(
                    "failed",
                    "SubDuet stopped before finishing. Reopen SubDuet to try again.",
                )
            server.state.delivered.wait(timeout=30)
    except KeyboardInterrupt:
        print("\nPairCue Setup cancelled.")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    return server.state


def _validate_config(config: str, *, mode: str) -> None:
    raw_values = parse_config_values(config)
    values: dict[str, str] = {}
    for name, decoded in raw_values.items():
        if name.startswith("PAIRCUE_"):
            values[name.removeprefix("PAIRCUE_").casefold()] = decoded
    if mode == "single":
        # One-video learning deliberately bypasses the selected media server, while retaining the
        # user's platform choice in the saved file for the next setup visit.
        values["platform"] = "filesystem"
    PairCueSettings.model_validate(values)


def parse_config_values(config: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in config.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        name, separator, raw = stripped.partition("=")
        name = name.strip()
        if not separator or not name or not re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
            continue
        candidate = raw.strip()
        decoded = json.loads(candidate) if candidate.startswith('"') else candidate
        if not isinstance(decoded, str):
            raise ValueError("configuration values must be strings")
        values[name] = decoded
    return values
