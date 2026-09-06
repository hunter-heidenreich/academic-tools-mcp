"""Shared streaming PDF download helper.

Backs all four ``download_pdf`` paths (arxiv, biorxiv, acl_anthology,
oa_download). Slot acquisition stays per-provider — each has its own gap and
concurrency caps — while streaming, size-capping, PDF sniffing and atomic
rename are identical, so they live here.

Streaming is the load-bearing choice: peak memory stays at one chunk rather
than 2× the PDF, and the size cap fires partway through rather than after the
whole response is already buffered in RAM.
"""

from __future__ import annotations

import contextlib
import copy
import os
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx

from . import _http, _singleflight, _stats, cache, config

# Clears an image-heavy preprint; catches a 10 GB non-PDF.
_DEFAULT_MAX_PDF_BYTES = 200_000_000

# Also bounds cap overshoot: a run-away response aborts within one chunk.
_CHUNK_SIZE = 64 * 1024


def is_usable_pdf(path: Path) -> bool:
    """Whether a cached PDF should be trusted as a hit.

    Rejects what an interrupted or degenerate download leaves behind: a
    0-byte file, and an HTML landing page saved under a .pdf name. Gate
    every cached-PDF check on this, never ``Path.exists()``.

    Not a validity proof — a file truncated after the header passes. That
    is recoverable (the converter fails); silently serving an empty file
    is not.
    """
    try:
        if path.stat().st_size == 0:
            return False
        with path.open("rb") as f:
            return f.read(5) == b"%PDF-"
    except OSError:
        return False


def cached_hit(dest: Path) -> dict[str, Any] | None:
    """Return the cache hit for ``dest``, or None to re-download.

    Payload is ``{path, size_bytes, cached}``. Owns the ``stat``, so a file
    unlinked between the check and the size read is a miss rather than an
    ``OSError`` out of the caller.
    """
    try:
        if not is_usable_pdf(dest):
            return None
        return {"path": str(dest), "size_bytes": dest.stat().st_size, "cached": True}
    except OSError:
        return None


def is_definitive_failure(result: dict[str, Any]) -> bool:
    """Whether a failure is paper-intrinsic, and so worth negative-caching.

    An allowlist: explicit ``retryable: False``, nothing else. Loosening it to
    "not marked retryable" would negative-cache every unclassified 4xx, and a
    paywalled 403 is not known to be permanent. ``max_bytes`` is excluded on
    the same principle — a cap bump fixes it, so it says nothing about the
    paper.
    """
    return "error" in result and result.get("retryable") is False and "max_bytes" not in result


def resolve_max_pdf_bytes() -> int | None:
    """Resolve the MAX_PDF_BYTES env var.

    Returns the cap in bytes, or None to disable it. The disable vocabulary is
    exactly "none" / "off" / "disabled" / "0". Anything else — unset, empty,
    unparseable, or a non-positive number — falls back to the default, so a
    mistyped cap can't silently drop the disk guard.
    """
    raw = config.get("MAX_PDF_BYTES")
    if raw is None:
        return _DEFAULT_MAX_PDF_BYTES
    raw = raw.strip().lower()
    if raw in {"none", "off", "disabled", "0"}:
        return None
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_MAX_PDF_BYTES
    # Only the vocabulary above disables. "-1" is an unlimited idiom elsewhere;
    # here it's a typo, and honouring it would remove the guard entirely.
    return value if value > 0 else _DEFAULT_MAX_PDF_BYTES


async def stream_to_file(
    client: httpx.AsyncClient,
    url: str,
    dest: Path,
    *,
    slot_factory: Callable[[], Any],
    namespace: str,
    provider_label: str,
    timeout: float,  # noqa: ASYNC109 — httpx's own timeout, not a cancel scope
    not_found_message: str | None = None,
    require_pdf: bool = False,
) -> dict[str, Any]:
    """Stream a GET response to ``dest``, atomically, with a size cap.

    ``slot_factory()`` returns an async context manager holding the
    provider's rate-limit slot on entry. It is held for the whole download:
    an open connection counts toward the concurrency cap, so releasing early
    would let a fan-out exceed documented limits.

    ``namespace`` is the provider's cache namespace and ``provider_label`` its
    human-facing name — the same split as ``Throttle``: the label reaches the
    agent in the error message, the namespace files a disk failure under the
    row that already holds this provider's cache counters.

    ``require_pdf=True`` rejects a non-PDF before anything is written — the
    open-access path's URL can resolve to a publisher landing page.
    Content-Type is an advisory early-out only (publishers mislabel both
    ways); the ``%PDF-`` magic bytes are authoritative, so an
    ``octet-stream`` PDF still passes. Native providers leave it ``False``.

    Returns ``{path, size_bytes, cached: False}``, or an error dict: a 404 or
    a not-a-PDF rejection → ``{error, retryable: False}``; over the cap adds
    ``max_bytes``; a transport, empty-body or disk failure → ``retryable:
    True``, never a raised ``OSError``.
    """
    max_bytes = resolve_max_pdf_bytes()
    tmp_path: Path | None = None
    written = 0

    try:
        async with slot_factory(), client.stream("GET", url, timeout=timeout) as response:
            if response.status_code == 404:
                return {
                    "error": (not_found_message or f"{provider_label}: PDF not found at {url}"),
                    "retryable": False,
                }
            if response.status_code >= 400:
                # error_dict reads exc.response.text; unread, that raises
                # ResponseNotRead — a RuntimeError, so it escapes HTTPX_ERRORS.
                await response.aread()
            response.raise_for_status()
            if require_pdf:
                content_type = response.headers.get("content-type", "")
                if content_type.lower().lstrip().startswith(("text/html", "text/plain")):
                    return {
                        "error": (
                            f"{provider_label}: {url} returned an HTML "
                            f"page (Content-Type: {content_type}), not a "
                            "PDF — likely a landing or paywall page."
                        ),
                        "retryable": False,
                    }
            dest.parent.mkdir(parents=True, exist_ok=True)
            # Not mkstemp: this binds the fd to the file object, so none leaks.
            tmp_file = tempfile.NamedTemporaryFile(  # noqa: SIM115 — entered by the `with` below
                mode="wb",
                prefix=dest.name + ".",
                suffix=".tmp",
                dir=str(dest.parent),
                delete=False,
            )
            tmp_path = Path(tmp_file.name)

            checked_pdf = not require_pdf
            with tmp_file as f:
                async for chunk in response.aiter_bytes(_CHUNK_SIZE):
                    if not checked_pdf:
                        # First chunk suffices: ByteChunker shortens only the last.
                        if not chunk.startswith(b"%PDF-"):
                            return {
                                "error": (
                                    f"{provider_label}: {url} did not "
                                    "return a PDF (missing %PDF- header) "
                                    "— likely a landing or paywall page."
                                ),
                                "retryable": False,
                            }
                        checked_pdf = True
                    if max_bytes is not None and written + len(chunk) > max_bytes:
                        return {
                            "error": (
                                f"{provider_label}: PDF exceeds "
                                f"MAX_PDF_BYTES ({max_bytes} bytes). "
                                "Increase MAX_PDF_BYTES or set it to "
                                "'none' to disable the cap."
                            ),
                            "retryable": False,
                            "max_bytes": max_bytes,
                        }
                    f.write(chunk)
                    written += len(chunk)
        if written == 0:
            # The %PDF- sniff can't catch this: with no chunks it never ran.
            return {
                "error": (
                    f"{provider_label}: {url} returned an empty body "
                    "(0 bytes) — nothing was cached."
                ),
                "retryable": True,
            }
        os.replace(tmp_path, dest)
        tmp_path = None
        return {"path": str(dest), "size_bytes": written, "cached": False}
    except _http.HTTPX_ERRORS as e:
        return _http.error_dict(provider_label, e)
    except OSError as e:
        # Retryable, so a full disk is never recorded against the paper.
        _stats.incr(namespace, "cache_write_failures")
        return {
            "error": f"{provider_label}: could not write the PDF to {dest}: {e}",
            "retryable": True,
        }
    finally:
        if tmp_path is not None:
            with contextlib.suppress(OSError):
                tmp_path.unlink(missing_ok=True)


async def cached_download(
    *,
    single_flight: _singleflight.SingleFlight,
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
    the PDF *is* the positive entry, with ``is_usable_pdf`` as its freshness
    rule. Pass ``sf_key`` when ``fetch`` awaits another getter sharing this
    ``SingleFlight``, or it will await its own slot and deadlock.

    ``force_refresh`` drops the negative entry and re-fetches, but never
    unlinks the PDF, so a failed refresh leaves the caller the copy they had.
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
        hit = cached_hit(dest)
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
        # A leader may have landed the file, or recorded a definitive
        # failure, while we waited. Skipped under force_refresh: the caller
        # asked for fresh bytes.
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
