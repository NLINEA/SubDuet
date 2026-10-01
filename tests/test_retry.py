from __future__ import annotations

import os
import shutil
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import pytest
import srt

from paircue.config import PairCueSettings
from paircue.models import MediaItem, ProcessResult
from paircue.runtime import CoreRuntime, JobCoordinator
from paircue.services.glossary import GlossaryStore
from paircue.services.pipeline import SubtitlePipeline
from paircue.services.readability import ReadabilityProfile
from paircue.services.retry import JobInputs, RetryPolicy, StaleClaimError, canonical_hash
from paircue.services.state import StateStore
from paircue.services.subtitle_files import parse_srt, write_srt


class Clock:
    now = 0

    def __call__(self) -> int:
        return self.now


class Backend:
    def __init__(self) -> None:
        self.calls = 0
        self.succeed = False
        self.write_then_fail = False
        self.entered: threading.Event | None = None
        self.release: threading.Event | None = None

    def download(self, item: MediaItem, languages: set[str]) -> tuple[Path, ...]:
        self.calls += 1
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            assert self.release.wait(5), "test worker did not release"
        if self.succeed or self.write_then_fail:
            source, target = item.path.with_suffix(".en.srt"), item.path.with_suffix(".zh-TW.srt")
            for path, text in ((source, "Keep the old report."), (target, "保留舊報告。")):
                write_srt(path, [srt.Subtitle(1, timedelta(seconds=1),
                                            timedelta(seconds=5), text)])
            if self.succeed:
                return source, target
        raise OSError("synthetic retryable failure")

    def close(self) -> None:
        pass


class Extractor:
    def extract(self, *args: object) -> tuple[Path, ...]:
        return ()


class Source:
    platform = "filesystem"

    def __init__(self, item: MediaItem) -> None:
        self.item = item

    def scan_items(self) -> list[MediaItem]:
        return [self.item]

    def item_for_id(self, item_id: str) -> MediaItem | None:
        return self.item if item_id == self.item.item_id else None

    def close(self) -> None:
        pass


def pipeline(tmp_path: Path, clock: Clock, backend: Backend,
             policy: RetryPolicy | None = None) -> tuple[SubtitlePipeline, MediaItem]:
    media = tmp_path / "Synthetic.mkv"
    if not media.exists():
        media.write_bytes(b"Synthetic media identity; no audio or video")
    item = MediaItem("synthetic", "movie", media, "Synthetic")
    result = SubtitlePipeline(media_root=tmp_path, state=StateStore(tmp_path / "state.sqlite"),
                              downloader=backend, extractor=Extractor(), synchronizer=None,
                              translator=None, glossary=GlossaryStore(tmp_path / "glossaries"),
                              bilingual_order="source-first", retry_policy=policy,
                              retry_clock=clock)
    return result, item


def jobs(p: SubtitlePipeline) -> list[tuple[object, ...]]:
    with sqlite3.connect(p.state.database) as connection:
        return connection.execute("SELECT auto_count, manual_count, status, next_retry_ms, job_id "
                                  "FROM retry_job ORDER BY rowid").fetchall()


def test_c12_polls_restart_deadlines_and_one_manual_attempt(tmp_path: Path) -> None:
    clock, backend = Clock(), Backend()
    policy = RetryPolicy(max_auto_attempts=3, retry_delays_ms=(1000, 4000))
    p, item = pipeline(tmp_path, clock, backend, policy)
    coordinator = JobCoordinator(p)
    coordinator.start()
    runtime = CoreRuntime(Source(item), coordinator, 1800)  # type: ignore[arg-type]
    try:
        for now, expected in ((0, 1), (1000, 2)):
            clock.now = now
            runtime.scan_now()
            coordinator._queue.join()
            assert backend.calls == expected
        assert jobs(p)[0][:4] == (2, 0, "retry_wait", 5000)
        coordinator.stop()
        p, item = pipeline(tmp_path, clock, backend, policy)
        coordinator = JobCoordinator(p)
        coordinator.start()
        runtime = CoreRuntime(Source(item), coordinator, 1800)  # type: ignore[arg-type]
        clock.now = 3000
        runtime.scan_now()
        coordinator._queue.join()
        assert backend.calls == 2 and jobs(p)[0][3] == 5000
        for clock.now in range(5000, 16000, 1000):
            runtime.scan_now()
            coordinator._queue.join()
            assert backend.calls == 3
            assert p.state.status_for(item.path) == "retry_exhausted"
        clock.now = 17000
        backend.succeed = True
        assert runtime.retry_item_id(item.item_id)
        coordinator._queue.join()
        assert backend.calls == 4 and jobs(p)[0][:3] == (3, 1, "completed")
        original = item.path.with_suffix(".mul.srt").read_bytes()
        clock.now = 18000
        runtime.scan_now()
        coordinator._queue.join()
        assert backend.calls == 4 and item.path.with_suffix(".mul.srt").read_bytes() == original
        assert p.state.status_for(item.path) == "completed"  # Complete own publication proof.
    finally:
        coordinator.stop()


def test_two_workers_claim_once_and_expired_worker_cannot_publish(tmp_path: Path) -> None:
    clock = Clock()
    slow, fast = Backend(), Backend()
    slow.entered, slow.release = threading.Event(), threading.Event()
    slow.succeed = True
    policy = RetryPolicy(max_auto_attempts=3, retry_delays_ms=(0,), lease_ms=100)
    first, item = pipeline(tmp_path, clock, slow, policy)
    second, _ = pipeline(tmp_path, clock, fast, policy)
    with ThreadPoolExecutor(max_workers=2) as workers:
        task = workers.submit(first.process, item, retry_mode="automatic")
        assert slow.entered.wait(5)
        busy = second.process(item, retry_mode="automatic")
        assert busy.status == "skipped" and fast.calls == 0 and slow.calls == 1
        moved = tmp_path / "Moved.mkv"
        moved.write_bytes(item.path.read_bytes())
        duplicate = MediaItem(item.item_id, item.media_type, moved, item.title)
        assert second.process(duplicate, retry_mode="automatic").status == "skipped"
        assert fast.calls == 0
        clock.now = 101
        assert second.process(item, retry_mode="automatic").status == "retry_wait"
        slow.release.set()
        stale = task.result(timeout=5)
    assert stale.status == "blocked" and jobs(second)[0][:3] == (2, 0, "retry_wait")
    assert not list(tmp_path.glob("Synthetic.*.srt"))  # Stale side effects stay in staging.


def test_full_content_and_settings_identity_and_restored_allowance(tmp_path: Path) -> None:
    clock, backend = Clock(), Backend()
    p, item = pipeline(tmp_path, clock, backend)
    source = item.path.with_suffix(".en.srt")
    source.write_text("1\n00:00:01,000 --> 00:00:05,000\nFirst\n", encoding="utf-8")
    before, stat = source.read_bytes(), source.stat()
    assert p.process(item, retry_mode="automatic").status == "retry_exhausted"
    first_id = jobs(p)[0][4]
    source.write_bytes(before.replace(b"First", b"Later"))
    os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert p.process(item, retry_mode="automatic").status == "retry_exhausted"
    assert backend.calls == 2 and jobs(p)[1][4] != first_id
    p.readability_profile = ReadabilityProfile(source_line_codepoints_max=48)
    assert p.process(item, retry_mode="automatic").status == "retry_exhausted"
    assert backend.calls == 3 and len(jobs(p)) == 3
    source.write_bytes(before)
    p.readability_profile = None
    p.retry_policy = RetryPolicy(max_auto_attempts=3, retry_delays_ms=(0,))
    assert p.process(item, retry_mode="automatic").status == "retry_exhausted"
    assert backend.calls == 3 and len(jobs(p)) == 3 and jobs(p)[0][0] == 1
    # A complete media-content change also creates a distinct job.
    media_stat = item.path.stat()
    item.path.write_bytes(item.path.read_bytes().replace(b"Synthetic", b"synthetic"))
    os.utime(item.path, ns=(media_stat.st_atime_ns, media_stat.st_mtime_ns))
    p.process(item, retry_mode="automatic")
    assert backend.calls == 4 and len(jobs(p)) == 4


def test_identity_excludes_credentials_paths_polling_and_retry_policy() -> None:
    first = PairCueSettings()
    second = PairCueSettings(api_token="synthetic-secret", translation_api_key="another-secret",
                             media_root=Path("/different"), state_dir=Path("/elsewhere"),
                             scan_interval_seconds=120, translation_timeout_seconds=300,
                             retry_policy=RetryPolicy(max_auto_attempts=3))
    original = canonical_hash(first.job_recipe_settings())
    assert original == canonical_hash(second.job_recipe_settings())
    assert "secret" not in str(second.job_recipe_settings())
    third = PairCueSettings(audio_stream_index=2, translation_model="different-model")
    assert original != canonical_hash(third.job_recipe_settings())


def test_moving_identical_input_cannot_reset_the_automatic_allowance(tmp_path: Path) -> None:
    clock, backend = Clock(), Backend()
    p, item = pipeline(tmp_path, clock, backend)
    assert p.process(item, retry_mode="automatic").status == "retry_exhausted"
    moved = tmp_path / "Moved.mkv"
    moved.write_bytes(item.path.read_bytes())
    duplicate = MediaItem(item.item_id, item.media_type, moved, item.title)
    assert p.process(duplicate, retry_mode="automatic").status == "retry_exhausted"
    assert backend.calls == 1 and len(jobs(p)) == 1


def test_moving_recorded_generated_source_keeps_one_automatic_allowance(tmp_path: Path) -> None:
    class EmptyBackend(Backend):
        def download(self, item: MediaItem, languages: set[str]) -> tuple[Path, ...]:
            return ()

    class Transcriber:
        calls = 0

        def transcribe(self, media: Path, output: Path, language: str) -> Path:
            self.calls += 1
            write_srt(output, [srt.Subtitle(1, timedelta(seconds=1), timedelta(seconds=5),
                                           "Generated source")])
            return output

    class Translator:
        calls = 0

        def translate_all(self, source: list[srt.Subtitle], **kwargs: object) -> dict[int, str]:
            self.calls += 1
            return dict.fromkeys(range(len(source)), "Generated target")

    clock, backend = Clock(), EmptyBackend()
    transcriber, translator = Transcriber(), Translator()
    p, item = pipeline(tmp_path, clock, backend)
    p.transcriber, p.translator = transcriber, translator  # type: ignore[assignment]
    assert p.process(item, retry_mode="automatic").status == "completed"
    job_id = jobs(p)[0][4]
    item.path.with_suffix(".mul.srt").unlink()
    item.path.with_suffix(".zh-TW.srt").unlink()
    assert p.process(item, retry_mode="automatic").status == "blocked"
    moved = tmp_path / "Relocated" / item.path.name
    moved.parent.mkdir()
    shutil.copyfile(item.path, moved)
    shutil.copyfile(item.path.with_suffix(".en.srt"), moved.with_suffix(".en.srt"))
    p, _ = pipeline(tmp_path, clock, backend)  # Fresh pipeline/ledger, same recorded content.
    p.transcriber, p.translator = transcriber, translator  # type: ignore[assignment]
    duplicate = MediaItem(item.item_id, item.media_type, moved, item.title)
    for _ in range(3):
        assert p.process(duplicate, retry_mode="automatic").status in {"blocked", "retry_exhausted"}
    assert transcriber.calls == translator.calls == 1
    assert jobs(p) == [(1, 0, "completed", None, job_id)]
    assert not moved.with_suffix(".mul.srt").exists()
    assert moved.read_bytes() == item.path.read_bytes()
    original_source = item.path.with_suffix(".en.srt").read_bytes()
    assert moved.with_suffix(".en.srt").read_bytes() == original_source
    # Changing the actual source content is a different input, rather than a path-only reset.
    write_srt(moved.with_suffix(".en.srt"), [srt.Subtitle(
        1, timedelta(seconds=1), timedelta(seconds=5), "New original source",
    )])
    assert p.process(duplicate, retry_mode="automatic").status == "completed"
    assert translator.calls == 2 and transcriber.calls == 1 and len(jobs(p)) == 2


def test_conflicting_recorded_derived_inputs_do_not_guess_an_identity(tmp_path: Path) -> None:
    p, item = pipeline(tmp_path, Clock(), Backend())
    ledger = p.retry_ledger
    for original in ("original-A", "original-B"):
        inputs = JobInputs("media", {".en.srt": original}, "settings")
        claim = ledger.claim(item.path, inputs, RetryPolicy()).claim
        assert claim is not None
        derived = {".en.srt": "same-generated-bytes"}
        with ledger.publication_guard(claim) as connection:
            ledger.finish(connection, claim, ProcessResult("completed", "test completed"), derived)
    before = jobs(p)
    with pytest.raises(ValueError, match="derived-track identity is ambiguous"):
        ledger.normalize_inputs(JobInputs("media", {".en.srt": "same-generated-bytes"}, "settings"))
    assert jobs(p) == before


def test_observed_publication_cannot_replace_a_successor_state(tmp_path: Path) -> None:
    clock = Clock()
    p, item = pipeline(tmp_path, clock, Backend(), RetryPolicy(lease_ms=100))
    ledger = p.retry_ledger
    first = ledger.claim(item.path, JobInputs("media", {}, "old-settings"), p.retry_policy).claim
    assert first is not None
    clock.now = 101
    successor = ledger.claim(
        item.path, JobInputs("media", {}, "new-settings"), p.retry_policy,
    ).claim
    assert successor is not None
    before = p.state.recent()
    with pytest.raises(StaleClaimError):
        ledger.stop_publication(first, publication_occurred=True)
    assert p.state.recent() == before
    with ledger.publication_guard(successor) as connection:
        ledger.check_claim(connection, successor)


@pytest.mark.parametrize("overwrite", [False, True])
def test_atomic_publication_guard_preserves_existing_file_and_cleans_temp(
    tmp_path: Path, overwrite: bool,
) -> None:
    from paircue.services.atomic import atomic_write_bytes

    final = tmp_path / "existing.mul.srt"
    final.write_bytes(b"Existing final must survive")
    publications: list[bool] = []

    def refuse() -> None:
        raise StaleClaimError("synthetic expired claim at publication boundary")

    with pytest.raises(StaleClaimError):
        atomic_write_bytes(final, b"New content", overwrite=overwrite,
                           before_publish=refuse, after_publish=lambda: publications.append(True))
    assert final.read_bytes() == b"Existing final must survive"
    assert not publications and list(tmp_path.iterdir()) == [final]


def test_legacy_attempts_remain_unknown_and_manual_failure_grants_only_one(tmp_path: Path) -> None:
    clock, backend = Clock(), Backend()
    p, item = pipeline(tmp_path, clock, backend)
    p.state.record(item.path, "old-stat-fingerprint", "failed", "old failure")
    assert p.process(item, retry_mode="automatic").status == "blocked"
    assert jobs(p)[0][0] is None and backend.calls == 0
    assert p.process(item, retry_mode="manual").status == "blocked"
    for _ in range(10):
        assert p.process(item, retry_mode="automatic").status == "blocked"
    assert backend.calls == 1 and jobs(p)[0][:2] == (None, 1)
    p.readability_profile = ReadabilityProfile(source_line_codepoints_max=48)
    assert p.process(item, retry_mode="automatic").status == "blocked"
    assert all(job[0] is None for job in jobs(p)) and backend.calls == 1
    moved = tmp_path / "Moved.mkv"
    moved.write_bytes(item.path.read_bytes())
    assert p.process(MediaItem(item.item_id, item.media_type, moved, item.title),
                     retry_mode="automatic").status == "blocked"
    assert all(job[0] is None for job in jobs(p)) and backend.calls == 1


def test_failed_generated_tracks_do_not_mint_new_jobs(tmp_path: Path) -> None:
    clock, backend = Clock(), Backend()
    backend.write_then_fail = True
    p, item = pipeline(tmp_path, clock, backend,
                       RetryPolicy(max_auto_attempts=3, retry_delays_ms=(0,)))
    for _ in range(15):
        p.process(item, retry_mode="automatic")
    assert backend.calls == 3 and len(jobs(p)) == 1
    assert jobs(p)[0][:3] == (3, 0, "retry_exhausted")
    assert not list(tmp_path.glob("Synthetic.*.srt"))
    assert not list((tmp_path / "retry-staging").iterdir())


def test_failed_manual_attempt_does_not_reset_exhausted_allowance(tmp_path: Path) -> None:
    clock, backend = Clock(), Backend()
    p, item = pipeline(tmp_path, clock, backend)
    assert p.process(item, retry_mode="automatic").status == "retry_exhausted"
    assert p.process(item, retry_mode="manual").status == "retry_exhausted"
    for _ in range(10):
        assert p.process(item, retry_mode="automatic").status == "retry_exhausted"
    assert backend.calls == 2 and jobs(p)[0][:3] == (1, 1, "retry_exhausted")


def test_manual_rebuild_keeps_derived_tracks_out_of_the_original_identity(tmp_path: Path) -> None:
    clock, backend = Clock(), Backend()
    backend.succeed = True
    p, item = pipeline(tmp_path, clock, backend)
    assert p.process(item, retry_mode="automatic").status == "completed"
    job_id = jobs(p)[0][4]
    item.path.with_suffix(".mul.srt").unlink()
    assert p.process(item, retry_mode="manual").status == "completed"
    item.path.with_suffix(".mul.srt").unlink()
    assert p.process(item, retry_mode="automatic").status == "blocked"
    assert backend.calls == 1 and jobs(p)[0][4] == job_id and len(jobs(p)) == 1
    assert jobs(p)[0][:2] == (1, 1)


def test_lease_expiry_during_publication_prevents_final_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from paircue.services import pipeline as module

    clock, backend = Clock(), Backend()
    backend.succeed = True
    p, item = pipeline(tmp_path, clock, backend,
                       RetryPolicy(max_auto_attempts=3, retry_delays_ms=(0,), lease_ms=100))
    native = module.atomic_write_bytes

    def expire(path: Path, content: bytes, **kwargs: object) -> None:
        native(path, content, **kwargs)  # type: ignore[arg-type]
        clock.now = 101

    monkeypatch.setattr(module, "atomic_write_bytes", expire)
    assert p.process(item, retry_mode="automatic").status == "blocked"
    assert not item.path.with_suffix(".mul.srt").exists()
    assert p.process(item, retry_mode="automatic").status == "blocked"
    assert backend.calls == 1


def test_lease_expiry_after_final_preparation_prevents_atomic_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from paircue.services import atomic

    clock, backend = Clock(), Backend()
    p, item = pipeline(tmp_path, clock, backend, RetryPolicy(lease_ms=100))
    for language, text in (("en", "Source"), ("zh-TW", "Target")):
        write_srt(item.path.with_suffix(f".{language}.srt"), [srt.Subtitle(
            1, timedelta(seconds=1), timedelta(seconds=5), text,
        )])
    original = {path: path.read_bytes() for path in tmp_path.glob("Synthetic.*")}
    native = atomic.os.chmod

    def expire_after_preparation(path: Path, mode: int) -> None:
        native(path, mode)
        if path.parent == tmp_path and path.name.startswith(".Synthetic.mul.srt."):
            clock.now = 101

    monkeypatch.setattr(atomic.os, "chmod", expire_after_preparation)
    result = p.process(item, retry_mode="automatic")
    assert result.status == "blocked" and "no publication" in result.message
    assert p.state.status_for(item.path) == "blocked"
    assert not result.outputs and not item.path.with_suffix(".mul.srt").exists()
    assert not list(tmp_path.glob(".*.tmp")) and backend.calls == 0
    assert all(path.read_bytes() == content for path, content in original.items())


@pytest.mark.parametrize("expiry", ["before_native_link", "after_native_link"])
def test_successful_final_publication_race_is_reported_without_false_no_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, expiry: str,
) -> None:
    from paircue.services import atomic

    clock, backend = Clock(), Backend()
    p, item = pipeline(tmp_path, clock, backend, RetryPolicy(lease_ms=100))
    for language, text in (("en", "Source"), ("zh-TW", "Target")):
        write_srt(item.path.with_suffix(f".{language}.srt"), [srt.Subtitle(
            1, timedelta(seconds=1), timedelta(seconds=5), text,
        )])
    native, publications = atomic.os.link, []
    final = item.path.with_suffix(".mul.srt")

    def expire_at_final(source: Path, destination: Path) -> None:
        if destination == final and expiry == "before_native_link":
            clock.now = 101  # Exact independent review seam, inside the OS call wrapper.
        native(source, destination)
        if destination == final:
            publications.append(destination)
            clock.now = 101

    monkeypatch.setattr(atomic.os, "link", expire_at_final)
    result = p.process(item, retry_mode="automatic")
    assert result.status == "blocked" and "publication occurred" in result.message
    assert "no publication" not in result.message and result.outputs == (final,)
    assert publications == [final] and backend.calls == 0
    assert p.state.status_for(item.path) == "blocked"
    assert "publication occurred" in p.state.recent()[0].message
    assert [cue.content for cue in parse_srt(final)] == ["Source\nTarget"]
    with sqlite3.connect(p.state.database) as connection:
        observed = connection.execute(
            "SELECT observed_paths_json FROM retry_publication",
        ).fetchone()[0]
    assert str(final) in observed  # Lease expiry must not lose the durable publication fact.
    before = final.read_bytes()
    p, _ = pipeline(tmp_path, clock, backend)
    for mode in ("automatic", "manual"):
        assert p.process(item, retry_mode=mode).status == "completed"  # type: ignore[arg-type]
    assert publications == [final] and final.read_bytes() == before
    assert jobs(p)[0][:3] == (1, 0, "completed")


@pytest.mark.parametrize("crash_point", ["before_publish", "after_first_sidecar", "after_final"])
def test_uncertain_publication_stops_without_adopting_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, crash_point: str,
) -> None:
    from paircue.services import pipeline as module

    clock, backend = Clock(), Backend()
    backend.succeed = True
    p, item = pipeline(tmp_path, clock, backend,
                       RetryPolicy(max_auto_attempts=3, retry_delays_ms=(0,)))
    native_write, writes = module.atomic_write_bytes, []

    def fault(path: Path, content: bytes, **kwargs: object) -> None:
        if crash_point == "before_publish" or (crash_point == "after_first_sidecar" and writes):
            raise OSError("synthetic publication interruption")
        native_write(path, content, **kwargs)  # type: ignore[arg-type]
        writes.append(path)

    monkeypatch.setattr(module, "atomic_write_bytes", fault)
    if crash_point == "after_final":
        def fail_finish(*args: object, **kwargs: object) -> None:
            raise RuntimeError("synthetic crash after final before state commit")
        monkeypatch.setattr(p.retry_ledger, "finish", fail_finish)
    assert p.process(item, retry_mode="automatic").status == "blocked"
    before = {path: path.read_bytes() for path in writes}
    p, item = pipeline(tmp_path, clock, backend)
    expected = "completed" if crash_point == "after_final" else "blocked"
    for mode in ("automatic", "automatic", "manual"):
        assert p.process(item, retry_mode=mode).status == expected  # type: ignore[arg-type]
    assert backend.calls == 1
    assert jobs(p)[0][:3] == (1, 0, "completed" if crash_point == "after_final" else "publishing")
    assert all(path.read_bytes() == content for path, content in before.items())


def test_existing_output_kept_unverified_even_for_explicit_retry(tmp_path: Path) -> None:
    clock, backend = Clock(), Backend()
    p, item = pipeline(tmp_path, clock, backend)
    output = item.path.with_suffix(".mul.srt")
    output.write_bytes(b"existing unverified output")
    for mode in (None, "automatic", "manual"):
        result = p.process(item, retry_mode=mode)  # type: ignore[arg-type]
        assert result.status == "blocked" and result.outputs == ()
    assert backend.calls == 0 and output.read_bytes() == b"existing unverified output"


def test_partial_readability_details_survive_restart_and_output_hold(tmp_path: Path) -> None:
    clock, backend = Clock(), Backend()
    p, item = pipeline(tmp_path, clock, backend)
    source = [srt.Subtitle(i + 1, timedelta(seconds=i * 3), timedelta(seconds=i * 3 + 1),
                          "A" * 48 if i == 3 else f"Source {i}") for i in range(4)]
    target = [srt.Subtitle(cue.index, cue.start, cue.end, "中文") for cue in source[:3]]
    write_srt(item.path.with_suffix(".en.srt"), source)
    write_srt(item.path.with_suffix(".zh-TW.srt"), target)
    result = p.process(item, retry_mode="automatic")
    assert result.status == "completed" and backend.calls == 0
    stored, = p.state.recent()
    assert stored.review_id is not None
    before = p.state.review_page(stored.review_id)
    p, item = pipeline(tmp_path, clock, backend)
    assert p.process(item, retry_mode="automatic").status == "completed"
    after, = p.state.recent()
    assert "partial timing pairing" in after.message and "Readability review" in after.message
    assert p.state.review_page(stored.review_id) == before
    assert len(parse_srt(item.path.with_suffix(".mul.srt"))) == 4
