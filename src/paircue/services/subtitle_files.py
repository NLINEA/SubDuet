from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import srt

from paircue.languages import language_matches
from paircue.services.atomic import atomic_write_text


class SubtitleLanguage(StrEnum):
    ENGLISH = "en"
    TRADITIONAL_CHINESE = "zh-TW"
    SIMPLIFIED_CHINESE = "zh-CN"
    GENERIC_CHINESE = "zh"


LANGUAGE_TAGS: dict[str, SubtitleLanguage] = {
    "en": SubtitleLanguage.ENGLISH,
    "eng": SubtitleLanguage.ENGLISH,
    "zh-tw": SubtitleLanguage.TRADITIONAL_CHINESE,
    "zht": SubtitleLanguage.TRADITIONAL_CHINESE,
    "cht": SubtitleLanguage.TRADITIONAL_CHINESE,
    "zh-hant": SubtitleLanguage.TRADITIONAL_CHINESE,
    "traditional": SubtitleLanguage.TRADITIONAL_CHINESE,
    "zh-cn": SubtitleLanguage.SIMPLIFIED_CHINESE,
    "zhs": SubtitleLanguage.SIMPLIFIED_CHINESE,
    "chs": SubtitleLanguage.SIMPLIFIED_CHINESE,
    "zh-hans": SubtitleLanguage.SIMPLIFIED_CHINESE,
    "simplified": SubtitleLanguage.SIMPLIFIED_CHINESE,
    "zh": SubtitleLanguage.GENERIC_CHINESE,
    "zho": SubtitleLanguage.GENERIC_CHINESE,
    "chi": SubtitleLanguage.GENERIC_CHINESE,
}

SUPPORTED_EXTENSIONS = {".srt"}
BILINGUAL_LANGUAGE_TAG = "mul"
MAX_SUBTITLE_BYTES = 16 * 1024 * 1024
MAX_SUBTITLE_CUES = 100_000
WHITESPACE = re.compile(r"[ \t]+")


@dataclass(frozen=True, slots=True)
class Sidecars:
    english: Path | None = None
    traditional_chinese: Path | None = None
    simplified_chinese: Path | None = None
    generic_chinese: Path | None = None
    bilingual: Path | None = None


@dataclass(frozen=True, slots=True)
class BilingualMergeResult:
    subtitles: list[srt.Subtitle]
    source_match_ratio: float
    target_match_ratio: float


def classify_sidecar(media_path: Path, subtitle_path: Path) -> SubtitleLanguage | None:
    if subtitle_path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        return None
    prefix = f"{media_path.stem}."
    if not subtitle_path.name.startswith(prefix):
        return None
    tag = subtitle_path.name[len(prefix) : -len(subtitle_path.suffix)].lower()
    if tag in {"zh-tw.cc", "bilingual"}:
        return None
    return LANGUAGE_TAGS.get(tag)


def discover_sidecars(media_path: Path) -> Sidecars:
    values: dict[SubtitleLanguage, Path] = {}
    bilingual: Path | None = None
    for candidate in media_path.parent.glob(f"{media_path.stem}.*.srt"):
        tag = candidate.name[len(media_path.stem) + 1 : -4].lower()
        if tag in {BILINGUAL_LANGUAGE_TAG, "zh-tw.cc", "bilingual"}:
            bilingual = candidate
            continue
        language = classify_sidecar(media_path, candidate)
        if language is not None:
            values.setdefault(language, candidate)
    bare = media_path.with_suffix(".srt")
    if bare.exists():
        values.setdefault(SubtitleLanguage.ENGLISH, bare)
    return Sidecars(
        english=values.get(SubtitleLanguage.ENGLISH),
        traditional_chinese=values.get(SubtitleLanguage.TRADITIONAL_CHINESE),
        simplified_chinese=values.get(SubtitleLanguage.SIMPLIFIED_CHINESE),
        generic_chinese=values.get(SubtitleLanguage.GENERIC_CHINESE),
        bilingual=bilingual,
    )


def find_language_sidecar(
    media_path: Path,
    language: str,
    *,
    bilingual: bool = False,
) -> Path | None:
    """Find a monolingual language track or the standard multiple-language track."""

    prefix_length = len(media_path.stem) + 1
    for candidate in media_path.parent.glob(f"{media_path.stem}.*.srt"):
        tag = candidate.name[prefix_length:-4]
        if bilingual:
            if tag.casefold() == BILINGUAL_LANGUAGE_TAG:
                return candidate
            continue
        if tag.casefold() == BILINGUAL_LANGUAGE_TAG or tag.casefold().endswith(".cc"):
            continue
        if language_matches(tag, language):
            return candidate
    return None


def sidecar_path(media_path: Path, language: str, *, bilingual: bool = False) -> Path:
    if bilingual:
        return media_path.parent / f"{media_path.stem}.{BILINGUAL_LANGUAGE_TAG}.srt"
    return media_path.parent / f"{media_path.stem}.{language}.srt"


def parse_srt(path: Path) -> list[srt.Subtitle]:
    if path.is_symlink():
        raise ValueError(f"subtitle file must not be a symbolic link: {path.name}")
    size = path.stat().st_size
    if size <= 0 or size > MAX_SUBTITLE_BYTES:
        raise ValueError(f"subtitle file size is outside the safe limit: {path.name}")
    content = path.read_text(encoding="utf-8-sig", errors="strict")
    subtitles = list(srt.parse(content, ignore_errors=False))
    if not subtitles:
        raise ValueError(f"subtitle file contains no valid cues: {path.name}")
    if len(subtitles) > MAX_SUBTITLE_CUES:
        raise ValueError(f"subtitle file contains too many cues: {path.name}")
    return subtitles


def clean_spoken_dialogue(subtitles: list[srt.Subtitle]) -> list[srt.Subtitle]:
    """Normalize whitespace, never guess which subtitle words are disposable.

    Parentheses, speaker labels, sound descriptions and lyrics can all carry meaning.
    Keep them available to translation and review, and leave the input cues untouched.
    """

    cleaned: list[srt.Subtitle] = []
    for cue in subtitles:
        lines = [WHITESPACE.sub(" ", line).strip() for line in cue.content.splitlines()]
        text = "\n".join(line for line in lines if line).strip()
        if not text:
            continue
        cleaned.append(
            srt.Subtitle(
                index=len(cleaned) + 1,
                start=cue.start,
                end=cue.end,
                content=text,
                proprietary=cue.proprietary,
            )
        )
    if not cleaned:
        raise ValueError("dialogue cleaning removed every subtitle cue")
    return cleaned


def write_srt(path: Path, subtitles: list[srt.Subtitle]) -> None:
    normalized = [
        srt.Subtitle(
            index=index,
            start=cue.start,
            end=cue.end,
            content=cue.content.strip(),
            proprietary=cue.proprietary,
        )
        for index, cue in enumerate(subtitles, start=1)
    ]
    atomic_write_text(path, srt.compose(normalized, reindex=False, strict=True))


def translated_subtitles(
    source: list[srt.Subtitle], translations: dict[int, str]
) -> list[srt.Subtitle]:
    expected = set(range(len(source)))
    if set(translations) != expected:
        missing = sorted(expected - set(translations))
        raise ValueError(f"translation coverage is incomplete; missing cue IDs: {missing[:10]}")
    return [
        srt.Subtitle(
            index=index + 1,
            start=cue.start,
            end=cue.end,
            content=translations[index].strip(),
        )
        for index, cue in enumerate(source)
    ]


def bilingual_subtitles(
    source: list[srt.Subtitle],
    translated: list[srt.Subtitle],
    *,
    order: str = "target-first",
) -> list[srt.Subtitle]:
    if order not in {"target-first", "source-first"}:
        raise ValueError("bilingual order must be target-first or source-first")
    if len(source) != len(translated):
        raise ValueError("source and translated subtitle counts differ")
    output: list[srt.Subtitle] = []
    for index, (source_cue, target_cue) in enumerate(zip(source, translated, strict=True), start=1):
        if source_cue.start != target_cue.start or source_cue.end != target_cue.end:
            raise ValueError("source and translated timings differ")
        lines = (
            (target_cue.content.strip(), source_cue.content.strip())
            if order == "target-first"
            else (source_cue.content.strip(), target_cue.content.strip())
        )
        output.append(
            srt.Subtitle(
                index=index,
                start=source_cue.start,
                end=source_cue.end,
                content="\n".join(lines),
            )
        )
    return output


def merge_bilingual_subtitles(
    source: list[srt.Subtitle],
    target: list[srt.Subtitle],
    *,
    order: str = "target-first",
    tolerance_ms: int = 350,
    min_match_ratio: float = 0.7,
) -> BilingualMergeResult:
    """Merge independently segmented language tracks using their synchronized timings."""

    if order not in {"target-first", "source-first"}:
        raise ValueError("bilingual order must be target-first or source-first")
    if not source or not target:
        raise ValueError("both subtitle tracks must contain cues")
    source = sorted(source, key=lambda cue: (cue.start, cue.end))
    target = sorted(target, key=lambda cue: (cue.start, cue.end))
    total = len(source) + len(target)
    parent = list(range(total))

    def find(node: int) -> int:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    tolerance_seconds = tolerance_ms / 1000
    matched_source: set[int] = set()
    matched_target: set[int] = set()
    target_start = 0
    for source_index, source_cue in enumerate(source):
        while (
            target_start < len(target)
            and target[target_start].end.total_seconds()
            < source_cue.start.total_seconds() - tolerance_seconds
        ):
            target_start += 1
        target_index = target_start
        while (
            target_index < len(target)
            and target[target_index].start.total_seconds()
            <= source_cue.end.total_seconds() + tolerance_seconds
        ):
            if _timings_match(source_cue, target[target_index], tolerance_seconds):
                union(source_index, len(source) + target_index)
                matched_source.add(source_index)
                matched_target.add(target_index)
            target_index += 1

    source_ratio = len(matched_source) / len(source)
    target_ratio = len(matched_target) / len(target)
    if min(source_ratio, target_ratio) < min_match_ratio:
        raise ValueError(
            "subtitle timing match is too low "
            f"(source={source_ratio:.0%}, target={target_ratio:.0%})"
        )

    components: dict[int, tuple[list[srt.Subtitle], list[srt.Subtitle]]] = {}
    for index, cue in enumerate(source):
        source_cues, _ = components.setdefault(find(index), ([], []))
        source_cues.append(cue)
    for index, cue in enumerate(target):
        _, target_cues = components.setdefault(find(len(source) + index), ([], []))
        target_cues.append(cue)

    groups = sorted(
        components.values(),
        key=lambda pair: min(cue.start for cues in pair for cue in cues),
    )
    output: list[srt.Subtitle] = []
    for index, (source_cues, target_cues) in enumerate(groups, start=1):
        all_cues = [*source_cues, *target_cues]
        source_text = _join_unique_cues(source_cues)
        target_text = _join_unique_cues(target_cues)
        text_blocks = (
            (target_text, source_text) if order == "target-first" else (source_text, target_text)
        )
        output.append(
            srt.Subtitle(
                index=index,
                start=min(cue.start for cue in all_cues),
                end=max(cue.end for cue in all_cues),
                content="\n".join(text for text in text_blocks if text),
            )
        )
    return BilingualMergeResult(output, source_ratio, target_ratio)


def _timings_match(
    source: srt.Subtitle,
    target: srt.Subtitle,
    tolerance_seconds: float,
) -> bool:
    start = max(source.start, target.start).total_seconds()
    end = min(source.end, target.end).total_seconds()
    overlap = end - start
    source_duration = max((source.end - source.start).total_seconds(), 0.001)
    target_duration = max((target.end - target.start).total_seconds(), 0.001)
    if overlap > 0:
        return bool(overlap / min(source_duration, target_duration) >= 0.25)
    start_delta = abs((source.start - target.start).total_seconds())
    end_delta = abs((source.end - target.end).total_seconds())
    return bool(start_delta <= tolerance_seconds and end_delta <= tolerance_seconds)


def _join_unique_cues(cues: list[srt.Subtitle]) -> str:
    lines: list[str] = []
    seen: set[str] = set()
    for cue in cues:
        text = cue.content.strip()
        if text and text not in seen:
            lines.append(text)
            seen.add(text)
    return "\n".join(lines)
