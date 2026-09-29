"""Command line entry point.

    jobmatch init                      create the database
    jobmatch ingest --file postings.json
    jobmatch ingest --url https://...
    jobmatch run                       score everything pending
    jobmatch status                    queue counts
    jobmatch report --top 10 --csv out.csv
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from . import db, ingest, report
from .client import AnthropicClient, FakeClient, build_http_client
from .config import Config
from .models import Profile
from .worker import run_pool

DEFAULT_PROFILE = Path("profile.json")


def load_profile(path: str | Path) -> Profile:
    return Profile.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))


async def _run(cfg: Config, profile: Profile, workers: int | None) -> int:
    if cfg.backend == "fake":
        stats = await run_pool(cfg, FakeClient(cfg), profile, workers=workers)
    else:
        async with build_http_client(cfg) as http:
            stats = await run_pool(cfg, AnthropicClient(cfg, http), profile, workers=workers)
    print(f"scored={stats.scored} retried={stats.retried} failed={stats.failed}")
    return 0 if stats.failed == 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jobmatch", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profile", default=str(DEFAULT_PROFILE))
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init")

    p_ingest = sub.add_parser("ingest")
    src = p_ingest.add_mutually_exclusive_group(required=True)
    src.add_argument("--file")
    src.add_argument("--url")

    p_run = sub.add_parser("run")
    p_run.add_argument("--workers", type=int)

    sub.add_parser("status")

    p_report = sub.add_parser("report")
    p_report.add_argument("--top", type=int, default=20)
    p_report.add_argument("--csv")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )

    # `init` needs no API key and no profile, so it reads the one setting it
    # cares about directly instead of going through Config.from_env(), which
    # would refuse to build without a key.
    if args.command == "init":
        db_path = os.getenv("JOBMATCH_DB", "jobmatch.db")
        db.init_db(db_path)
        print(f"initialised {db_path}")
        return 0

    cfg = Config.from_env()
    profile = load_profile(args.profile)

    if args.command == "ingest":
        if args.file:
            rep = ingest.ingest_file(cfg, args.file, profile.version)
        else:
            rep = ingest.ingest_url(cfg, args.url, profile.version)
        print(f"seen={rep.seen} new_work={rep.new_work} unchanged={rep.unchanged} "
              f"rejected={rep.rejected}")
        return 0

    if args.command == "run":
        try:
            return asyncio.run(_run(cfg, profile, args.workers))
        except KeyboardInterrupt:
            print("\ninterrupted; claims released", file=sys.stderr)
            return 130

    if args.command == "status":
        conn = db.connect(cfg.db_path)
        try:
            counts = db.queue_counts(conn)
        finally:
            conn.close()
        print(json.dumps(counts, indent=2) if counts else "queue empty")
        return 0

    if args.command == "report":
        print(report.markdown_report(cfg, profile.version, args.top))
        if args.csv:
            n = report.write_csv(cfg, profile.version, args.csv)
            print(f"\nwrote {n} rows to {args.csv}", file=sys.stderr)
        return 0

    parser.error(f"unknown command {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
