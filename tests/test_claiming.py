"""The concurrency tests.

These spawn real workers against a real database file. Mocking the claim would
mean testing that the mock returns what it was told to return, which is the
thing under test.
"""

from __future__ import annotations

import asyncio
import multiprocessing as mp
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from conftest import fetch_all, seed

from jobmatch import db
from jobmatch.client import FakeClient
from jobmatch.worker import run_pool

pytestmark = pytest.mark.asyncio


async def test_every_job_is_scored_exactly_once(cfg, profile):
    seed(cfg, 40)
    client = FakeClient(cfg, latency_s=0.01)

    stats = await run_pool(cfg, client, profile, workers=8)

    assert stats.scored == 40
    assert client.calls == 40, "a posting was sent to the model more than once"
    rows = fetch_all(cfg, "SELECT COUNT(*) AS n FROM scores")
    assert rows[0]["n"] == 40
    statuses = fetch_all(cfg, "SELECT status, COUNT(*) AS n FROM score_jobs GROUP BY status")
    assert statuses == [{"status": "done", "n": 40}]
    # More than one worker actually did work, otherwise this proves nothing.
    assert len([w for w, n in stats.by_worker.items() if n]) > 1


async def test_two_workers_cannot_claim_the_same_row(cfg):
    """Hammer claim_job directly: 12 concurrent claimers, 30 jobs, no overlap."""
    seed(cfg, 30)

    def claim_until_empty(worker_id: str) -> list[int]:
        conn = db.connect(cfg.db_path)
        got = []
        try:
            while True:
                row = db.claim_job(conn, worker_id, cfg.lease_seconds, cfg.max_attempts)
                if row is None:
                    return got
                got.append(row["id"])
        finally:
            conn.close()

    results = await asyncio.gather(
        *[asyncio.to_thread(claim_until_empty, f"w{i}") for i in range(12)]
    )

    claimed = [job_id for batch in results for job_id in batch]
    assert sorted(claimed) == sorted(set(claimed)), "the same job was claimed twice"
    assert len(claimed) == 30


async def test_dead_worker_lease_is_reclaimed(cfg):
    """A worker claims a row and dies. Nobody reaps it. It comes back anyway."""
    seed(cfg, 1)
    conn = db.connect(cfg.db_path)
    try:
        first = db.claim_job(conn, "doomed", lease_seconds=60, max_attempts=5)
        assert first is not None

        # Still held: a second claimer gets nothing.
        assert db.claim_job(conn, "other", lease_seconds=60, max_attempts=5) is None

        # Simulate the lease expiring rather than sleeping through it.
        expired = (datetime.now(UTC) - timedelta(seconds=1)).isoformat(timespec="seconds")
        conn.execute("UPDATE score_jobs SET lease_expires_at = ? WHERE id = ?",
                     (expired, first["id"]))

        second = db.claim_job(conn, "rescuer", lease_seconds=60, max_attempts=5)
        assert second is not None and second["id"] == first["id"]
        assert second["attempts"] == 2, "the reclaim must count as another attempt"
    finally:
        conn.close()


async def test_cancelled_run_releases_claims_and_resumes(cfg, profile):
    seed(cfg, 12)
    slow = FakeClient(cfg, latency_s=0.4)

    task = asyncio.create_task(run_pool(cfg, slow, profile, workers=4))
    await asyncio.sleep(0.15)          # long enough to claim, too short to finish
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    held = fetch_all(cfg, "SELECT COUNT(*) AS n FROM score_jobs WHERE status = 'running'")
    assert held[0]["n"] == 0, "cancelled workers left claims behind"

    stats = await run_pool(cfg, FakeClient(cfg, latency_s=0.01), profile, workers=4)
    total = fetch_all(cfg, "SELECT COUNT(*) AS n FROM scores")[0]["n"]
    assert total == 12
    assert stats.scored <= 12


def _claim_in_subprocess(db_path: str, worker_id: str, queue) -> None:
    conn = db.connect(db_path)
    got = []
    try:
        while True:
            row = db.claim_job(conn, worker_id, 60, 5)
            if row is None:
                break
            got.append(row["id"])
    finally:
        conn.close()
    queue.put(got)


async def test_separate_processes_do_not_double_claim(cfg):
    """Same guarantee across processes, where a GIL cannot be hiding the bug."""
    seed(cfg, 25)
    ctx = mp.get_context("spawn")
    queue = ctx.Queue()
    procs = [ctx.Process(target=_claim_in_subprocess, args=(cfg.db_path, f"p{i}", queue))
             for i in range(4)]
    for p in procs:
        p.start()
    claimed = [job_id for _ in procs for job_id in queue.get(timeout=60)]
    for p in procs:
        p.join(timeout=60)

    assert sorted(claimed) == sorted(set(claimed))
    assert len(claimed) == 25


async def test_score_cannot_be_written_twice(cfg, profile):
    """Even with the queue bypassed, the schema refuses a duplicate score."""
    seed(cfg, 1)
    conn = db.connect(cfg.db_path)
    try:
        job = db.claim_job(conn, "w", cfg.lease_seconds, cfg.max_attempts)
        result = await FakeClient(cfg, latency_s=0).score(
            db.load_job_input(conn, job["posting_id"]), profile
        )
        db.complete_job(conn, job["id"], job["posting_id"], job["profile_version"],
                        job["content_hash"], result)
        db.complete_job(conn, job["id"], job["posting_id"], job["profile_version"],
                        job["content_hash"], result)  # second write, same key
        n = conn.execute("SELECT COUNT(*) AS n FROM scores").fetchone()["n"]
        assert n == 1

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO scores (job_id, posting_id, profile_version, content_hash, "
                "overall, recommendation, rationale, model, scored_at) "
                "VALUES (999, ?, ?, ?, 50, 'maybe', 'x', 'm', 'now')",
                (job["posting_id"], job["profile_version"], job["content_hash"]),
            )
    finally:
        conn.close()
