from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from paircue.services.review import ReviewDetails

MediaType = Literal["movie", "episode"]


@dataclass(frozen=True, slots=True)
class MediaItem:
    item_id: str
    media_type: MediaType
    path: Path
    title: str
    year: int | None = None
    show_title: str = ""
    season: int | None = None
    episode: int | None = None
    library_key: str = ""

    @property
    def context_label(self) -> str:
        if self.media_type == "episode":
            show = self.show_title or self.title
            return f"{show} S{self.season or 0:02d}E{self.episode or 0:02d}"
        return f"{self.title} ({self.year})" if self.year else self.title

    @property
    def queue_key(self) -> str:
        return str(self.path.resolve(strict=False))

    @property
    def rating_key(self) -> str:
        """Backward-compatible Plex name for the platform-neutral item id."""
        return self.item_id


@dataclass(frozen=True, slots=True)
class ProcessResult:
    status: Literal["skipped", "completed", "failed", "running", "retry_wait",
                    "retry_exhausted", "blocked"]
    message: str
    outputs: tuple[Path, ...] = ()
    review_details: ReviewDetails | None = None
