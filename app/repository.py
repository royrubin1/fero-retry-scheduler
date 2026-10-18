"""SQLite storage for jobs, including the atomic claim that keeps workers from colliding.

Every method opens its own short-lived connection, so the API threads and any
number of worker processes can share one database file.
"""
import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from app.models import JobStatus
from app.retry_policy import Decision

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id            TEXT PRIMARY KEY,
    url           TEXT NOT NULL,
    payload       TEXT NOT NULL,     -- JSON
    status        TEXT NOT NULL,
    attempts      INTEGER NOT NULL DEFAULT 0,
    next_retry_at TEXT,              -- NULL means due now
    locked_until  TEXT,              -- lease of the worker running it
    last_error    TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_due ON jobs (status, next_retry_at);
"""

# One statement, so the SELECT and the UPDATE run under the same write lock:
# SQLite has a single writer and a write statement takes the lock before it reads,
# so two workers can't both see the same job as free.
# A `running` job whose lease ran out belongs to a worker that crashed, so it is free again.
CLAIM = """
UPDATE jobs
SET status = 'running', attempts = attempts + 1, next_retry_at = NULL,
    locked_until = :locked_until, updated_at = :now
WHERE id = (
    SELECT id FROM jobs
    WHERE (status = 'pending' AND (next_retry_at IS NULL OR next_retry_at <= :now))
       OR (status = 'running' AND locked_until <= :now)
    ORDER BY created_at
    LIMIT 1
)
RETURNING *
"""

# `attempts` doubles as a fencing token: every claim bumps it, so a worker whose
# lease expired (and whose job was claimed again) can no longer overwrite the result.
FINISH = """
UPDATE jobs
SET status = :status, next_retry_at = :next_retry_at, locked_until = NULL,
    last_error = :last_error, updated_at = :now
WHERE id = :id AND status = 'running' AND attempts = :attempts
"""


def _ts(dt: datetime | None) -> str | None:
    # Fixed-width UTC text, so comparing the strings in SQL compares the times.
    return None if dt is None else dt.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _dt(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


@dataclass(frozen=True)
class Job:
    id: str
    url: str
    payload: Any
    status: JobStatus
    attempts: int
    next_retry_at: datetime | None
    last_error: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Job":
        return cls(
            id=row["id"],
            url=row["url"],
            payload=json.loads(row["payload"]),
            status=JobStatus(row["status"]),
            attempts=row["attempts"],
            next_retry_at=_dt(row["next_retry_at"]),
            last_error=row["last_error"],
        )


class JobRepository:
    def __init__(self, path: str):
        self.path = path

    @contextmanager
    def _connect(self):
        # isolation_level=None: autocommit, every statement is its own transaction.
        # timeout: wait up to 5 s for another writer instead of failing at once.
        conn = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")  # readers don't block the writer
            conn.executescript(SCHEMA)

    def create(self, url: str, payload: Any, now: datetime) -> Job:
        job_id = uuid.uuid4().hex
        with self._connect() as conn:
            row = conn.execute(
                "INSERT INTO jobs (id, url, payload, status, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?) RETURNING *",
                (job_id, url, json.dumps(payload), JobStatus.PENDING, _ts(now), _ts(now)),
            ).fetchone()
        return Job.from_row(row)

    def get(self, job_id: str) -> Job | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return None if row is None else Job.from_row(row)

    def claim_next(self, now: datetime, lease: timedelta) -> Job | None:
        with self._connect() as conn:
            row = conn.execute(CLAIM, {"now": _ts(now), "locked_until": _ts(now + lease)}).fetchone()
        return None if row is None else Job.from_row(row)

    def finish(self, job: Job, decision: Decision, error: str | None, now: datetime) -> bool:
        """Store the result of an attempt. False if the claim had expired and someone else owns the job."""
        with self._connect() as conn:
            cursor = conn.execute(FINISH, {
                "id": job.id,
                "attempts": job.attempts,
                "status": decision.status,
                "next_retry_at": _ts(decision.next_retry_at),
                "last_error": error,
                "now": _ts(now),
            })
        return cursor.rowcount == 1
