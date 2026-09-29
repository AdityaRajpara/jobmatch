from __future__ import annotations

from pathlib import Path

import pytest

from jobmatch import db
from jobmatch.config import Config
from jobmatch.models import Posting, Profile

PROFILE = Profile(
    version="v1",
    summary="Data engineer, four years, Python and SQL, banking and insurance.",
    must_haves=["python", "sql"],
    nice_to_haves=["snowflake"],
    dealbreakers=["on-site five days"],
)


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    c = Config(db_path=str(tmp_path / "test.db"), backend="fake", api_key=None,
               lease_seconds=60, max_attempts=3, workers=4, job_timeout_s=5.0)
    db.init_db(c.db_path)
    return c


@pytest.fixture
def profile() -> Profile:
    return PROFILE


def make_posting(n: int, *, description: str = "python and sql all day") -> Posting:
    return Posting(
        source="test", external_id=str(n), title=f"Data Engineer {n}",
        company=f"Company {n}", location="Remote", url=f"https://example.test/{n}",
        description=description,
    )


def seed(cfg: Config, count: int, profile_version: str = "v1") -> None:
    conn = db.connect(cfg.db_path)
    try:
        for i in range(count):
            db.upsert_posting(conn, make_posting(i), profile_version)
    finally:
        conn.close()


def fetch_all(cfg: Config, sql: str, params: tuple = ()) -> list[dict]:
    conn = db.connect(cfg.db_path)
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()
