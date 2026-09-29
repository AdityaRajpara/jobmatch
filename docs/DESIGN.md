# Design notes

Decisions, and the reasoning behind them, written down before or while building.
Where I chose the cheaper option, it says so.

## 1. The work unit key is a content hash, not a job id

A posting is scored once per `(posting, profile version, content hash)`. This is
what makes ingestion safe to re-run: the second ingest of an unchanged posting
inserts nothing.

The alternative, an idempotency flag set after the work completes, has a window
where the work is done but the flag is not yet written. Keying on content
removes the window because the uniqueness lives in the database, not in the
order that two statements happen to run.

The cost is real: edit a typo in a posting and it becomes new work, which costs
another model call. For this workload that is the right trade, since the score
depends on the text. If postings were edited constantly I would hash only the
fields the prompt actually consumes, or normalise whitespace first.

## 2. Constraints over application checks

`schema.sql` enforces that a score exists at most once per work unit, that a
`running` job has both a holder and a lease, that `overall` is 0–100 and that
`recommendation` is one of three values. Application code can then be wrong
without corrupting anything: `complete_job` uses `ON CONFLICT DO NOTHING` and
the test `test_score_cannot_be_written_twice` goes around the queue entirely and
still cannot insert a duplicate.

## 3. Claiming: lock first, serialise, lease

`BEGIN IMMEDIATE` takes the write lock at the start of the transaction. A
deferred transaction upgrades to a write lock only on first write, which is how
two readers end up believing they own the same row and one of them fails at
commit time.

Leases rather than heartbeats: a claim is good for `lease_seconds`, and an
expired claim is claimable by anyone. A dead worker needs no reaper process, and
there is no separate liveness channel to get wrong. The cost is latency after a
crash, bounded by the lease. `attempts` increments at claim time, not at failure
time, so a worker that dies on the same poisoned row repeatedly still exhausts
its budget and the row lands in `failed`.

Postgres would use `FOR UPDATE SKIP LOCKED` and claims would run concurrently.
SQLite has one writer; claims serialise. At this scale (hundreds of postings, a
handful of workers) the claim is microseconds and the model call is seconds, so
the writer lock is not the bottleneck. It would be the first thing to change if
this moved to a server database.

## 4. Calling the API directly

No SDK and no agent framework. The loop is roughly forty lines: build the
request, post it, find the tool call, validate it, repair once, give up. When
something goes wrong I want to look at the request and the response, not at a
library's interpretation of them. It also keeps the retry policy honest, because
retries are visible in the code that owns the timeout.

`tool_choice` forces the tool call. Without it, "I think this is a good fit!" is
a perfectly reasonable thing for a model to say and a parse error here.

## 5. Retry policy

- Transport errors, timeouts, 408, 429, 5xx: retried with exponential backoff
  and full jitter, honouring `retry-after`. Jitter matters because synchronised
  workers backing off in lockstep turn one transient 429 into a herd.
- Other 4xx: not retried. A 400 is a bug in the request; retrying it spends
  money to get the same answer.
- Invalid model output: one repair turn carrying the validation error, then the
  queue's own attempt budget takes over.

Retrying a scoring call is safe because it is a pure read on the provider side.
If the model were taking actions, a timeout would be a genuinely ambiguous
result and would need an idempotency key on the request, not a blind retry.

## 6. Async, and where the thread pool sits

The workload is I/O bound on a multi-second model call, so async is the right
shape: N concurrent calls, one connection pool, one process. There is no CPU
work here that would justify threads or processes.

SQLite is a blocking C extension and its connections are thread-bound.
`asyncio.to_thread` hands work to an arbitrary pool thread, which breaks that,
so `db.AsyncDb` gives each worker a connection pinned to its own single-thread
executor. The alternative, `check_same_thread=False`, works only as long as
nobody ever uses a connection from two places at once, which is a rule you
cannot enforce with a flag.

`asyncio.timeout` bounds the whole unit of work rather than a single HTTP
request, because a provider that accepts a connection and then stalls would
otherwise hold a worker for the length of the lease.

## 7. Prompt injection

Ranked by how much each defence is actually worth:

1. The output contract. A fully hijacked model still cannot return anything but
   a valid `Score`, because everything else is rejected. This is the one that
   holds.
2. Structural separation: instructions in the system prompt, posting text in a
   delimited block in the user turn, announced as untrusted data.
3. Telling the model to ignore instructions found in the data. Cheap, worth
   having, not something to rely on.

There is a hostile posting in the sample data and a test that assumes the model
falls for it.

## 8. Cost and latency as recorded numbers

Every score row carries input tokens, output tokens and latency, and the report
sums them with a rough price. Without that, "did this prompt change help" and
"is this worth running" are both unanswerable. It also makes the effect of the
repair turn visible, since a repair doubles the input tokens for that posting.

## 9. What I left out, and what I would do

**Streaming.** Nothing here is user-facing; the score is only useful complete,
and streaming a tool call to a background worker would add parser complexity for
no benefit. For an interactive product I would stream, and the interesting part
would be validating a partial tool-call payload before committing to it.

**Evals.** The honest gap. Right now "is this prompt better" is judged by
reading the output. The shape I would build: a small labelled set of postings
with my own apply/maybe/skip call, then measure agreement and rank correlation
per prompt version, with the prompt version stored alongside every score so the
comparison is possible after the fact. The `profile_version` column is already
half of that.

**Multi-tenancy.** Single user, so there is no tenant column. With one, tenant
id belongs in every table and every unique key, and the isolation belongs in the
database (row-level security or a per-tenant role), not in a `WHERE` clause that
someone will eventually forget.

**A real queue.** SQLite is enough for a few hundred postings on one machine. At
a scale where multiple machines run workers, this becomes Postgres with
`SKIP LOCKED`, and the code changes in one function.
