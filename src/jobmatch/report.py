"""Reporting: ranked results, and what the run cost.

Token usage and latency are recorded per call and summed here because "is this
prompt change better" and "is this worth running" are both questions you can
only answer with numbers you kept.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from . import db
from .config import Config


def cost_usd(cfg: Config, input_tokens: int, output_tokens: int) -> float:
    return (input_tokens / 1_000_000 * cfg.price_in_per_mtok
            + output_tokens / 1_000_000 * cfg.price_out_per_mtok)


def markdown_report(cfg: Config, profile_version: str, limit: int = 20) -> str:
    conn = db.connect(cfg.db_path)
    try:
        rows = db.ranked_scores(conn, profile_version, limit)
        totals = db.usage_totals(conn, profile_version)
        counts = db.queue_counts(conn)
    finally:
        conn.close()

    lines = [f"# Ranked postings ({profile_version})", ""]
    if not rows:
        lines.append("_No scores yet._")
    for i, r in enumerate(rows, 1):
        blockers = json.loads(r["blockers_json"])
        lines.append(f"**{i}. {r['title']} — {r['company']}**  ")
        lines.append(f"{r['overall']}/100 · {r['recommendation']} · {r['location'] or 'n/a'}  ")
        lines.append(f"{r['rationale']}  ")
        if blockers:
            lines.append(f"_Blockers: {'; '.join(blockers)}_  ")
        if r["url"]:
            lines.append(f"<{r['url']}>  ")
        lines.append("")

    spend = cost_usd(cfg, totals["input_tokens"], totals["output_tokens"])
    lines += [
        "## Run",
        "",
        "- Queue: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "- Queue: empty",
        f"- Scored: {totals['scored']}",
        f"- Tokens: {totals['input_tokens']} in / {totals['output_tokens']} out",
        f"- Latency: {totals['avg_latency_ms']:.0f}ms avg, {totals['max_latency_ms']}ms max",
        f"- Estimated spend: ${spend:.4f}",
    ]
    return "\n".join(lines)


def write_csv(cfg: Config, profile_version: str, path: str | Path, limit: int = 1000) -> int:
    conn = db.connect(cfg.db_path)
    try:
        rows = db.ranked_scores(conn, profile_version, limit)
    finally:
        conn.close()

    out = Path(path)
    with out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["rank", "overall", "recommendation", "title", "company",
                         "location", "url", "blockers", "rationale"])
        for i, r in enumerate(rows, 1):
            writer.writerow([i, r["overall"], r["recommendation"], r["title"], r["company"],
                             r["location"] or "", r["url"] or "",
                             "; ".join(json.loads(r["blockers_json"])), r["rationale"]])
    return len(rows)
