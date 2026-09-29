-- Correctness lives here, not in application code.
--
-- The three rules this schema enforces on its own:
--   1. A posting exists once per (source, external_id).
--   2. A unit of work exists once per (posting, profile version, content hash).
--   3. A score exists once per unit of work, and cannot exist without one.
--
-- Everything the workers do is a consequence of those constraints. If two
-- workers race, the database refuses the second write; nothing upstream has to
-- remember to check.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS postings (
    id            TEXT PRIMARY KEY,
    source        TEXT NOT NULL,
    external_id   TEXT NOT NULL,
    title         TEXT NOT NULL,
    company       TEXT NOT NULL,
    location      TEXT,
    url           TEXT,
    description   TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    UNIQUE (source, external_id)
);

CREATE TABLE IF NOT EXISTS score_jobs (
    id               INTEGER PRIMARY KEY,
    posting_id       TEXT NOT NULL REFERENCES postings(id) ON DELETE CASCADE,
    profile_version  TEXT NOT NULL,
    content_hash     TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending', 'running', 'done', 'failed')),
    attempts         INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    claimed_by       TEXT,
    lease_expires_at TEXT,
    last_error       TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    -- The idempotency key. Same posting text, same criteria, same work unit.
    UNIQUE (posting_id, profile_version, content_hash),
    -- A claimed job must record who holds it and until when.
    CHECK (status <> 'running' OR (claimed_by IS NOT NULL AND lease_expires_at IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS idx_jobs_claimable
    ON score_jobs (status, lease_expires_at, id);

CREATE TABLE IF NOT EXISTS scores (
    job_id          INTEGER PRIMARY KEY REFERENCES score_jobs(id) ON DELETE CASCADE,
    posting_id      TEXT NOT NULL REFERENCES postings(id) ON DELETE CASCADE,
    profile_version TEXT NOT NULL,
    content_hash    TEXT NOT NULL,
    overall         INTEGER NOT NULL CHECK (overall BETWEEN 0 AND 100),
    recommendation  TEXT NOT NULL CHECK (recommendation IN ('apply', 'maybe', 'skip')),
    rationale       TEXT NOT NULL,
    blockers_json   TEXT NOT NULL DEFAULT '[]',
    dimensions_json TEXT NOT NULL DEFAULT '[]',
    model           TEXT NOT NULL,
    input_tokens    INTEGER NOT NULL DEFAULT 0,
    output_tokens   INTEGER NOT NULL DEFAULT 0,
    latency_ms      INTEGER NOT NULL DEFAULT 0,
    scored_at       TEXT NOT NULL,
    -- One score per unit of work, enforced twice: by the job_id primary key and
    -- by the natural key. Belt and braces, because this is the thing that must
    -- not happen twice.
    UNIQUE (posting_id, profile_version, content_hash)
);

CREATE INDEX IF NOT EXISTS idx_scores_rank
    ON scores (profile_version, overall DESC);
