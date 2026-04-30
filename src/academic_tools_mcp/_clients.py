"""Per-provider persistent ``httpx.AsyncClient`` pool.

Every API client used to create a fresh ``AsyncClient`` per request, which
meant a TCP+TLS handshake on every call. With this pool, each provider
gets one long-lived client that reuses keep-alive connections across all
its calls. Latency win on multi-call sessions (browse a paper's 50
references → 50 follow-up metadata lookups), no behavior change.

A note on rate limits: pooling is orthogonal to throttling. Servers count
*requests*, not connections, so reusing one socket vs. opening a fresh
one doesn't change your 429 risk. arXiv's documented "single connection"
rule is in fact better honoured by a persistent client than by per-call
sockets.

Lifecycle: the FastMCP server registers ``aclose_all`` via lifespan so
sockets close cleanly on shutdown.
"""

import asyncio
from typing import Any

import httpx

# Per-provider singletons keyed by provider name string ("arxiv",
# "openalex", "crossref", ...). Lazy-built on first get_client call.
_clients: dict[str, httpx.AsyncClient] = {}


# Conservative pool config. The cap is per-provider, not global, so a
# slow OpenAlex doesn't starve arXiv. keepalive_expiry drops sockets
# that have been idle long enough that intermediate NAT/firewall
# devices may have evicted them.
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

    ``headers`` and ``timeout`` are baked into the client on construction
    and apply to every call through it. Per-call overrides still work via
    ``client.get(url, timeout=...)``.

    Subsequent calls with the same ``name`` ignore the construction kwargs
    and just return the existing client — clients are configured once and
    reused. Passing different headers on a later call is a programming
    error (silently ignored).
    """
    existing = _clients.get(name)
    if existing is not None:
        return existing
    client = httpx.AsyncClient(
        timeout=timeout,
        limits=_DEFAULT_LIMITS,
        headers=headers or {},
        follow_redirects=follow_redirects,
        **kwargs,
    )
    _clients[name] = client
    return client


# A wedged socket (server gone but the kernel hasn't surfaced an error
# yet, or a TLS shutdown that's hanging on the peer) can pin aclose
# indefinitely. 5s is well past any healthy close and short enough that
# a buggy provider doesn't keep the FastMCP lifespan alive on shutdown.
_ACLOSE_TIMEOUT_SECONDS = 5.0


async def aclose_all() -> None:
    """Close every pooled client. Idempotent; safe to call multiple times.

    Drains the registry first so a concurrent ``get_client`` call during
    shutdown can't see a half-closed client (it would build a new one
    instead, which is fine — that one will leak, but only briefly).

    Each ``aclose`` is bounded by ``_ACLOSE_TIMEOUT_SECONDS``: a wedged
    socket on one provider must not block shutdown on the others, and
    must not pin the FastMCP lifespan. Timeout falls through silently
    (the socket gets reaped when the process exits).
    """
    clients = list(_clients.values())
    _clients.clear()
    for client in clients:
        try:
            await asyncio.wait_for(
                client.aclose(), timeout=_ACLOSE_TIMEOUT_SECONDS
            )
        except (asyncio.TimeoutError, Exception):
            # Shutdown is best-effort; do not let one stuck client
            # block the others from closing.
            pass
