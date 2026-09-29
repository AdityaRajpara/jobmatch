"""Typed boundaries.

Two kinds of data cross a boundary in this project and neither is trusted:

1. Job postings arriving from a REST endpoint or a file. Third-party data.
2. Whatever the model sends back. This is the important one: a language model
   returns text that *looks* like an answer. It is input, not output, and it is
   validated here before any other part of the system is allowed to see it.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Posting(BaseModel):
    """A job posting as ingested. `id` is derived, never supplied by the source."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    source: str = Field(min_length=1, max_length=64)
    external_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=512)
    company: str = Field(min_length=1, max_length=512)
    location: str | None = Field(default=None, max_length=512)
    url: str | None = Field(default=None, max_length=2048)
    description: str = Field(min_length=1)

    @property
    def id(self) -> str:
        return f"{self.source}:{self.external_id}"

    @property
    def content_hash(self) -> str:
        """Changes only when the text a score depends on changes.

        Re-ingesting an unchanged posting produces the same hash, so the work
        unit key already exists and no second score is ever written. That is
        where idempotency comes from here: the key, not a flag set afterwards.
        """
        payload = "\x1f".join([self.title, self.company, self.location or "", self.description])
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


class Dimension(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Literal["skills", "seniority", "domain", "location", "compensation"]
    score: int = Field(ge=0, le=100)
    note: str = Field(min_length=1, max_length=400)


class Score(BaseModel):
    """The shape the model is asked for, and the shape it must return.

    `extra="forbid"` is deliberate. If a future model version invents a field,
    the payload is rejected and retried rather than silently half-understood.
    """

    model_config = ConfigDict(extra="forbid")

    overall: int = Field(ge=0, le=100)
    recommendation: Literal["apply", "maybe", "skip"]
    rationale: str = Field(min_length=1, max_length=1200)
    blockers: list[str] = Field(default_factory=list, max_length=8)
    dimensions: list[Dimension] = Field(min_length=1, max_length=5)

    @field_validator("dimensions")
    @classmethod
    def unique_dimensions(cls, v: list[Dimension]) -> list[Dimension]:
        names = [d.name for d in v]
        if len(names) != len(set(names)):
            raise ValueError("duplicate dimension names")
        return v

    @field_validator("blockers")
    @classmethod
    def clean_blockers(cls, v: list[str]) -> list[str]:
        out = [s.strip() for s in v if s and s.strip()]
        if any(len(s) > 200 for s in out):
            raise ValueError("blocker too long")
        return out


class Usage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    input_tokens: int = 0
    output_tokens: int = 0


class ScoreResult(BaseModel):
    """A validated score plus what it cost to get it."""

    model_config = ConfigDict(extra="forbid")

    score: Score
    model: str
    usage: Usage
    latency_ms: int
    attempts: int = 1


class Profile(BaseModel):
    """The candidate side of the comparison. Versioned on purpose: change the
    criteria, change the version, and every posting becomes new work."""

    model_config = ConfigDict(extra="forbid")

    version: str = Field(min_length=1, max_length=32)
    summary: str = Field(min_length=1)
    must_haves: list[str] = Field(default_factory=list)
    nice_to_haves: list[str] = Field(default_factory=list)
    dealbreakers: list[str] = Field(default_factory=list)
