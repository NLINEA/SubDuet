from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from paircue.services.review import ReviewDetails, ReviewEntry, ReviewPage


@dataclass(frozen=True, slots=True)
class RecentMediaState:
    media_name: str
    status: str
    message: str
    updated_at: str
    review_id: str | None = None


class StateStore:
    def __init__(self, database: Path) -> None:
        self.database = database
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=30)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS media_state (
                    media_path TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL,
                    status TEXT NOT NULL,
                    message TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS media_review ("
                "media_path TEXT PRIMARY KEY, review_id TEXT UNIQUE NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS review_entry ("
                "review_id TEXT NOT NULL, ordinal INTEGER NOT NULL, category TEXT NOT NULL, "
                "output_cue INTEGER NOT NULL, metric TEXT NOT NULL, measured INTEGER, "
                "limit_value INTEGER, PRIMARY KEY(review_id, ordinal))"
            )

    def record(
        self, media_path: Path, fingerprint: str, status: str, message: str = "", *,
        review_details: ReviewDetails | None = None,
    ) -> None:
        with self._connect() as connection:
            self._record(connection, media_path, fingerprint, status, message,
                         review_details=review_details)

    def _record(
        self, connection: sqlite3.Connection, media_path: Path, fingerprint: str,
        status: str, message: str = "", *, review_details: ReviewDetails | None = None,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        review_id = hashlib.sha256(str(media_path).encode()).hexdigest()
        connection.execute(
            """
            INSERT INTO media_state(media_path, fingerprint, status, message, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(media_path) DO UPDATE SET
                fingerprint = excluded.fingerprint,
                status = excluded.status,
                message = excluded.message,
                updated_at = excluded.updated_at
            """,
            (str(media_path), fingerprint, status, message[:1000], now),
        )
        # Details use one fixed-shape row per diagnostic, bounded by validated SRT cues.
        # Replace atomically with the current result; never pack them into the summary.
        connection.execute("DELETE FROM review_entry WHERE review_id = ?", (review_id,))
        connection.execute("DELETE FROM media_review WHERE media_path = ?", (str(media_path),))
        entries = review_details.entries() if review_details else ()
        if entries:
            connection.execute("INSERT INTO media_review VALUES (?, ?)",
                               (str(media_path), review_id))
            connection.executemany(
                "INSERT INTO review_entry VALUES (?, ?, ?, ?, ?, ?, ?)",
                ((review_id, index, entry.category, entry.output_cue, entry.metric,
                  entry.measured, entry.limit) for index, entry in enumerate(entries)),
            )

    def review_page(self, review_id: str, offset: int = 0, limit: int = 100) -> ReviewPage | None:
        offset = max(0, offset)
        limit = max(1, min(limit, 100))
        with self._connect() as connection:
            exists = connection.execute("SELECT 1 FROM media_review WHERE review_id = ?",
                                        (review_id,)).fetchone()
            if not exists:
                return None
            counts = dict(connection.execute(
                "SELECT category, COUNT(*) FROM review_entry WHERE review_id = ? GROUP BY category",
                (review_id,),
            ).fetchall())
            rows = connection.execute(
                "SELECT category, output_cue, metric, measured, limit_value FROM review_entry "
                "WHERE review_id = ? ORDER BY ordinal LIMIT ? OFFSET ?",
                (review_id, limit, offset),
            ).fetchall()
        return ReviewPage(counts, sum(counts.values()), offset,
                          tuple(ReviewEntry(*row) for row in rows))

    def status_for(self, media_path: Path) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT status FROM media_state WHERE media_path = ?", (str(media_path),)
            ).fetchone()
        return str(row[0]) if row else None

    def summary(self) -> dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) FROM media_state GROUP BY status ORDER BY status"
            ).fetchall()
        return {str(status): int(count) for status, count in rows}

    def recent(self, limit: int = 20) -> tuple[RecentMediaState, ...]:
        bounded_limit = max(1, min(limit, 100))
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT s.media_path, status, message, updated_at, r.review_id
                FROM media_state s LEFT JOIN media_review r ON s.media_path = r.media_path
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (bounded_limit,),
            ).fetchall()
        recent: list[RecentMediaState] = []
        for media_path, status, message, updated_at, review_id in rows:
            path = Path(str(media_path))
            safe_message = str(message).replace(str(path), path.name)
            for separator in ("/", "\\"):
                safe_message = safe_message.replace(f"{path.parent}{separator}", "")
            recent.append(
                RecentMediaState(
                    media_name=path.name,
                    status=str(status),
                    message=safe_message,
                    updated_at=str(updated_at),
                    review_id=review_id,
                )
            )
        return tuple(recent)


def media_fingerprint(path: Path) -> str:
    stat = path.stat()
    return f"{stat.st_size}:{stat.st_mtime_ns}"
