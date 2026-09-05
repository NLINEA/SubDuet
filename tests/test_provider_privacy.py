from __future__ import annotations

import logging
import traceback
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from urllib.parse import quote

import httpx
import pytest

from paircue.models import MediaItem
from paircue.services.glossary import GlossaryStore
from paircue.services.pipeline import SubtitlePipeline
from paircue.services.provider_privacy import (
    ProviderResponseTooLargeError,
    private_provider_diagnostics,
    safe_provider_failure,
)
from paircue.services.state import StateStore
from paircue.services.transcriber import (
    AudioChunk,
    OpenAICompatibleTranscriber,
    TranscriptionConfig,
    TranscriptionError,
)
from paircue.services.translator import (
    OpenAICompatibleProvider,
    ProviderConfig,
    TranslationError,
)

CANARY = "test-only-provider-canary/with-private-detail"
ENCODED_CANARY = quote(CANARY, safe="")


def _failure(request: httpx.Request, scenario: str) -> httpx.Response:
    if scenario == "timeout":
        raise httpx.ReadTimeout(CANARY, request=request)
    if scenario == "network":
        raise httpx.ConnectError(CANARY, request=request)
    if scenario == "invalid-json":
        return httpx.Response(200, content=CANARY.encode())
    if scenario == "invalid-shape":
        return httpx.Response(200, json={"choices": [], "segments": CANARY})
    status = int(scenario)
    return httpx.Response(
        status,
        headers={"Location": f"https://redirect.example/?detail={ENCODED_CANARY}"},
        content=CANARY.encode(),
        extensions={"reason_phrase": CANARY.encode()},
    )


def _request(
    operation: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    client: httpx.Client,
) -> Callable[[], object]:
    if operation == "transcription":
        chunk = tmp_path / "chunk.flac"
        chunk.write_bytes(b"synthetic audio")
        transcriber = OpenAICompatibleTranscriber(
            TranscriptionConfig(
                "https://ai.example/v1", CANARY, "test-model", max_attempts=1,
                approved_origin="https://ai.example",
            ),
            temporary_root=tmp_path / "work",
            client=client,
        )
        return lambda: transcriber._transcribe_chunk(AudioChunk(chunk, 0, 10), "en")
    monkeypatch.setattr("paircue.services.translator.httpx.Client", lambda **kwargs: client)
    provider = OpenAICompatibleProvider(
        ProviderConfig(
            "test", "https://ai.example/v1", CANARY, "test-model", max_attempts=1,
            approved_origin="https://ai.example",
        )
    )
    return lambda: provider._request_translations(
        {}, expected={0}, target_language="en", operation=operation,
    )


@pytest.mark.parametrize("operation", ["translation", "final quality check", "transcription"])
@pytest.mark.parametrize(
    "scenario", ["302", "307", "401", "500", "timeout", "network", "invalid-json", "invalid-shape"],
)
def test_provider_failures_keep_untrusted_details_out_of_errors_and_logs(
    operation: str,
    scenario: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["Authorization"] == f"Bearer {CANARY}"
        logging.getLogger("httpcore.http11").debug("provider diagnostic: %s", CANARY)
        return _failure(request, scenario)

    with closing(httpx.Client(transport=httpx.MockTransport(handler))) as client:
        invoke = _request(operation, tmp_path, monkeypatch, client)
        with caplog.at_level(logging.DEBUG), pytest.raises(
            (TranslationError, TranscriptionError),
        ) as caught:
            invoke()
    diagnostic = "".join(traceback.format_exception(caught.value)) + caplog.text
    assert CANARY not in diagnostic
    assert ENCODED_CANARY not in diagnostic
    assert "redirect.example" not in diagnostic
    assert len(str(caught.value)) < 200
    if scenario.isdigit():
        assert f"HTTP {scenario}" in str(caught.value)
    assert len(requests) == 1
    assert requests[0].url.host == "ai.example"


@pytest.mark.parametrize("operation", ["translation", "transcription"])
def test_provider_failure_stays_private_through_pipeline_state_and_dashboard_data(
    operation: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    media_path = tmp_path / "Synthetic.mkv"
    media_path.write_bytes(b"synthetic media")
    state = StateStore(tmp_path / "state.sqlite3")
    with closing(httpx.Client(
        transport=httpx.MockTransport(lambda request: _failure(request, "302")),
    )) as client:
        invoke = _request(operation, tmp_path, monkeypatch, client)
        pipeline = SubtitlePipeline(
            media_root=tmp_path, state=state,
            downloader=None, extractor=None, synchronizer=None, translator=None,  # type: ignore[arg-type]
            glossary=GlossaryStore(tmp_path / "glossary"),
        )

        def process_locked(item: MediaItem, path: Path) -> object:
            return invoke()

        monkeypatch.setattr(pipeline, "_process_locked", process_locked)
        with caplog.at_level(logging.DEBUG):
            result = pipeline.process(MediaItem("1", "movie", media_path, "Synthetic"))
    assert result.status == "failed"
    assert "HTTP 302" in result.message
    assert not (tmp_path / "Synthetic.mul.srt").exists()
    # recent() is also the dashboard's data source. Inspect persisted bytes as well as views.
    diagnostic = result.message + repr(state.recent()) + caplog.text
    for secret in (CANARY, ENCODED_CANARY, "redirect.example"):
        assert secret not in diagnostic
        assert secret.encode() not in state.database.read_bytes()


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (OSError(CANARY), "could not read temporary audio"),
        (ValueError(CANARY), "provider returned an invalid response"),
        (ProviderResponseTooLargeError(), "provider response exceeds the size limit"),
        (
            httpx.HTTPStatusError(
                CANARY, request=httpx.Request("POST", "https://ai.example"),
                response=httpx.Response(999),
            ),
            "provider returned an invalid HTTP status",
        ),
    ],
)
def test_failure_categories_never_copy_exception_text(error: Exception, expected: str) -> None:
    assert safe_provider_failure(error) == expected


def test_http_diagnostic_privacy_is_scoped_and_restored_after_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = logging.getLogger("httpx")
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(ValueError), private_provider_diagnostics():
            with private_provider_diagnostics():
                logger.info("hidden nested request: %s", CANARY)
            logger.info("hidden outer request: %s", CANARY)
            logging.getLogger("paircue").info("application diagnostics still work")
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(logger.info, "unrelated thread still works").result()
            raise ValueError("synthetic failure")
        logger.info("later HTTP diagnostics still work")
    assert CANARY not in caplog.text
    assert "application diagnostics still work" in caplog.text
    assert "unrelated thread still works" in caplog.text
    assert "later HTTP diagnostics still work" in caplog.text
