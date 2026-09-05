"""Lifecycle for the private dashboard used by frozen desktop builds."""

from __future__ import annotations

import threading
import time
from typing import Literal
from urllib.parse import quote

import uvicorn

from paircue.api import create_core_app
from paircue.config import PairCueSettings
from paircue.factory import build_runtime


class DesktopServiceError(RuntimeError):
    pass


class DesktopService:
    def __init__(self, settings: PairCueSettings) -> None:
        self.settings = settings
        self._action: Literal["stop", "edit"] = "stop"
        self._action_lock = threading.Lock()
        self._failure: BaseException | None = None
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        token = quote(self.settings.api_token.get_secret_value(), safe="")
        return f"http://127.0.0.1:{self.settings.api_port}/#token={token}"

    def start(self, timeout: float = 15) -> None:
        if self._thread is not None:
            raise DesktopServiceError("the SubDuet dashboard is already running")
        runtime = build_runtime(self.settings)
        app = create_core_app(self.settings, runtime, desktop_control=self)
        configuration = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=self.settings.api_port,
            log_level="warning",
            access_log=False,
            proxy_headers=False,
        )
        self._server = uvicorn.Server(configuration)
        self._thread = threading.Thread(
            target=self._serve,
            name="paircue-desktop-service",
            daemon=False,
        )
        self._thread.start()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._server.started:
                return
            if not self._thread.is_alive():
                break
            time.sleep(0.05)
        self._server.should_exit = True
        self._thread.join(timeout=5)
        reason = type(self._failure).__name__ if self._failure is not None else "startup timeout"
        raise DesktopServiceError(f"the SubDuet dashboard could not start ({reason})")

    def request(self, action: Literal["stop", "edit"]) -> None:
        with self._action_lock:
            self._action = action
        if self._server is not None:
            self._server.should_exit = True

    def wait(self) -> Literal["stop", "edit"]:
        if self._thread is None:
            raise DesktopServiceError("the SubDuet dashboard has not started")
        self._thread.join()
        if self._failure is not None:
            raise DesktopServiceError(
                f"the SubDuet dashboard stopped unexpectedly ({type(self._failure).__name__})"
            )
        with self._action_lock:
            return self._action

    def _serve(self) -> None:
        try:
            if self._server is None:
                raise DesktopServiceError("the SubDuet dashboard server is unavailable")
            self._server.run()
        except BaseException as exc:  # Uvicorn raises SystemExit when its port cannot be bound.
            self._failure = exc
