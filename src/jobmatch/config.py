"""Configuration. Environment in, frozen object out, no globals read later."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


@dataclass(frozen=True)
class Config:
    db_path: str = "jobmatch.db"
    backend: str = "anthropic"          # "anthropic" | "fake"
    api_key: str | None = None
    model: str = "claude-sonnet-5-5"
    max_output_tokens: int = 1024

    # Timeouts are per-attempt; job_timeout_s bounds the whole unit of work
    # including repair turns, so one stuck posting cannot hold a worker forever.
    request_timeout_s: float = 45.0
    connect_timeout_s: float = 10.0
    job_timeout_s: float = 120.0

    max_provider_retries: int = 3       # transport / 429 / 5xx
    max_repair_turns: int = 1           # invalid model output
    max_attempts: int = 3               # queue-level attempts before `failed`
    lease_seconds: int = 300            # how long a claim is honoured
    workers: int = 4
    poll_interval_s: float = 1.0

    # Rough USD per million tokens, only used for the cost line in reports.
    price_in_per_mtok: float = 3.0
    price_out_per_mtok: float = 15.0

    @classmethod
    def from_env(cls) -> Config:
        backend = os.getenv("JOBMATCH_BACKEND", "anthropic").strip().lower()
        key = os.getenv("ANTHROPIC_API_KEY") or None
        if backend == "anthropic" and not key:
            # Fail here rather than three layers down inside a worker.
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. Export it, or run with "
                "JOBMATCH_BACKEND=fake to use the offline stub model."
            )
        return cls(
            db_path=os.getenv("JOBMATCH_DB", "jobmatch.db"),
            backend=backend,
            api_key=key,
            model=os.getenv("JOBMATCH_MODEL", "claude-sonnet-5-5"),
            max_output_tokens=_int("JOBMATCH_MAX_OUTPUT_TOKENS", 1024),
            request_timeout_s=float(os.getenv("JOBMATCH_REQUEST_TIMEOUT", "45")),
            job_timeout_s=float(os.getenv("JOBMATCH_JOB_TIMEOUT", "120")),
            max_attempts=_int("JOBMATCH_MAX_ATTEMPTS", 3),
            lease_seconds=_int("JOBMATCH_LEASE_SECONDS", 300),
            workers=_int("JOBMATCH_WORKERS", 4),
        )
