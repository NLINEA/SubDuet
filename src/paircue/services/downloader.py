from __future__ import annotations

import gzip
import io
import logging
import struct
import threading
import zipfile
from pathlib import Path
from typing import Protocol, cast
from urllib.parse import urlparse

import httpx
import srt
from charset_normalizer import from_bytes

from paircue import __version__
from paircue.languages import language_matches
from paircue.models import MediaItem
from paircue.services.atomic import atomic_write_bytes

log = logging.getLogger(__name__)

OPEN_SUBTITLES_API = "https://api.opensubtitles.com/api/v1"
MAX_SUBTITLE_BYTES = 5 * 1024 * 1024
HASH_BLOCK_BYTES = 64 * 1024
UINT64_MASK = (1 << 64) - 1


def opensubtitles_movie_hash(path: Path) -> tuple[str, int] | None:
    """Return the OSDB hash from file size plus first/last 64 KiB uint64 words."""
    size = path.stat().st_size
    if size < HASH_BLOCK_BYTES * 2:
        return None
    with path.open("rb") as media:
        first = media.read(HASH_BLOCK_BYTES)
        media.seek(-HASH_BLOCK_BYTES, 2)
        last = media.read(HASH_BLOCK_BYTES)
    if len(first) != HASH_BLOCK_BYTES or len(last) != HASH_BLOCK_BYTES:
        return None
    checksum = size
    for (value,) in struct.iter_unpack("<Q", first + last):
        checksum = (checksum + value) & UINT64_MASK
    return f"{checksum:016x}", size


class SubtitleDownloader(Protocol):
    def download(self, item: MediaItem, languages: set[str]) -> tuple[Path, ...]: ...

    def close(self) -> None: ...


class DisabledSubtitleDownloader:
    def download(self, item: MediaItem, languages: set[str]) -> tuple[Path, ...]:
        return ()

    def close(self) -> None:
        return


class OpenSubtitlesDownloader:
    """Small first-party client for the documented OpenSubtitles.com REST API."""

    def __init__(
        self,
        *,
        api_key: str,
        username: str = "",
        password: str = "",
        timeout_seconds: float = 30,
        client: httpx.Client | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("an OpenSubtitles API key is required")
        if bool(username) != bool(password):
            raise ValueError("OpenSubtitles username and password must be configured together")
        self.api_key = api_key
        self.username = username
        self.password = password
        self._owns_client = client is None
        self.client = client or httpx.Client(
            follow_redirects=False,
            timeout=httpx.Timeout(timeout_seconds),
        )
        self._token = ""
        self._api_base = OPEN_SUBTITLES_API
        self._login_lock = threading.Lock()

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def download(self, item: MediaItem, languages: set[str]) -> tuple[Path, ...]:
        outputs: list[Path] = []
        for language in sorted(languages):
            target = item.path.parent / f"{item.path.stem}.{language}.srt"
            if target.exists():
                continue
            try:
                file_id = self._search_file(item, language)
                if file_id is None:
                    continue
                content = self._download_file(file_id, target.name)
                atomic_write_bytes(target, content)
                outputs.append(target)
                log.info("downloaded %s subtitle for %s", language, item.path.name)
            except (
                EOFError,
                OSError,
                httpx.HTTPError,
                KeyError,
                TypeError,
                ValueError,
                zipfile.BadZipFile,
            ) as exc:
                log.warning(
                    "OpenSubtitles request failed for %s (%s): %s",
                    item.path.name,
                    language,
                    exc,
                )
        return tuple(outputs)

    def _search_file(self, item: MediaItem, language: str) -> int | None:
        hash_result = opensubtitles_movie_hash(item.path)
        if hash_result is not None:
            movie_hash, movie_size = hash_result
            file_id = self._search(
                {
                    "languages": language.casefold(),
                    "moviebytesize": movie_size,
                    "moviehash": movie_hash,
                    "order_by": "download_count",
                    "order_direction": "desc",
                },
                language,
            )
            if file_id is not None:
                return file_id
        params: dict[str, str | int] = {
            "languages": language.casefold(),
            "order_by": "download_count",
            "order_direction": "desc",
            "query": item.show_title or item.title,
            "type": item.media_type,
        }
        if item.media_type == "movie" and item.year is not None:
            params["year"] = item.year
        if item.media_type == "episode":
            if item.season is not None:
                params["season_number"] = item.season
            if item.episode is not None:
                params["episode_number"] = item.episode
        return self._search(params, language)

    def _search(self, params: dict[str, str | int], language: str) -> int | None:
        response = self.client.get(
            f"{self._api_base}/subtitles",
            params=params,
            headers=self._headers(),
        )
        response.raise_for_status()
        data = response.json().get("data", [])
        if not isinstance(data, list):
            raise ValueError("OpenSubtitles returned an invalid search response")
        for result in data:
            if not isinstance(result, dict):
                continue
            attributes = result.get("attributes", {})
            if not isinstance(attributes, dict):
                continue
            observed = str(attributes.get("language") or "")
            if observed and not language_matches(observed, language):
                continue
            files = attributes.get("files", [])
            if not isinstance(files, list):
                continue
            for file in files:
                if isinstance(file, dict) and isinstance(file.get("file_id"), int):
                    return int(file["file_id"])
        return None

    def _download_file(self, file_id: int, output_name: str) -> bytes:
        token = self._login_token()
        response = self.client.post(
            f"{self._api_base}/download",
            json={"file_id": file_id, "file_name": output_name, "sub_format": "srt"},
            headers=self._headers(token),
        )
        response.raise_for_status()
        link = response.json().get("link")
        if not isinstance(link, str) or not self._is_allowed_download_url(link):
            raise ValueError("OpenSubtitles returned an unsafe download URL")
        with self.client.stream("GET", link, headers={"User-Agent": self._user_agent}) as download:
            download.raise_for_status()
            if not self._is_allowed_download_url(str(download.url)):
                raise ValueError("OpenSubtitles redirected to an unsafe download URL")
            content = bytearray()
            for chunk in download.iter_bytes():
                content.extend(chunk)
                if len(content) > MAX_SUBTITLE_BYTES:
                    raise ValueError("downloaded subtitle exceeds the size limit")
        return self._normalize_subtitle(bytes(content))

    def _login_token(self) -> str:
        if not self.username:
            return ""
        with self._login_lock:
            if self._token:
                return self._token
            response = self.client.post(
                f"{OPEN_SUBTITLES_API}/login",
                json={"username": self.username, "password": self.password},
                headers=self._headers(),
            )
            response.raise_for_status()
            payload = response.json()
            token = payload.get("token")
            if not isinstance(token, str) or not token:
                raise ValueError("OpenSubtitles login returned no token")
            base_url = payload.get("base_url")
            if isinstance(base_url, str):
                parsed = urlparse(base_url if "://" in base_url else f"https://{base_url}")
                if parsed.scheme == "https" and self._is_opensubtitles_host(parsed.hostname):
                    self._api_base = f"https://{parsed.hostname}/api/v1"
            self._token = token
            return token

    @property
    def _user_agent(self) -> str:
        return f"SubDuet v{__version__}"

    def _headers(self, token: str = "") -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Api-Key": self.api_key,
            "User-Agent": self._user_agent,
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    @staticmethod
    def _is_opensubtitles_host(hostname: str | None) -> bool:
        return hostname == "opensubtitles.com" or bool(
            hostname and hostname.endswith(".opensubtitles.com")
        )

    @classmethod
    def _is_allowed_download_url(cls, value: str) -> bool:
        parsed = urlparse(value)
        return parsed.scheme == "https" and cls._is_opensubtitles_host(parsed.hostname)

    @staticmethod
    def _normalize_subtitle(content: bytes) -> bytes:
        if not content or len(content) > MAX_SUBTITLE_BYTES:
            raise ValueError("downloaded subtitle has an invalid size")
        if content.startswith(b"\x1f\x8b"):
            with gzip.GzipFile(fileobj=io.BytesIO(content)) as archive:
                content = archive.read(MAX_SUBTITLE_BYTES + 1)
        elif content.startswith(b"PK\x03\x04"):
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                candidates = [
                    entry
                    for entry in archive.infolist()
                    if not entry.is_dir()
                    and entry.filename.casefold().endswith(".srt")
                    and 0 < entry.file_size <= MAX_SUBTITLE_BYTES
                ]
                if not candidates:
                    raise ValueError("subtitle archive contains no safe SRT file")
                with archive.open(min(candidates, key=lambda entry: entry.file_size)) as file:
                    content = file.read(MAX_SUBTITLE_BYTES + 1)
        if len(content) > MAX_SUBTITLE_BYTES:
            raise ValueError("expanded subtitle exceeds the size limit")
        match = from_bytes(content).best()
        if match is None:
            raise ValueError("downloaded subtitle encoding could not be detected")
        text = str(match).replace("\r\n", "\n").replace("\r", "\n")
        cues = list(srt.parse(text, ignore_errors=False))
        if not cues:
            raise ValueError("downloaded subtitle contains no valid cues")
        rendered = cast(str, srt.compose(cues, reindex=True))
        return rendered.encode("utf-8")
