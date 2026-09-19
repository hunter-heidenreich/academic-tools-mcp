"""The cached-download protocol every ``download_pdf`` shares.

``cache.cached_lookup``'s file-on-disk sibling, plus the predicate deciding which
failures are worth a negative entry. Kept apart from the transport: what is worth
caching is a policy question, not a wire one.
"""

import copy
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from ..store import cache, singleflight
from . import artifact


def is_definitive_failure(result: dict[str, Any]) -> bool:
    """Whether a failure is paper-intrinsic, and so worth negative-caching.

    An allowlist: an ``error`` dict explicitly marked ``retryable: False``,
    ``max_bytes`` aborts excepted. "Not marked retryable" would negative-cache
    every unclassified 4xx, and a paywalled 403 is not known to be permanent; a
    cap bump fixes ``max_bytes``, which says nothing about the paper.
    """
    return "error" in result and result.get("retryable") is False and "max_bytes" not in result


async def cached_download(
    *,
    single_flight: singleflight.SingleFlight,
    namespace: str,
    entity: str,
    canonical: str,
    dest: Path,
    fetch: Callable[[], Awaitable[dict[str, Any]]],
    neg_ttl: float,
    force_refresh: bool = False,
    sf_key: Any = None,
    extra_fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the shared cached-download protocol around a provider's ``fetch``.

    The file-on-disk sibling of :func:`cache.cached_lookup`: force_refresh →
    check → single-flight → in-slot re-check → ``fetch``, in one place so the
    four ``download_pdf`` implementations can't drift.

    ``fetch`` resolves the URL and streams it, returning a plain result dict.
    Provider quirks (arXiv/bioRxiv awaiting their own ``get_paper``, OpenAlex
    resolution for the OA path) live in that closure, and it never touches
    ``cache`` — this function decides what is worth negative-caching.

    ``neg_ttl`` is required because only the negative half is a cache record:
    the PDF *is* the positive entry, with ``artifact.is_usable_pdf`` as its freshness
    rule. Pass ``sf_key`` when ``fetch`` awaits another getter sharing this
    ``SingleFlight``, or it will await its own slot and deadlock.

    ``force_refresh`` re-fetches and drops the negative entry but never the
    PDF, so a failed refresh leaves the caller the copy they had.
    ``extra_fields`` decorates every *successful* payload, cached and fresh
    alike, so the two branches can't disagree; errors stay undecorated. Each
    caller gets an independent deep copy, safe to mutate.
    """

    def _decorate(result: dict[str, Any]) -> dict[str, Any]:
        if extra_fields and "error" not in result:
            return {**extra_fields, **result}
        return result

    def _short_circuit() -> dict[str, Any] | None:
        # Artifact before negative entry: a force_refresh that 404s leaves a
        # good PDF on disk beside a fresh negative, and the next plain call
        # must serve the PDF.
        hit = artifact.cached_hit(dest)
        if hit is not None:
            return _decorate(hit)
        return cache.get_negative(namespace, entity, canonical)

    if force_refresh:
        cache.invalidate(namespace, entity, canonical)
    else:
        early = _short_circuit()
        if early is not None:
            return copy.deepcopy(early)

    async def _runner() -> dict[str, Any]:
        # A leader may have landed the file or a definitive failure while we
        # waited. Skipped under force_refresh: the caller asked for fresh bytes.
        if not force_refresh:
            early = _short_circuit()
            if early is not None:
                return early
        result = await fetch()
        if is_definitive_failure(result):
            cache.put_negative(namespace, entity, canonical, result, ttl_seconds=neg_ttl)
        return _decorate(result)

    result = await single_flight.do(sf_key if sf_key is not None else canonical, _runner)
    return copy.deepcopy(result)
