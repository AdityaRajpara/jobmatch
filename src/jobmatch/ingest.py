"""Ingestion from a REST endpoint or a local file.

Both paths end in the same place: parse into a Posting (which validates), then
upsert. Anything that fails validation is skipped and counted, not crashed on:
one malformed record out of two hundred should not stop a run.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from pydantic import ValidationError

from . import db
from .config import Config
from .models import Posting

log = logging.getLogger("jobmatch.ingest")


@dataclass
class IngestReport:
    seen: int = 0
    new_work: int = 0
    unchanged: int = 0
    rejected: int = 0


def _parse(records: Iterable[dict[str, Any]]) -> tuple[list[Posting], int]:
    postings, rejected = [], 0
    for raw in records:
        try:
            postings.append(Posting.model_validate(raw))
        except ValidationError as exc:
            rejected += 1
            log.warning("rejected posting %r: %s", raw.get("external_id", "?"),
                        exc.errors()[0]["msg"] if exc.errors() else exc)
    return postings, rejected


def ingest_records(cfg: Config, records: Iterable[dict[str, Any]],
                   profile_version: str) -> IngestReport:
    postings, rejected = _parse(records)
    report = IngestReport(seen=len(postings) + rejected, rejected=rejected)
    conn = db.connect(cfg.db_path)
    try:
        for posting in postings:
            if db.upsert_posting(conn, posting, profile_version):
                report.new_work += 1
            else:
                report.unchanged += 1
    finally:
        conn.close()
    return report


def ingest_file(cfg: Config, path: str | Path, profile_version: str) -> IngestReport:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("expected a JSON array of postings")
    return ingest_records(cfg, data, profile_version)


def ingest_url(cfg: Config, url: str, profile_version: str,
               *, headers: dict[str, str] | None = None) -> IngestReport:
    """Pull postings from a REST endpoint returning a JSON array (or {items: [...]})."""
    with httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0)) as http:
        response = http.get(url, headers=headers or {})
        response.raise_for_status()
        body = response.json()
    records = body.get("items", []) if isinstance(body, dict) else body
    return ingest_records(cfg, records, profile_version)
