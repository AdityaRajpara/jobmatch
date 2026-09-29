"""The model call.

This talks to /v1/messages over HTTP directly rather than through an SDK or an
agent framework. That is deliberate: the loop is small enough to own, and when
something goes wrong you want to see the request and the response, not a
library's idea of what happened.

The shape of the loop:

    build request -> POST -> HTTP-level retry on 429/5xx/transport
                          -> find the tool_use block
                          -> validate its input with Pydantic
                          -> on validation failure, one repair turn
                          -> give up with an error the queue understands

Everything the model returns is treated as untrusted. There is no path where an
unvalidated payload reaches the database.
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any, Protocol

import httpx
from pydantic import ValidationError

from .config import Config
from .errors import InvalidModelOutput, ProviderError, ProviderRefused
from .models import Posting, Profile, Score, ScoreResult, Usage
from .prompts import REPAIR_PROMPT, SUBMIT_SCORE_TOOL, SYSTEM, build_user_message

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"


class ScoringClient(Protocol):
    async def score(self, posting: Posting, profile: Profile) -> ScoreResult: ...


class AnthropicClient:
    def __init__(self, cfg: Config, http: httpx.AsyncClient) -> None:
        self._cfg = cfg
        self._http = http

    async def score(self, posting: Posting, profile: Profile) -> ScoreResult:
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": build_user_message(posting, profile)}
        ]
        started = time.monotonic()
        usage = Usage()
        last_error: str | None = None

        for turn in range(self._cfg.max_repair_turns + 1):
            payload = await self._post(messages)
            turn_usage = payload.get("usage", {})
            usage = Usage(
                input_tokens=usage.input_tokens + turn_usage.get("input_tokens", 0),
                output_tokens=usage.output_tokens + turn_usage.get("output_tokens", 0),
            )
            try:
                score = _extract_score(payload)
            except InvalidModelOutput as exc:
                last_error = str(exc)
                if turn >= self._cfg.max_repair_turns:
                    raise
                # Feed the failure back. The assistant turn has to be echoed or
                # the model has no idea what it is being asked to fix.
                messages.append({"role": "assistant", "content": payload.get("content", [])})
                messages.append({
                    "role": "user",
                    "content": REPAIR_PROMPT.format(error=last_error[:500]),
                })
                continue

            return ScoreResult(
                score=score,
                model=payload.get("model", self._cfg.model),
                usage=usage,
                latency_ms=int((time.monotonic() - started) * 1000),
                attempts=turn + 1,
            )

        raise InvalidModelOutput(last_error or "model never produced a valid score")

    async def _post(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        body = {
            "model": self._cfg.model,
            "max_tokens": self._cfg.max_output_tokens,
            "system": SYSTEM,
            "messages": messages,
            "tools": [SUBMIT_SCORE_TOOL],
            # Force the tool. Without this the model is free to answer in prose,
            # which is a valid response to a human and a parse error here.
            "tool_choice": {"type": "tool", "name": "submit_score"},
        }
        headers = {
            "x-api-key": self._cfg.api_key or "",
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }

        delay = 1.0
        for attempt in range(1, self._cfg.max_provider_retries + 1):
            try:
                response = await self._http.post(ANTHROPIC_URL, json=body, headers=headers)
            except httpx.TimeoutException as exc:
                # A timeout is not a failure of the request, it is the absence of
                # an answer. The call may well have been billed and the model may
                # have answered into the void. Retrying is safe here only because
                # scoring is a pure read: nothing on the provider side changed.
                if attempt == self._cfg.max_provider_retries:
                    raise ProviderError(f"timed out after {attempt} attempts: {exc}") from exc
            except httpx.HTTPError as exc:
                if attempt == self._cfg.max_provider_retries:
                    raise ProviderError(f"transport error: {exc}") from exc
            else:
                if response.status_code < 300:
                    return response.json()
                if response.status_code in (408, 429) or response.status_code >= 500:
                    if attempt == self._cfg.max_provider_retries:
                        raise ProviderError(
                            f"provider returned {response.status_code}: {response.text[:300]}",
                            status=response.status_code,
                        )
                    delay = _retry_after(response) or delay
                else:
                    # 400, 401, 403, 413. Retrying changes nothing.
                    raise ProviderRefused(
                        f"provider rejected request ({response.status_code}): {response.text[:300]}"
                    )

            # Full jitter: synchronised workers backing off in lockstep is how a
            # transient 429 turns into a self-inflicted thundering herd.
            await asyncio.sleep(random.uniform(0, min(delay, 30.0)))
            delay *= 2

        raise ProviderError("exhausted provider retries")


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


def _extract_score(payload: dict[str, Any]) -> Score:
    """Pull the tool call out of the response and validate it. Nothing else."""
    content = payload.get("content")
    if not isinstance(content, list):
        raise InvalidModelOutput("response had no content blocks", raw=payload)

    tool_uses = [
        b for b in content
        if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "submit_score"
    ]
    if not tool_uses:
        # Usually means the model answered in prose, or stopped early on
        # max_tokens. Both are the model being wrong, not the caller.
        stop = payload.get("stop_reason")
        raise InvalidModelOutput(f"no submit_score tool call (stop_reason={stop})", raw=payload)
    if len(tool_uses) > 1:
        raise InvalidModelOutput(f"expected one tool call, got {len(tool_uses)}", raw=payload)

    try:
        return Score.model_validate(tool_uses[0].get("input"))
    except ValidationError as exc:
        raise InvalidModelOutput(_short_validation_error(exc), raw=tool_uses[0]) from exc


def _short_validation_error(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors()[:4]:
        loc = ".".join(str(p) for p in err["loc"]) or "(root)"
        parts.append(f"{loc}: {err['msg']}")
    return "; ".join(parts)


class FakeClient:
    """Deterministic stand-in so the pipeline runs, and CI passes, with no API key.

    It is not a mock of the HTTP layer: the tests that care about the HTTP layer
    exercise the real client against a stub transport. This exists so someone can
    clone the repo and see the thing work.
    """

    def __init__(self, cfg: Config, *, latency_s: float = 0.05,
                 fail_first_n: int = 0, bad_output_for: set[str] | None = None) -> None:
        self._cfg = cfg
        self._latency = latency_s
        self._remaining_failures = fail_first_n
        self._bad = bad_output_for or set()
        self.calls = 0

    async def score(self, posting: Posting, profile: Profile) -> ScoreResult:
        self.calls += 1
        await asyncio.sleep(self._latency)

        if self._remaining_failures > 0:
            self._remaining_failures -= 1
            raise ProviderError("stub provider failure", status=503)
        if posting.id in self._bad:
            raise InvalidModelOutput("stub produced an unusable payload")

        text = f"{posting.title} {posting.description}".lower()
        hits = sum(1 for kw in profile.must_haves if kw.lower() in text)
        base = 30 + int(60 * hits / max(len(profile.must_haves), 1))
        blockers = [kw for kw in profile.must_haves if kw.lower() not in text][:3]
        score = Score(
            overall=min(base, 100),
            recommendation="apply" if base >= 70 else ("maybe" if base >= 45 else "skip"),
            rationale=f"Stub scorer matched {hits} of {len(profile.must_haves)} must-haves.",
            blockers=[f"no evidence of {b}" for b in blockers],
            dimensions=[{"name": "skills", "score": min(base, 100), "note": "keyword overlap"}],
        )
        return ScoreResult(
            score=score, model="stub", usage=Usage(input_tokens=len(text) // 4, output_tokens=120),
            latency_ms=int(self._latency * 1000), attempts=1,
        )


def build_http_client(cfg: Config) -> httpx.AsyncClient:
    timeout = httpx.Timeout(
        cfg.request_timeout_s, connect=cfg.connect_timeout_s, read=cfg.request_timeout_s
    )
    # The connection pool is the real concurrency limit against the provider.
    limits = httpx.Limits(max_connections=cfg.workers * 2, max_keepalive_connections=cfg.workers)
    return httpx.AsyncClient(timeout=timeout, limits=limits)
