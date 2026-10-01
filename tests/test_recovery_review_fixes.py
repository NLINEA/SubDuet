from __future__ import annotations

import json
import os
import sqlite3
import stat
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
from paircue.services import atomic, publication
from paircue.services.publication import OutputProof
from paircue.services.retry import RetryPolicy, canonical_hash
from paircue.services.subtitle_files import write_srt


def own_output(folder: Path) -> OutputProof:
    path, content = folder / "own.srt", b"Owned synthetic bytes, unchanged."
    with atomic.prepare_write_bytes(path, content) as ready:
        proof = OutputProof.planned(path, content, False).prepared(ready)
        atomic.atomic_write_bytes(path, content, overwrite=False, prepared=ready)
    return proof


def substitute(path: Path, kind: str) -> Path:
    archived = path.with_name("kept-original.srt")
    path.rename(archived)
    if kind == "symlink":
        path.symlink_to(archived)
    elif kind == "fifo":
        os.mkfifo(path)
    else:
        path.write_bytes(b"Unrelated synthetic file; do not read or change.")
    return archived


@pytest.mark.parametrize("kind", ["regular", "symlink", "fifo"])
def test_static_output_type_is_checked_before_any_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str,
) -> None:
    proof = own_output(tmp_path)
    before = proof.path.read_bytes()
    archived = substitute(proof.path, kind) if kind != "regular" else proof.path
    opened = []
    native = publication.os.open

    def observe(path: Path, flags: int) -> int:
        opened.append((path, flags))
        return native(path, flags)

    monkeypatch.setattr(publication.os, "open", observe)
    assert proof.matches() == (kind == "regular")
    assert bool(opened) == (kind == "regular")
    assert archived.read_bytes() == before


def test_fifo_with_matching_identity_metadata_still_refuses_before_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = own_output(tmp_path)
    substitute(proof.path, "fifo")
    metadata = proof.path.lstat()
    proof = proof.model_copy(update={"device": metadata.st_dev, "inode": metadata.st_ino,
                                     "size": metadata.st_size})

    def no_open(*args: object) -> None:
        pytest.fail("file type must be regular even when identity metadata matches")

    monkeypatch.setattr(publication.os, "open", no_open)
    assert not proof.matches()


def test_descriptor_type_is_verified_even_when_path_metadata_claims_regular(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fifo = tmp_path / "recovery.srt"
    os.mkfifo(fifo)
    metadata = fifo.lstat()
    proof = OutputProof.planned(fifo, b"", False).model_copy(update={
        "device": metadata.st_dev, "inode": metadata.st_ino,
    })
    fields = list(metadata)
    fields[0] = stat.S_IFREG | 0o644  # Simulate stale/racy path metadata, not descriptor metadata.
    monkeypatch.setattr(Path, "lstat", lambda path: os.stat_result(fields))

    def no_read(*args: object) -> None:
        pytest.fail("actual FIFO descriptor must be rejected before hashing")

    monkeypatch.setattr(publication.hashlib, "file_digest", no_read)
    assert not proof.matches()


@pytest.mark.parametrize("kind", ["symlink", "fifo", "unrelated"])
def test_type_identity_change_between_lstat_and_open_never_reads_substitute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str,
) -> None:
    proof = own_output(tmp_path)
    before = proof.path.read_bytes()
    native, descriptors = publication.os.open, []

    def race(path: Path, flags: int) -> int:
        assert flags & os.O_NONBLOCK
        substitute(path, kind)
        descriptor = native(path, flags)
        descriptors.append(descriptor)
        return descriptor

    def no_read(*args: object, **kwargs: object) -> None:
        pytest.fail("substituted FIFO/symlink/foreign file must be rejected before hashing")

    monkeypatch.setattr(publication.os, "open", race)
    monkeypatch.setattr(publication.hashlib, "file_digest", no_read)
    assert not proof.matches()
    assert (tmp_path / "kept-original.srt").read_bytes() == before
    for descriptor in descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)  # Every rejection closes the opened descriptor.


@pytest.mark.parametrize("kind", ["symlink", "fifo", "unrelated"])
def test_path_substitution_after_open_only_hashes_original_regular_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str,
) -> None:
    proof = own_output(tmp_path)
    before = proof.path.read_bytes()
    native, descriptors = publication.os.open, []

    def race(path: Path, flags: int) -> int:
        descriptor = native(path, flags)
        descriptors.append(descriptor)
        substitute(path, kind)
        return descriptor

    monkeypatch.setattr(publication.os, "open", race)
    assert not proof.matches()  # The opened own file is still regular; current path differs.
    assert (tmp_path / "kept-original.srt").read_bytes() == before
    for descriptor in descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)


def test_missing_nonblocking_support_fails_closed_without_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = own_output(tmp_path)
    before = proof.path.read_bytes()
    monkeypatch.delattr(publication.os, "O_NONBLOCK")

    def no_open(*args: object, **kwargs: object) -> None:
        pytest.fail("cannot safely open when nonblocking support is unavailable")

    monkeypatch.setattr(publication.os, "open", no_open)
    assert not proof.matches() and proof.path.read_bytes() == before


@pytest.mark.parametrize("target", ["owned", "unrelated"])
def test_symlink_race_without_no_follow_still_rejects_and_keeps_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str,
) -> None:
    proof = own_output(tmp_path)
    before = proof.path.read_bytes()
    unrelated = tmp_path / "unrelated.srt"
    unrelated.write_bytes(b"Unrelated synthetic data")
    native = publication.os.open
    monkeypatch.delattr(publication.os, "O_NOFOLLOW")

    def race(path: Path, flags: int) -> int:
        kept = path.with_name("kept-original.srt")
        path.rename(kept)
        path.symlink_to(kept if target == "owned" else unrelated)
        return native(path, flags)

    monkeypatch.setattr(publication.os, "open", race)
    if target == "unrelated":
        def no_read(*args: object) -> None:
            pytest.fail("foreign symlink target must fail descriptor identity before hashing")
        monkeypatch.setattr(publication.hashlib, "file_digest", no_read)
    assert not proof.matches()
    assert (tmp_path / "kept-original.srt").read_bytes() == before
    assert unrelated.read_bytes() == b"Unrelated synthetic data"


def test_fifo_recovery_returns_and_releases_sqlite_writer_lock(tmp_path: Path) -> None:
    clock, backend = Clock(), Backend()
    backend.succeed = True
    p, item = pipeline(tmp_path, clock, backend)
    assert p.process(item, retry_mode="automatic").status == "completed"
    final = item.path.with_suffix(".mul.srt")
    before = final.read_bytes()
    archived = substitute(final, "fifo")
    root = Path(__file__).resolve().parents[1]
    script = """
import json, socket, sqlite3, sys, time
from pathlib import Path
from test_retry import Backend, Clock, pipeline, jobs
def deny(*args, **kwargs): raise AssertionError('no network')
socket.socket.connect = socket.create_connection = deny
backend = Backend()
p, item = pipeline(Path(sys.argv[1]), Clock(), backend)
started = time.monotonic()
r = p.process(item, retry_mode='automatic')
elapsed = time.monotonic() - started
with sqlite3.connect(p.state.database, timeout=0) as writer:
    writer.execute('BEGIN IMMEDIATE')
print(json.dumps({'returned':r.status, 'stored':p.state.status_for(item.path),
    'recent':p.state.recent()[0].status,'calls':backend.calls,'counts':jobs(p)[0][:2],
    'writer_released':True,'elapsed_seconds':elapsed}))
"""
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(("PAIRCUE_", "SUBDUET_"))}
    environment.update(PYTHONPATH=os.pathsep.join((str(root / "src"), str(root / "tests"))),
                       PYTHONDONTWRITEBYTECODE="1")
    # Literal local test code in the current Python, with a hard timeout for this regression.
    child = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script, str(tmp_path)], env=environment,
        capture_output=True, timeout=3,
    )
    assert child.returncode == 0, child.stderr.decode()
    result = json.loads(child.stdout)
    assert result.pop("elapsed_seconds") < 3
    assert result == {
        "returned": "blocked", "stored": "blocked", "recent": "blocked", "calls": 0,
        "counts": [1, 0], "writer_released": True,
    }
    with sqlite3.connect(p.state.database, timeout=0) as connection:
        connection.execute("BEGIN IMMEDIATE")  # Prompt failure must release the writer guard.
    assert archived.read_bytes() == before
    assert final.lstat().st_ino != archived.stat().st_ino and backend.calls == 1


def review_pair(folder: Path, persistent: bool = True):
    clock, backend = Clock(), Backend()
    p, item = pipeline(folder, clock, backend)
    source = [srt.Subtitle(i + 1, timedelta(seconds=i * 3 + 1), timedelta(seconds=i * 3 + 3),
                          "A" * 48 if i == 3 else f"Source {i}") for i in range(4)]
    target = [srt.Subtitle(cue.index, cue.start, cue.end, "中文") for cue in source[:3]]
    write_srt(item.path.with_suffix(".en.srt"), source)
    write_srt(item.path.with_suffix(".zh-TW.srt"), target)
    assert p.process(item, retry_mode="automatic" if persistent else None).status == "completed"
    return p, item, clock, backend


@pytest.mark.parametrize("error", ["symlink", "oserror", "valueerror"])
def test_completed_to_input_error_persists_hold_and_retains_review_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: str,
) -> None:
    p, item, clock, backend = review_pair(tmp_path)
    stored, = p.state.recent()
    assert stored.review_id is not None
    details = p.state.review_page(stored.review_id)
    before = {path: path.read_bytes() for path in tmp_path.glob("Synthetic.*")}
    restarted, _ = pipeline(tmp_path, clock, backend)
    source = item.path.with_suffix(".en.srt")
    if error == "symlink":
        substitute(source, "symlink")
    else:
        exception = OSError("synthetic input read error") if error == "oserror" else ValueError(
            "synthetic invalid input",
        )

        def fail(*args: object) -> None:
            raise exception

        monkeypatch.setattr(restarted, "_job_inputs", fail)
    for mode in ("automatic", "manual", "automatic"):
        result = restarted.process(item, retry_mode=mode)  # type: ignore[arg-type]
        assert result.status == restarted.state.status_for(item.path) == "blocked"
        recent, = restarted.state.recent()
        assert recent.status == "blocked" and recent.message == result.message
        assert recent.message.startswith("job inputs could not be frozen:")
        assert "partial timing pairing" in recent.message and "Readability review" in recent.message
        assert len(recent.message) < 1000 and recent.review_id == stored.review_id
        assert restarted.state.review_page(stored.review_id) == details
    assert backend.calls == 0 and jobs(restarted)[0][:2] == (1, 0)
    assert all(path.read_bytes() == content for path, content in before.items())
    if error == "symlink":
        source.unlink()
        (tmp_path / "kept-original.srt").rename(source)
    else:
        monkeypatch.undo()
    assert restarted.process(item, retry_mode="automatic").status == "completed"
    assert restarted.state.review_page(stored.review_id) == details
    assert backend.calls == 0 and jobs(restarted)[0][:2] == (1, 0)


def test_legacy_completed_state_input_error_keeps_diagnostics_without_minting_job(
    tmp_path: Path,
) -> None:
    p, item, clock, backend = review_pair(tmp_path, persistent=False)
    stored, = p.state.recent()
    assert stored.review_id is not None
    details = p.state.review_page(stored.review_id)
    substitute(item.path.with_suffix(".en.srt"), "symlink")
    restarted, _ = pipeline(tmp_path, clock, backend)
    assert restarted.process(item, retry_mode="automatic").status == "blocked"
    assert restarted.state.status_for(item.path) == restarted.state.recent()[0].status == "blocked"
    assert restarted.state.review_page(stored.review_id) == details
    assert jobs(restarted) == [] and backend.calls == 0


def test_first_input_error_does_not_invent_legacy_unknown_attempts_after_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = Backend()
    backend.succeed = True
    p, item = pipeline(tmp_path, Clock(), backend)

    def fail(*args: object) -> None:
        raise ValueError("synthetic first input-freezing failure, before any claim")

    with monkeypatch.context() as seam:
        seam.setattr(p, "_job_inputs", fail)
        for mode in ("automatic", "manual", "automatic"):
            assert p.process(item, retry_mode=mode).status == "blocked"  # type: ignore[arg-type]
        assert p.state.status_for(item.path) == "blocked" and jobs(p) == [] and backend.calls == 0
    assert p.process(item, retry_mode="automatic").status == "completed"
    assert jobs(p)[0][:2] == (1, 0) and backend.calls == 1


def test_input_error_hold_fences_expired_publisher_and_keeps_its_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock, backend = Clock(), Backend()
    backend.succeed = True
    first, item = pipeline(tmp_path, clock, backend, RetryPolicy(lease_ms=100))
    second, _ = pipeline(tmp_path, clock, backend, RetryPolicy(lease_ms=100))
    entered, release = threading.Event(), threading.Event()
    native = first.retry_ledger.prepared_publication

    def pause(*args: object) -> None:
        native(*args)  # type: ignore[arg-type]
        entered.set()
        assert release.wait(5)

    def fail(*args: object) -> None:
        raise ValueError("synthetic input error observed while the other claim expires")

    monkeypatch.setattr(first.retry_ledger, "prepared_publication", pause)
    monkeypatch.setattr(second, "_job_inputs", fail)
    with ThreadPoolExecutor(max_workers=2) as workers:
        pending = workers.submit(first.process, item, retry_mode="automatic")
        assert entered.wait(5)
        clock.now = 101
        assert second.process(item, retry_mode="automatic").status == "blocked"
        before = second.state.recent()
        clock.now = 0  # A clock rollback cannot revive the owner fenced by this hold.
        release.set()
        assert pending.result(timeout=5).status == "blocked"
    assert second.state.recent() == before and backend.calls == 1
    assert jobs(second)[0][:2] == (1, 0) and not item.path.with_suffix(".mul.srt").exists()
    with sqlite3.connect(second.state.database) as connection:
        fence, owner = connection.execute("SELECT fence, owner FROM retry_head").fetchone()
        proof_fence, = connection.execute("SELECT head_fence FROM retry_publication").fetchone()
    assert fence == proof_fence and owner is None


@pytest.mark.parametrize("successor", ["new_head", "new_state", "live_owner"])
def test_input_error_hold_cannot_replace_newer_or_active_worker_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, successor: str,
) -> None:
    p, item, _, backend = review_pair(tmp_path)
    original_inputs = p._job_inputs(item, item.path)
    next_inputs = replace(original_inputs, settings_hash=canonical_hash({"different": "settings"}))
    before = []

    def advance() -> None:
        if successor in {"new_head", "live_owner"}:
            claim = p.retry_ledger.claim(item.path, next_inputs, p.retry_policy).claim
            assert claim is not None
            if successor == "new_head":
                with p.retry_ledger.publication_guard(claim) as connection:
                    p.retry_ledger.finish(connection, claim,
                                         ProcessResult("completed", "successor completed"))
        else:
            p.state.record(item.path, "newer-state", "completed", "newer completed result")
        before.append((p.state.recent(), jobs(p), p.retry_ledger.input_state_token(item.path)))

    if successor == "live_owner":
        advance()  # The token already contains a different live worker's claim.

    def fail(*args: object) -> None:
        if successor != "live_owner":
            advance()  # A successor arrives while the caller freezes inputs.
        raise ValueError("synthetic frozen-input failure")

    monkeypatch.setattr(p, "_job_inputs", fail)
    result = p.process(item, retry_mode="automatic")
    assert result.status == "blocked" and "state kept" in result.message
    assert (p.state.recent(), jobs(p), p.retry_ledger.input_state_token(item.path)) == before[-1]
    assert backend.calls == 0
