import json
import shutil
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

import paircue
from paircue.ai_connections import ai_origin, normalize_ai_url, validate_ai_connection
from paircue.config import PairCueSettings
from paircue.services.transcriber import OpenAICompatibleTranscriber, TranscriptionConfig
from paircue.services.translator import OpenAICompatibleProvider, ProviderConfig


@pytest.mark.parametrize("purpose", ["translation", "transcription"])
def test_enabling_ai_never_chooses_a_hidden_remote_destination(purpose: str) -> None:
    with pytest.raises(ValidationError, match="BASE_URL must be explicitly configured"):
        PairCueSettings(_env_file=None, **{f"{purpose}_enabled": True})


@pytest.mark.parametrize("purpose", ["translation", "transcription", "fallback"])
def test_old_or_changed_ai_destination_fails_before_network(purpose: str) -> None:
    values = {
        "translation_enabled": True,
        "translation_base_url": "https://one.example/v1",
        "translation_approved_origin": "https://one.example",
        "translation_api_key": "test-only-key",
        "translation_model": "model",
        f"{purpose}_base_url": "https://two.example/v1",
        f"{purpose}_api_key": "test-only-key",
        f"{purpose}_model": "model",
        f"{purpose}_approved_origin": "",
    }
    if purpose == "transcription":
        values["transcription_enabled"] = True
    with pytest.raises(ValidationError, match="APPROVED_ORIGIN"):
        PairCueSettings(_env_file=None, **values)
    values[f"{purpose}_approved_origin"] = "https://one.example"
    with pytest.raises(ValidationError, match="destination changed"):
        PairCueSettings(_env_file=None, **values)


@pytest.mark.parametrize("url", [
    "https://api.openai.com.evil.example/v1", "https://evilapi.openai.com/v1",
    "https://api.openai.com:9443/v1",
])
def test_named_provider_requires_its_exact_origin(url: str) -> None:
    with pytest.raises(ValueError, match="selected provider"):
        validate_ai_connection(url, ai_origin(url), "openai")


@pytest.mark.parametrize("url", [
    "https://user:password@ai.example/v1", "https://@ai.example/v1",
    "https://ai.example/v1?api_key=test-only-key", "https://ai.example/v1#secret",
    "https://ai.example\\@elsewhere.example/v1", "https://ai.example/\nother",
    "https://ai.example:wrong/v1", "ftp://ai.example/v1", "http://ai.example/v1",
    "https://ai.example/\x7fother", "https://%61i.example/v1",
])
def test_ambiguous_ai_urls_are_rejected_without_echoing_them(url: str) -> None:
    with pytest.raises(ValueError) as error:
        normalize_ai_url(url)
    assert url not in str(error.value)
    assert "test-only-key" not in str(error.value)
    assert "password@" not in str(error.value)


def test_normalized_origins_allow_explicit_local_or_custom_providers() -> None:
    assert validate_ai_connection(
        "https://API.OPENAI.COM:443/v1/", "https://api.openai.com", "openai",
    ) == "https://api.openai.com/v1"
    assert validate_ai_connection(
        "http://[::1]:9000/v1", "http://[::1]:9000", "local",
    ) == "http://[::1]:9000/v1"
    with pytest.raises(ValueError, match="loopback"):
        validate_ai_connection("https://remote.example/v1", "https://remote.example", "local")
    with pytest.raises(ValueError, match="only a scheme"):
        validate_ai_connection("https://remote.example/v1", "https://remote.example/v1")


def test_low_level_clients_also_enforce_approval_and_hide_key_in_repr(tmp_path: Path) -> None:
    translation = ProviderConfig("test", "https://ai.example/v1", "test-only-secret", "model")
    transcription = TranscriptionConfig("https://ai.example/v1", "test-only-secret", "model")
    assert "test-only-secret" not in repr(translation)
    assert "test-only-secret" not in repr(transcription)
    with pytest.raises(ValueError, match="APPROVED_ORIGIN"):
        OpenAICompatibleProvider(translation)
    with pytest.raises(ValueError, match="APPROVED_ORIGIN"):
        OpenAICompatibleTranscriber(transcription, temporary_root=tmp_path / "not-created")
    assert not (tmp_path / "not-created").exists()


def test_configuration_errors_never_render_raw_credentials() -> None:
    with pytest.raises(ValidationError) as error:
        PairCueSettings(
            _env_file=None, translation_enabled=True,
            translation_api_key="test-only-secret-do-not-render",
        )
    assert "test-only-secret-do-not-render" not in str(error.value)


def test_disabled_ai_allows_empty_models_from_visual_setup() -> None:
    PairCueSettings(_env_file=None, transcription_model="", translation_model="")


def test_remote_fallback_requires_its_own_key() -> None:
    with pytest.raises(ValidationError, match="FALLBACK_API_KEY"):
        PairCueSettings(
            _env_file=None, translation_enabled=True,
            translation_base_url="http://localhost:9000/v1", translation_model="model",
            translation_approved_origin="http://localhost:9000",
            fallback_base_url="https://fallback.example/v1", fallback_model="model",
            fallback_approved_origin="https://fallback.example",
        )


def test_frontend_ai_policy_agrees_with_backend() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is needed for the pure frontend policy regression")
    policy = (Path(paircue.__file__).parent / "setup" / "ai-connection.js").read_text()
    cases = [
        ["openai", "https://api.openai.com/v1"],
        ["openai", "https://api.openai.com.evil.example/v1"],
        ["zai", "https://api.openai.com/v1"],
        ["custom", "https://private.example/v1"],
        ["local", "http://[::1]:9000/v1"],
        ["local", "https://remote.example/v1"],
        ["custom", "javascript:alert(1)"],
        ["custom", "https://user:password@remote.example/v1"],
        ["custom", "https://remote.example/v1?token=test-only-key"],
        ["", "https://api.openai.com/v1"],
        ["custom", "https://@remote.example/v1"],
        ["custom", "https://%61i.example/v1"],
        ["custom", "https://remote.example/\x7fother"],
    ]
    script = policy + "\n" + """
const cases = JSON.parse(process.argv[1]);
const results = cases.map(([provider, url]) => {
  try { return aiConnections.describe(provider, url).origin; } catch { return null; }
});
process.stdout.write(JSON.stringify(results));
"""
    result = subprocess.run(  # noqa: S603 - resolved Node and project-owned code only
        [node, "-e", script, json.dumps(cases)], capture_output=True, text=True,
        check=True, timeout=10,
    )
    assert json.loads(result.stdout) == [
        "https://api.openai.com", None, None, "https://private.example",
        "http://[::1]:9000", None, None, None, None, None, None, None, None,
    ]
