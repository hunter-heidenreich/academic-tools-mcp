"""Shared HTTP error normalization for API clients.

Every client wraps its request block in ``try/except HTTPX_ERRORS`` and
returns ``error_dict(provider, exc)`` so transient failures (5xx, 429,
timeouts, network) surface as the same ``{error, ...}`` dict shape that
the rest of the codebase uses for per-paper / per-author lookup misses.
This keeps the agent on a single error contract regardless of why the
call failed.

Usage:

    from . import _http

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, params=params)
        if response.status_code == 404:
            return {"error": "No paper found for ..."}
        response.raise_for_status()
        # ... parse and return success
    except _http.HTTPX_ERRORS as e:
        return _http.error_dict("OpenAlex", e)
"""

import asyncio
from typing import Any

import httpx

from . import _stats


class LocalBackpressureError(Exception):
    """Raised when a throttle has too many requests already queued.

    Distinct from server-side 429: this is the client refusing to stack
    more work behind its own rate limiter. Surfaces to agents as a
    structured ``{error, retryable: True, backpressure: True}`` with a
    concrete remediation (the throttle gap and concurrency cap) so
    they can pick a sensible retry interval instead of guessing.
    """

    def __init__(
        self,
        provider: str,
        pending: int,
        max_pending: int,
        min_gap_seconds: float = 0.0,
    ):
        self.provider = provider
        self.pending = pending
        self.max_pending = max_pending
        self.min_gap_seconds = min_gap_seconds
        super().__init__(
            f"{provider}: {pending} requests already queued (cap {max_pending})"
        )


# Families a well-behaved client should catch around its HTTP block.
# Anything else is a programming error and should propagate.
# LocalBackpressureError is included so that the existing `try/except
# HTTPX_ERRORS → error_dict` flow turns it into the same structured
# error dict shape as a real upstream failure.
HTTPX_ERRORS = (
    httpx.HTTPStatusError,
    httpx.TimeoutException,
    httpx.RequestError,
    LocalBackpressureError,
)


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """Parse a numeric ``Retry-After`` value if the server sent one.

    HTTP-date forms (``Retry-After: Wed, 21 Oct 2015 07:28:00 GMT``) are
    not supported; we just return None and fall back to our own backoff.
    """
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def error_dict(provider: str, exc: Exception) -> dict[str, Any]:
    """Convert an httpx exception into a structured error dict.

    Provider-aware messages so the agent can distinguish transient
    (retry-worthy) failures from permanent ones. ``retry_after_seconds``
    is included on 429 responses when the server advertises it.
    """
    if isinstance(exc, LocalBackpressureError):
        # Concrete remediation: tell the agent the throttle gap (so it
        # picks a sensible retry interval) and the concurrency cap (so
        # it knows how many parallel calls are safe). Agents that
        # branch on the structured fields below get the same data
        # without parsing the error string.
        gap = exc.min_gap_seconds
        if gap > 0:
            wait_hint = f"wait ≥{gap:.2f}s before retrying"
        else:
            wait_hint = "retry shortly"
        result: dict[str, Any] = {
            "error": (
                f"Local backpressure: {exc.pending} {provider} requests "
                f"already queued (cap {exc.max_pending}). "
                f"{wait_hint.capitalize()} or reduce concurrency to "
                f"≤{exc.max_pending} parallel calls. The server enforces "
                "this cap before hitting the upstream rate limiter."
            ),
            "retryable": True,
            "backpressure": True,
            "max_concurrency": exc.max_pending,
        }
        if gap > 0:
            result["retry_after_seconds"] = gap
        return result
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status == 429:
            result: dict[str, Any] = {
                "error": f"{provider} rate limit (HTTP 429). Transient — wait and retry.",
            }
            retry_after = _retry_after_seconds(exc.response)
            if retry_after is not None:
                result["retry_after_seconds"] = retry_after
            return result
        if 500 <= status < 600:
            return {
                "error": f"{provider} server error (HTTP {status}). Transient — retry.",
            }
        # Other 4xx — surface a snippet of the body for debugging
        return {
            "error": f"{provider} HTTP {status}: {exc.response.text[:200]}",
        }
    if isinstance(exc, httpx.TimeoutException):
        return {"error": f"{provider} request timed out. Transient — retry."}
    if isinstance(exc, httpx.RequestError):
        return {"error": f"{provider} network error: {exc!s}"}
    # Defensive: should never hit because callers narrow their except clause
    return {"error": f"{provider} unexpected error: {exc!s}"}


# ---------------------------------------------------------------------------
# Transparent one-shot retry on transient failures
# ---------------------------------------------------------------------------

# HTTP statuses that are universally agreed-upon as transient. 408
# (Request Timeout) and 425 (Too Early) are bundled in for completeness;
# the rest are 429 + standard 5xx.
_RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})


async def get_with_retry(
    client: httpx.AsyncClient,
    url: str,
    *,
    max_attempts: int = 2,
    backoff_seconds: float = 1.0,
    provider: str | None = None,
    **kwargs: Any,
) -> httpx.Response:
    """Issue a GET with one transparent retry on transient failure.

    Transient = httpx network/timeout exception, 408/425/429, or any 5xx
    response. All other outcomes (200, 4xx other than the above) are
    returned as-is on the first attempt — the caller's ``raise_for_status``
    or status-code branch handles them.

    On 429 (and 503) we honour ``Retry-After`` when present. The actual
    sleep is ``max(Retry-After, backoff_seconds)`` so a server that asks
    us to wait 5 minutes is respected, but a missing or zero header
    doesn't drop us below the provider's own throttle gap. There's a
    safety cap at ``backoff_seconds * 30`` so a misconfigured server can
    not pin our throttle indefinitely.

    On the FINAL attempt the result is returned (or the exception
    re-raised) without further retry. ``max_attempts=2`` means 1 original
    + 1 retry — the upstream APIs are well-behaved enough that a single
    transient blip is the common case and a sustained outage is not
    something we should mask from the agent.

    GET-only by design: every cached lookup in this codebase is a GET
    and the caller's existing test mocks all stub ``client.get``, so a
    method-agnostic helper would force unrelated mock churn.
    """
    cap = backoff_seconds * 30
    for attempt in range(1, max_attempts + 1):
        try:
            response = await client.get(url, **kwargs)
        except (httpx.TimeoutException, httpx.RequestError):
            if attempt >= max_attempts:
                raise
            if provider is not None:
                _stats.incr(provider, "http_retries")
            await asyncio.sleep(backoff_seconds)
            continue

        if attempt >= max_attempts:
            return response
        if response.status_code not in _RETRYABLE_STATUSES:
            return response

        if provider is not None:
            _stats.incr(provider, "http_retries")
        retry_after = _retry_after_seconds(response) or 0.0
        sleep_for = min(max(retry_after, backoff_seconds), cap)
        await asyncio.sleep(sleep_for)

    # Unreachable: the loop always returns or raises before falling out.
    return response  # pragma: no cover
