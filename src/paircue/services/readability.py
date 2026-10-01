from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ReadabilityProfile(BaseModel):
    """Configurable draft-review thresholds, not language or playback correctness."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    source_line_codepoints_max: int = Field(default=42, ge=1, strict=True)
    target_line_codepoints_max: int = Field(default=42, ge=1, strict=True)
    lines_per_language_max: int = Field(default=2, ge=1, strict=True)
    total_lines_max: int = Field(default=4, ge=1, strict=True)
    duration_ms_max: int = Field(default=7000, ge=1, strict=True)


ReadabilityMetric = Literal[
    "source_line_codepoints", "target_line_codepoints", "source_lines",
    "target_lines", "total_lines", "duration_ms",
]


@dataclass(frozen=True, slots=True)
class ReadabilityIssue:
    output_cue: int
    metric: ReadabilityMetric
    measured: int
    limit: int

    @property
    def description(self) -> str:
        label, unit = {
            "source_line_codepoints": ("source line length", "codepoints"),
            "target_line_codepoints": ("target line length", "codepoints"),
            "source_lines": ("source lines", "lines"),
            "target_lines": ("target lines", "lines"),
            "total_lines": ("total lines", "lines"),
            "duration_ms": ("duration", "ms"),
        }[self.metric]
        return (
            f"output SRT cue {self.output_cue}: {label} "
            f"{self.measured} {unit} exceeds {self.limit} {unit}"
        )


def review_readability(
    output_cue: int, source_text: str, target_text: str, duration_ms: int,
    profile: ReadabilityProfile,
) -> tuple[ReadabilityIssue, ...]:
    """Measure the emitted language blocks without rewriting or guessing their alignment."""

    source_lines = source_text.splitlines()
    target_lines = target_text.splitlines()
    measurements: tuple[tuple[ReadabilityMetric, int, int], ...] = (
        ("source_line_codepoints", max(map(len, source_lines), default=0),
         profile.source_line_codepoints_max),
        ("target_line_codepoints", max(map(len, target_lines), default=0),
         profile.target_line_codepoints_max),
        ("source_lines", len(source_lines), profile.lines_per_language_max),
        ("target_lines", len(target_lines), profile.lines_per_language_max),
        ("total_lines", len(source_lines) + len(target_lines), profile.total_lines_max),
        ("duration_ms", duration_ms, profile.duration_ms_max),
    )
    return tuple(
        ReadabilityIssue(output_cue, metric, measured, limit)
        for metric, measured, limit in measurements if measured > limit
    )


def readability_review_notice(issues: tuple[ReadabilityIssue, ...]) -> str:
    if not issues:
        return ""
    # Reserve a separate, fixed budget for this category even with long pairing lists.
    details = "; ".join(issue.description for issue in issues[:3])[:300]
    if len(issues) > 3:
        details += f"; {len(issues) - 3} additional readability issues"
    return (
        f"Readability review needed (proposed profile): {len(issues)} issues; {details}. "
        "Text and timings are unchanged."
    )
