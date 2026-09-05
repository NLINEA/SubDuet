from __future__ import annotations

import ipaddress
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, SecretStr, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from paircue.ai_connections import normalize_ai_url, validate_ai_connection
from paircue.languages import canonicalize_language_tag, language_name


def _secret_value(secret: SecretStr) -> str:
    return secret.get_secret_value()


def _is_loopback_url(value: str) -> bool:
    hostname = (urlparse(value).hostname or "").rstrip(".").casefold()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


class PairCueSettings(BaseSettings):
    """Settings for the subtitle service only.

    Download Station has a separate settings class so it never inherits media-server,
    translation, or media-library credentials.
    """

    model_config = SettingsConfigDict(
        env_prefix="PAIRCUE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    platform: Literal["plex", "jellyfin", "emby", "filesystem"] = "plex"
    server_url: str = ""
    server_token: SecretStr = SecretStr("")
    server_user_id: str = ""
    server_path_prefix: str = ""
    filesystem_extensions: str = ".mkv,.mp4,.avi,.m4v,.mov,.ts,.webm"

    # Deprecated Plex-specific aliases retained for existing installations.
    plex_url: str = "http://127.0.0.1:32400"
    plex_token: SecretStr = SecretStr("")
    plex_path_prefix: str = "/volume1/MediaForPlex"
    media_root: Path = Path("/media")
    state_dir: Path = Path("/state")

    scan_interval_seconds: int = Field(default=1800, ge=60, le=86400)
    worker_queue_size: int = Field(default=1000, ge=1, le=10000)
    subtitle_download_enabled: bool = True
    opensubtitles_api_key: SecretStr = SecretStr("")
    opensubtitles_username: str = ""
    opensubtitles_password: SecretStr = SecretStr("")
    subtitle_download_timeout_seconds: float = Field(default=30, ge=5, le=120)
    sync_enabled: bool = True
    sync_max_offset_seconds: int = Field(default=120, ge=1, le=600)
    sync_min_confidence: float = Field(default=0.24, ge=0.1, le=0.95)
    audio_stream_index: int | None = Field(default=None, ge=0, le=65535)
    transcription_enabled: bool = False
    transcription_base_url: str = ""
    transcription_provider: Literal["custom", "openai", "local"] = "custom"
    transcription_approved_origin: str = ""
    transcription_api_key: SecretStr = SecretStr("")
    transcription_model: str = Field(default="whisper-1", max_length=100)
    transcription_timeout_seconds: float = Field(default=300, ge=10, le=900)
    transcription_max_attempts: int = Field(default=3, ge=1, le=6)
    transcription_chunk_seconds: int = Field(default=300, ge=60, le=600)
    transcription_prompt: str = Field(default="", max_length=1_000)
    clean_source_output: bool = False

    source_language: str = "en"
    source_language_name: str = Field(default="", max_length=80)
    target_language: str = "zh-TW"
    target_language_name: str = Field(default="", max_length=80)
    target_language_style: str = Field(
        default="natural, concise dialogue suitable for subtitles",
        min_length=1,
        max_length=200,
    )
    bilingual_order: Literal["target-first", "source-first"] = "target-first"
    bilingual_merge_tolerance_ms: int = Field(default=350, ge=0, le=2000)
    bilingual_merge_min_match_ratio: float = Field(default=0.7, ge=0.5, le=1)

    translation_enabled: bool = False
    translation_base_url: str = ""
    translation_provider: Literal["custom", "openai", "zai", "local"] = "custom"
    translation_approved_origin: str = ""
    translation_api_key: SecretStr = SecretStr("")
    translation_model: str = ""
    translation_disable_thinking: bool = False
    translation_final_check_enabled: bool = True
    translation_batch_size: int = Field(default=30, ge=1, le=50)
    translation_timeout_seconds: float = Field(default=120, ge=5, le=600)
    translation_max_attempts: int = Field(default=3, ge=1, le=6)
    fallback_base_url: str = ""
    fallback_provider: Literal["custom", "openai", "zai", "local"] = "custom"
    fallback_approved_origin: str = ""
    fallback_api_key: SecretStr = SecretStr("")
    fallback_model: str = ""
    fallback_disable_thinking: bool = False

    webhook_enabled: bool = False
    api_host: str = "127.0.0.1"
    api_port: int = Field(default=9292, ge=1, le=65535)
    api_token: SecretStr = SecretStr("")
    api_docs_enabled: bool = False
    trusted_hosts: str = "localhost,127.0.0.1"
    max_webhook_bytes: int = Field(default=131072, ge=1024, le=1048576)

    @field_validator(
        "server_url",
        "plex_url",
    )
    @classmethod
    def validate_service_urls(cls, value: str) -> str:
        if not value:
            return value
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("service URLs must use http or https and include a host")
        if parsed.username or parsed.password:
            raise ValueError("credentials must not be embedded in service URLs")
        return value.rstrip("/")

    @field_validator("translation_base_url", "fallback_base_url", "transcription_base_url")
    @classmethod
    def validate_ai_provider_transport(cls, value: str) -> str:
        return normalize_ai_url(value) if value else value

    @field_validator("source_language", "target_language")
    @classmethod
    def validate_language(cls, value: str) -> str:
        return canonicalize_language_tag(value)

    @field_validator(
        "source_language_name",
        "target_language_name",
        "target_language_style",
        "transcription_prompt",
    )
    @classmethod
    def validate_translation_prompt_setting(cls, value: str, info: ValidationInfo) -> str:
        value = value.strip()
        if info.field_name == "target_language_style" and not value:
            raise ValueError("target language style must not be empty")
        if any(ord(character) < 32 for character in value):
            raise ValueError("translation language settings must be a single line")
        return value

    @model_validator(mode="after")
    def validate_secure_runtime(self) -> PairCueSettings:
        token = _secret_value(self.api_token)
        exposed = self.api_host not in {"127.0.0.1", "::1", "localhost"}
        if (self.webhook_enabled or exposed) and len(token) < 32:
            raise ValueError("PAIRCUE_API_TOKEN must contain at least 32 characters")
        for purpose, enabled in (
            ("translation", self.translation_enabled),
            ("transcription", self.transcription_enabled),
            ("fallback", self.translation_enabled and bool(
                self.fallback_base_url or self.fallback_model
                or _secret_value(self.fallback_api_key)
            )),
        ):
            if not enabled:
                continue
            base_url = getattr(self, f"{purpose}_base_url")
            if not base_url:
                raise ValueError(
                    f"PAIRCUE_{purpose.upper()}_BASE_URL must be explicitly configured"
                )
            try:
                validate_ai_connection(
                    base_url,
                    getattr(self, f"{purpose}_approved_origin"),
                    getattr(self, f"{purpose}_provider"),
                )
            except ValueError as exc:
                raise ValueError(f"{purpose}: {exc}") from None
            if not getattr(self, f"{purpose}_model").strip():
                raise ValueError(f"PAIRCUE_{purpose.upper()}_MODEL must be explicitly configured")
        if (
            self.translation_enabled
            and not _secret_value(self.translation_api_key)
            and not _is_loopback_url(self.translation_base_url)
        ):
            raise ValueError(
                "translation is enabled for a remote provider but "
                "PAIRCUE_TRANSLATION_API_KEY is empty"
            )
        if (
            self.transcription_enabled
            and not _secret_value(self.transcription_api_key)
            and not _is_loopback_url(self.transcription_base_url)
        ):
            raise ValueError(
                "transcription is enabled for a remote provider but "
                "PAIRCUE_TRANSCRIPTION_API_KEY is empty"
            )
        if (
            self.translation_enabled and self.fallback_base_url
            and not _secret_value(self.fallback_api_key)
            and not _is_loopback_url(self.fallback_base_url)
        ):
            raise ValueError("a remote fallback requires PAIRCUE_FALLBACK_API_KEY")
        opensubtitles_password = _secret_value(self.opensubtitles_password)
        opensubtitles_key = _secret_value(self.opensubtitles_api_key)
        if bool(self.opensubtitles_username) != bool(opensubtitles_password):
            raise ValueError("OpenSubtitles username and password must be configured together")
        if (self.opensubtitles_username or opensubtitles_password) and not opensubtitles_key:
            raise ValueError("PAIRCUE_OPENSUBTITLES_API_KEY is required with account credentials")
        if self.source_language.casefold() == self.target_language.casefold():
            raise ValueError("source and target languages must differ")
        if self.platform in {"jellyfin", "emby"}:
            if not self.server_url:
                raise ValueError("PAIRCUE_SERVER_URL is required for Jellyfin and Emby")
            if not _secret_value(self.server_token):
                raise ValueError("PAIRCUE_SERVER_TOKEN is required for Jellyfin and Emby")
            if not self.server_user_id:
                raise ValueError("PAIRCUE_SERVER_USER_ID is required for Jellyfin and Emby")
            if not self.server_path_prefix:
                raise ValueError("PAIRCUE_SERVER_PATH_PREFIX is required for Jellyfin and Emby")
        return self

    @property
    def allowed_hosts(self) -> list[str]:
        hosts = [host.strip() for host in self.trusted_hosts.split(",") if host.strip()]
        return hosts or ["localhost", "127.0.0.1"]

    @property
    def effective_server_url(self) -> str:
        return self.server_url or self.plex_url

    @property
    def effective_server_token(self) -> str:
        return _secret_value(self.server_token) or _secret_value(self.plex_token)

    @property
    def effective_server_path_prefix(self) -> str:
        return self.server_path_prefix or self.plex_path_prefix

    @property
    def media_extensions(self) -> tuple[str, ...]:
        output: list[str] = []
        for raw in self.filesystem_extensions.split(","):
            extension = raw.strip().casefold()
            if not extension:
                continue
            if not extension.startswith("."):
                extension = f".{extension}"
            if len(extension) > 9 or not extension[1:].isalnum():
                raise ValueError("PAIRCUE_FILESYSTEM_EXTENSIONS contains an invalid extension")
            output.append(extension)
        if not output:
            raise ValueError("PAIRCUE_FILESYSTEM_EXTENSIONS must not be empty")
        return tuple(dict.fromkeys(output))

    @property
    def effective_target_language_name(self) -> str:
        return self.target_language_name or language_name(self.target_language)

    @property
    def effective_source_language_name(self) -> str:
        return self.source_language_name or language_name(self.source_language)


class DownloadStationSettings(BaseSettings):
    """Credentials and paths available only to the optional download service."""

    model_config = SettingsConfigDict(
        env_prefix="PAIRCUE_DS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    url: str = "http://127.0.0.1:5000"
    username: str = ""
    password: SecretStr = SecretStr("")
    destination: str = "MediaForPlex/Download"
    watch_dir: Path = Path("/torrents")
    host: str = "127.0.0.1"
    port: int = Field(default=9293, ge=1, le=65535)
    api_token: SecretStr = SecretStr("")
    trusted_hosts: str = "localhost,127.0.0.1"
    max_torrent_bytes: int = Field(default=4 * 1024 * 1024, ge=1024, le=16 * 1024 * 1024)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("PAIRCUE_DS_URL must use http or https and include a host")
        if parsed.username or parsed.password:
            raise ValueError("Download Station credentials must not be embedded in its URL")
        return value.rstrip("/")

    @model_validator(mode="after")
    def validate_credentials(self) -> DownloadStationSettings:
        if len(_secret_value(self.api_token)) < 32:
            raise ValueError("PAIRCUE_DS_API_TOKEN must contain at least 32 characters")
        if not self.username or not _secret_value(self.password):
            raise ValueError("Download Station username and password are required")
        return self

    @property
    def allowed_hosts(self) -> list[str]:
        hosts = [host.strip() for host in self.trusted_hosts.split(",") if host.strip()]
        return hosts or ["localhost", "127.0.0.1"]
