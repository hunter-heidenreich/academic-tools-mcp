"""Per-provider persistent ``httpx.AsyncClient`` pool.

Each provider gets one long-lived client, so a multi-call session pays one
TCP+TLS handshake rather than one per request. ``_app._lifespan`` calls
``aclose_all`` on shutdown.

Pooling is orthogonal to throttling — servers count requests, not connections —
so this neither raises nor lowers 429 risk, and arXiv's documented "single
connection" rule is better honoured by a persistent client than by per-call
sockets.
"""

import asyncio
import contextlib
from typing import Any

import httpx

# Keyed by each provider module's NAMESPACE constant.
_POOL: dict[str, httpx.AsyncClient] = {}


# Each client gets its own pool, so these caps are per-provider: slow OpenAlex can't starve arXiv.
# keepalive_expiry undercuts the idle window after which NAT/firewall devices evict a socket.
_DEFAULT_LIMITS = httpx.Limits(
    max_connections=10,
    max_keepalive_connections=5,
    keepalive_expiry=30.0,
)


def get_client(
    name: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 30.0,
    follow_redirects: bool = True,
    **kwargs: Any,
) -> httpx.AsyncClient:
    """Return the shared AsyncClient for ``name``, creating it on first use.

    ``headers`` and ``timeout`` are baked in at construction; a later call with
    the same ``name`` returns the existing client and **silently ignores its
    kwargs**, so each provider must configure its client in exactly one place.
    Per-call overrides still work through ``client.get(url, timeout=...)``.
    """
    existing = _POOL.get(name)
    if existing is not None:
        return existing
    client = httpx.AsyncClient(
        timeout=timeout,
        limits=_DEFAULT_LIMITS,
        headers=headers or {},
        follow_redirects=follow_redirects,
        **kwargs,
    )
    _POOL[name] = client
    return client


# aclose can hang indefinitely on a wedged socket. Sized well past any healthy close,
# yet short enough that a buggy provider can't hold the FastMCP lifespan open on shutdown.
_ACLOSE_TIMEOUT_SECONDS = 5.0


async def aclose_all() -> None:
    """Close every pooled client. Idempotent.

    Drains the registry first, so a ``get_client`` racing shutdown builds a fresh
    client instead of seeing a half-closed one (that client leaks until exit).

    Invariant: the bound is hard — ``_ACLOSE_TIMEOUT_SECONDS`` covers the whole set
    and ``asyncio.wait`` returns when it expires regardless of whether the closes
    honour cancellation. Transport errors are swallowed per client;
    ``CancelledError`` propagates, so a cancelled shutdown never reports success.
    """
    clients = list(_POOL.values())
    _POOL.clear()
    if not clients:
        return

    async def _close(client: httpx.AsyncClient) -> None:
        # asyncio.wait never retrieves task exceptions, so a raising close swallows its own.
        with contextlib.suppress(Exception):
            await client.aclose()

    tasks = [asyncio.create_task(_close(c)) for c in clients]
    try:
        await asyncio.wait(tasks, timeout=_ACLOSE_TIMEOUT_SECONDS)
    finally:
        # In finally so our cancellation reaps them; never await them — that unbounds shutdown.
        for task in tasks:
            task.cancel()
