from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest
import srt

from paircue.services.translator import (
    CompleteTranslator,
    OpenAICompatibleProvider,
    ProviderConfig,
    TranslationError,
)


class FakeProvider:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0
        self.final_check_calls = 0
        self.languages: list[str] = []

    def translate(
        self,
        cues: dict[int, str],
        *,
        context: str,
        glossary: dict[str, str],
        source_language: str,
        source_language_name: str,
        target_language: str,
        target_language_name: str,
        target_language_style: str,
    ) -> dict[int, str]:
        self.calls += 1
        self.languages.append(f"{source_language}->{target_language}")
        if self.fail:
            raise TranslationError("failed")
        return {cue_id: f"中:{text}" for cue_id, text in cues.items()}

    def final_check(
        self,
        cues: dict[int, str],
        drafts: dict[int, str],
        *,
        context: str,
        glossary: dict[str, str],
        source_language: str,
        source_language_name: str,
        target_language: str,
        target_language_name: str,
        target_language_style: str,
    ) -> dict[int, str]:
        self.final_check_calls += 1
        if self.fail:
            raise TranslationError("failed")
        return {cue_id: f"final:{drafts[cue_id]}" for cue_id in cues}


def _cues(count: int) -> list[srt.Subtitle]:
    return [
        srt.Subtitle(
            index + 1,
            timedelta(seconds=index),
            timedelta(seconds=index + 1),
            f"line {index}",
        )
        for index in range(count)
    ]


def test_fallback_is_used_only_after_primary_fails() -> None:
    primary = FakeProvider(fail=True)
    fallback = FakeProvider()
    translator = CompleteTranslator(
        primary,  # type: ignore[arg-type]
        fallback=fallback,  # type: ignore[arg-type]
        batch_size=2,
        source_language="ko",
        target_language="en",
    )

    result = translator.translate_all(_cues(3), context="Movie")

    assert set(result) == {0, 1, 2}
    assert primary.calls == 2
    assert fallback.calls == 2
    assert primary.languages == ["ko->en", "ko->en"]
    assert fallback.languages == ["ko->en", "ko->en"]


def test_incomplete_provider_output_is_rejected() -> None:
    class IncompleteProvider(FakeProvider):
        def translate(
            self,
            cues: dict[int, str],
            *,
            context: str,
            glossary: dict[str, str],
            source_language: str,
            source_language_name: str,
            target_language: str,
            target_language_name: str,
            target_language_style: str,
        ) -> dict[int, str]:
            return {min(cues): "only one"}

    translator = CompleteTranslator(IncompleteProvider(), batch_size=10)  # type: ignore[arg-type]

    with pytest.raises(TranslationError, match="missing"):
        translator.translate_all(_cues(2), context="Movie")


def test_ai_final_check_runs_after_each_complete_translation_batch() -> None:
    provider = FakeProvider()
    translator = CompleteTranslator(
        provider,  # type: ignore[arg-type]
        batch_size=2,
        final_check_enabled=True,
    )

    result = translator.translate_all(_cues(3), context="Movie")

    assert provider.calls == 2
    assert provider.final_check_calls == 2
    assert result == {
        0: "final:中:line 0",
        1: "final:中:line 1",
        2: "final:中:line 2",
    }


def test_ai_final_check_is_fail_closed_on_incomplete_output() -> None:
    class IncompleteFinalProvider(FakeProvider):
        def final_check(
            self,
            cues: dict[int, str],
            drafts: dict[int, str],
            *,
            context: str,
            glossary: dict[str, str],
            source_language: str,
            source_language_name: str,
            target_language: str,
            target_language_name: str,
            target_language_style: str,
        ) -> dict[int, str]:
            return {min(cues): drafts[min(cues)]}

    translator = CompleteTranslator(
        IncompleteFinalProvider(),  # type: ignore[arg-type]
        batch_size=10,
        final_check_enabled=True,
    )

    with pytest.raises(TranslationError, match="final quality check coverage mismatch"):
        translator.translate_all(_cues(2), context="Movie")


def test_ai_final_check_can_be_disabled_for_a_single_pass() -> None:
    provider = FakeProvider()
    translator = CompleteTranslator(provider, final_check_enabled=False)  # type: ignore[arg-type]

    translator.translate_all(_cues(1), context="Movie")

    assert provider.final_check_calls == 0


def test_final_check_request_contains_only_the_documented_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class FakeResponse:
        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def raise_for_status(self) -> None:
            return None

        def iter_bytes(self) -> list[bytes]:
            content = '{"translations":[{"id":0,"text":"Hola"}]}'
            payload = {"choices": [{"message": {"content": content}}]}
            return [json.dumps(payload).encode()]

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            captured["client"] = kwargs

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def stream(self, method: str, url: str, **kwargs: object) -> FakeResponse:
            captured.update({"method": method, "url": url, **kwargs})
            return FakeResponse()

    monkeypatch.setattr("paircue.services.translator.httpx.Client", FakeClient)
    provider = OpenAICompatibleProvider(
        ProviderConfig(
            "test", "https://example.com/v1", "private-key", "model", max_attempts=1,
            approved_origin="https://example.com",
        )
    )

    result = provider.final_check(
        {0: "Hello"},
        {0: "Hola draft"},
        context="Movie · episode 1",
        glossary={"Hello": "Hola"},
        source_language="en",
        source_language_name="English",
        target_language="es",
        target_language_name="Spanish",
        target_language_style="natural dialogue",
    )

    assert result == {0: "Hola"}
    body = captured["json"]
    assert isinstance(body, dict)
    user_data = json.loads(body["messages"][1]["content"])
    assert set(user_data) == {
        "context",
        "glossary",
        "source_language",
        "source_language_name",
        "target_language",
        "target_language_name",
        "subtitles",
    }
    assert user_data["subtitles"] == [
        {"id": 0, "source_text": "Hello", "draft_translation": "Hola draft"}
    ]
    assert "private-key" not in json.dumps(body)
    assert captured["headers"]["Authorization"] == "Bearer private-key"


def test_keyless_local_provider_omits_authorization_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_headers: dict[str, str] = {}

    class FakeResponse:
        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def raise_for_status(self) -> None:
            return None

        def iter_bytes(self) -> list[bytes]:
            content = '{"translations":[{"id":0,"text":"Hola"}]}'
            payload = {"choices": [{"message": {"content": content}}]}
            return [json.dumps(payload).encode()]

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            return None

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def stream(self, method: str, url: str, **kwargs: object) -> FakeResponse:
            headers = kwargs["headers"]
            assert isinstance(headers, dict)
            captured_headers.update(headers)
            return FakeResponse()

    monkeypatch.setattr("paircue.services.translator.httpx.Client", FakeClient)
    provider = OpenAICompatibleProvider(
        ProviderConfig(
            "local", "http://127.0.0.1:11434/v1", "", "model", max_attempts=1,
            approved_origin="http://127.0.0.1:11434",
        )
    )

    provider.translate(
        {0: "Hello"},
        context="Movie",
        glossary={},
        source_language="en",
        source_language_name="English",
        target_language="es",
        target_language_name="Spanish",
        target_language_style="natural dialogue",
    )

    assert "Authorization" not in captured_headers


def test_only_chinese_targets_are_normalized_with_opencc() -> None:
    provider = OpenAICompatibleProvider(
        ProviderConfig(
            "test", "https://example.com", "key", "model", approved_origin="https://example.com",
        )
    )
    response = '{"translations":[{"id":0,"text":"软件"}]}'

    taiwan = provider._parse_response(response, target_language="zh-TW")
    hong_kong = provider._parse_response(response, target_language="zh-HK")
    japanese = provider._parse_response(response, target_language="ja")

    assert taiwan == {0: "軟體"}
    assert hong_kong == {0: "軟件"}
    assert japanese == {0: "软件"}
