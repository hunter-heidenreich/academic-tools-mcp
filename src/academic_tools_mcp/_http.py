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
import json
import math
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
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
        super().__init__(f"{provider}: {pending} requests already queued (cap {max_pending})")


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


# JSON providers all decode with ``json.loads`` and all want the same
# treatment for a body that won't parse. Single-homed here so a new provider
# can't forget one of the exception types.
JSON_PARSE_ERRORS: tuple[type[Exception], ...] = (json.JSONDecodeError,)


def parse_error_dict(provider: str, *, detail: str = "could not be parsed") -> dict[str, Any]:
    """Fresh structured error for an unparseable / malformed provider response.

    A parse failure is *transient* — a truncated or garbled body says nothing
    about whether the identifier exists — so the result carries
    ``retryable: True`` and callers must not negative-cache it.

    A new dict each call (like ``error_dict``) so a caller, or a single-flight
    follower sharing the result, can't mutate a shared object.

    ``detail`` lets a provider be more specific (arXiv speaks XML, not JSON)
    without forking the shape.
    """
    return {
        "error": f"{provider} returned a response that {detail}.",
        "retryable": True,
    }


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """Parse a ``Retry-After`` value, in either form RFC 9110 permits.

    **Both forms must parse.** The delay-seconds form (``Retry-After: 120``)
    and the HTTP-date form (``Retry-After: Wed, 21 Oct 2015 07:28:00 GMT``) are
    equally valid, and Wikimedia- and Cloudflare-fronted endpoints do emit
    dates. Dropping either silently falls back to our own backoff — as little
    as 1.0s against a server that just asked for minutes, in the one situation
    where politeness matters most: it told us explicitly.

    Returns ``None`` for a missing, unparseable, non-positive, or non-finite
    value, in which case the caller's own backoff applies.
    """
    raw = response.headers.get("retry-after")
    if not raw:
        return None

    raw = raw.strip()
    value: float | None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = _retry_after_from_http_date(raw)
    if value is None:
        return None

    if not math.isfinite(value):
        # "inf"/"nan" parse as floats but are not a wait instruction.
        return None
    return value if value > 0 else None


def _retry_after_from_http_date(raw: str) -> float | None:
    """Seconds until an HTTP-date ``Retry-After``, or None if unparseable."""
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        # RFC 9110 requires GMT; treat a naive value as UTC rather than
        # local time, which would shift the wait by the host's offset.
        when = when.replace(tzinfo=UTC)
    return (when - datetime.now(UTC)).total_seconds()


# Absolute ceiling on a single Retry-After sleep. We honour genuine
# multi-minute cooldowns (max_attempts=2 means at most one sleep ever
# happens), but a misconfigured ``Retry-After: 86400`` must not pin our
# throttle for hours.
_MAX_RETRY_AFTER_SECONDS = 600.0  # 10 minutes


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
            result = {
                "error": f"{provider} rate limit (HTTP 429). Transient — wait and retry.",
            }
            retry_after = _retry_after_seconds(exc.response)
            if retry_after is not None:
                # Invariant: the hint we hand the agent honours the same
                # _MAX_RETRY_AFTER_SECONDS ceiling as the internal retry path,
                # so a misconfigured "Retry-After: 86400" can't tell the agent
                # to wait a day. Change one, change both.
                result["retry_after_seconds"] = min(retry_after, _MAX_RETRY_AFTER_SECONDS)
            return result
        if 500 <= status < 600:
            return {
                "error": f"{provider} server error (HTTP {status}). Transient — retry.",
            }
        # Other 4xx — surface a snippet of the body for debugging.
        #
        # `.text` is only available once the body has been read. On a
        # `client.stream()` response it raises httpx.ResponseNotRead, which is
        # a RuntimeError rather than an HTTPError and so is *not* caught by the
        # `except HTTPX_ERRORS` blocks that call this helper: it escaped the
        # provider entirely and replaced a plain "HTTP 403" with
        # "Attempted to access streaming response content, without having
        # called read()". Callers that stream should aread() the body on an
        # error status (see `_pdf_download.stream_to_file`); this guard keeps a
        # caller that does not from losing the status code as well.
        try:
            snippet = exc.response.text[:200]
        except httpx.ResponseNotRead:
            snippet = "<streaming response body not read>"
        return {
            "error": f"{provider} HTTP {status}: {snippet}",
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
    """Issue a GET with transparent retries on transient failure.

    Transient = httpx network/timeout exception, or a status in
    ``_RETRYABLE_STATUSES`` — an explicit allowlist, *not* every 5xx. All
    other outcomes are returned as-is on the first attempt; the caller's
    ``raise_for_status`` or status-code branch handles them.

    On 429 (and 503) we honour ``Retry-After`` when present. The actual
    sleep is ``min(max(Retry-After, effective_backoff), _MAX_RETRY_AFTER_SECONDS)``,
    so a server that asks us to wait several minutes is respected (up to a
    10-minute ceiling), a missing or zero header doesn't drop us below the
    provider's own throttle gap (``backoff_seconds`` is the floor), and a
    misconfigured ``Retry-After: 86400`` can't pin our throttle for hours.

    Backoff grows exponentially across attempts: the sleep *after* a failed
    attempt *n* uses ``backoff_seconds * 2**(n-1)``, so the first retry
    waits exactly ``backoff_seconds``. At the default ``max_attempts=2``
    only one sleep ever happens (factor ``2**0 = 1``), so single-retry
    providers are unchanged. A provider that opts into more attempts — e.g.
    arXiv, whose Fastly edge returns 429/503 with **no** ``Retry-After`` when
    an IP is briefly penalty-boxed — gets a widening gap (backoff, 2×, 4×…)
    so the later retries straddle the cooldown instead of all landing inside
    the same throttled window.

    On the FINAL attempt the result is returned (or the exception
    re-raised) without further retry. ``max_attempts=2`` means 1 original +
    1 retry; the throttle a provider holds (`_throttle.Throttle`) sets this
    per provider, so a flakier upstream can ask for more without every
    caller masking a sustained outage.

    GET-only by design: every cached lookup in this codebase is a GET
    and the caller's existing test mocks all stub ``client.get``, so a
    method-agnostic helper would force unrelated mock churn.
    """
    for attempt in range(1, max_attempts + 1):
        # Exponential growth per attempt; capped so a high max_attempts can't
        # produce an absurd sleep. Factor is 1 on the first attempt, so the
        # default single-retry path is byte-for-byte unchanged.
        effective_backoff = min(backoff_seconds * (2 ** (attempt - 1)), _MAX_RETRY_AFTER_SECONDS)
        if provider is not None:
            # Counted per actual outbound request, not per throttle slot. A
            # slot can issue up to max_attempts requests (3 for arXiv), so
            # counting at slot entry under-reported real outbound volume by
            # up to 3x — exactly the number a politeness audit reads.
            _stats.incr(provider, "http_calls")
        try:
            response = await client.get(url, **kwargs)
        except (httpx.TimeoutException, httpx.RequestError):
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
