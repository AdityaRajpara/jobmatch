"""End-to-end behaviour of ingest -> queue -> workers -> report."""

from __future__ import annotations

import json

import pytest
from conftest import fetch_all, make_posting, seed

from jobmatch import db, ingest, report
from jobmatch.client import FakeClient
from jobmatch.worker import run_pool

pytestmark = pytest.mark.asyncio


async def test_reingesting_the_same_posting_creates_no_new_work(cfg, profile, tmp_path):
    records = [json.loads(make_posting(i).model_dump_json()) for i in range(5)]
    path = tmp_path / "postings.json"
    path.write_text(json.dumps(records), encoding="utf-8")

    first = ingest.ingest_file(cfg, path, profile.version)
    second = ingest.ingest_file(cfg, path, profile.version)

    assert first.new_work == 5
    assert second.new_work == 0 and second.unchanged == 5

    await run_pool(cfg, FakeClient(cfg, latency_s=0), profile, workers=3)
    ingest.ingest_file(cfg, path, profile.version)      # third pass, after scoring
    pending = fetch_all(cfg, "SELECT COUNT(*) AS n FROM score_jobs WHERE status='pending'")
    assert pending[0]["n"] == 0, "re-ingest must not re-queue already scored postings"


async def test_edited_posting_becomes_new_work(cfg, profile):
    conn = db.connect(cfg.db_path)
    try:
        db.upsert_posting(conn, make_posting(1, description="python and sql"), profile.version)
        db.upsert_posting(conn, make_posting(1, description="python, sql and dbt"), profile.version)
    finally:
        conn.close()
    jobs = fetch_all(cfg, "SELECT content_hash FROM score_jobs")
    assert len(jobs) == 2, "changed posting text should produce a second unit of work"


async def test_malformed_records_are_skipped_not_fatal(cfg, profile):
    records = [
        json.loads(make_posting(1).model_dump_json()),
        {"source": "test", "external_id": "2"},                  # no title/description
        {"source": "test", "external_id": "3", "title": "x", "company": "c",
         "description": ""},                                      # empty description
    ]
    rep = ingest.ingest_records(cfg, records, profile.version)
    assert rep.new_work == 1 and rep.rejected == 2


async def test_provider_failures_retry_then_park_as_failed(cfg, profile):
    seed(cfg, 2)
    # Fails more times than max_attempts allows, so both jobs end up `failed`.
    client = FakeClient(cfg, latency_s=0, fail_first_n=99)

    for _ in range(cfg.max_attempts):
        await run_pool(cfg, client, profile, workers=2)

    statuses = {r["status"]: r["n"] for r in
                fetch_all(cfg, "SELECT status, COUNT(*) AS n FROM score_jobs GROUP BY status")}
    assert statuses.get("failed") == 2
    assert fetch_all(cfg, "SELECT COUNT(*) AS n FROM scores")[0]["n"] == 0


async def test_report_ranks_and_totals(cfg, profile):
    seed(cfg, 6)
    await run_pool(cfg, FakeClient(cfg, latency_s=0), profile, workers=3)

    md = report.markdown_report(cfg, profile.version, limit=3)
    assert "Ranked postings (v1)" in md
    assert "Estimated spend" in md

    out = cfg.db_path + ".csv"
    assert report.write_csv(cfg, profile.version, out) == 6
