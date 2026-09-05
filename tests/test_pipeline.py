from pathlib import Path

import srt

from paircue.models import MediaItem
from paircue.services.glossary import GlossaryStore
from paircue.services.pipeline import SubtitlePipeline
from paircue.services.state import StateStore


class NoopExtractor:
    def extract(self, media_path: Path, languages: set[str] | None = None) -> tuple[Path, ...]:
        return ()


class NoopDownloader:
    def download(self, item: MediaItem, languages: set[str]) -> tuple[Path, ...]:
        return ()

    def close(self) -> None:
        return


class TargetDownloader(NoopDownloader):
    def __init__(self) -> None:
        self.requests: list[set[str]] = []

    def download(self, item: MediaItem, languages: set[str]) -> tuple[Path, ...]:
        self.requests.append(languages)
        if "ja" not in languages:
            return ()
        media_path = item.path
        output = media_path.parent / f"{media_path.stem}.ja.srt"
        output.write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nこんにちは\n\n",
            encoding="utf-8",
        )
        return (output,)


class DualTrackDownloader(NoopDownloader):
    def __init__(self) -> None:
        self.requests: list[set[str]] = []

    def download(self, item: MediaItem, languages: set[str]) -> tuple[Path, ...]:
        self.requests.append(languages)
        outputs: list[Path] = []
        for language, text in (("ja", "こんにちは"), ("en", "Hello")):
            if language not in languages:
                continue
            output = item.path.parent / f"{item.path.stem}.{language}.srt"
            output.write_text(
                f"1\n00:00:00,000 --> 00:00:01,000\n{text}\n\n",
                encoding="utf-8",
            )
            outputs.append(output)
        return tuple(outputs)


class FullTranslator:
    def translate_all(
        self, subtitles: list[object], *, context: str, glossary: dict[str, str]
    ) -> dict[int, str]:
        return {index: f"翻譯 {index}" for index in range(len(subtitles))}


class PartialTranslator(FullTranslator):
    def translate_all(
        self, subtitles: list[object], *, context: str, glossary: dict[str, str]
    ) -> dict[int, str]:
        return {}


class FailingTranslator(FullTranslator):
    def translate_all(
        self, subtitles: list[object], *, context: str, glossary: dict[str, str]
    ) -> dict[int, str]:
        raise AssertionError("existing subtitle tracks should be merged without AI")


class RecordingSynchronizer:
    def __init__(self) -> None:
        self.paths: list[Path] = []

    def sync(self, media_path: Path, subtitle_path: Path) -> bool:
        self.paths.append(subtitle_path)
        return True


class MutatingSynchronizer(RecordingSynchronizer):
    def sync(self, media_path: Path, subtitle_path: Path) -> bool:
        super().sync(media_path, subtitle_path)
        text = subtitle_path.read_text(encoding="utf-8")
        subtitle_path.write_text(text.replace("00:00:00,", "00:00:00,"), encoding="utf-8")
        return True


class GeneratingTranscriber:
    def __init__(self) -> None:
        self.calls: list[tuple[Path, str]] = []

    def transcribe(self, media_path: Path, output_path: Path, language: str) -> Path:
        self.calls.append((media_path, language))
        output_path.write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nGenerated source\n\n",
            encoding="utf-8",
        )
        return output_path

    def close(self) -> None:
        return


def _pipeline(
    tmp_path: Path,
    translator: object,
    *,
    source_language: str = "en",
    target_language: str = "zh-TW",
    bilingual_order: str = "target-first",
    downloader: object | None = None,
    synchronizer: object | None = None,
    transcriber: object | None = None,
    clean_source_output: bool = False,
) -> SubtitlePipeline:
    return SubtitlePipeline(
        media_root=tmp_path,
        state=StateStore(tmp_path / "state" / "paircue.sqlite3"),
        downloader=downloader or NoopDownloader(),  # type: ignore[arg-type]
        extractor=NoopExtractor(),  # type: ignore[arg-type]
        synchronizer=synchronizer,  # type: ignore[arg-type]
        transcriber=transcriber,  # type: ignore[arg-type]
        translator=translator,  # type: ignore[arg-type]
        glossary=GlossaryStore(tmp_path / "state" / "glossaries"),
        clean_source_output=clean_source_output,
        source_language=source_language,
        target_language=target_language,
        bilingual_order=bilingual_order,
    )


def _media(tmp_path: Path) -> MediaItem:
    media = tmp_path / "Movie.mkv"
    media.write_bytes(b"fake media")
    (tmp_path / "Movie.en.srt").write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nHello\n\n2\n00:00:01,000 --> 00:00:02,000\nWorld\n\n",
        encoding="utf-8",
    )
    return MediaItem("1", "movie", media, "Movie")


def _media_with_source(tmp_path: Path, language: str, text: str) -> MediaItem:
    media = tmp_path / "Lesson.mkv"
    media.write_bytes(b"fake media")
    (tmp_path / f"Lesson.{language}.srt").write_text(
        f"1\n00:00:00,000 --> 00:00:01,000\n{text}\n\n",
        encoding="utf-8",
    )
    return MediaItem("2", "movie", media, "Lesson")


def test_pipeline_writes_complete_atomic_outputs(tmp_path: Path) -> None:
    result = _pipeline(tmp_path, FullTranslator()).process(_media(tmp_path))

    assert result.status == "completed"
    assert (tmp_path / "Movie.zh-TW.srt").exists()
    assert (tmp_path / "Movie.mul.srt").exists()
    assert not list(tmp_path.glob(".*.tmp"))


def test_pipeline_preserves_meaning_before_ai_and_in_bilingual_output(tmp_path: Path) -> None:
    original = "ANA: I will (not) go. [Really.]\n♪ Stay with me ♪"

    class RecordingTranslator:
        def translate_all(
            self, subtitles: list[srt.Subtitle], *, context: str, glossary: dict[str, str],
        ) -> dict[int, str]:
            assert len(subtitles) == 1
            assert subtitles[0].content == original
            return {0: "Synthetic translation"}

    item = _media_with_source(tmp_path, "en", original)
    source_path = tmp_path / "Lesson.en.srt"
    source_bytes = source_path.read_bytes()
    result = _pipeline(tmp_path, RecordingTranslator()).process(item)
    assert result.status == "completed"
    assert original in (tmp_path / "Lesson.mul.srt").read_text(encoding="utf-8")
    assert source_path.read_bytes() == source_bytes


def test_pipeline_does_not_publish_partial_translation(tmp_path: Path) -> None:
    result = _pipeline(tmp_path, PartialTranslator()).process(_media(tmp_path))

    assert result.status == "failed"
    assert not (tmp_path / "Movie.zh-TW.srt").exists()
    assert not (tmp_path / "Movie.mul.srt").exists()


def test_target_only_file_does_not_count_as_complete_bilingual_output(tmp_path: Path) -> None:
    item = _media(tmp_path)
    (tmp_path / "Movie.zh-TW.srt").write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n舊\n\n"
        "2\n00:00:01,000 --> 00:00:02,000\n字幕\n\n",
        encoding="utf-8",
    )

    result = _pipeline(tmp_path, FullTranslator()).process(item)

    assert result.status == "completed"
    assert (tmp_path / "Movie.mul.srt").exists()


def test_pipeline_uses_custom_target_language_in_output_names(tmp_path: Path) -> None:
    result = _pipeline(tmp_path, FullTranslator(), target_language="ja").process(_media(tmp_path))

    assert result.status == "completed"
    assert "to ja" in result.message
    assert (tmp_path / "Movie.ja.srt").exists()
    assert (tmp_path / "Movie.mul.srt").exists()
    assert not (tmp_path / "Movie.zh-TW.srt").exists()


def test_download_only_mode_requests_the_custom_target(tmp_path: Path) -> None:
    downloader = TargetDownloader()
    result = _pipeline(
        tmp_path,
        None,
        target_language="ja",
        downloader=downloader,
    ).process(_media(tmp_path))

    assert result.status == "completed"
    assert downloader.requests == [{"en", "ja"}]
    assert result.outputs == (tmp_path / "Movie.ja.srt",)


def test_download_only_mode_fetches_and_merges_both_languages(tmp_path: Path) -> None:
    media = tmp_path / "Lesson.mkv"
    media.write_bytes(b"fake media")
    item = MediaItem("5", "movie", media, "Lesson")
    downloader = DualTrackDownloader()

    result = _pipeline(
        tmp_path,
        None,
        source_language="ja",
        target_language="en",
        downloader=downloader,
    ).process(item)

    assert result.status == "completed"
    assert result.message == "merged existing subtitle tracks (100%/100% matched)"
    assert downloader.requests == [{"en", "ja"}]
    assert "Hello\nこんにちは" in (tmp_path / "Lesson.mul.srt").read_text()


def test_pipeline_aligns_any_source_language_and_writes_learning_pair(tmp_path: Path) -> None:
    synchronizer = RecordingSynchronizer()
    item = _media_with_source(tmp_path, "ja", "こんにちは")

    result = _pipeline(
        tmp_path,
        FullTranslator(),
        source_language="ja",
        target_language="en",
        bilingual_order="source-first",
        synchronizer=synchronizer,
    ).process(item)

    assert result.status == "completed"
    assert result.message == "aligned and translated 1 cues from ja to en"
    assert [path.name for path in synchronizer.paths] == ["Lesson.ja.srt"]
    assert all(path != tmp_path / "Lesson.ja.srt" for path in synchronizer.paths)
    assert (tmp_path / "Lesson.en.srt").exists()
    bilingual = (tmp_path / "Lesson.mul.srt").read_text(encoding="utf-8")
    assert "こんにちは\n翻譯 0" in bilingual


def test_pipeline_generates_missing_source_then_builds_bilingual_track(tmp_path: Path) -> None:
    media = tmp_path / "New Movie.mkv"
    media.write_bytes(b"fake media")
    item = MediaItem("3", "movie", media, "New Movie")
    transcriber = GeneratingTranscriber()
    synchronizer = RecordingSynchronizer()

    result = _pipeline(
        tmp_path,
        FullTranslator(),
        transcriber=transcriber,
        synchronizer=synchronizer,
    ).process(item)

    assert result.status == "completed"
    assert result.message == "generated and translated 1 cues from en to zh-TW"
    assert transcriber.calls == [(media, "en")]
    assert synchronizer.paths == []
    assert (tmp_path / "New Movie.mul.srt").exists()


def test_pipeline_merges_two_existing_languages_without_ai(tmp_path: Path) -> None:
    item = _media_with_source(tmp_path, "ja", "こんにちは")
    (tmp_path / "Lesson.en.srt").write_text(
        "1\n00:00:00,050 --> 00:00:01,050\nHello\n\n",
        encoding="utf-8",
    )
    synchronizer = RecordingSynchronizer()

    result = _pipeline(
        tmp_path,
        FailingTranslator(),
        source_language="ja",
        target_language="en",
        synchronizer=synchronizer,
    ).process(item)

    assert result.status == "completed"
    assert result.message == "merged existing subtitle tracks (100%/100% matched)"
    assert [path.name for path in synchronizer.paths] == ["Lesson.ja.srt", "Lesson.en.srt"]
    assert all(path.parent != tmp_path for path in synchronizer.paths)
    bilingual = (tmp_path / "Lesson.mul.srt").read_text(encoding="utf-8")
    assert "Hello\nこんにちは" in bilingual


def test_pipeline_preserves_existing_subtitles_byte_for_byte_by_default(tmp_path: Path) -> None:
    item = _media_with_source(tmp_path, "ja", "こんにちは")
    target = tmp_path / "Lesson.en.srt"
    target.write_bytes(b"1\r\n00:00:00,050 --> 00:00:01,050\r\nHello\r\n\r\n")
    source = tmp_path / "Lesson.ja.srt"
    source_before = source.read_bytes()
    target_before = target.read_bytes()

    result = _pipeline(
        tmp_path,
        FailingTranslator(),
        source_language="ja",
        target_language="en",
        synchronizer=MutatingSynchronizer(),
    ).process(item)

    assert result.status == "completed"
    assert source.read_bytes() == source_before
    assert target.read_bytes() == target_before


def test_pipeline_never_replaces_existing_target_after_low_confidence_merge(
    tmp_path: Path,
) -> None:
    item = _media_with_source(tmp_path, "ja", "こんにちは")
    source = tmp_path / "Lesson.ja.srt"
    target = tmp_path / "Lesson.en.srt"
    target.write_text(
        "1\n00:01:00,000 --> 00:01:01,000\nExisting translation\n\n",
        encoding="utf-8",
    )
    source_before = source.read_bytes()
    target_before = target.read_bytes()

    result = _pipeline(
        tmp_path,
        FullTranslator(),
        source_language="ja",
        target_language="en",
    ).process(item)

    assert result.status == "failed"
    assert "kept both existing subtitle tracks unchanged" in result.message
    assert source.read_bytes() == source_before
    assert target.read_bytes() == target_before
    assert not (tmp_path / "Lesson.mul.srt").exists()
