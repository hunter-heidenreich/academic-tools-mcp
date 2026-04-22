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

from typing import Any

import httpx

# The three families that a well-behaved client should catch around its
# HTTP block. Anything else is a programming error and should propagate.
HTTPX_ERRORS = (httpx.HTTPStatusError, httpx.TimeoutException, httpx.RequestError)


def error_dict(provider: str, exc: Exception) -> dict[str, Any]:
    """Convert an httpx exception into a structured error dict.

    Provider-aware messages so the agent can distinguish transient
    (retry-worthy) failures from permanent ones. ``retry_after_seconds``
    is included on 429 responses when the server advertises it.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status == 429:
            result: dict[str, Any] = {
                "error": f"{provider} rate limit (HTTP 429). Transient — wait and retry.",
            }
            retry_after = exc.response.headers.get("retry-after")
            if retry_after:
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
