from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
import srt
from test_retry import Backend, Clock, jobs, pipeline

from paircue.models import ProcessResult
from paircue.services import atomic
from paircue.services.publication import OutputProof, PublicationProof
from paircue.services.readability import ReadabilityProfile
from paircue.services.retry import RetryPolicy, StaleClaimError
from paircue.services.subtitle_files import parse_srt, write_srt


class Crash(BaseException):
    """Simulate abrupt exit; ordinary pipeline error recovery must not catch this."""


def publications(database: Path) -> list[tuple[object, ...]]:
    with sqlite3.connect(database) as connection:
        return connection.execute(
            "SELECT proof_json, status, observed_paths_json FROM retry_publication ORDER BY fence",
        ).fetchall()


def test_unknown_file_identity_refuses_persistent_publication_only(tmp_path: Path) -> None:
    final, content = tmp_path / "final.srt", b"Synthetic completed bytes"
    with atomic.prepare_write_bytes(final, content) as prepared:
        unknown = replace(prepared, inode=0)
        with pytest.raises(ValueError, match="stable prepared-file identity unavailable"):
            OutputProof.planned(final, content, False).prepared(unknown)
    assert not final.exists() and not list(tmp_path.glob(".*.tmp"))
    atomic.atomic_write_bytes(final, content, overwrite=False)
    assert final.read_bytes() == content


@pytest.mark.parametrize("point", [
    "after_intent_commit", "after_prepared_commit", "before_final_link", "after_final_link",
    "before_completion_commit", "after_completion_commit",
])
def test_crash_points_preserve_proof_and_never_repeat_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, point: str,
) -> None:
    clock, backend = Clock(), Backend()
    backend.succeed = True
    p, item = pipeline(tmp_path, clock, backend, RetryPolicy(lease_ms=100))
    final = item.path.with_suffix(".mul.srt")
    with monkeypatch.context() as seam:
        if point in {"after_intent_commit", "after_prepared_commit"}:
            name = "begin_publication" if point == "after_intent_commit" else "prepared_publication"
            original = getattr(p.retry_ledger, name)

            def committed(*args: object, **kwargs: object) -> None:
                original(*args, **kwargs)
                raise Crash(point)

            seam.setattr(p.retry_ledger, name, committed)
        elif point in {"before_final_link", "after_final_link"}:
            original_link = atomic.os.link

            def link(source: Path, destination: Path) -> None:
                if destination == final and point == "before_final_link":
                    raise Crash(point)
                original_link(source, destination)
                if destination == final:
                    raise Crash(point)  # Before the callback or any completion DB update.

            seam.setattr(atomic.os, "link", link)
        elif point == "before_completion_commit":
            original_finish = p.retry_ledger.finish

            def uncommitted(*args: object, **kwargs: object) -> ProcessResult:
                original_finish(*args, **kwargs)  # type: ignore[arg-type]
                raise Crash(point)  # Rolls back completion while prepared proof survives.

            seam.setattr(p.retry_ledger, "finish", uncommitted)
        else:
            from contextlib import contextmanager

            original_guard = p.retry_ledger.publication_guard

            @contextmanager
            def after_commit(claim: object):
                with original_guard(claim) as connection:  # type: ignore[arg-type]
                    yield connection
                    status = connection.execute(
                        "SELECT status FROM retry_job WHERE job_id=?", (claim.job_id,),
                    ).fetchone()[0]
                if status == "completed":
                    raise Crash(point)

            seam.setattr(p.retry_ledger, "publication_guard", after_commit)
        with pytest.raises(Crash):
            p.process(item, retry_mode="automatic")

    complete = point in {"after_final_link", "before_completion_commit", "after_completion_commit"}
    assert backend.calls == 1 and final.exists() == complete
    row, = publications(p.state.database)
    proof = PublicationProof.model_validate_json(row[0])
    assert proof.final_path == final and proof.message
    assert proof.job_id == jobs(p)[0][4]
    before = {path: path.read_bytes() for path in tmp_path.glob("Synthetic.*")}
    clock.now = 101
    restarted, _ = pipeline(tmp_path, clock, backend, RetryPolicy(lease_ms=100))
    for mode in ("automatic", "automatic", "manual"):
        result = restarted.process(item, retry_mode=mode)  # type: ignore[arg-type]
        assert result.status == ("completed" if complete else "blocked")
    assert backend.calls == 1 and jobs(restarted)[0][:2] == (1, 0)
    assert all(path.read_bytes() == content for path, content in before.items())
    if complete:
        assert result.outputs == proof.result_outputs
        assert [cue.content for cue in parse_srt(final)] == ["Keep the old report.\n保留舊報告。"]
        assert all(output.matches() for output in proof.outputs)
        row, = publications(restarted.state.database)
        assert row[1] == "completed"
        assert set(json.loads(row[2])) == {str(output.path) for output in proof.outputs}


@pytest.mark.parametrize("mutation", [
    "modified_output", "identical_copy", "missing_output", "symlink_output", "source",
    "media", "settings", "missing_proof", "malformed_proof", "legacy_version",
    "outside_path", "unexpected_track",
])
def test_changed_or_unproven_output_is_kept_and_blocked(
    tmp_path: Path, mutation: str,
) -> None:
    clock, backend = Clock(), Backend()
    backend.succeed = True
    p, item = pipeline(tmp_path, clock, backend)
    assert p.process(item, retry_mode="automatic").status == "completed"
    final, source = item.path.with_suffix(".mul.srt"), item.path.with_suffix(".en.srt")
    if mutation == "modified_output":
        final.write_bytes(final.read_bytes().replace(b"old", b"new"))
    elif mutation == "identical_copy":
        archived = tmp_path / "archived.srt"
        final.rename(archived)  # Keep the original inode allocated, then make a distinct copy.
        shutil.copyfile(archived, final)
        assert final.stat().st_ino != archived.stat().st_ino
    elif mutation == "missing_output":
        final.unlink()
    elif mutation == "symlink_output":
        archived = tmp_path / "archived.srt"
        final.rename(archived)
        final.symlink_to(archived)
    elif mutation == "source":
        source.write_bytes(source.read_bytes().replace(b"old", b"new"))
    elif mutation == "media":
        item.path.write_bytes(b"Changed complete media contents")
    elif mutation == "unexpected_track":
        item.path.with_suffix(".fr.srt").write_bytes(source.read_bytes())
    elif mutation in {"missing_proof", "malformed_proof", "legacy_version", "outside_path"}:
        with sqlite3.connect(p.state.database) as connection:
            if mutation == "missing_proof":
                connection.execute("DELETE FROM retry_publication")
            else:
                proof = json.loads(publications(p.state.database)[0][0])
                if mutation == "legacy_version":
                    proof["validation_version"] = "unknown-validation-v0"
                elif mutation == "outside_path":
                    proof["outputs"][0]["path"] = str(tmp_path.parent / "outside.srt")
                value = "{}" if mutation == "malformed_proof" else json.dumps(proof)
                connection.execute("UPDATE retry_publication SET proof_json=?", (value,))
    before = {path: path.read_bytes() for path in tmp_path.glob("*.srt")}
    restarted, _ = pipeline(tmp_path, clock, backend)
    if mutation == "settings":
        restarted.readability_profile = ReadabilityProfile(source_line_codepoints_max=48)
    for _ in range(3):
        assert restarted.process(item, retry_mode="automatic").status == "blocked"
    if mutation != "missing_output":
        assert restarted.process(item, retry_mode="manual").status == "blocked"
    assert backend.calls == 1 and jobs(restarted)[0][:2] == (1, 0)
    assert all(path.read_bytes() == content for path, content in before.items())
    if mutation == "missing_output":
        assert not final.exists()  # Automatic polling never rebuilds a missing final.


def test_same_bytes_created_by_a_racer_are_not_adopted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock, backend = Clock(), Backend()
    backend.succeed = True
    p, item = pipeline(tmp_path, clock, backend)
    final, native = item.path.with_suffix(".mul.srt"), atomic.os.link

    def collide(source: Path, destination: Path) -> None:
        if destination == final:
            destination.write_bytes(source.read_bytes())  # Same hash, different file identity.
        native(source, destination)

    with monkeypatch.context() as seam:
        seam.setattr(atomic.os, "link", collide)
        assert p.process(item, retry_mode="automatic").status == "blocked"
    before = final.read_bytes()
    restarted, _ = pipeline(tmp_path, clock, backend)
    assert restarted.process(item, retry_mode="automatic").status == "blocked"
    assert final.read_bytes() == before and backend.calls == 1


@pytest.mark.parametrize("mutation", ["source", "settings"])
def test_inputs_changing_during_output_verification_block_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str,
) -> None:
    clock, backend = Clock(), Backend()
    backend.succeed = True
    p, item = pipeline(tmp_path, clock, backend)
    assert p.process(item, retry_mode="automatic").status == "completed"
    restarted, _ = pipeline(tmp_path, clock, backend)
    native = OutputProof.matches

    def change(output: OutputProof) -> bool:
        matched = native(output)
        if output.path == item.path.with_suffix(".mul.srt"):
            if mutation == "source":
                source = item.path.with_suffix(".en.srt")
                source.write_bytes(source.read_bytes().replace(b"old", b"new"))
            else:
                restarted.bilingual_order = "target-first"
        return matched

    monkeypatch.setattr(OutputProof, "matches", change)
    final = item.path.with_suffix(".mul.srt")
    before = final.read_bytes()
    assert restarted.process(item, retry_mode="automatic").status == "blocked"
    assert backend.calls == 1 and final.read_bytes() == before
    assert jobs(restarted)[0][:2] == (1, 0)


@pytest.mark.parametrize("mutation", ["source", "settings", "legacy_version"])
def test_manual_missing_final_does_not_bypass_mismatched_identity_or_versions(
    tmp_path: Path, mutation: str,
) -> None:
    clock, backend = Clock(), Backend()
    backend.succeed = True
    p, item = pipeline(tmp_path, clock, backend)
    assert p.process(item, retry_mode="automatic").status == "completed"
    item.path.with_suffix(".mul.srt").unlink()
    if mutation == "source":
        source = item.path.with_suffix(".en.srt")
        source.write_bytes(source.read_bytes().replace(b"old", b"new"))
    elif mutation == "legacy_version":
        row, = publications(p.state.database)
        proof = json.loads(row[0])
        proof["recipe_version"] = "unverified-legacy-recipe"
        with sqlite3.connect(p.state.database) as connection:
            connection.execute("UPDATE retry_publication SET proof_json=?", (json.dumps(proof),))
    restarted, _ = pipeline(tmp_path, clock, backend)
    if mutation == "settings":
        restarted.bilingual_order = "target-first"
    assert restarted.process(item, retry_mode="manual").status == "blocked"
    assert backend.calls == 1 and jobs(restarted)[0][:2] == (1, 0)
    assert not item.path.with_suffix(".mul.srt").exists()


def test_full_review_details_restore_after_final_creation_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock, backend = Clock(), Backend()
    p, item = pipeline(tmp_path, clock, backend, RetryPolicy(lease_ms=100))
    source = [srt.Subtitle(i + 1, timedelta(seconds=i * 3), timedelta(seconds=i * 3 + 1),
                          "A" * 48 if i == 699 else f"Source {i}") for i in range(700)]
    target = [srt.Subtitle(cue.index, cue.start, cue.end, "中文") for cue in source[:490]]
    write_srt(item.path.with_suffix(".en.srt"), source)
    write_srt(item.path.with_suffix(".zh-TW.srt"), target)
    before = {path: path.read_bytes() for path in tmp_path.glob("Synthetic.*")}

    def crash_finish(*args: object, **kwargs: object) -> None:
        raise Crash("after final creation before state commit")

    monkeypatch.setattr(p.retry_ledger, "finish", crash_finish)
    with pytest.raises(Crash):
        p.process(item, retry_mode="automatic")
    proof = PublicationProof.model_validate_json(publications(p.state.database)[0][0])
    assert proof.review is not None and len(proof.review.source_only) == 210
    assert len(proof.review.readability) == 1 and len(proof.message) < 1000
    clock.now = 101
    restarted, _ = pipeline(tmp_path, clock, backend)
    result = restarted.process(item, retry_mode="automatic")
    assert result.status == "completed" and result.review_details == proof.review.details()
    assert "partial timing pairing" in result.message and "Readability review" in result.message
    stored, = restarted.state.recent()
    assert stored.review_id is not None and stored.message == proof.message
    pages = [restarted.state.review_page(stored.review_id, offset=offset)
             for offset in (0, 100, 200)]
    assert sum(len(page.entries) for page in pages) == 211
    assert backend.calls == 0 and jobs(restarted)[0][:2] == (1, 0)
    assert all(path.read_bytes() == content for path, content in before.items())


def test_recovery_fences_old_owner_and_preserves_successor_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock, backend = Clock(), Backend()
    backend.succeed = True
    p, item = pipeline(tmp_path, clock, backend, RetryPolicy(lease_ms=100))
    captured = []

    def crash_finish(connection: sqlite3.Connection, claim: object, *args: object) -> None:
        captured.append(claim)
        raise Crash("final created, claim still owned")

    monkeypatch.setattr(p.retry_ledger, "finish", crash_finish)
    with pytest.raises(Crash):
        p.process(item, retry_mode="automatic")
    second, _ = pipeline(tmp_path, clock, backend, RetryPolicy(lease_ms=100))
    assert second.process(item, retry_mode="automatic").status == "skipped"
    clock.now = 101
    third, _ = pipeline(tmp_path, clock, backend, RetryPolicy(lease_ms=100))
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(lambda worker: worker.process(item, retry_mode="automatic"),
                                   (second, third)))
    assert [result.status for result in results] == ["completed", "completed"]
    before = jobs(second), second.state.recent()
    claim, = captured
    with pytest.raises(StaleClaimError), p.retry_ledger.publication_guard(claim):
        pytest.fail("stale owner must not reach filesystem publication")
    with pytest.raises(StaleClaimError):
        p.retry_ledger.stop_publication(claim, publication_occurred=True,
                                      published=(item.path.with_suffix(".mul.srt"),))
    assert (jobs(second), second.state.recent()) == before and backend.calls == 1
    assert item.path.with_suffix(".mul.srt").exists()
    proof_json, _, observed = publications(p.state.database)[0]
    proof = PublicationProof.model_validate_json(proof_json)
    assert set(json.loads(observed)) == {str(output.path) for output in proof.outputs}


def test_live_publisher_and_competing_poll_never_double_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock, backend = Clock(), Backend()
    backend.succeed = True
    first, item = pipeline(tmp_path, clock, backend)
    second, _ = pipeline(tmp_path, clock, backend)
    entered, release = threading.Event(), threading.Event()
    original = first.retry_ledger.prepared_publication

    def pause(*args: object, **kwargs: object) -> None:
        original(*args, **kwargs)  # type: ignore[arg-type]
        entered.set()
        assert release.wait(5)

    monkeypatch.setattr(first.retry_ledger, "prepared_publication", pause)
    with ThreadPoolExecutor(max_workers=2) as workers:
        pending = workers.submit(first.process, item, retry_mode="automatic")
        assert entered.wait(5)
        assert second.process(item, retry_mode="automatic").status == "skipped"
        release.set()
        assert pending.result(timeout=5).status == "completed"
    assert second.process(item, retry_mode="automatic").status == "completed"
    assert backend.calls == 1 and len(publications(first.state.database)) == 1


def test_incomplete_recovery_fences_expired_owner_despite_clock_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock, backend = Clock(), Backend()
    backend.succeed = True
    first, item = pipeline(tmp_path, clock, backend, RetryPolicy(lease_ms=100))
    captured = []
    original = first.retry_ledger.prepared_publication

    def crash(claim: object, proof: PublicationProof) -> None:
        original(claim, proof)  # type: ignore[arg-type]
        captured.append(claim)
        raise Crash("prepared intent before filesystem publication")

    monkeypatch.setattr(first.retry_ledger, "prepared_publication", crash)
    with pytest.raises(Crash):
        first.process(item, retry_mode="automatic")
    clock.now = 101
    second, _ = pipeline(tmp_path, clock, backend, RetryPolicy(lease_ms=100))
    assert second.process(item, retry_mode="automatic").status == "blocked"
    before = jobs(second), second.state.recent()
    clock.now = 0  # A clock change cannot restore that worker's already-fenced authority.
    with pytest.raises(StaleClaimError), first.retry_ledger.publication_guard(captured[0]):
        pytest.fail("held expired owner must remain fenced")
    assert (jobs(second), second.state.recent()) == before and backend.calls == 1
    assert not item.path.with_suffix(".mul.srt").exists()


def test_process_exit_after_native_link_recovers_without_backend_replay(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    script = """
import os, sys
from pathlib import Path
from test_retry import Backend, Clock, pipeline
from paircue.services import atomic
from paircue.services.retry import RetryPolicy
folder = Path(sys.argv[1])
backend = Backend()
backend.succeed = True
p, item = pipeline(folder, Clock(), backend, RetryPolicy(lease_ms=100))
final = item.path.with_suffix('.mul.srt')
native = atomic.os.link
def crash(source, destination):
    native(source, destination)
    if destination == final:
        (folder/'backend-count.txt').write_text(str(backend.calls))
        os._exit(73)
atomic.os.link = crash
p.process(item, retry_mode='automatic')
"""
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(("PAIRCUE_", "SUBDUET_"))}
    environment["PYTHONPATH"] = os.pathsep.join((str(root / "src"), str(root / "tests")))
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    # Execute only the literal test script above with this already-running Python.
    child = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script, str(tmp_path)], env=environment,
        capture_output=True, timeout=10,
    )
    assert child.returncode == 73, child.stderr.decode()
    assert (tmp_path / "backend-count.txt").read_text() == "1"
    clock, backend = Clock(), Backend()
    clock.now = 101
    restarted, item = pipeline(tmp_path, clock, backend, RetryPolicy(lease_ms=100))
    final = item.path.with_suffix(".mul.srt")
    before = final.read_bytes()
    result = restarted.process(item, retry_mode="automatic")
    assert result.status == "completed" and backend.calls == 0
    assert final.read_bytes() == before and jobs(restarted)[0][:2] == (1, 0)


def test_late_native_publication_and_competing_recovery_preserve_exact_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock, backend = Clock(), Backend()
    backend.succeed = True
    first, item = pipeline(tmp_path, clock, backend, RetryPolicy(lease_ms=100))
    second, _ = pipeline(tmp_path, clock, backend, RetryPolicy(lease_ms=100))
    linked, recovering, release = threading.Event(), threading.Event(), threading.Event()
    final, native_link = item.path.with_suffix(".mul.srt"), atomic.os.link
    native_recover = second.retry_ledger.recover_publication

    def late(source: Path, destination: Path) -> None:
        native_link(source, destination)
        if destination == final:
            clock.now = 101
            linked.set()
            assert release.wait(5)

    def recover(*args: object, **kwargs: object) -> ProcessResult | None:
        recovering.set()
        return native_recover(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(atomic.os, "link", late)
    monkeypatch.setattr(second.retry_ledger, "recover_publication", recover)
    with ThreadPoolExecutor(max_workers=2) as workers:
        old = workers.submit(first.process, item, retry_mode="automatic")
        assert linked.wait(5)
        successor = workers.submit(second.process, item, retry_mode="automatic")
        assert recovering.wait(5)
        release.set()
        old_result, new_result = old.result(timeout=5), successor.result(timeout=5)
    assert old_result.status == "blocked" and final in old_result.outputs
    assert "publication occurred" in old_result.message
    assert new_result.status == "completed" and second.state.status_for(item.path) == "completed"
    proof_json, status, observed = publications(second.state.database)[0]
    proof = PublicationProof.model_validate_json(proof_json)
    assert status == "completed" and set(json.loads(observed)) == {
        str(output.path) for output in proof.outputs
    }
    assert backend.calls == 1 and jobs(second)[0][:2] == (1, 0)
