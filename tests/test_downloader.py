import gzip
import io
import zipfile
from pathlib import Path

import httpx

from paircue.models import MediaItem
from paircue.services.downloader import OpenSubtitlesDownloader, opensubtitles_movie_hash

SUBTITLE = b"1\n00:00:00,000 --> 00:00:01,000\nHello\n\n"


def _movie(tmp_path: Path) -> MediaItem:
    media = tmp_path / "Movie.mkv"
    media.write_bytes(b"video")
    return MediaItem("1", "movie", media, "Movie", year=2024)


def test_official_api_searches_and_downloads_the_requested_language(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/subtitles":
            assert request.headers["Api-Key"] == "api-key"
            assert request.headers["User-Agent"].startswith("SubDuet v")
            assert request.url.params["languages"] == "ja"
            assert request.url.params["query"] == "Movie"
            assert request.url.params["type"] == "movie"
            assert request.url.params["year"] == "2024"
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "attributes": {
                                "language": "ja",
                                "files": [{"file_id": 42, "file_name": "Movie.srt"}],
                            }
                        }
                    ]
                },
            )
        if request.url.path == "/api/v1/download":
            assert request.headers.get("Authorization") is None
            return httpx.Response(
                200,
                json={"link": "https://dl.opensubtitles.com/subtitles/42.srt"},
            )
        if request.url.host == "dl.opensubtitles.com":
            return httpx.Response(200, content=SUBTITLE)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    downloader = OpenSubtitlesDownloader(api_key="api-key", client=client)

    outputs = downloader.download(_movie(tmp_path), {"ja"})

    assert outputs == (tmp_path / "Movie.ja.srt",)
    assert outputs[0].read_bytes() == SUBTITLE


def test_movie_hash_uses_file_edges_and_size(tmp_path: Path) -> None:
    media = tmp_path / "movie.mkv"
    media.write_bytes(b"\x01" * 65_536 + b"\x02" * 65_536)

    assert opensubtitles_movie_hash(media) == ("6060606060626000", 131_072)


def test_hash_match_is_preferred_over_title_search(tmp_path: Path) -> None:
    item = _movie(tmp_path)
    item.path.write_bytes(b"\x01" * 65_536 + b"\x02" * 65_536)
    search_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal search_calls
        if request.url.path == "/api/v1/subtitles":
            search_calls += 1
            assert request.url.params["moviehash"] == "6060606060626000"
            assert request.url.params["moviebytesize"] == "131072"
            assert "query" not in request.url.params
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"attributes": {"language": "en", "files": [{"file_id": 88}]}}
                    ]
                },
            )
        if request.url.path == "/api/v1/download":
            return httpx.Response(
                200,
                json={"link": "https://dl.opensubtitles.com/subtitles/88.srt"},
            )
        if request.url.host == "dl.opensubtitles.com":
            return httpx.Response(200, content=SUBTITLE)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    downloader = OpenSubtitlesDownloader(
        api_key="api-key",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert downloader.download(item, {"en"}) == (tmp_path / "Movie.en.srt",)
    assert search_calls == 1


def test_title_search_is_fallback_when_hash_has_no_match(tmp_path: Path) -> None:
    item = _movie(tmp_path)
    item.path.write_bytes(b"\x01" * 65_536 + b"\x02" * 65_536)
    search_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal search_calls
        if request.url.path == "/api/v1/subtitles":
            search_calls += 1
            if "moviehash" in request.url.params:
                return httpx.Response(200, json={"data": []})
            assert request.url.params["query"] == "Movie"
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"attributes": {"language": "en", "files": [{"file_id": 89}]}}
                    ]
                },
            )
        if request.url.path == "/api/v1/download":
            return httpx.Response(
                200,
                json={"link": "https://dl.opensubtitles.com/subtitles/89.srt"},
            )
        return httpx.Response(200, content=SUBTITLE)

    downloader = OpenSubtitlesDownloader(
        api_key="api-key",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert downloader.download(item, {"en"}) == (tmp_path / "Movie.en.srt",)
    assert search_calls == 2


def test_account_login_token_and_returned_api_host_are_used(tmp_path: Path) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host or "")
        if request.url.path == "/api/v1/subtitles":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"attributes": {"language": "en", "files": [{"file_id": 7}]}}
                    ]
                },
            )
        if request.url.path == "/api/v1/login":
            return httpx.Response(
                200,
                json={"token": "jwt-token", "base_url": "vip-api.opensubtitles.com"},
            )
        if request.url.path == "/api/v1/download":
            assert request.url.host == "vip-api.opensubtitles.com"
            assert request.headers["Authorization"] == "Bearer jwt-token"
            return httpx.Response(
                200,
                json={"link": "https://dl.opensubtitles.com/subtitles/7.srt.gz"},
            )
        if request.url.host == "dl.opensubtitles.com":
            return httpx.Response(200, content=gzip.compress(SUBTITLE))
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    downloader = OpenSubtitlesDownloader(
        api_key="api-key",
        username="user",
        password="password",
        client=client,
    )

    outputs = downloader.download(_movie(tmp_path), {"en"})

    assert outputs == (tmp_path / "Movie.en.srt",)
    assert "vip-api.opensubtitles.com" in calls


def test_archive_is_bounded_and_normalized_to_utf8() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("subtitle.srt", SUBTITLE)

    normalized = OpenSubtitlesDownloader._normalize_subtitle(buffer.getvalue())

    assert normalized == SUBTITLE


def test_download_rejects_non_opensubtitles_url(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/subtitles":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"attributes": {"language": "en", "files": [{"file_id": 9}]}}
                    ]
                },
            )
        return httpx.Response(200, json={"link": "https://example.com/subtitle.srt"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    downloader = OpenSubtitlesDownloader(api_key="api-key", client=client)

    assert downloader.download(_movie(tmp_path), {"en"}) == ()
    assert not (tmp_path / "Movie.en.srt").exists()


def test_download_rejects_redirect_away_from_opensubtitles(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/subtitles":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"attributes": {"language": "en", "files": [{"file_id": 10}]}}
                    ]
                },
            )
        if request.url.path == "/api/v1/download":
            return httpx.Response(
                200,
                json={"link": "https://dl.opensubtitles.com/subtitles/10.srt"},
            )
        if request.url.host == "dl.opensubtitles.com":
            return httpx.Response(302, headers={"Location": "https://example.com/file.srt"})
        return httpx.Response(200, content=SUBTITLE)

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    downloader = OpenSubtitlesDownloader(api_key="api-key", client=client)

    assert downloader.download(_movie(tmp_path), {"en"}) == ()
    assert not (tmp_path / "Movie.en.srt").exists()
