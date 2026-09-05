from __future__ import annotations

from paircue.config import PairCueSettings
from paircue.runtime import CoreRuntime, JobCoordinator
from paircue.services.downloader import (
    DisabledSubtitleDownloader,
    OpenSubtitlesDownloader,
    SubtitleDownloader,
)
from paircue.services.filesystem import FilesystemSource
from paircue.services.glossary import GlossaryStore
from paircue.services.media_browser import EmbyClient, JellyfinClient
from paircue.services.media_source import MediaSource
from paircue.services.media_tools import EmbeddedSubtitleExtractor, SubtitleSynchronizer
from paircue.services.pipeline import SubtitlePipeline
from paircue.services.plex import PlexClient
from paircue.services.state import StateStore
from paircue.services.transcriber import OpenAICompatibleTranscriber, TranscriptionConfig
from paircue.services.translator import (
    CompleteTranslator,
    OpenAICompatibleProvider,
    ProviderConfig,
)


def build_runtime(settings: PairCueSettings) -> CoreRuntime:
    pipeline = build_pipeline(settings)
    media_source = build_media_source(settings)
    coordinator = JobCoordinator(pipeline, max_size=settings.worker_queue_size)
    return CoreRuntime(media_source, coordinator, settings.scan_interval_seconds)


def build_pipeline(settings: PairCueSettings) -> SubtitlePipeline:
    """Build the subtitle pipeline without requiring a media-server client."""

    state = StateStore(settings.state_dir / "paircue.sqlite3")
    opensubtitles_key = settings.opensubtitles_api_key.get_secret_value()
    downloader: SubtitleDownloader
    if settings.subtitle_download_enabled and opensubtitles_key:
        downloader = OpenSubtitlesDownloader(
            api_key=opensubtitles_key,
            username=settings.opensubtitles_username,
            password=settings.opensubtitles_password.get_secret_value(),
            timeout_seconds=settings.subtitle_download_timeout_seconds,
        )
    else:
        downloader = DisabledSubtitleDownloader()
    glossary = GlossaryStore(settings.state_dir / "glossaries")
    synchronizer = (
        SubtitleSynchronizer(
            max_offset_seconds=settings.sync_max_offset_seconds,
            min_confidence=settings.sync_min_confidence,
            source_language=settings.source_language,
            audio_stream_index=settings.audio_stream_index,
        )
        if settings.sync_enabled
        else None
    )

    translator: CompleteTranslator | None = None
    if settings.translation_enabled:
        primary = OpenAICompatibleProvider(
            ProviderConfig(
                name="primary",
                base_url=settings.translation_base_url,
                api_key=settings.translation_api_key.get_secret_value(),
                model=settings.translation_model,
                timeout_seconds=settings.translation_timeout_seconds,
                max_attempts=settings.translation_max_attempts,
                disable_thinking=settings.translation_disable_thinking,
                provider=settings.translation_provider,
                approved_origin=settings.translation_approved_origin,
            )
        )
        fallback = None
        if settings.fallback_base_url and settings.fallback_model:
            fallback_key = settings.fallback_api_key.get_secret_value()
            fallback = OpenAICompatibleProvider(
                ProviderConfig(
                    name="fallback",
                    base_url=settings.fallback_base_url,
                    api_key=fallback_key,
                    model=settings.fallback_model,
                    timeout_seconds=settings.translation_timeout_seconds,
                    max_attempts=settings.translation_max_attempts,
                    disable_thinking=settings.fallback_disable_thinking,
                    provider=settings.fallback_provider,
                    approved_origin=settings.fallback_approved_origin,
                )
            )
        translator = CompleteTranslator(
            primary,
            fallback=fallback,
            batch_size=settings.translation_batch_size,
            source_language=settings.source_language,
            source_language_name=settings.effective_source_language_name,
            target_language=settings.target_language,
            target_language_name=settings.effective_target_language_name,
            target_language_style=settings.target_language_style,
            final_check_enabled=settings.translation_final_check_enabled,
        )

    transcriber = None
    if settings.transcription_enabled:
        transcriber = OpenAICompatibleTranscriber(
            TranscriptionConfig(
                base_url=settings.transcription_base_url,
                api_key=settings.transcription_api_key.get_secret_value(),
                model=settings.transcription_model,
                timeout_seconds=settings.transcription_timeout_seconds,
                max_attempts=settings.transcription_max_attempts,
                chunk_seconds=settings.transcription_chunk_seconds,
                prompt=settings.transcription_prompt,
                provider=settings.transcription_provider,
                approved_origin=settings.transcription_approved_origin,
                audio_stream_index=settings.audio_stream_index,
            ),
            temporary_root=settings.state_dir / "transcription",
        )

    return SubtitlePipeline(
        media_root=settings.media_root,
        state=state,
        downloader=downloader,
        extractor=EmbeddedSubtitleExtractor(),
        synchronizer=synchronizer,
        transcriber=transcriber,
        translator=translator,
        glossary=glossary,
        clean_source_output=settings.clean_source_output,
        source_language=settings.source_language,
        target_language=settings.target_language,
        bilingual_order=settings.bilingual_order,
        bilingual_merge_tolerance_ms=settings.bilingual_merge_tolerance_ms,
        bilingual_merge_min_match_ratio=settings.bilingual_merge_min_match_ratio,
    )


def build_media_source(settings: PairCueSettings) -> MediaSource:
    if settings.platform == "filesystem":
        return FilesystemSource(
            media_root=settings.media_root,
            extensions=settings.media_extensions,
        )
    if settings.platform == "plex":
        return PlexClient(
            base_url=settings.effective_server_url,
            token=settings.effective_server_token,
            plex_path_prefix=settings.effective_server_path_prefix,
            media_root=settings.media_root,
        )
    client_type = JellyfinClient if settings.platform == "jellyfin" else EmbyClient
    media_source: MediaSource = client_type(
        base_url=settings.effective_server_url,
        token=settings.effective_server_token,
        user_id=settings.server_user_id,
        server_path_prefix=settings.effective_server_path_prefix,
        media_root=settings.media_root,
    )
    return media_source


def check_media_source_connection(settings: PairCueSettings) -> str:
    """Make the smallest authenticated request that proves the selected source is reachable."""

    source = build_media_source(settings)
    try:
        if isinstance(source, PlexClient):
            libraries = source.libraries()
            noun = "library" if len(libraries) == 1 else "libraries"
            return f"Connected to Plex. Found {len(libraries)} {noun}."
        if isinstance(source, (JellyfinClient, EmbyClient)):
            user_name = source.user_name()
            return f"Connected to {source.platform.title()} as {user_name}."
        root = settings.media_root.resolve(strict=True)
        if not root.is_dir():
            raise ValueError("the selected media location is not a folder")
        return "Connected to the media folder."
    finally:
        source.close()
