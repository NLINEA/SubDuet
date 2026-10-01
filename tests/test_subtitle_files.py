from datetime import timedelta
from pathlib import Path

import pytest
import srt

from paircue.services.readability import ReadabilityProfile
from paircue.services.subtitle_files import (
    SubtitleLanguage,
    bilingual_subtitles,
    clean_spoken_dialogue,
    discover_sidecars,
    find_language_sidecar,
    merge_bilingual_subtitles,
    parse_srt,
    sidecar_path,
    translated_subtitles,
)


def test_simplified_is_not_misclassified_as_traditional(tmp_path: Path) -> None:
    media = tmp_path / "Movie.mkv"
    media.write_bytes(b"video")
    (tmp_path / "Movie.zh-CN.srt").write_text("x", encoding="utf-8")

    sidecars = discover_sidecars(media)

    assert sidecars.simplified_chinese == tmp_path / "Movie.zh-CN.srt"
    assert sidecars.traditional_chinese is None


def test_spoken_dialogue_cleanup_keeps_alignment() -> None:
    cues = [
        srt.Subtitle(1, timedelta(seconds=0), timedelta(seconds=1), "♪ theme song ♪"),
        srt.Subtitle(
            2,
            timedelta(seconds=1),
            timedelta(seconds=2),
            "[NARRATOR]: Hello [door slams] who is there?",
        ),
    ]

    cleaned = clean_spoken_dialogue(cues)

    assert len(cleaned) == 2
    assert cleaned[0].index == 1
    assert cleaned[0].content == "♪ theme song ♪"
    assert cleaned[1].content == "[NARRATOR]: Hello [door slams] who is there?"
    assert cleaned[1].start == timedelta(seconds=1)


@pytest.mark.parametrize("text", [
    "The answer is (not) yes.", "答案是（不）可以。", "[不]可以。",
    "♪ Don't leave me ♪\nWait!", "【旁白】：佢未返。", "[door slams]",
    "(I said no.)", "No [emphasis] means no.",
])
def test_dialogue_cleanup_preserves_meaning_in_every_script(text: str) -> None:
    cue = srt.Subtitle(7, timedelta(seconds=1), timedelta(seconds=2), text)
    cleaned = clean_spoken_dialogue([cue])
    assert cleaned[0].content == text
    assert (cleaned[0].start, cleaned[0].end) == (cue.start, cue.end)
    assert cue.index == 7
    assert cue.content == text


def test_translation_and_bilingual_require_exact_coverage() -> None:
    source = [srt.Subtitle(1, timedelta(0), timedelta(seconds=1), "Hello")]
    chinese = translated_subtitles(source, {0: "你好"})
    bilingual = bilingual_subtitles(source, chinese)

    assert chinese[0].content == "你好"
    assert bilingual[0].content == "你好\nHello"

    source_first = bilingual_subtitles(source, chinese, order="source-first")
    assert source_first[0].content == "Hello\n你好"


def test_language_enum_uses_plex_sidecar_names() -> None:
    assert SubtitleLanguage.TRADITIONAL_CHINESE.value == "zh-TW"
    assert SubtitleLanguage.SIMPLIFIED_CHINESE.value == "zh-CN"


def test_custom_language_sidecar_matches_common_three_letter_tag(tmp_path: Path) -> None:
    media = tmp_path / "Movie.mkv"
    media.write_bytes(b"video")
    japanese = tmp_path / "Movie.jpn.srt"
    japanese.write_text("x", encoding="utf-8")

    assert find_language_sidecar(media, "ja") == japanese


def test_subtitle_parser_refuses_symbolic_links(tmp_path: Path) -> None:
    private_text = tmp_path / "private.txt"
    private_text.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nprivate\n\n",
        encoding="utf-8",
    )
    subtitle = tmp_path / "Movie.en.srt"
    subtitle.symlink_to(private_text)

    with pytest.raises(ValueError, match="symbolic link"):
        parse_srt(subtitle)


def test_subtitle_parser_refuses_oversized_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subtitle = tmp_path / "Movie.en.srt"
    subtitle.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nHello\n\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("paircue.services.subtitle_files.MAX_SUBTITLE_BYTES", 10)

    with pytest.raises(ValueError, match="safe limit"):
        parse_srt(subtitle)


def test_bilingual_sidecar_uses_standard_multiple_languages_tag(tmp_path: Path) -> None:
    media = tmp_path / "Movie.mkv"
    media.write_bytes(b"video")
    bilingual = tmp_path / "Movie.mul.srt"
    bilingual.write_text("x", encoding="utf-8")
    misleading_cc = tmp_path / "Movie.en.cc.srt"
    misleading_cc.write_text("x", encoding="utf-8")

    assert sidecar_path(media, "en", bilingual=True) == bilingual
    assert find_language_sidecar(media, "en", bilingual=True) == bilingual
    assert find_language_sidecar(media, "en") is None


def test_time_based_merge_handles_one_to_many_segmentation() -> None:
    source = [srt.Subtitle(1, timedelta(0), timedelta(seconds=2), "Hello world")]
    target = [
        srt.Subtitle(1, timedelta(0), timedelta(seconds=1), "你好"),
        srt.Subtitle(2, timedelta(seconds=1), timedelta(seconds=2), "世界"),
    ]

    merged = merge_bilingual_subtitles(source, target)

    assert merged.source_match_ratio == 1
    assert merged.target_match_ratio == 1
    assert merged.fully_paired
    assert merged.unmatched_source_cues == merged.unmatched_target_cues == ()
    assert merged.review_notice == ""
    assert len(merged.subtitles) == 1
    assert merged.subtitles[0].content == "你好\n世界\nHello world"
    assert merged.subtitles[0].start == timedelta(0)
    assert merged.subtitles[0].end == timedelta(seconds=2)


def test_readability_review_preserves_fully_paired_text_timing_and_order() -> None:
    source = [srt.Subtitle(1, timedelta(seconds=1), timedelta(seconds=6),
                           "Keep the silver box closed until the bell rings.")]
    target = [srt.Subtitle(1, timedelta(seconds=1), timedelta(seconds=6), "先不要打開銀色盒子。")]
    result = merge_bilingual_subtitles(source, target, order="source-first")

    assert result.fully_paired and result.needs_review
    assert result.unmatched_source_cues == result.unmatched_target_cues == ()
    assert result.subtitles[0].content == source[0].content + "\n" + target[0].content
    assert (result.subtitles[0].start, result.subtitles[0].end) == (
        source[0].start, source[0].end,
    )
    assert [(i.metric, i.measured, i.limit) for i in result.readability_issues] == [
        ("source_line_codepoints", 48, 42),
    ]
    assert "partial" not in result.review_notice
    assert "output SRT cue 1: source line length 48 codepoints exceeds 42" in result.review_notice

    relaxed = merge_bilingual_subtitles(
        source, target, order="source-first",
        readability_profile=ReadabilityProfile(source_line_codepoints_max=48),
    )
    assert not relaxed.needs_review and relaxed.review_notice == ""
    assert relaxed.subtitles == result.subtitles


def test_readability_review_reports_combined_component_limits_without_splitting() -> None:
    source = [srt.Subtitle(1, timedelta(seconds=1), timedelta(seconds=17.5),
                           "One\nTwo\nThree\nFour\nFive")]
    target = [srt.Subtitle(1, timedelta(seconds=1), timedelta(seconds=17.5),
                           "甲\n乙\n丙")]
    result = merge_bilingual_subtitles(source, target)

    assert result.fully_paired and result.needs_review
    assert len(result.subtitles) == 1
    assert result.subtitles[0].content == target[0].content + "\n" + source[0].content
    assert (result.subtitles[0].start, result.subtitles[0].end) == (
        source[0].start, source[0].end,
    )
    assert [(i.metric, i.measured, i.limit) for i in result.readability_issues] == [
        ("source_lines", 5, 2), ("target_lines", 3, 2),
        ("total_lines", 8, 4), ("duration_ms", 16500, 7000),
    ]


def test_partial_and_readability_reviews_remain_distinct() -> None:
    source = [
        srt.Subtitle(i + 1, timedelta(seconds=i * 4), timedelta(seconds=i * 4 + 2),
                     "A deliberately long source sentence for review." if i == 3 else f"Source {i}")
        for i in range(4)
    ]
    target = [srt.Subtitle(i + 1, c.start, c.end, f"Target {i}") for i, c in enumerate(source[:3])]
    result = merge_bilingual_subtitles(source, target)

    assert not result.fully_paired and result.needs_review
    assert result.unmatched_source_cues == (4,)
    assert result.readability_issues[0].output_cue == 4
    assert "partial timing pairing" in result.review_notice
    assert "Readability review needed (proposed profile)" in result.review_notice
    assert result.subtitles[-1].content == source[-1].content


def test_readability_limits_are_inclusive_and_language_roles_ignore_display_order() -> None:
    source = [srt.Subtitle(1, timedelta(0), timedelta(seconds=7), "A" * 42)]
    target = [srt.Subtitle(1, timedelta(0), timedelta(seconds=7), "字" * 22)]
    profile = ReadabilityProfile(target_line_codepoints_max=22)
    clean = merge_bilingual_subtitles(source, target, readability_profile=profile)
    assert clean.fully_paired and not clean.needs_review
    target[0].content += "。"
    for order in ["target-first", "source-first"]:
        result = merge_bilingual_subtitles(source, target, order=order, readability_profile=profile)
        assert [(i.metric, i.measured, i.limit) for i in result.readability_issues] == [
            ("target_line_codepoints", 23, 22),
        ]


def test_time_based_merge_rejects_unrelated_timelines() -> None:
    source = [srt.Subtitle(1, timedelta(0), timedelta(seconds=1), "Hello")]
    target = [srt.Subtitle(1, timedelta(seconds=10), timedelta(seconds=11), "你好")]

    with pytest.raises(ValueError, match="timing match is too low"):
        merge_bilingual_subtitles(source, target)


def test_partial_merge_identifies_output_cues_and_retains_both_original_texts() -> None:
    source = [
        srt.Subtitle(100 + i, timedelta(seconds=i * 4), timedelta(seconds=i * 4 + 2),
                     f"English {i}")
        for i in range(4)
    ]
    target = [
        srt.Subtitle(200 + i, timedelta(seconds=i * 4), timedelta(seconds=i * 4 + 2),
                     f"中文 {i}")
        for i in range(4)
    ]
    target[-1].start += timedelta(seconds=60)
    target[-1].end += timedelta(seconds=60)

    result = merge_bilingual_subtitles(source, target)

    assert not result.fully_paired
    assert result.source_match_ratio == result.target_match_ratio == 0.75
    assert result.unmatched_source_cues == (4,)
    assert result.unmatched_target_cues == (5,)
    assert result.subtitles[3].content == source[-1].content
    assert result.subtitles[4].content == target[-1].content
    assert (result.subtitles[3].start, result.subtitles[3].end) == (
        source[-1].start, source[-1].end,
    )
    assert (result.subtitles[4].start, result.subtitles[4].end) == (
        target[-1].start, target[-1].end,
    )
