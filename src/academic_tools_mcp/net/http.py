"""Shared HTTP error normalization for API clients.

Every client wraps its request block in ``try/except HTTPX_ERRORS`` and returns
``error_dict(provider, exc)``, so a transient failure (``_RETRYABLE_STATUSES``, timeout,
network, backpressure) reaches the agent as the same ``{error, ...}`` dict a per-paper /
per-author lookup miss does — one error contract regardless of why the call failed.

Usage. The client is the provider's pooled singleton and the GET goes through its throttle
— never a bare ``httpx.AsyncClient``, which bypasses pooling, rate limiting, retry and stats::

    from ..net import http

    try:
        response = await _throttled_get(url, params=params)
        if response.status_code == 404:
            return http.not_found("No paper found for ...")
        response.raise_for_status()
        # ... parse and return success
    except _PARSE_ERRORS:
        return _parse_error_dict()
    except http.HTTPX_ERRORS as e:
        return http.error_dict("OpenAlex", e)
"""

import asyncio
import json
import math
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from . import stats


class LocalBackpressureError(Exception):
    """Raised when a throttle has too many requests already queued.

    Not a server-side 429: the client is refusing to stack more work behind its own rate
    limiter. It is in ``HTTPX_ERRORS``, so it reaches the agent through ``error_dict`` like
    any upstream failure, and ``provider`` is the throttle's ``label`` — the agent-facing
    name, which ``error_dict`` prefers over its own argument.
    """

    def __init__(
        self,
        provider: str,
        pending: int,
        max_pending: int,
        min_gap_seconds: float = 0.0,
    ) -> None:
        """Record which provider refused, and how deep its queue was."""
        self.provider = provider
        self.pending = pending
        self.max_pending = max_pending
        self.min_gap_seconds = min_gap_seconds
        super().__init__(f"{provider}: {pending} requests already queued (cap {max_pending})")


# The except tuple every client wraps its request block in — and the roster of
# what a client must handle, so redundant entries stay listed.
HTTPX_ERRORS = (
    httpx.HTTPStatusError,
    httpx.TimeoutException,  # a RequestError subclass; its own failure mode
    httpx.RequestError,
    LocalBackpressureError,  # a local refusal reaches the agent as an upstream one
)


# A tuple of one, so a new provider inherits any type added here.
JSON_PARSE_ERRORS: tuple[type[Exception], ...] = (json.JSONDecodeError,)


# Bounds the sleep, the backoff's growth and the agent-facing hint: honours a real
# multi-minute cooldown, not a bogus 86400.
_MAX_RETRY_AFTER_SECONDS = 600.0  # 10 minutes


# The single definition of "transient status": `error_dict` and `get_with_retry` both read it.
# An allowlist, not a 5xx range — a 501 Not Implemented will not fix itself on retry.
_RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})


def addresses_a_record(url: str) -> bool:
    """Whether ``url``'s path still names the single record it was built for.

    The request-side guard ``quote`` cannot be: a ``.``/``..`` segment is *removed* by RFC 3986
    resolution after percent-encoding — both characters are unreserved, so no encoder escapes
    them — and an identifier that normalized to nothing leaves the path at the collection.
    Either way the request lands on a shorter, existing endpoint whose answer then caches under
    the key we asked for.
    """
    path = urlsplit(url).path
    if not path or path.endswith("/"):
        return False
    # Read on the *encoded* path, so an escaped `%2E` is a normal segment, not a dot segment.
    return not any(segment in (".", "..") for segment in path.split("/"))


def not_found(message: str) -> dict[str, Any]:
    """Fresh definitive miss: the record is absent, not briefly unreachable.

    ``not_found: True`` is the flag callers classify on. A new dict each call, as
    ``parse_error_dict`` is.
    """
    return {"error": message, "not_found": True}


def parse_error_dict(provider: str, *, detail: str = "could not be parsed") -> dict[str, Any]:
    """Fresh structured error for an unparseable / malformed provider response.

    Transient — a garbled body says nothing about whether the identifier exists — so
    ``retryable: True``, never negative-cached. A new dict each call, never a shared
    constant: a single-flight follower receives this same object.
    """
    return {
        "error": f"{provider} returned a response that {detail}.",
        "retryable": True,
    }


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """Parse a ``Retry-After`` value, in either form RFC 9110 permits.

    **Both forms must parse**: Cloudflare- and Wikimedia-fronted endpoints emit the HTTP-date
    form, and dropping it falls back to our own backoff against a server that explicitly asked
    for minutes. Returns ``None`` for a missing, unparseable, non-positive or non-finite value,
    leaving the caller's own backoff to apply.
    """
    raw = (response.headers.get("retry-after") or "").strip()
    if not raw:
        return None

    value: float | None
    try:
        value = float(raw)
    except ValueError:
        value = _retry_after_from_http_date(raw)
    # `nan` parses as a float and fails every comparison, so `<= 0` misses it;
    # isfinite is what keeps it out of a sleep.
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
        # RFC 9110 requires GMT; a naive value is UTC, not local time — which would
        # shift the wait by the host's offset.
        when = when.replace(tzinfo=UTC)
    return (when - datetime.now(UTC)).total_seconds()


def _backpressure_dict(provider: str, exc: LocalBackpressureError) -> dict[str, Any]:
    """Structured local refusal, carrying both remediations.

    How long to wait and how much parallelism is safe each go in the message *and* in a
    field, so neither kind of agent has to parse the other's form — except
    ``retry_after_seconds``, omitted at gap 0, where "retry after 0s" is not advice.
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
    """Convert an ``HTTPX_ERRORS`` exception into a structured, provider-aware error dict.

    **Every transient outcome carries ``retryable: True``** — that key, not the "Transient —
    retry." prose, is what a caller branches on; other 4xx are left unflagged rather than
    ``retryable: False``. ``retry_after_seconds`` rides along on any transient status the
    server advertises one for, clamped to ``_MAX_RETRY_AFTER_SECONDS``.
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
    # Order is load-bearing: TimeoutException subclasses RequestError, so the narrower
    # check must come first or every timeout reads "network error".
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

    The sleep after a failed attempt *n* is ``min(max(Retry-After, backoff_seconds * 2**(n-1)),
    _MAX_RETRY_AFTER_SECONDS)``; on the transport-exception path there is no response, so only
    the backoff term applies. ``backoff_seconds`` is the floor — ``Throttle.get`` passes the
    provider's gap floored at one second, so a retry cannot undercut the documented rate; the
    exponential term widens later retries so they straddle a cooldown instead of landing in the
    same window; the ceiling stops a misconfigured ``Retry-After`` pinning the throttle.
    ``Retry-After`` is read on any retryable status, in both RFC 9110 forms.

    The final attempt returns its response or re-raises. ``max_attempts=2`` is 1 original + 1
    retry, set per provider by ``throttle.Throttle``. GET-only: every cached lookup is a GET.
    """
    # Clamp: a skipped loop leaves `response` unbound, and UnboundLocalError is a NameError,
    # which HTTPX_ERRORS does not catch.
    max_attempts = max(1, max_attempts)
    for attempt in range(1, max_attempts + 1):
        # Factor 1 on attempt 1, so the first retry waits exactly backoff_seconds.
        effective_backoff = min(backoff_seconds * (2 ** (attempt - 1)), _MAX_RETRY_AFTER_SECONDS)
        if provider is not None:
            # Per outbound request, not per throttle slot: one slot issues up
            # to max_attempts of them, and this is the politeness-audit number.
            stats.incr(provider, "http_calls")
        try:
            response = await client.get(url, **kwargs)
        except httpx.RequestError:  # includes TimeoutException
            if attempt >= max_attempts:
                raise
            if provider is not None:
                stats.incr(provider, "http_retries")
            await asyncio.sleep(effective_backoff)
            continue

        if attempt >= max_attempts:
            return response
        if response.status_code not in _RETRYABLE_STATUSES:
            return response

        if provider is not None:
            stats.incr(provider, "http_retries")
        retry_after = _retry_after_seconds(response) or 0.0
        sleep_for = min(max(retry_after, effective_backoff), _MAX_RETRY_AFTER_SECONDS)
        await asyncio.sleep(sleep_for)

    # Unreachable: the loop always returns or raises before falling out.
    return response  # pragma: no cover
