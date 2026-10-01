from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from paircue.services.readability import ReadabilityIssue


@dataclass(frozen=True, slots=True)
class ReviewEntry:
    category: Literal["source_only", "target_only", "readability"]
    output_cue: int
    metric: str = ""
    measured: int | None = None
    limit: int | None = None


@dataclass(frozen=True, slots=True)
class ReviewDetails:
    """Cue diagnostics only: no subtitle text, paths, or provider data."""

    source_only: tuple[int, ...] = ()
    target_only: tuple[int, ...] = ()
    readability: tuple[ReadabilityIssue, ...] = ()

    def entries(self) -> tuple[ReviewEntry, ...]:
        return (
            *(ReviewEntry("source_only", cue) for cue in self.source_only),
            *(ReviewEntry("target_only", cue) for cue in self.target_only),
            *(ReviewEntry("readability", issue.output_cue, issue.metric,
                          issue.measured, issue.limit) for issue in self.readability),
        )


@dataclass(frozen=True, slots=True)
class ReviewPage:
    counts: dict[str, int]
    total: int
    offset: int
    entries: tuple[ReviewEntry, ...]
