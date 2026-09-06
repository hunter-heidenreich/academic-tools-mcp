"""Shared HTTP error normalization for API clients.

Every client wraps its request block in ``try/except HTTPX_ERRORS`` and
returns ``error_dict(provider, exc)`` so transient failures (``_RETRYABLE_STATUSES``,
timeouts, network) surface as the same ``{error, ...}`` dict shape that
the rest of the codebase uses for per-paper / per-author lookup misses.
This keeps the agent on a single error contract regardless of why the
call failed.

Usage. The client is the provider's pooled singleton and the GET goes through
its throttle — never a bare ``httpx.AsyncClient``, which would bypass pooling,
rate limiting, retry and stats (see ``.claude/rules/http.md``)::

    from . import _http

    try:
        response = await _throttled_get(_get_client(), url, params=params)
        if response.status_code == 404:
            return {"error": "No paper found for ..."}
        response.raise_for_status()
        # ... parse and return success
    except _PARSE_ERRORS:
        return _parse_error_dict()
    except _http.HTTPX_ERRORS as e:
        return _http.error_dict("OpenAlex", e)
"""

import asyncio
import json
import math
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from . import _stats


class LocalBackpressureError(Exception):
    """Raised when a throttle has too many requests already queued.

    Distinct from a server-side 429: this is the client refusing to stack more
    work behind its own rate limiter. It is in ``HTTPX_ERRORS``, so it reaches
    the agent through ``error_dict`` like any upstream failure.

    ``provider`` is the throttle's ``label`` — the agent-facing name, which
    ``error_dict`` prefers over its own argument.
    """

    def __init__(
        self,
        provider: str,
        pending: int,
        max_pending: int,
        min_gap_seconds: float = 0.0,
    ) -> None:
        self.provider = provider
        self.pending = pending
        self.max_pending = max_pending
        self.min_gap_seconds = min_gap_seconds
        super().__init__(f"{provider}: {pending} requests already queued (cap {max_pending})")


# The except tuple every client wraps its request block in. Also the documented
# roster of what a client must handle, so redundant entries stay listed.
HTTPX_ERRORS = (
    httpx.HTTPStatusError,
    httpx.TimeoutException,  # a RequestError subclass; its own failure mode
    httpx.RequestError,
    LocalBackpressureError,  # a local refusal reaches the agent as an upstream one
)


# A tuple of one, so a new provider inherits any type added here.
JSON_PARSE_ERRORS: tuple[type[Exception], ...] = (json.JSONDecodeError,)


# Bounds three things at once: the sleep, the exponential backoff's growth, and
# the agent-facing hint. Honours a real multi-minute cooldown, not a bogus 86400.
_MAX_RETRY_AFTER_SECONDS = 600.0  # 10 minutes


# The single definition of "transient status" — both `error_dict` and
# `get_with_retry` read it. An allowlist, not a 5xx range: a 501 Not
# Implemented will not fix itself on retry.
_RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})


def parse_error_dict(provider: str, *, detail: str = "could not be parsed") -> dict[str, Any]:
    """Fresh structured error for an unparseable / malformed provider response.

    Transient — a garbled body says nothing about whether the identifier
    exists — so it is ``retryable: True`` and must never be negative-cached.
    A new dict each call, never a shared constant: a single-flight follower
    receives this same object.
    """
    return {
        "error": f"{provider} returned a response that {detail}.",
        "retryable": True,
    }


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """Parse a ``Retry-After`` value, in either form RFC 9110 permits.

    **Both forms must parse**: Cloudflare- and Wikimedia-fronted endpoints emit
    the HTTP-date form, and dropping it falls back to our own backoff against a
    server that explicitly asked for minutes. Returns ``None`` for a missing,
    unparseable, non-positive or non-finite value, in which case the caller's
    own backoff applies.
    """
    raw = (response.headers.get("retry-after") or "").strip()
    if not raw:
        return None

    value: float | None
    try:
        value = float(raw)
    except ValueError:
        value = _retry_after_from_http_date(raw)
    # `nan` parses as a float and fails every comparison, so `<= 0` never
    # catches it; isfinite is what keeps it out of a sleep.
    if value is None or not math.isfinite(value) or value <= 0:
        return None
    return value


def _retry_after_from_http_date(raw: str) -> float | None:
    """Seconds until an HTTP-date ``Retry-After``, or None if unparseable."""
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        # RFC 9110 requires GMT; treat a naive value as UTC rather than
        # local time, which would shift the wait by the host's offset.
        when = when.replace(tzinfo=UTC)
    return (when - datetime.now(UTC)).total_seconds()


def _backpressure_dict(provider: str, exc: LocalBackpressureError) -> dict[str, Any]:
    """Structured local refusal, carrying both remediations.

    How long to wait and how much parallelism is safe each go in the message
    *and* in a field, so neither kind of agent has to parse the other's form.
    """
    # A Throttle's `label` is the agent-facing name; the argument is a fallback.
    provider = exc.provider or provider
    gap = exc.min_gap_seconds
    hint = f"Wait ≥{gap:.2f}s before retrying" if gap > 0 else "Retry shortly"
    result: dict[str, Any] = {
        "error": (
            f"Local backpressure: {exc.pending} {provider} requests "
            f"already queued (cap {exc.max_pending}). "
            f"{hint} or reduce concurrency to "
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


def error_dict(provider: str, exc: Exception) -> dict[str, Any]:
    """Convert an httpx exception into a structured error dict.

    Provider-aware messages so the agent can distinguish transient
    (retry-worthy) failures from permanent ones.

    **Every transient outcome carries ``retryable: True``**; other 4xx are
    left unflagged rather than ``retryable: False``. ``retry_after_seconds``
    rides along on any transient status the server advertises one for.
    ``.claude/rules/http.md`` has the why for both.
    """
    if isinstance(exc, LocalBackpressureError):
        return _backpressure_dict(provider, exc)
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        transient: str | None = None
        if status == 429:
            transient = f"{provider} rate limit (HTTP 429). Transient — wait and retry."
        elif status in _RETRYABLE_STATUSES:
            kind = "server error" if status >= 500 else "temporary rejection"
            transient = f"{provider} {kind} (HTTP {status}). Transient — retry."
        if transient is not None:
            result = {"error": transient, "retryable": True}
            retry_after = _retry_after_seconds(exc.response)
            if retry_after is not None:
                # Same ceiling as the internal retry path. Change one, change both.
                result["retry_after_seconds"] = min(retry_after, _MAX_RETRY_AFTER_SECONDS)
            return result
        # ResponseNotRead is a RuntimeError, so HTTPX_ERRORS misses it upstream.
        try:
            snippet = exc.response.content[:200].decode("utf-8", "replace")
        except httpx.ResponseNotRead:
            snippet = "<streaming response body not read>"
        return {
            "error": f"{provider} HTTP {status}: {snippet}",
        }
    # Order is load-bearing: TimeoutException is a RequestError subclass, so
    # the narrower check must come first or every timeout reads "network error".
    if isinstance(exc, httpx.TimeoutException):
        return {"error": f"{provider} request timed out. Transient — retry.", "retryable": True}
    if isinstance(exc, httpx.RequestError):
        return {"error": f"{provider} network error: {exc!s}", "retryable": True}
    # Defensive: should never hit because callers narrow their except clause
    return {"error": f"{provider} unexpected error: {exc!s}"}


# ---------------------------------------------------------------------------
# Transparent retry on transient failures
# ---------------------------------------------------------------------------


async def get_with_retry(
    client: httpx.AsyncClient,
    url: str,
    *,
    max_attempts: int = 2,
    backoff_seconds: float = 1.0,
    provider: str | None = None,
    **kwargs: Any,
) -> httpx.Response:
    """Issue a GET with transparent retries on transient failure.

    Transient = an httpx network/timeout exception, or a status in
    ``_RETRYABLE_STATUSES``. Everything else is returned as-is on the first
    attempt, for the caller's ``raise_for_status`` or status branch to handle.

    The sleep after a failed attempt *n* is ``min(max(Retry-After,
    backoff_seconds * 2**(n-1)), _MAX_RETRY_AFTER_SECONDS)``. ``backoff_seconds``
    floors it at the provider's own throttle gap; the exponential term widens
    later retries so they straddle a cooldown instead of landing in the same
    window; the ceiling stops a misconfigured ``Retry-After`` pinning the
    throttle. ``Retry-After`` is read on any retryable status, in both RFC 9110
    forms.

    The final attempt returns its response or re-raises. ``max_attempts=2`` is
    1 original + 1 retry, set per provider by ``_throttle.Throttle``.

    GET-only: every cached lookup in this codebase is a GET.
    """
    # Clamp: skipping the loop leaves `response` unbound, and the resulting
    # UnboundLocalError is a NameError, which HTTPX_ERRORS does not catch.
    max_attempts = max(1, max_attempts)
    for attempt in range(1, max_attempts + 1):
        # Factor is 1 on the first attempt, so the first retry waits exactly
        # backoff_seconds.
        effective_backoff = min(backoff_seconds * (2 ** (attempt - 1)), _MAX_RETRY_AFTER_SECONDS)
        if provider is not None:
            # Per outbound request, not per throttle slot: one slot issues up
            # to max_attempts of them, and this is the politeness-audit number.
            _stats.incr(provider, "http_calls")
        try:
            response = await client.get(url, **kwargs)
        except httpx.RequestError:  # includes TimeoutException
            if attempt >= max_attempts:
                raise
            if provider is not None:
                _stats.incr(provider, "http_retries")
            await asyncio.sleep(effective_backoff)
            continue

        if attempt >= max_attempts:
            return response
        if response.status_code not in _RETRYABLE_STATUSES:
            return response

        if provider is not None:
            _stats.incr(provider, "http_retries")
        retry_after = _retry_after_seconds(response) or 0.0
        sleep_for = min(max(retry_after, effective_backoff), _MAX_RETRY_AFTER_SECONDS)
        await asyncio.sleep(sleep_for)

    # Unreachable: the loop always returns or raises before falling out.
    return response  # pragma: no cover
