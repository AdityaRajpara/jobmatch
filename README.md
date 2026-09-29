# jobmatch

Scores job postings against one candidate profile using a language model, and
keeps the results in a small SQLite database so the same posting is never scored
twice.

It exists because I was reading too many postings by hand. The scoring itself is
the least interesting part. What it turned into was a study of the things that
get awkward as soon as a model is allowed to do real work against real data:
making sure a unit of work happens exactly once, treating the model's answer as
untrusted input, and behaving sensibly when a worker dies halfway through.

```
ingest ──> postings ──> score_jobs (queue) ──> N async workers ──> scores ──> report
              │              │                       │
       content hash      atomic claim          Claude API, tool call,
       = idempotency     + lease               validated with Pydantic
```

## Run it without an API key

The stub backend replaces the model with a deterministic keyword scorer, so the
pipeline, the queue and the reports all work offline.

```bash
pip install -e ".[dev]"

export JOBMATCH_BACKEND=fake
jobmatch init
jobmatch ingest --file data/sample_postings.json
jobmatch run --workers 3
jobmatch report --top 5
```

## Run it for real

```bash
cp .env.example .env          # fill in ANTHROPIC_API_KEY
export ANTHROPIC_API_KEY=sk-ant-...
export JOBMATCH_BACKEND=anthropic

jobmatch init
jobmatch ingest --url https://example.com/api/jobs      # or --file
jobmatch run
jobmatch report --top 20 --csv shortlist.csv
```

`profile.json` is the candidate side of the comparison. Editing it should come
with a bump to `version`, which makes every posting new work rather than leaving
old scores lying around under new criteria.

## The parts worth reading

| Where | What it does |
| --- | --- |
| `src/jobmatch/schema.sql` | The constraints that make duplicate work impossible |
| `src/jobmatch/db.py` → `claim_job` | One row, one worker, plus lease recovery |
| `src/jobmatch/client.py` | The model call: tool use, retries, one repair turn |
| `src/jobmatch/models.py` | The contract every model response has to satisfy |
| `src/jobmatch/worker.py` | Concurrency, timeouts, cancellation, failure routing |
| `tests/test_claiming.py` | Real workers and real processes racing for the same rows |
| `docs/DESIGN.md` | Why the above is shaped the way it is, and what I left out |

## Exactly once

A unit of work is keyed on `(posting, profile version, content hash)`. Ingesting
the same posting again produces the same key, the insert is a no-op, and no
second score is written. If the posting text changes, the hash changes and it
becomes genuinely new work.

Workers claim a row inside a `BEGIN IMMEDIATE` transaction:

```sql
UPDATE score_jobs
   SET status = 'running', attempts = attempts + 1,
       claimed_by = ?, lease_expires_at = ?
 WHERE id = (SELECT id FROM score_jobs
              WHERE attempts < ?
                AND (status = 'pending'
                     OR (status = 'running' AND lease_expires_at < ?))
              ORDER BY id LIMIT 1)
RETURNING id, posting_id, profile_version, content_hash, attempts;
```

Two workers cannot come away with the same row, because the update and its
sub-select happen inside one serialised write transaction. A worker that is
killed mid-task holds a lease that expires; the row then becomes claimable
again, with the attempt already counted so it cannot loop forever. The score and
the job's completion are written in one transaction, so there is no window where
one exists without the other.

In Postgres the same job would be `SELECT ... FOR UPDATE SKIP LOCKED`, which
lets claims proceed in parallel instead of serialising on a single writer.

## Model output is input

Nothing the model returns reaches the database unvalidated.

- The request forces a tool call (`tool_choice`), so a prose answer is a
  contract violation rather than something to parse.
- The tool arguments are validated against a Pydantic model with
  `extra="forbid"`, ranges on every number and an enum on the recommendation.
- A failure gets one repair turn: the model is shown its own output and the
  validation error. If it fails again the job is retried and eventually parked
  as `failed`, where a human can see it.
- Posting text is untrusted and is delimited and labelled as data. The real
  defence is the contract: `data/sample_postings.json` includes a posting that
  tells automated screening tools to return a perfect score, and there is a test
  (`test_posting_text_cannot_smuggle_instructions`) that assumes the model falls
  for it and asserts that the caller still gets an error.

## Concurrency, and what async buys

The slowest thing in the system is a model call of a few seconds, nearly all of
it waiting. One process with N tasks keeps N calls in flight over one connection
pool; there is no CPU work to parallelise. SQLite is blocking, so every database
call runs on a thread, and each worker pins its connection to a single-thread
executor rather than passing it around the default pool.

Each unit of work is bounded by `asyncio.timeout`, which covers the repair turn
as well, so one stalled posting cannot hold a worker until the lease expires.
Cancellation is not swallowed: a cancelled worker releases its claim and
re-raises, so Ctrl-C leaves the queue in a state you can resume from.

## Tests

```bash
pytest          # 24 tests, no API key needed
ruff check .
```

The concurrency tests spawn real workers against a real database file, including
one that races four separate OS processes, because a mock of the claim would
only prove the mock returns what it was told to. The HTTP-level tests use
`httpx.MockTransport`, which runs the real client code against a stubbed socket:
429s with backoff, a 400 that is deliberately not retried, timeouts, malformed
tool arguments, and a model that answers in prose.

## What is not here

Streaming, an eval harness, and a multi-tenant story. Reasons, and what I would
do about each, are in `docs/DESIGN.md`.
