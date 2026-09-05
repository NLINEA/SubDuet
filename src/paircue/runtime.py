from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass

from paircue.models import MediaItem
from paircue.services.media_source import MediaSource
from paircue.services.pipeline import SubtitlePipeline
from paircue.services.state import RecentMediaState

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RuntimeSnapshot:
    pending: int
    queued: int
    results: dict[str, int]
    recent: tuple[RecentMediaState, ...]
    scan_status: str
    scan_message: str


class JobCoordinator:
    def __init__(self, pipeline: SubtitlePipeline, max_size: int = 1000) -> None:
        self.pipeline = pipeline
        self._queue: queue.Queue[MediaItem | None] = queue.Queue(maxsize=max_size)
        self._pending: set[str] = set()
        self._guard = threading.Lock()
        self._worker: threading.Thread | None = None

    def start(self) -> None:
        if self._worker is not None:
            return
        self._worker = threading.Thread(target=self._run, name="paircue-worker", daemon=True)
        self._worker.start()

    def submit(self, item: MediaItem) -> bool:
        with self._guard:
            if item.queue_key in self._pending:
                return False
            self._pending.add(item.queue_key)
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            with self._guard:
                self._pending.discard(item.queue_key)
            raise RuntimeError("subtitle job queue is full") from None
        return True

    def stop(self) -> None:
        self._queue.put(None)
        if self._worker is not None:
            self._worker.join(timeout=10)

    def counts(self) -> tuple[int, int]:
        with self._guard:
            pending = len(self._pending)
        return pending, self._queue.qsize()

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                self._queue.task_done()
                return
            try:
                result = self.pipeline.process(item)
                log.info("%s: %s", item.context_label, result.message)
            except Exception:
                log.exception("worker failed before processing %s", item.context_label)
            finally:
                with self._guard:
                    self._pending.discard(item.queue_key)
                self._queue.task_done()


class CoreRuntime:
    def __init__(
        self,
        media_source: MediaSource,
        coordinator: JobCoordinator,
        scan_interval_seconds: int,
    ) -> None:
        self.media_source = media_source
        self.coordinator = coordinator
        self.scan_interval_seconds = scan_interval_seconds
        self._stop = threading.Event()
        self._poller: threading.Thread | None = None
        self._scan_lock = threading.Lock()
        self._scan_status = "waiting"
        self._scan_message = "Waiting for the first library scan."

    def start(self) -> None:
        self.coordinator.start()
        self._poller = threading.Thread(target=self._poll, name="paircue-poller", daemon=True)
        self._poller.start()

    def stop(self) -> None:
        self._stop.set()
        if self._poller is not None:
            self._poller.join(timeout=10)
        self.coordinator.stop()
        self.coordinator.pipeline.close()
        self.media_source.close()

    def status_snapshot(self, recent_limit: int = 20) -> RuntimeSnapshot:
        pending, queued = self.coordinator.counts()
        with self._scan_lock:
            scan_status = self._scan_status
            scan_message = self._scan_message
        return RuntimeSnapshot(
            pending=pending,
            queued=queued,
            results=self.coordinator.pipeline.state.summary(),
            recent=self.coordinator.pipeline.state.recent(recent_limit),
            scan_status=scan_status,
            scan_message=scan_message,
        )

    def scan_now(self) -> int:
        self._set_scan_state("scanning", f"Checking the {self.media_source.platform} library…")
        try:
            submitted = 0
            for item in self.media_source.scan_items():
                submitted += int(self.coordinator.submit(item))
        except Exception:
            self._set_scan_state(
                "error",
                f"SubDuet could not scan {self.media_source.platform.title()}. "
                "Check the platform connection and media folder.",
            )
            raise
        noun = "item" if submitted == 1 else "items"
        self._set_scan_state("ready", f"Latest scan queued {submitted} {noun}.")
        return submitted

    def submit_item_id(self, item_id: str) -> bool:
        item = self.media_source.item_for_id(item_id)
        return self.coordinator.submit(item) if item is not None else False

    def submit_rating_key(self, rating_key: str) -> bool:
        """Backward-compatible name used by the Plex webhook API."""
        return self.submit_item_id(rating_key)

    def _poll(self) -> None:
        while not self._stop.is_set():
            try:
                count = self.scan_now()
                log.info("%s scan queued %s item(s)", self.media_source.platform.title(), count)
            except Exception:
                log.exception("%s scan failed", self.media_source.platform.title())
            self._stop.wait(self.scan_interval_seconds)

    def _set_scan_state(self, status: str, message: str) -> None:
        with self._scan_lock:
            self._scan_status = status
            self._scan_message = message
