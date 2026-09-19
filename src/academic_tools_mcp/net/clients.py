"""Per-provider persistent ``httpx.AsyncClient`` pool.

One long-lived client per provider amortizes the TCP+TLS handshake rather than
paying one per request; ``app._lifespan`` calls ``aclose_all`` on shutdown.

Pooling is orthogonal to throttling — servers count requests, not connections —
so this neither raises nor lowers 429 risk, and arXiv's documented "single
connection" rule is better honoured by a persistent client than by per-call
sockets.
"""

import asyncio
import contextlib
from typing import Any

import httpx

from . import stats

# Keyed by each provider module's NAMESPACE constant.
_POOL: dict[str, httpx.AsyncClient] = {}

# What each pooled client was built with, so an ignored reconfiguration is countable.
_BUILD_CONFIG: dict[str, dict[str, Any]] = {}


# Per-client connection pool, so these caps are per-provider: slow OpenAlex can't starve arXiv.
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

    Every argument is baked in at construction; a later call with the same
    ``name`` returns the existing client and **silently ignores its kwargs**, so
    a provider configures its client in exactly one place.
    Per-call overrides still work through ``client.get(url, timeout=...)``.

    Silently, but not invisibly: a second call asking for a *different* configuration
    counts ``client_config_ignored``, whose only other symptom is a wrong timeout.
    """
    config: dict[str, Any] = {
        "timeout": timeout,
        "headers": headers or {},
        "follow_redirects": follow_redirects,
        **kwargs,
    }
    existing = _POOL.get(name)
    if existing is not None:
        if _BUILD_CONFIG.get(name) != config:
            stats.incr(name, "client_config_ignored")
        return existing
    client = httpx.AsyncClient(limits=_DEFAULT_LIMITS, **config)
    _POOL[name] = client
    _BUILD_CONFIG[name] = config
    return client


# aclose can hang forever on a wedged socket: past any healthy close, short of
# pinning the FastMCP lifespan open.
_ACLOSE_TIMEOUT_SECONDS = 5.0


async def aclose_all() -> None:
    """Close every pooled client. Idempotent.

    Drains the registry first: a ``get_client`` racing shutdown then builds a
    fresh client (which leaks until exit) rather than getting a half-closed one.

    Invariant: the bound is hard — ``_ACLOSE_TIMEOUT_SECONDS`` covers the whole
    set and ``asyncio.wait`` returns at expiry whether or not a close honours
    cancellation. Any exception from a close is swallowed; ``CancelledError``
    propagates, so a cancelled shutdown never reports success.
    """
    clients = list(_POOL.values())
    _POOL.clear()
    _BUILD_CONFIG.clear()
    if not clients:
        return

    async def _close(client: httpx.AsyncClient) -> None:
        # asyncio.wait never retrieves a task's exception, so swallow it here, not at GC.
        with contextlib.suppress(Exception):
            await client.aclose()

    tasks = [asyncio.create_task(_close(c)) for c in clients]
    try:
        await asyncio.wait(tasks, timeout=_ACLOSE_TIMEOUT_SECONDS)
    finally:
        # In finally so our cancellation reaps them; never await them — that unbounds shutdown.
        for task in tasks:
            task.cancel()
