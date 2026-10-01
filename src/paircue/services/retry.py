from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from paircue.models import ProcessResult
from paircue.services.publication import PublicationProof
from paircue.services.state import StateStore

RECIPE_VERSION = "library-staging-v1"
VALIDATION_VERSION = "pairing-readability-v1"
INPUT_ERROR_FINGERPRINT = "retry-input-freeze-v1:no-attempt"
InputHeadToken = tuple[str, int, str | None, int | None]
InputStateToken = tuple[InputHeadToken | None, tuple[str, str, str, str] | None]
AttemptKind = Literal["automatic", "manual"]


class RetryPolicy(BaseModel):
    """Opt-in additional automatic attempts; values are operational policy, not correctness."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    max_auto_attempts: int = Field(default=1, ge=1, le=10, strict=True)
    retry_delays_ms: tuple[int, ...] = (60_000, 300_000)
    lease_ms: int = Field(default=900_000, ge=1, le=86_400_000, strict=True)

    @field_validator("retry_delays_ms", mode="before")
    @classmethod
    def valid_delays(cls, value: object) -> object:
        if not isinstance(value, (list, tuple)) or not 1 <= len(value) <= 9:
            raise ValueError("provide between one and nine retry delays")
        if any(type(delay) is not int or not 0 <= delay <= 86_400_000 for delay in value):
            raise ValueError("retry delays must be nonnegative integer milliseconds")
        return value

    def delay(self, spent: int) -> int:
        return self.retry_delays_ms[min(max(spent - 1, 0), len(self.retry_delays_ms) - 1)]


def milliseconds() -> int:
    return time.time_ns() // 1_000_000


def content_hash(path: Path) -> str:
    if path.is_symlink():
        raise ValueError("job inputs must not be subtitle symlinks")
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def canonical_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def input_tracks(media: Path) -> dict[str, str]:
    tracks = {}
    for path in sorted(media.parent.glob(f"{media.stem}*.srt")):
        if path.name != f"{media.stem}.srt" and not path.name.startswith(f"{media.stem}."):
            continue
        label = path.name[len(media.stem) :]
        if label.lower() in {".mul.srt", ".bilingual.srt", ".zh-tw.cc.srt"}:
            continue
        tracks[label] = content_hash(path)
    return tracks


@dataclass(frozen=True, slots=True)
class JobInputs:
    media_hash: str
    tracks: dict[str, str]
    settings_hash: str

    @property
    def job_id(self) -> str:
        return canonical_hash(
            {
                "media": self.media_hash,
                "tracks": self.tracks,
                "settings": self.settings_hash,
                "recipe": RECIPE_VERSION,
                "validation": VALIDATION_VERSION,
            }
        )


@dataclass(frozen=True, slots=True)
class Claim:
    path: Path
    job_id: str
    fence: int
    owner: str
    kind: AttemptKind
    auto_count: int | None
    manual_count: int
    policy: RetryPolicy


@dataclass(frozen=True, slots=True)
class ClaimDecision:
    claim: Claim | None
    status: Literal["running", "retry_wait", "retry_exhausted", "blocked", "skipped"]
    message: str


class StaleClaimError(RuntimeError):
    pass


class RetryLedger:
    """SQLite claims fence final publication; no provider exactly-once guarantee."""

    def __init__(self, state: StateStore, clock: Callable[[], int] = milliseconds) -> None:
        self.state = state
        self.clock = clock
        with state._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS retry_job (
                    media_path TEXT NOT NULL, job_id TEXT NOT NULL, inputs_json TEXT NOT NULL,
                    derived_json TEXT NOT NULL DEFAULT '{}', auto_count INTEGER,
                    manual_count INTEGER NOT NULL DEFAULT 0, policy_json TEXT NOT NULL,
                    status TEXT NOT NULL, reason TEXT NOT NULL, next_retry_ms INTEGER,
                    PRIMARY KEY(job_id)
                );
                CREATE TABLE IF NOT EXISTS retry_head (
                    media_path TEXT PRIMARY KEY, job_id TEXT NOT NULL, fence INTEGER NOT NULL,
                    owner TEXT, lease_until_ms INTEGER
                );
                CREATE TABLE IF NOT EXISTS retry_attempt (
                    media_path TEXT NOT NULL, fence INTEGER NOT NULL, job_id TEXT NOT NULL,
                    kind TEXT NOT NULL, started_ms INTEGER NOT NULL, finished_ms INTEGER,
                    outcome TEXT, PRIMARY KEY(media_path, fence)
                );
                CREATE TABLE IF NOT EXISTS retry_legacy (media_hash TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS retry_publication (
                    media_path TEXT NOT NULL, fence INTEGER NOT NULL, job_id TEXT NOT NULL,
                    owner TEXT NOT NULL, head_fence INTEGER NOT NULL, proof_json TEXT NOT NULL,
                    status TEXT NOT NULL, observed_paths_json TEXT NOT NULL DEFAULT '[]',
                    PRIMARY KEY(media_path, fence)
                );
            """)

    def normalize_inputs(self, inputs: JobInputs) -> JobInputs:
        # Generated tracks are output, not new original inputs granting another allowance.
        # A path-local head is insufficient: the same recorded content may have moved.
        with self.state._connect() as connection:
            rows = connection.execute(
                "SELECT inputs_json, derived_json FROM retry_job "
                "WHERE json_extract(inputs_json, '$.media_hash')=?",
                (inputs.media_hash,),
            ).fetchall()
        candidates: dict[str, dict[str, str]] = {}
        for encoded_original, encoded_derived in rows:
            original, derived = json.loads(encoded_original), json.loads(encoded_derived)
            if not any(derived.get(label) == digest for label, digest in inputs.tracks.items()):
                continue
            tracks = {}
            for label, digest in inputs.tracks.items():
                if derived.get(label) == digest:
                    if label in original["tracks"]:
                        tracks[label] = original["tracks"][label]
                else:
                    tracks[label] = digest
            candidates[canonical_hash(tracks)] = tracks
        if not candidates:
            return inputs
        if len(candidates) != 1:
            raise ValueError("recorded derived-track identity is ambiguous; no new attempt granted")
        tracks = next(iter(candidates.values()))
        return JobInputs(inputs.media_hash, tracks, inputs.settings_hash)

    def claim(
        self, path: Path, inputs: JobInputs, policy: RetryPolicy, kind: AttemptKind = "automatic"
    ) -> ClaimDecision:
        now, name, job_id = self.clock(), str(path), inputs.job_id
        with self.state._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            head = connection.execute(
                "SELECT job_id, fence, owner, lease_until_ms FROM retry_head WHERE media_path=?",
                (name,),
            ).fetchone()
            job = connection.execute(
                "SELECT auto_count, manual_count, policy_json, status, "
                "next_retry_ms FROM retry_job WHERE job_id=?",
                (job_id,),
            ).fetchone()
            active = connection.execute(
                "SELECT media_path, fence, owner, lease_until_ms FROM retry_head "
                "WHERE job_id=? AND owner IS NOT NULL ORDER BY lease_until_ms DESC LIMIT 1",
                (job_id,),
            ).fetchone()
            if active and active[3] > now:
                return ClaimDecision(None, "skipped", "another worker holds this job claim")
            if not job:
                legacy = connection.execute(
                    "SELECT 1 FROM retry_legacy WHERE media_hash=?",
                    (inputs.media_hash,),
                ).fetchone() or (
                    not head
                    and connection.execute(
                        "SELECT 1 FROM media_state WHERE media_path=? AND fingerprint!=?",
                        (name, INPUT_ERROR_FINGERPRINT),
                    ).fetchone()
                )
                if legacy:
                    connection.execute(
                        "INSERT OR IGNORE INTO retry_legacy VALUES (?)", (inputs.media_hash,)
                    )
                auto_count = None if legacy else 0
                connection.execute(
                    "INSERT INTO retry_job VALUES (?, ?, ?, '{}', ?, 0, ?, 'pending', '', NULL)",
                    (
                        name,
                        job_id,
                        json.dumps(
                            {
                                "media_hash": inputs.media_hash,
                                "tracks": inputs.tracks,
                                "settings_hash": inputs.settings_hash,
                            }
                        ),
                        auto_count,
                        policy.model_dump_json(),
                    ),
                )
                job = (auto_count, 0, policy.model_dump_json(), "pending", None)
            auto, manual, encoded_policy, status, deadline = job
            frozen = RetryPolicy.model_validate_json(encoded_policy)
            fence = head[1] + 1 if head else 1
            if active:
                connection.execute(
                    "UPDATE retry_attempt SET finished_ms=?, outcome='lease_expired' "
                    "WHERE media_path=? AND fence=? AND outcome IS NULL",
                    (now, active[0], active[1]),
                )
                connection.execute(
                    "UPDATE retry_head SET fence=fence+1, owner=NULL, lease_until_ms=NULL "
                    "WHERE media_path=?",
                    (active[0],),
                )
                if auto is not None and kind == "automatic":
                    deadline = max(deadline or 0, active[3] + frozen.delay(auto))
            if head and head[2]:
                connection.execute(
                    "UPDATE retry_attempt SET finished_ms=?, outcome=? "
                    "WHERE media_path=? AND fence=? AND outcome IS NULL",
                    (now, "superseded" if head[0] != job_id else "lease_expired", name, head[1]),
                )
            if status == "publishing":
                message = "publication outcome uncertain; provenance recovery required"
                connection.execute(
                    "INSERT INTO retry_head VALUES (?, ?, 1, NULL, NULL) "
                    "ON CONFLICT(media_path) DO UPDATE SET job_id=excluded.job_id, "
                    "fence=fence+1, owner=NULL, lease_until_ms=NULL",
                    (name, job_id),
                )
                self._status(connection, name, job_id, "blocked", message)
                return ClaimDecision(None, "blocked", message)
            if kind == "automatic":
                if auto is None:
                    return self._deny(
                        connection,
                        name,
                        job_id,
                        "blocked",
                        "legacy_attempts_unknown; explicit manual retry required",
                    )
                if status == "completed":
                    connection.execute(
                        "INSERT INTO retry_head VALUES (?, ?, 1, NULL, NULL) "
                        "ON CONFLICT(media_path) DO UPDATE SET job_id=excluded.job_id, "
                        "fence=fence+1, owner=NULL, lease_until_ms=NULL",
                        (name, job_id),
                    )
                    self._status(
                        connection,
                        name,
                        job_id,
                        "blocked",
                        "previous attempt completed; explicit manual retry required",
                    )
                    return ClaimDecision(
                        None,
                        "blocked",
                        "previous attempt completed; explicit manual retry required",
                    )
                if auto >= frozen.max_auto_attempts:
                    return self._deny(
                        connection,
                        name,
                        job_id,
                        "retry_exhausted",
                        f"automatic attempts exhausted ({auto}/{frozen.max_auto_attempts})",
                    )
                if deadline is not None and now < deadline:
                    return self._deny(
                        connection,
                        name,
                        job_id,
                        "retry_wait",
                        f"waiting until {deadline} UTC epoch ms",
                        deadline,
                    )
                auto += 1
            else:
                manual += 1
            owner = uuid.uuid4().hex
            connection.execute(
                "INSERT INTO retry_head VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(media_path) DO UPDATE SET job_id=excluded.job_id, "
                "fence=excluded.fence, owner=excluded.owner, "
                "lease_until_ms=excluded.lease_until_ms",
                (name, job_id, fence, owner, now + frozen.lease_ms),
            )
            connection.execute(
                "UPDATE retry_job SET auto_count=?, manual_count=?, status='running', "
                "reason='', next_retry_ms=? WHERE job_id=?",
                (auto, manual, deadline, job_id),
            )
            connection.execute(
                "INSERT INTO retry_attempt VALUES (?, ?, ?, ?, ?, NULL, NULL)",
                (name, fence, job_id, kind, now),
            )
            self._status(connection, name, job_id, "running", f"{kind} attempt claimed")
            claim = Claim(path, job_id, fence, owner, kind, auto, manual, frozen)
            return ClaimDecision(claim, "running", f"{kind} attempt claimed")

    def _deny(
        self,
        connection: sqlite3.Connection,
        path: str,
        job_id: str,
        status: Literal["retry_wait", "retry_exhausted", "blocked"],
        message: str,
        deadline: int | None = None,
    ) -> ClaimDecision:
        connection.execute(
            "UPDATE retry_job SET status=?, reason=?, next_retry_ms=? WHERE job_id=?",
            (status, message, deadline, job_id),
        )
        # Invalidate an expired claim even if the replacement is waiting/exhausted.
        connection.execute(
            "INSERT INTO retry_head VALUES (?, ?, 1, NULL, NULL) "
            "ON CONFLICT(media_path) DO UPDATE SET job_id=excluded.job_id, "
            "fence=fence+1, owner=NULL, lease_until_ms=NULL",
            (path, job_id),
        )
        self._status(connection, path, job_id, status, message)
        return ClaimDecision(None, status, message)

    @staticmethod
    def _status(
        connection: sqlite3.Connection, path: str, fingerprint: str, status: str, message: str
    ) -> None:
        connection.execute(
            "INSERT INTO media_state VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(media_path) DO UPDATE SET fingerprint=excluded.fingerprint, "
            "status=excluded.status, message=excluded.message, "
            "updated_at=excluded.updated_at",
            (path, fingerprint, status, message[:1000], datetime.now(UTC).isoformat()),
        )

    @staticmethod
    def _input_state_token(connection: sqlite3.Connection, path: Path) -> InputStateToken:
        head = connection.execute(
            "SELECT job_id, fence, owner, lease_until_ms FROM retry_head WHERE media_path=?",
            (str(path),),
        ).fetchone()
        state = connection.execute(
            "SELECT fingerprint, status, message, updated_at FROM media_state WHERE media_path=?",
            (str(path),),
        ).fetchone()
        head_token = ((str(head[0]), int(head[1]),
                       str(head[2]) if head[2] is not None else None,
                       int(head[3]) if head[3] is not None else None) if head else None)
        state_token = ((str(state[0]), str(state[1]), str(state[2]), str(state[3]))
                       if state else None)
        return head_token, state_token

    def input_state_token(self, path: Path) -> InputStateToken:
        with self.state._connect() as connection:
            connection.execute("BEGIN")
            return self._input_state_token(connection, path)

    def hold_input_error(
        self, path: Path, token: InputStateToken, error: str,
    ) -> ProcessResult:
        message = "job inputs could not be frozen: " + error[:240]
        with self.state._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._input_state_token(connection, path)
            if current != token:
                return ProcessResult("blocked", message + "; newer job state kept")
            head, state = current
            if head and head[2] and (head[3] is None or int(head[3]) > self.clock()):
                return ProcessResult("blocked", message + "; active worker state kept")
            if head and head[2]:
                connection.execute(
                    "UPDATE retry_head SET fence=fence+1, owner=NULL, lease_until_ms=NULL "
                    "WHERE media_path=?",
                    (str(path),),
                )
                connection.execute(
                    "UPDATE retry_publication SET head_fence=? "
                    "WHERE media_path=? AND job_id=? AND head_fence=?",
                    (int(head[1]) + 1, str(path), head[0], head[1]),
                )
            # Keep full cue diagnostics untouched; summarize their categories separately
            # from the current input error so repeated polls cannot hide either category.
            counts = dict(connection.execute(
                "SELECT e.category, COUNT(*) FROM review_entry e JOIN media_review r "
                "ON r.review_id=e.review_id WHERE r.media_path=? GROUP BY e.category",
                (str(path),),
            ).fetchall())
            if counts.get("source_only", 0) or counts.get("target_only", 0):
                message += ("; previous output review: partial timing pairing "
                            f"({counts.get('source_only', 0)} source-only, "
                            f"{counts.get('target_only', 0)} target-only cues)")
            if counts.get("readability", 0):
                message += f"; previous output Readability review: {counts['readability']} issues"
            # A new pre-claim input failure is known to have spent no attempt. Keep it
            # distinct from legacy state whose unknown allowance must never become zero.
            fingerprint = (state[0] if state else head[0] if head else INPUT_ERROR_FINGERPRINT)
            self._status(connection, str(path), fingerprint, "blocked", message)
        return ProcessResult("blocked", message)

    def hold_existing_output(self, path: Path) -> ProcessResult:
        with self.state._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT fingerprint, message FROM media_state WHERE media_path=?", (str(path),)
            ).fetchone()
            previous = str(row[1]) if row and "review" in row[1].lower() else ""
            message = "existing output kept unverified; publication provenance is unavailable"
            if previous and not previous.startswith(message):
                message += "; " + previous
            elif previous:
                message = previous
            connection.execute(
                "UPDATE retry_head SET fence=fence+1, owner=NULL, lease_until_ms=NULL "
                "WHERE media_path=?",
                (str(path),),
            )
            self._status(
                connection,
                str(path),
                str(row[0]) if row else "legacy-unverified",
                "blocked",
                message,
            )
        return ProcessResult("blocked", message)

    def pending_publication(self, path: Path) -> ProcessResult | None:
        with self.state._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT j.status, h.owner, h.lease_until_ms FROM retry_head h "
                "JOIN retry_job j ON h.job_id=j.job_id "
                "WHERE h.media_path=?",
                (str(path),),
            ).fetchone()
            if not row or row[0] != "publishing":
                return None
            if row[1] and row[2] > self.clock():
                return ProcessResult("skipped", "another worker is publishing this job")
            message = (
                "publication outcome uncertain; existing files kept; provenance recovery required"
            )
            connection.execute(
                "UPDATE retry_head SET fence=fence+1, owner=NULL, lease_until_ms=NULL "
                "WHERE media_path=?",
                (str(path),),
            )
            self._status(connection, str(path), "publication-uncertain", "blocked", message)
            return ProcessResult("blocked", message)

    def recover_publication(
        self, path: Path, inputs: JobInputs, tracks: dict[str, str], kind: AttemptKind,
        verify_current: Callable[[], bool],
    ) -> ProcessResult | None:
        with self.state._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            head = connection.execute(
                "SELECT job_id, fence, owner, lease_until_ms FROM retry_head WHERE media_path=?",
                (str(path),),
            ).fetchone()
            if not head:
                return None
            row = connection.execute(
                "SELECT fence, owner, proof_json, status FROM retry_publication "
                "WHERE media_path=? AND job_id=? AND head_fence=?",
                (str(path), head[0], head[1]),
            ).fetchone()
            if not row:
                return None  # Legacy intents never acquire proof from filename existence.
            if head[2] and head[3] is not None and head[3] > self.clock():
                return ProcessResult("skipped", "another worker is publishing this job")
            message = "publication proof incomplete or mismatched; existing files kept"
            try:
                proof = PublicationProof.model_validate_json(row[2])
                valid = (
                    proof.valid_location(path) and proof.job_id == head[0] == inputs.job_id
                    and proof.media_hash == inputs.media_hash
                    and proof.settings_hash == inputs.settings_hash
                    and proof.recipe_version == RECIPE_VERSION
                    and proof.validation_version == VALIDATION_VERSION
                    and JobInputs(proof.media_hash, proof.original_tracks,
                                  proof.settings_hash).job_id == proof.job_id
                )
                # Explicit retry can rebuild an archived final from a completed attempt.
                # It is a new manual attempt, never adoption of a missing or changed file.
                if (valid and row[3] == "completed" and kind == "manual"
                        and proof.final_path is not None and not proof.final_path.exists()
                        and not proof.final_path.is_symlink()):
                    return None
                valid = (valid and bool(proof.outputs) and tracks == proof.expected_tracks
                         and all(output.matches() for output in proof.outputs) and verify_current())
            except (ValueError, OSError, TypeError):
                valid = False
            if not valid:
                if head[2]:
                    # An expired owner must remain fenced even if the wall clock moves back.
                    # Keep this intent associated with the held head for later verification.
                    connection.execute(
                        "UPDATE retry_head SET fence=fence+1, owner=NULL, lease_until_ms=NULL "
                        "WHERE media_path=?",
                        (str(path),),
                    )
                    connection.execute(
                        "UPDATE retry_publication SET head_fence=? "
                        "WHERE media_path=? AND fence=?",
                        (head[1] + 1, str(path), row[0]),
                    )
                self._status(connection, str(path), str(head[0]), "blocked", message)
                return ProcessResult("blocked", message)
            result = proof.result()
            fence = head[1] + 1 if head[2] or row[3] != "completed" else head[1]
            connection.execute(
                "UPDATE retry_head SET fence=?, owner=NULL, lease_until_ms=NULL WHERE media_path=?",
                (fence, str(path)),
            )
            connection.execute(
                "UPDATE retry_publication SET status='completed', head_fence=? "
                "WHERE media_path=? AND fence=?",
                (fence, str(path), row[0]),
            )
            connection.execute(
                "UPDATE retry_job SET status='completed', reason=?, next_retry_ms=NULL "
                "WHERE job_id=?",
                (result.message[:1000], inputs.job_id),
            )
            if row[3] != "completed":
                connection.execute(
                    "UPDATE retry_attempt SET finished_ms=?, outcome='recovered_completed' "
                    "WHERE media_path=? AND fence=?",
                    (self.clock(), str(path), row[0]),
                )
            connection.execute(
                "UPDATE retry_publication SET observed_paths_json=? "
                "WHERE media_path=? AND fence=?",
                (json.dumps([str(output.path) for output in proof.outputs]), str(path), row[0]),
            )
            self.state._record(connection, path, inputs.job_id, result.status, result.message,
                               review_details=result.review_details)
            return result

    def begin_publication(
        self, claim: Claim, derived: dict[str, str], proof: PublicationProof | None = None,
    ) -> None:
        with self.publication_guard(claim) as connection:
            connection.execute(
                "UPDATE retry_job SET status='publishing', derived_json=? WHERE job_id=?",
                (json.dumps(derived), claim.job_id),
            )
            if proof is not None:
                connection.execute(
                    "INSERT INTO retry_publication VALUES (?, ?, ?, ?, ?, ?, 'intent', '[]')",
                    (str(claim.path), claim.fence, claim.job_id, claim.owner, claim.fence,
                     proof.model_dump_json()),
                )
                self.state._record(connection, claim.path, claim.job_id, "publishing",
                                   "publication pending; " + proof.message,
                                   review_details=proof.result().review_details)

    def prepared_publication(self, claim: Claim, proof: PublicationProof) -> None:
        with self.publication_guard(claim) as connection:
            updated = connection.execute(
                "UPDATE retry_publication SET proof_json=?, status='prepared' "
                "WHERE media_path=? AND fence=? AND owner=? AND job_id=?",
                (proof.model_dump_json(), str(claim.path), claim.fence, claim.owner, claim.job_id),
            )
            if updated.rowcount != 1:
                raise StaleClaimError("publication intent is unavailable")

    def stop_publication(
        self, claim: Claim, *, publication_occurred: bool = False, published: tuple[Path, ...] = (),
    ) -> None:
        # Record only this attempt's observed paths even if a successor already took the
        # head. This audit update cannot change that successor or authorize publication.
        if published:
            with self.state._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT observed_paths_json FROM retry_publication "
                    "WHERE media_path=? AND fence=? AND owner=? AND job_id=?",
                    (str(claim.path), claim.fence, claim.owner, claim.job_id),
                ).fetchone()
                observed = json.loads(row[0]) if row else []
                observed = list(dict.fromkeys([*observed, *(str(path) for path in published)]))
                connection.execute(
                    "UPDATE retry_publication SET observed_paths_json=? "
                    "WHERE media_path=? AND fence=? AND owner=? AND job_id=?",
                    (json.dumps(observed), str(claim.path), claim.fence,
                     claim.owner, claim.job_id),
                )
        with self.state._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            # Expiry must not hide a stopped publication or an observed filesystem effect.
            # Only the unchanged owner/fence may record a blocked outcome, never success
            # or a change to a successor's state. This check cannot authorize file writes.
            self.check_claim(connection, claim, require_lease=False)
            connection.execute(
                "UPDATE retry_publication SET head_fence=? "
                "WHERE media_path=? AND fence=? AND owner=? AND job_id=?",
                (claim.fence + 1, str(claim.path), claim.fence, claim.owner, claim.job_id),
            )
            connection.execute(
                "UPDATE retry_head SET fence=fence+1, owner=NULL, lease_until_ms=NULL "
                "WHERE media_path=?",
                (str(claim.path),),
            )
            self._status(
                connection,
                str(claim.path),
                claim.job_id,
                "blocked",
                ("publication occurred; " if publication_occurred else "")
                + "publication outcome uncertain; files kept; provenance recovery required",
            )
            connection.execute(
                "UPDATE retry_attempt SET finished_ms=?, outcome='blocked_publication' "
                "WHERE media_path=? AND fence=?",
                (self.clock(), str(claim.path), claim.fence),
            )

    @contextmanager
    def publication_guard(self, claim: Claim) -> Iterator[sqlite3.Connection]:
        with self.state._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self.check_claim(connection, claim)
            yield connection

    def check_claim(
        self, connection: sqlite3.Connection, claim: Claim, *, require_lease: bool = True,
    ) -> None:
        row = connection.execute(
            "SELECT job_id, fence, owner, lease_until_ms FROM retry_head WHERE media_path=?",
            (str(claim.path),),
        ).fetchone()
        if (
            not row
            or tuple(row[:3]) != (claim.job_id, claim.fence, claim.owner)
            or (require_lease and (row[3] is None or row[3] <= self.clock()))
        ):
            raise StaleClaimError("worker claim expired or was superseded")

    def finish(
        self,
        connection: sqlite3.Connection,
        claim: Claim,
        result: ProcessResult,
        derived: dict[str, str] | None = None,
    ) -> ProcessResult:
        self.check_claim(connection, claim)
        now = self.clock()
        status, message, deadline = result.status, result.message, None
        if result.status == "failed":
            if claim.auto_count is None:
                status = "blocked"
                message = "legacy_attempts_unknown; manual attempt failed: " + message
            elif claim.auto_count >= claim.policy.max_auto_attempts:
                status = "retry_exhausted"
                message = f"automatic attempts exhausted ({claim.auto_count}/" + (
                    f"{claim.policy.max_auto_attempts}); {message}"
                )
            else:
                status = "retry_wait"
                prior = connection.execute(
                    "SELECT next_retry_ms FROM retry_job WHERE job_id=?",
                    (claim.job_id,),
                ).fetchone()[0]
                deadline = (
                    now + claim.policy.delay(claim.auto_count)
                    if claim.kind == "automatic"
                    else max(prior or now, now)
                )
        if derived is None:
            derived = json.loads(
                connection.execute(
                    "SELECT derived_json FROM retry_job WHERE job_id=?",
                    (claim.job_id,),
                ).fetchone()[0]
            )
        connection.execute(
            "UPDATE retry_job SET status=?, reason=?, next_retry_ms=?, derived_json=? "
            "WHERE job_id=?",
            (
                status,
                message[:1000],
                deadline,
                json.dumps(derived or {}),
                claim.job_id,
            ),
        )
        connection.execute(
            "UPDATE retry_head SET owner=NULL, lease_until_ms=NULL WHERE media_path=?",
            (str(claim.path),),
        )
        connection.execute(
            "UPDATE retry_attempt SET finished_ms=?, outcome=? WHERE media_path=? AND fence=?",
            (now, status, str(claim.path), claim.fence),
        )
        self.state._record(
            connection,
            claim.path,
            claim.job_id,
            status,
            message,
            review_details=result.review_details,
        )
        row = connection.execute(
            "SELECT proof_json FROM retry_publication WHERE media_path=? AND fence=?",
            (str(claim.path), claim.fence),
        ).fetchone()
        if row and status == "completed":
            proof = PublicationProof.model_validate_json(row[0])
            connection.execute(
                "UPDATE retry_publication SET status='completed', observed_paths_json=? "
                "WHERE media_path=? AND fence=? AND owner=? AND job_id=?",
                (json.dumps([str(output.path) for output in proof.outputs]), str(claim.path),
                 claim.fence, claim.owner, claim.job_id),
            )
        return ProcessResult(status, message, result.outputs, result.review_details)
