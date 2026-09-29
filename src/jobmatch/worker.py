"""Workers.

What async buys here: the slowest thing in the system is a model call that takes
somewhere between two and twenty seconds, and almost all of that is waiting. One
process with N tasks keeps N calls in flight against one connection pool. There
is no CPU work to speak of; threads would also work and cost more.

What async does not buy: the SQLite writes are blocking and serialised. They run
in a thread so they do not stall the event loop, and they are short.

Failure handling in one place:

    claim      -> attempts += 1 immediately, so a worker that dies mid-task
                  cannot retry forever
    success    -> score + status written in one transaction
    error      -> retryable errors go back to `pending`, the rest to `failed`
    timeout    -> retryable; the lease expires on its own if we cannot write
    cancelled  -> claim released, exception re-raised, never swallowed
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import uuid
from dataclasses import dataclass, field

from . import db
from .client import ScoringClient
from .config import Config
from .errors import JobmatchError
from .models import Profile

log = logging.getLogger("jobmatch.worker")


@dataclass
class RunStats:
    scored: int = 0
    retried: int = 0
    failed: int = 0
    by_worker: dict[str, int] = field(default_factory=dict)


async def run_pool(cfg: Config, client: ScoringClient, profile: Profile,
                   *, drain: bool = True, workers: int | None = None) -> RunStats:
    """Run `workers` concurrent workers until the queue is empty (drain=True).

    Cancelling this (Ctrl-C, SIGTERM) cancels the workers; each one releases its
    claim on the way out, so a restart picks up where it stopped.
    """
    n = workers or cfg.workers
    stats = RunStats()
    run_id = uuid.uuid4().hex[:8]

    tasks = [
        asyncio.create_task(
            _worker_loop(f"{os.getpid()}-{run_id}-{i}", cfg, client, profile, stats, drain),
            name=f"worker-{i}",
        )
        for i in range(n)
    ]
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        for t in tasks:
            t.cancel()
        # Give each worker a moment to release its claim before the loop closes.
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    return stats


async def _worker_loop(worker_id: str, cfg: Config, client: ScoringClient,
                       profile: Profile, stats: RunStats, drain: bool) -> None:
    handle = await db.AsyncDb(cfg.db_path).open()
    try:
        while True:
            job = await handle.call(db.claim_job, worker_id, cfg.lease_seconds, cfg.max_attempts)
            if job is None:
                if drain:
                    return
                await asyncio.sleep(cfg.poll_interval_s)
                continue
            await _process(worker_id, handle, cfg, client, profile, job, stats)
    finally:
        await handle.close()


async def _process(worker_id: str, handle: db.AsyncDb, cfg: Config,
                   client: ScoringClient, profile: Profile, job: sqlite3.Row,
                   stats: RunStats) -> None:
    job_id = job["id"]
    posting_id = job["posting_id"]

    try:
        posting = await handle.call(db.load_job_input, posting_id)

        # Bounds the whole unit of work, repair turns included. Without this a
        # provider that accepts the connection and then stalls holds a worker
        # until the lease expires, and the lease is measured in minutes.
        async with asyncio.timeout(cfg.job_timeout_s):
            result = await client.score(posting, profile)

        await handle.call(db.complete_job, job_id, posting_id,
                          job["profile_version"], job["content_hash"], result)
        stats.scored += 1
        stats.by_worker[worker_id] = stats.by_worker.get(worker_id, 0) + 1
        log.info("scored %s -> %d (%s, %dms)", posting_id, result.score.overall,
                 result.score.recommendation, result.latency_ms)

    except asyncio.CancelledError:
        # Shutdown, not failure. Hand the claim back and let the exception
        # continue; swallowing it here would break cancellation for the caller.
        # The executor call already in flight is not cancellable, so this runs
        # after it settles. Shielded so a second cancel cannot leave the claim held.
        await asyncio.shield(handle.call(db.release_job, job_id))
        log.info("worker %s cancelled, released job %s", worker_id, job_id)
        raise

    except TimeoutError:
        status = await handle.call(
            db.fail_job, job_id, f"job exceeded {cfg.job_timeout_s}s", True, cfg.max_attempts
        )
        _count(stats, status)
        log.warning("timeout on %s -> %s", posting_id, status)

    except JobmatchError as exc:
        status = await handle.call(
            db.fail_job, job_id, f"{type(exc).__name__}: {exc}", exc.retryable, cfg.max_attempts
        )
        _count(stats, status)
        log.warning("%s on %s -> %s", type(exc).__name__, posting_id, status)

    except Exception as exc:  # noqa: BLE001 - a bug in our code, not the model's
        # Unknown errors are not retried. Retrying a NameError just burns the
        # attempt budget and hides it.
        await handle.call(db.fail_job, job_id, f"unexpected {type(exc).__name__}: {exc}",
                          False, cfg.max_attempts)
        stats.failed += 1
        log.exception("unexpected error on %s", posting_id)


def _count(stats: RunStats, status: str) -> None:
    if status == "pending":
        stats.retried += 1
    else:
        stats.failed += 1
