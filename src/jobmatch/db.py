"""Database access. Synchronous by design, called from async code via a thread.

sqlite3 is a blocking C extension; wrapping it in `asyncio.to_thread` is honest
about that. The alternative (an async driver that still serialises on one
writer) would buy nothing here.

The one interesting function is `claim_job`. Everything else is bookkeeping.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from functools import partial
from importlib import resources
from typing import Any

from .models import Posting, ScoreResult, utcnow


def _now() -> datetime:
    return datetime.now(UTC)


class AsyncDb:
    """One SQLite connection, pinned to one thread.

    sqlite3 connections are bound to the thread that created them, and
    `asyncio.to_thread` hands work to an arbitrary pool thread, so a connection
    shared that way blows up as soon as two different pool threads touch it.

    A single-worker executor per connection fixes that without reaching for
    `check_same_thread=False`: the connection is created on that thread and every
    later call runs on the same one. Each worker owns one of these, so there is
    no sharing to reason about, and calls are awaited one at a time.
    """

    def __init__(self, path: str) -> None:
        self._path = path
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="jobmatch-db")
        self._conn: sqlite3.Connection | None = None

    async def open(self) -> AsyncDb:
        self._conn = await self._submit(connect, self._path)
        return self

    async def call(self, fn: Callable[..., Any], *args: Any) -> Any:
        """Run a db.* function against this connection, off the event loop."""
        if self._conn is None:
            raise RuntimeError("AsyncDb.open() was never awaited")
        return await self._submit(fn, self._conn, *args)

    async def close(self) -> None:
        if self._conn is not None:
            await self._submit(self._conn.close)
            self._conn = None
        self._executor.shutdown(wait=False)

    async def _submit(self, fn: Callable[..., Any], *args: Any) -> Any:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, partial(fn, *args))


def connect(path: str, timeout: float = 30.0) -> sqlite3.Connection:
    # isolation_level=None: transactions are explicit in this module. Implicit
    # BEGINs are exactly the kind of thing you do not want under contention.
    conn = sqlite3.connect(path, timeout=timeout, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_db(path: str) -> None:
    schema = resources.files("jobmatch").joinpath("schema.sql").read_text(encoding="utf-8")
    with connect(path) as conn:
        conn.executescript(schema)


@contextmanager
def immediate(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """A write transaction that takes the write lock up front.

    BEGIN IMMEDIATE is the point. A deferred transaction upgrades to a write
    lock only when it first writes, which is how you get SQLITE_BUSY at commit
    time instead of at the start, and two readers who both believe they own the
    same row. Taking the lock first makes the claim below a single serialised
    step: one winner, the loser waits (busy_timeout) and then sees the new state.

    In Postgres this would be `SELECT ... FOR UPDATE SKIP LOCKED` inside a
    normal transaction, which lets claims run in parallel. SQLite has one
    writer, so it serialises instead. Same guarantee, different cost.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


# --------------------------------------------------------------------------
# Ingestion
# --------------------------------------------------------------------------

def upsert_posting(conn: sqlite3.Connection, posting: Posting, profile_version: str) -> bool:
    """Insert or update a posting and make sure a unit of work exists for it.

    Returns True if new work was created. Re-running this with unchanged text
    creates nothing: the UNIQUE key on (posting, profile version, content hash)
    absorbs the second call. That is the whole idempotency story for ingest.
    """
    now = utcnow()
    with immediate(conn):
        conn.execute(
            """
            INSERT INTO postings (id, source, external_id, title, company, location,
                                  url, description, content_hash, first_seen_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                title        = excluded.title,
                company      = excluded.company,
                location     = excluded.location,
                url          = excluded.url,
                description  = excluded.description,
                content_hash = excluded.content_hash,
                updated_at   = excluded.updated_at
            """,
            (posting.id, posting.source, posting.external_id, posting.title, posting.company,
             posting.location, posting.url, posting.description, posting.content_hash, now, now),
        )
        cur = conn.execute(
            """
            INSERT INTO score_jobs (posting_id, profile_version, content_hash,
                                    status, created_at, updated_at)
            VALUES (?, ?, ?, 'pending', ?, ?)
            ON CONFLICT (posting_id, profile_version, content_hash) DO NOTHING
            """,
            (posting.id, profile_version, posting.content_hash, now, now),
        )
        return cur.rowcount > 0


# --------------------------------------------------------------------------
# The queue
# --------------------------------------------------------------------------

def claim_job(conn: sqlite3.Connection, worker_id: str, lease_seconds: int,
              max_attempts: int) -> sqlite3.Row | None:
    """Atomically take ownership of one unit of work, or return None.

    Two workers running this at the same moment cannot receive the same row:
    the UPDATE and the sub-select happen inside one write transaction, so the
    second worker's sub-select runs against the already-updated state.

    A `running` row whose lease has expired is claimable again. That is how a
    worker that died mid-task (SIGKILL, lost network, container evicted) gets
    its work picked up without anyone reaping anything by hand.
    """
    now = _now()
    lease_until = (now + timedelta(seconds=lease_seconds)).isoformat(timespec="seconds")
    now_s = now.isoformat(timespec="seconds")

    with immediate(conn):
        row = conn.execute(
            """
            UPDATE score_jobs
               SET status           = 'running',
                   attempts         = attempts + 1,
                   claimed_by       = ?,
                   lease_expires_at = ?,
                   updated_at       = ?
             WHERE id = (
                   SELECT id FROM score_jobs
                    WHERE attempts < ?
                      AND (status = 'pending'
                           OR (status = 'running' AND lease_expires_at < ?))
                    ORDER BY id
                    LIMIT 1
             )
         RETURNING id, posting_id, profile_version, content_hash, attempts
            """,
            (worker_id, lease_until, now_s, max_attempts, now_s),
        ).fetchone()
    return row


def load_job_input(conn: sqlite3.Connection, posting_id: str) -> Posting:
    row = conn.execute(
        "SELECT source, external_id, title, company, location, url, description "
        "FROM postings WHERE id = ?",
        (posting_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"posting {posting_id} disappeared")
    return Posting(**dict(row))


def complete_job(conn: sqlite3.Connection, job_id: int, posting_id: str,
                 profile_version: str, content_hash: str, result: ScoreResult) -> None:
    """Write the score and close the job in one transaction.

    Either both happen or neither does. A crash between them would otherwise
    leave a score with no finished job, or a finished job with no score, and
    the second is the one that quietly loses data.
    """
    s = result.score
    with immediate(conn):
        conn.execute(
            """
            INSERT INTO scores (job_id, posting_id, profile_version, content_hash,
                                overall, recommendation, rationale, blockers_json,
                                dimensions_json, model, input_tokens, output_tokens,
                                latency_ms, scored_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (posting_id, profile_version, content_hash) DO NOTHING
            """,
            (job_id, posting_id, profile_version, content_hash, s.overall, s.recommendation,
             s.rationale, json.dumps(s.blockers),
             json.dumps([d.model_dump() for d in s.dimensions]),
             result.model, result.usage.input_tokens, result.usage.output_tokens,
             result.latency_ms, utcnow()),
        )
        conn.execute(
            "UPDATE score_jobs SET status = 'done', claimed_by = NULL, "
            "lease_expires_at = NULL, last_error = NULL, updated_at = ? WHERE id = ?",
            (utcnow(), job_id),
        )


def fail_job(conn: sqlite3.Connection, job_id: int, error: str, retryable: bool,
             max_attempts: int) -> str:
    """Send the job back to the queue, or park it as failed.

    Returns the status it ended up in, which is what the worker logs.
    """
    with immediate(conn):
        row = conn.execute("SELECT attempts FROM score_jobs WHERE id = ?", (job_id,)).fetchone()
        attempts = row["attempts"] if row else max_attempts
        status = "pending" if (retryable and attempts < max_attempts) else "failed"
        conn.execute(
            "UPDATE score_jobs SET status = ?, claimed_by = NULL, lease_expires_at = NULL, "
            "last_error = ?, updated_at = ? WHERE id = ?",
            (status, error[:2000], utcnow(), job_id),
        )
    return status


def release_job(conn: sqlite3.Connection, job_id: int) -> None:
    """Give a claim back without counting it as a failure.

    Used on cancellation (Ctrl-C, shutdown). The attempt already counted at
    claim time, which is deliberate: a worker that is killed repeatedly on the
    same row should eventually stop retrying it rather than loop forever.
    """
    with immediate(conn):
        conn.execute(
            "UPDATE score_jobs SET status = 'pending', claimed_by = NULL, "
            "lease_expires_at = NULL, updated_at = ? WHERE id = ? AND status = 'running'",
            (utcnow(), job_id),
        )


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------

def queue_counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute("SELECT status, COUNT(*) AS n FROM score_jobs GROUP BY status").fetchall()
    return {r["status"]: r["n"] for r in rows}


def ranked_scores(conn: sqlite3.Connection, profile_version: str,
                  limit: int = 20) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT p.title, p.company, p.location, p.url,
               s.overall, s.recommendation, s.rationale, s.blockers_json,
               s.dimensions_json, s.input_tokens, s.output_tokens, s.latency_ms
          FROM scores s
          JOIN postings p ON p.id = s.posting_id
         WHERE s.profile_version = ?
         ORDER BY s.overall DESC, p.company
         LIMIT ?
        """,
        (profile_version, limit),
    ).fetchall()


def usage_totals(conn: sqlite3.Connection, profile_version: str) -> sqlite3.Row:
    return conn.execute(
        """
        SELECT COUNT(*)                AS scored,
               COALESCE(SUM(input_tokens), 0)  AS input_tokens,
               COALESCE(SUM(output_tokens), 0) AS output_tokens,
               COALESCE(AVG(latency_ms), 0)    AS avg_latency_ms,
               COALESCE(MAX(latency_ms), 0)    AS max_latency_ms
          FROM scores WHERE profile_version = ?
        """,
        (profile_version,),
    ).fetchone()
