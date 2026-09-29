"""Error taxonomy.

The distinction that matters is retryable or not. A 503 from the provider is
worth another attempt. A posting the model will not score in the required shape
is not, and should end up visible as `failed` rather than looping until the
attempt cap burns out.
"""

from __future__ import annotations


class JobmatchError(Exception):
    retryable = False


class ProviderError(JobmatchError):
    """Transport or HTTP-level failure talking to the model provider."""

    retryable = True

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class ProviderRefused(JobmatchError):
    """A 4xx that will not change on retry (bad key, bad request, too large)."""

    retryable = False


class InvalidModelOutput(JobmatchError):
    """The model answered, but not in the contract.

    Worth one repair turn, because models do recover from this. Past that it is
    a prompt or schema problem and a human should see it.
    """

    retryable = True

    def __init__(self, message: str, raw: object = None) -> None:
        super().__init__(message)
        self.raw = raw
