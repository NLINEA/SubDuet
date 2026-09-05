from pathlib import Path

import pytest

from paircue.config import PairCueSettings
from paircue.factory import build_media_source, build_pipeline, check_media_source_connection
from paircue.services.filesystem import FilesystemSource
from paircue.services.media_browser import EmbyClient, JellyfinClient
from paircue.services.plex import PlexClient


def test_pipeline_wires_approved_connections_and_the_same_audio_choice(tmp_path: Path) -> None:
    settings = PairCueSettings(
        _env_file=None, platform="filesystem", media_root=tmp_path, state_dir=tmp_path / "state",
        source_language="ja", audio_stream_index=3,
        translation_enabled=True, translation_provider="local",
        translation_base_url="http://localhost:9000/v1", translation_model="model",
        translation_approved_origin="http://localhost:9000",
        fallback_provider="local", fallback_base_url="http://localhost:9001/v1",
        fallback_model="fallback", fallback_approved_origin="http://localhost:9001",
        transcription_enabled=True, transcription_provider="local",
        transcription_base_url="http://localhost:9002/v1",
        transcription_approved_origin="http://localhost:9002",
    )
    pipeline = build_pipeline(settings)
    try:
        assert pipeline.synchronizer.source_language == "ja"
        assert pipeline.synchronizer.audio_stream_index == 3
        assert pipeline.transcriber.config.audio_stream_index == 3
        assert pipeline.transcriber.base_url == "http://localhost:9002/v1"
        assert pipeline.translator.primary.base_url == "http://localhost:9000/v1"
        assert pipeline.translator.fallback.base_url == "http://localhost:9001/v1"
    finally:
        pipeline.close()


@pytest.mark.parametrize(
    ("platform", "expected_type"),
    [
        ("jellyfin", JellyfinClient),
        ("emby", EmbyClient),
    ],
)
def test_factory_selects_media_server_connector(
    tmp_path: Path,
    platform: str,
    expected_type: type[JellyfinClient] | type[EmbyClient],
) -> None:
    settings = PairCueSettings(
        platform=platform,
        server_url="http://media-server:8096",
        server_token="s" * 16,
        server_user_id="user-id",
        server_path_prefix="/media",
        media_root=tmp_path,
    )

    source = build_media_source(settings)
    try:
        assert isinstance(source, expected_type)
    finally:
        source.close()


def test_factory_selects_filesystem_source(tmp_path: Path) -> None:
    source = build_media_source(PairCueSettings(platform="filesystem", media_root=tmp_path))

    assert isinstance(source, FilesystemSource)


def test_filesystem_connection_check_proves_the_selected_folder_exists(tmp_path: Path) -> None:
    settings = PairCueSettings(platform="filesystem", media_root=tmp_path)

    assert check_media_source_connection(settings) == "Connected to the media folder."


def test_factory_keeps_legacy_plex_connector(tmp_path: Path) -> None:
    source = build_media_source(
        PairCueSettings(
            plex_url="http://plex:32400",
            plex_token="p" * 16,
            plex_path_prefix="/media",
            media_root=tmp_path,
        )
    )
    try:
        assert isinstance(source, PlexClient)
    finally:
        source.close()
