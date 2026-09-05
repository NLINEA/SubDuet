from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

import httpx

from paircue import __version__
from paircue.models import MediaItem, MediaType
from paircue.services.media_source import MediaSource, MediaSourceError, remap_server_path

MediaBrowserPlatform = Literal["jellyfin", "emby"]
SAFE_ITEM_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class MediaBrowserError(MediaSourceError):
    pass


class MediaBrowserClient(MediaSource):
    """Read-only Jellyfin/Emby library adapter using their shared item API shape."""

    def __init__(
        self,
        *,
        platform: MediaBrowserPlatform,
        base_url: str,
        token: str,
        user_id: str,
        server_path_prefix: str,
        media_root: Path,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not token:
            raise ValueError(f"{platform} token is required")
        if '"' in token or "\\" in token or any(ord(character) < 33 for character in token):
            raise ValueError(f"{platform} token contains invalid header characters")
        if not SAFE_ITEM_ID.fullmatch(user_id):
            raise ValueError(f"{platform} user id contains invalid characters")
        self.platform = platform
        api_root = base_url.rstrip("/")
        if platform == "emby" and not api_root.casefold().endswith("/emby"):
            api_root = f"{api_root}/emby"
        self.server_path_prefix = server_path_prefix
        self.media_root = media_root
        authorization = (
            'MediaBrowser Client="SubDuet", Device="Server", DeviceId="paircue", '
            f'Version="{__version__}", Token="{token}"'
        )
        self._client = httpx.Client(
            base_url=f"{api_root}/",
            headers={
                "Authorization": authorization,
                "X-Emby-Token": token,
                "Accept": "application/json",
            },
            timeout=30,
            follow_redirects=False,
            transport=transport,
        )
        self.user_id = user_id

    def close(self) -> None:
        self._client.close()

    def _get(self, path: str, *, params: dict[str, str | int] | None = None) -> Any:
        response = self._client.get(path.lstrip("/"), params=params)
        response.raise_for_status()
        return response.json()

    def scan_items(self) -> list[MediaItem]:
        return self._paginated_items()

    def user_name(self) -> str:
        data = self._get(f"/Users/{self.user_id}")
        if not isinstance(data, dict):
            raise MediaBrowserError(f"{self.platform} returned an unexpected user response")
        return str(data.get("Name") or self.user_id)

    def _paginated_items(self, *, page_size: int = 200) -> list[MediaItem]:
        output: list[MediaItem] = []
        offset = 0
        previous_page_ids: tuple[str, ...] | None = None
        while True:
            data = self._get(
                f"/Users/{self.user_id}/Items",
                params={
                    "Recursive": "true",
                    "IncludeItemTypes": "Movie,Episode",
                    "Fields": "Path",
                    "StartIndex": offset,
                    "Limit": page_size,
                    "EnableTotalRecordCount": "true",
                },
            )
            if not isinstance(data, dict):
                raise MediaBrowserError(f"{self.platform} returned an unexpected response")
            rows = data.get("Items", [])
            if not isinstance(rows, list):
                raise MediaBrowserError(f"{self.platform} item page has an unexpected shape")
            page_ids = tuple(str(row.get("Id") or "") for row in rows if isinstance(row, dict))
            if rows and page_ids == previous_page_ids:
                raise MediaBrowserError(f"{self.platform} ignored pagination")
            previous_page_ids = page_ids
            for row in rows:
                if isinstance(row, dict):
                    item = self._extract(row)
                    if item is not None:
                        output.append(item)
            received = len(rows)
            total = data.get("TotalRecordCount")
            if received == 0 or (isinstance(total, int) and offset + received >= total):
                break
            if received < page_size and not isinstance(total, int):
                break
            offset += received
        return output

    def item_for_id(self, item_id: str) -> MediaItem | None:
        if not SAFE_ITEM_ID.fullmatch(item_id):
            raise ValueError("item id contains invalid characters")
        try:
            data = self._get(f"/Users/{self.user_id}/Items/{item_id}", params={"Fields": "Path"})
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise
        if not isinstance(data, dict):
            raise MediaBrowserError(f"{self.platform} returned an unexpected item response")
        return self._extract(data)

    def _extract(self, metadata: dict[str, Any]) -> MediaItem | None:
        raw_type = str(metadata.get("Type") or "").casefold()
        if raw_type not in {"movie", "episode"}:
            return None
        location_type = str(metadata.get("LocationType") or "filesystem").casefold()
        if location_type != "filesystem":
            return None
        server_path = str(metadata.get("Path") or "")
        if not server_path:
            media_sources = metadata.get("MediaSources", [])
            if isinstance(media_sources, list):
                for source in media_sources:
                    if isinstance(source, dict) and source.get("Path"):
                        server_path = str(source["Path"])
                        break
        if not server_path:
            return None
        media_type: MediaType = "movie" if raw_type == "movie" else "episode"
        return MediaItem(
            item_id=str(metadata.get("Id") or ""),
            media_type=media_type,
            path=self.remap_path(server_path),
            title=str(metadata.get("Name") or "Unknown"),
            year=self._integer(metadata.get("ProductionYear")),
            show_title=str(metadata.get("SeriesName") or ""),
            season=self._integer(metadata.get("ParentIndexNumber")),
            episode=self._integer(metadata.get("IndexNumber")),
            library_key=str(metadata.get("CollectionFolderId") or metadata.get("ParentId") or ""),
        )

    def remap_path(self, server_path: str) -> Path:
        return remap_server_path(
            server_path,
            server_path_prefix=self.server_path_prefix,
            media_root=self.media_root,
            platform=self.platform.title(),
        )

    @staticmethod
    def _integer(value: Any) -> int | None:
        return value if isinstance(value, int) and not isinstance(value, bool) else None


class JellyfinClient(MediaBrowserClient):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(platform="jellyfin", **kwargs)


class EmbyClient(MediaBrowserClient):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(platform="emby", **kwargs)
