from __future__ import annotations

import logging
import os
import shutil
import tempfile
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import replace
from functools import partial
from pathlib import Path

import srt
from opencc import OpenCC

from paircue.languages import opencc_profile
from paircue.models import MediaItem, ProcessResult
from paircue.services.atomic import atomic_write_bytes, prepare_write_bytes
from paircue.services.downloader import SubtitleDownloader
from paircue.services.glossary import GlossaryStore
from paircue.services.locks import KeyedLockPool
from paircue.services.media_tools import (
    EmbeddedSubtitleExtractor,
    SubtitleSynchronizer,
    ensure_media_path,
)
from paircue.services.publication import OutputProof, PublicationProof
from paircue.services.readability import ReadabilityProfile
from paircue.services.retry import (
    RECIPE_VERSION,
    VALIDATION_VERSION,
    AttemptKind,
    JobInputs,
    RetryLedger,
    RetryPolicy,
    StaleClaimError,
    canonical_hash,
    content_hash,
    input_tracks,
    milliseconds,
)
from paircue.services.state import StateStore, media_fingerprint
from paircue.services.subtitle_files import (
    Sidecars,
    bilingual_subtitles,
    clean_spoken_dialogue,
    discover_sidecars,
    find_language_sidecar,
    merge_bilingual_subtitles,
    parse_srt,
    sidecar_path,
    translated_subtitles,
    write_srt,
)
from paircue.services.transcriber import Transcriber
from paircue.services.translator import CompleteTranslator

log = logging.getLogger(__name__)

TRADITIONAL_CHINESE_TARGETS = {"zh-TW", "zh-HK", "zh-Hant"}
SIMPLIFIED_CHINESE_TARGETS = {"zh-CN", "zh-Hans"}


class SubtitlePipeline:
    def __init__(
        self,
        *,
        media_root: Path,
        state: StateStore,
        downloader: SubtitleDownloader,
        extractor: EmbeddedSubtitleExtractor,
        synchronizer: SubtitleSynchronizer | None,
        translator: CompleteTranslator | None,
        glossary: GlossaryStore,
        transcriber: Transcriber | None = None,
        clean_source_output: bool = False,
        source_language: str = "en",
        target_language: str = "zh-TW",
        bilingual_order: str = "target-first",
        bilingual_merge_tolerance_ms: int = 350,
        bilingual_merge_min_match_ratio: float = 0.7,
        readability_profile: ReadabilityProfile | None = None,
        retry_policy: RetryPolicy | None = None,
        retry_settings: dict[str, object] | None = None,
        retry_clock: Callable[[], int] = milliseconds,
    ) -> None:
        self.media_root = media_root
        self.state = state
        self.downloader = downloader
        self.extractor = extractor
        self.synchronizer = synchronizer
        self.transcriber = transcriber
        self.translator = translator
        self.glossary = glossary
        self.clean_source_output = clean_source_output
        self.source_language = source_language
        self.target_language = target_language
        self.bilingual_order = bilingual_order
        self.bilingual_merge_tolerance_ms = bilingual_merge_tolerance_ms
        self.bilingual_merge_min_match_ratio = bilingual_merge_min_match_ratio
        self.readability_profile = readability_profile
        self.retry_policy = retry_policy or RetryPolicy()
        self.retry_settings = retry_settings or {}
        self.retry_ledger = RetryLedger(state, retry_clock)
        self.locks = KeyedLockPool()
        profile = opencc_profile(target_language)
        self._target_converter = OpenCC(profile) if profile is not None else None

    def close(self) -> None:
        self.downloader.close()
        if self.transcriber is not None:
            self.transcriber.close()

    def process(self, item: MediaItem, *, retry_mode: AttemptKind | None = None) -> ProcessResult:
        try:
            media_path = ensure_media_path(item.path, self.media_root)
            fingerprint = media_fingerprint(media_path)
        except Exception as exc:
            return ProcessResult("failed", f"{type(exc).__name__}: {exc}")
        with self.locks.acquire(str(media_path)):
            if retry_mode is not None:
                return self._process_persistent(item, media_path, retry_mode)
            self.state.record(media_path, fingerprint, "processing", item.context_label)
            try:
                result = self._process_locked(item, media_path)
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                self.state.record(media_path, fingerprint, "failed", message)
                log.exception("subtitle processing failed for %s", item.context_label)
                return ProcessResult("failed", message)
            self.state.record(media_path, fingerprint, result.status, result.message,
                              review_details=result.review_details)
            return result

    def _job_inputs(self, item: MediaItem, media_path: Path) -> JobInputs:
        settings = {
            **self.retry_settings,
            "source_language": self.source_language, "target_language": self.target_language,
            "bilingual_order": self.bilingual_order,
            "clean_source_output": self.clean_source_output,
            "tolerance_ms": self.bilingual_merge_tolerance_ms,
            "minimum_match_ratio": self.bilingual_merge_min_match_ratio,
            "readability": (self.readability_profile or ReadabilityProfile()).model_dump(),
            "context": item.context_label,
            "glossary": canonical_hash(self.glossary.load(item.show_title or item.title)),
        }
        inputs = JobInputs(content_hash(media_path), input_tracks(media_path),
                           canonical_hash(settings))
        return self.retry_ledger.normalize_inputs(inputs)

    def _process_persistent(
        self, item: MediaItem, media_path: Path, kind: AttemptKind,
    ) -> ProcessResult:
        token = self.retry_ledger.input_state_token(media_path)
        try:
            inputs = self._job_inputs(item, media_path)
            tracks = input_tracks(media_path)
        except (OSError, ValueError) as exc:
            return self.retry_ledger.hold_input_error(media_path, token, str(exc))
        recovered = self.retry_ledger.recover_publication(
            media_path, inputs, tracks, kind,
            lambda: self._job_inputs(item, media_path) == inputs
            and input_tracks(media_path) == tracks,
        )
        if recovered is not None:
            return recovered
        pending = self.retry_ledger.pending_publication(media_path)
        if pending is not None:
            return pending
        if find_language_sidecar(media_path, self.target_language, bilingual=True) is not None:
            return self.retry_ledger.hold_existing_output(media_path)
        decision = self.retry_ledger.claim(media_path, inputs, self.retry_policy, kind)
        claim = decision.claim
        if claim is None:
            return ProcessResult(decision.status, decision.message)
        staging_root = self.state.database.parent / "retry-staging"
        publishing = False
        published: list[Path] = []
        try:
            staging_root.mkdir(mode=0o700, exist_ok=True)
            with tempfile.TemporaryDirectory(
                dir=staging_root, prefix=f"{claim.owner}-",
            ) as directory:
                staged_media = Path(directory) / media_path.name
                # Media is read-only to the existing engines. Recheck its complete hash before
                # publication; copy small SRT inputs so provider/engine writes stay disposable.
                try:
                    os.link(media_path, staged_media)
                except OSError:
                    staged_media.symlink_to(media_path)
                current_tracks = input_tracks(media_path)
                for label, digest in current_tracks.items():
                    staged = staged_media.with_name(staged_media.stem + label)
                    shutil.copyfile(media_path.with_name(media_path.stem + label), staged)
                    if content_hash(staged) != digest:
                        raise ValueError("subtitle input changed while staging")
                try:
                    result = self._process_locked(replace(item, path=staged_media), staged_media)
                except Exception as exc:
                    result = ProcessResult("failed", f"{type(exc).__name__}: {exc}")
                if result.status != "completed":
                    with self.retry_ledger.publication_guard(claim) as connection:
                        return self.retry_ledger.finish(connection, claim, result)
                staged_tracks = input_tracks(staged_media)
                derived = {label: digest for label, digest in staged_tracks.items()
                           if inputs.tracks.get(label) != digest}
                changed = {label for label, digest in staged_tracks.items()
                           if current_tracks.get(label) != digest}
                source = find_language_sidecar(staged_media, self.source_language)
                allowed_clean = "." + source.name[len(staged_media.stem) + 1:] if source else ""
                if any(label in current_tracks and not (
                    self.clean_source_output and label == allowed_clean
                ) for label in changed):
                    raise ValueError("refused an unexpected change to an existing subtitle")
                publish = [staged_media.with_name(staged_media.stem + label)
                           for label in sorted(changed)]
                bilingual = find_language_sidecar(staged_media, self.target_language,
                                                 bilingual=True)
                if bilingual is not None:
                    publish.append(bilingual)  # Final bilingual file is always last.
                writes = [(media_path.with_name(path.name), path.read_bytes(),
                           self.clean_source_output and path == source) for path in publish]
                result = replace(result, outputs=tuple(media_path.with_name(path.name)
                                                      for path in result.outputs))
                proof = PublicationProof(
                    media_path=media_path, job_id=claim.job_id, media_hash=inputs.media_hash,
                    settings_hash=inputs.settings_hash, original_tracks=inputs.tracks,
                    expected_tracks=staged_tracks, recipe_version=RECIPE_VERSION,
                    validation_version=VALIDATION_VERSION, message=result.message,
                    outputs=tuple(OutputProof.planned(*write) for write in writes),
                    final_path=media_path.with_name(bilingual.name) if bilingual else None,
                    result_outputs=result.outputs, review=PublicationProof.review_from(result),
                )
                # Commit intended bytes/review first, then the prepared inode proof, before
                # final creation. Filesystem publication and SQLite completion remain separate.
                self.retry_ledger.begin_publication(claim, derived, proof)
                publishing = True
                with ExitStack() as stack:
                    prepared = [stack.enter_context(prepare_write_bytes(path, content))
                                for path, content, _ in writes]
                    proof = proof.model_copy(update={"outputs": tuple(
                        output.prepared(ready) for output, ready in zip(proof.outputs, prepared,
                                                                       strict=True)
                    )})
                    self.retry_ledger.prepared_publication(claim, proof)
                    with self.retry_ledger.publication_guard(claim) as connection:
                        if self._job_inputs(item, media_path) != inputs:
                            raise ValueError(
                                "job inputs or output settings changed; no publication",
                            )
                        for (destination, content, overwrite), ready in zip(writes, prepared,
                                                                           strict=True):
                            atomic_write_bytes(
                                destination, content, overwrite=overwrite, prepared=ready,
                                before_publish=lambda: self.retry_ledger.check_claim(
                                    connection, claim,
                                ),
                                after_publish=partial(published.append, destination),
                            )
                        if (self._job_inputs(item, media_path) != inputs
                                or input_tracks(media_path) != proof.expected_tracks
                                or not all(output.matches() for output in proof.outputs)):
                            raise ValueError("publication verification failed; files kept")
                        return self.retry_ledger.finish(connection, claim, result, derived)
        except StaleClaimError as exc:
            # A system call may succeed while its lease expires. Never erase that fact or
            # let this stale worker commit success; the persisted intent still holds recovery.
            message = str(exc) + (
                "; publication occurred; files kept; provenance recovery required"
                if published else "; no publication"
            )
            if publishing:
                with suppress(StaleClaimError):
                    self.retry_ledger.stop_publication(claim, publication_occurred=bool(published),
                                                      published=tuple(published))
            return ProcessResult("blocked", message, tuple(published))
        except Exception as exc:
            if publishing:
                with suppress(StaleClaimError):
                    self.retry_ledger.stop_publication(claim, publication_occurred=bool(published),
                                                      published=tuple(published))
                return ProcessResult(
                    "blocked", "publication stopped; files kept; " + str(exc), tuple(published),
                )
            try:
                with self.retry_ledger.publication_guard(claim) as connection:
                    return self.retry_ledger.finish(
                        connection, claim, ProcessResult("failed", f"{type(exc).__name__}: {exc}"),
                    )
            except StaleClaimError as stale:
                return ProcessResult("blocked", str(stale))

    def _process_locked(self, item: MediaItem, media_path: Path) -> ProcessResult:
        bilingual = find_language_sidecar(media_path, self.target_language, bilingual=True)
        if bilingual is not None:
            return ProcessResult(
                "blocked", "existing bilingual output kept unverified; provenance is unavailable",
            )
        self.extractor.extract(media_path, {self.source_language, *self._download_targets()})
        target = find_language_sidecar(media_path, self.target_language)

        source_path = find_language_sidecar(media_path, self.source_language)
        merge_attempted = False
        if source_path is not None and target is not None:
            merge_attempted = True
            try:
                return self._merge_existing_pair(media_path, source_path, target)
            except ValueError as exc:
                log.warning("existing subtitle pair could not be merged: %s", exc)
                return ProcessResult(
                    "failed",
                    "kept both existing subtitle tracks unchanged because their timing match "
                    f"was too low: {exc}",
                    (source_path, target),
                )

        if self.translator is None:
            if target is not None:
                message = (
                    f"kept {self.target_language} subtitle; bilingual timing match was too low"
                    if merge_attempted
                    else f"{self.target_language} subtitle already exists"
                )
                return ProcessResult(
                    "completed",
                    message,
                    (target,),
                )
            requested_languages = {self.source_language, *self._download_targets()}
            downloaded = self.downloader.download(item, requested_languages)
            for path in downloaded:
                if self.synchronizer is not None:
                    self.synchronizer.sync(media_path, path)
            target = find_language_sidecar(media_path, self.target_language)
            if target is not None:
                source_path = find_language_sidecar(media_path, self.source_language)
                if source_path is not None and not merge_attempted:
                    try:
                        return self._merge_existing_pair(media_path, source_path, target)
                    except ValueError as exc:
                        log.warning("downloaded subtitle pair could not be merged: %s", exc)
                return ProcessResult(
                    "completed",
                    f"downloaded {self.target_language} subtitle",
                    (target,),
                )
            sidecars = discover_sidecars(media_path)
            conversion_source = self._conversion_source(sidecars)
            if conversion_source is not None and self._target_converter is not None:
                output = self._convert_to_target(media_path, conversion_source)
                source_path = find_language_sidecar(media_path, self.source_language)
                if source_path is not None:
                    try:
                        return self._merge_existing_pair(media_path, source_path, output)
                    except ValueError as exc:
                        log.warning("converted subtitle pair could not be merged: %s", exc)
                return ProcessResult(
                    "completed",
                    f"converted subtitle to {self.target_language}",
                    (output,),
                )
            raise RuntimeError(
                f"no {self.target_language} subtitle was found. Add both language SRT files "
                "beside the video, or reopen SubDuet Setup and enable translation"
            )

        source_was_generated = False
        if source_path is None:
            self.downloader.download(item, {self.source_language})
            source_path = find_language_sidecar(media_path, self.source_language)
        if source_path is None and self.transcriber is not None:
            generated_path = sidecar_path(media_path, self.source_language)
            self.transcriber.transcribe(media_path, generated_path, self.source_language)
            source_path = find_language_sidecar(media_path, self.source_language)
            source_was_generated = source_path is not None
        if source_path is None:
            raise RuntimeError(
                f"no {self.source_language} subtitle was found. Reopen SubDuet Setup and add "
                "subtitle search or speech generation, or place a source SRT beside the video"
            )
        if target is not None and not merge_attempted:
            try:
                return self._merge_existing_pair(media_path, source_path, target)
            except ValueError as exc:
                log.warning("existing subtitle pair could not be merged: %s", exc)
                return ProcessResult(
                    "failed",
                    "kept both existing subtitle tracks unchanged because their timing match "
                    f"was too low: {exc}",
                    (source_path, target),
                )
        synchronized = False
        if source_was_generated:
            source = clean_spoken_dialogue(parse_srt(source_path))
        else:
            with self._working_subtitle(media_path, source_path) as (
                working_source,
                synchronized,
            ):
                source = clean_spoken_dialogue(parse_srt(working_source))
        glossary = self.glossary.load(item.show_title or item.title)
        translations = self.translator.translate_all(
            source,
            context=item.context_label,
            glossary=glossary,
        )
        translated = translated_subtitles(source, translations)
        bilingual_output = bilingual_subtitles(
            source,
            translated,
            order=self.bilingual_order,
        )

        translated_path = sidecar_path(media_path, self.target_language)
        bilingual_path = sidecar_path(media_path, self.target_language, bilingual=True)
        # Each file is atomic; bilingual is last so it marks a complete learning-language pair.
        if self.clean_source_output:
            write_srt(source_path, source)
        write_srt(translated_path, translated)
        write_srt(bilingual_path, bilingual_output)
        if source_was_generated:
            action = "generated and translated"
        else:
            action = "aligned and translated" if synchronized else "translated"
        quality_suffix = (
            "; AI final quality check passed"
            if getattr(self.translator, "final_check_enabled", False)
            else ""
        )
        return ProcessResult(
            "completed",
            f"{action} {len(source)} cues from {self.source_language} to "
            f"{self.target_language}{quality_suffix}",
            (translated_path, bilingual_path),
        )

    def _merge_existing_pair(
        self,
        media_path: Path,
        source_path: Path,
        target_path: Path,
    ) -> ProcessResult:
        with self._working_subtitle(media_path, source_path) as (working_source, _):
            source = clean_spoken_dialogue(parse_srt(working_source))
        with self._working_subtitle(media_path, target_path) as (working_target, _):
            target = clean_spoken_dialogue(parse_srt(working_target))
        merged = merge_bilingual_subtitles(
            source,
            target,
            order=self.bilingual_order,
            tolerance_ms=self.bilingual_merge_tolerance_ms,
            min_match_ratio=self.bilingual_merge_min_match_ratio,
            readability_profile=self.readability_profile,
        )
        bilingual_path = sidecar_path(media_path, self.target_language, bilingual=True)
        if self.clean_source_output:
            write_srt(source_path, source)
        write_srt(bilingual_path, merged.subtitles)
        message = (
            "merged existing subtitle tracks "
            f"({merged.source_match_ratio:.0%}/{merged.target_match_ratio:.0%} matched)"
        )
        if merged.review_notice:
            message += f"; {merged.review_notice}"
        return ProcessResult(
            "completed",
            message,
            (target_path, bilingual_path),
            merged.review_details,
        )

    @contextmanager
    def _working_subtitle(
        self,
        media_path: Path,
        subtitle_path: Path,
    ) -> Iterator[tuple[Path, bool]]:
        """Align a disposable subtitle copy so user-owned tracks stay untouched."""

        if self.synchronizer is None:
            yield subtitle_path, False
            return
        with tempfile.TemporaryDirectory(prefix="paircue-sync-") as temporary_directory:
            working_path = Path(temporary_directory) / subtitle_path.name
            shutil.copy2(subtitle_path, working_path)
            synchronized = self.synchronizer.sync(media_path, working_path)
            yield working_path, synchronized

    def _download_targets(self) -> set[str]:
        targets = {self.target_language}
        if self.target_language in TRADITIONAL_CHINESE_TARGETS:
            targets.update({"zh-CN", "zh"})
        elif self.target_language in SIMPLIFIED_CHINESE_TARGETS:
            targets.update({"zh-TW", "zh"})
        return targets

    def _conversion_source(self, sidecars: Sidecars) -> Path | None:
        if self.target_language in TRADITIONAL_CHINESE_TARGETS:
            return sidecars.simplified_chinese or sidecars.generic_chinese
        if self.target_language in SIMPLIFIED_CHINESE_TARGETS:
            return sidecars.traditional_chinese or sidecars.generic_chinese
        return None

    def _convert_to_target(self, media_path: Path, source_path: Path) -> Path:
        if self._target_converter is None:
            raise ValueError(f"no script converter is available for {self.target_language}")
        source = parse_srt(source_path)
        converted = [
            srt.Subtitle(
                index=cue.index,
                start=cue.start,
                end=cue.end,
                content=self._target_converter.convert(cue.content),
                proprietary=cue.proprietary,
            )
            for cue in source
        ]
        output = sidecar_path(media_path, self.target_language)
        write_srt(output, converted)
        return output
