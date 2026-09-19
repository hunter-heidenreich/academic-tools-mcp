"""Shared streaming PDF download helper.

Backs all four ``download_pdf`` paths (arxiv, biorxiv, acl, openaccess). Slot
acquisition stays per-provider — each has its own gap and concurrency caps —
while streaming, size-capping, PDF sniffing and atomic rename are identical.

Streaming is load-bearing: peak memory is one chunk rather than 2× the PDF, and
the size cap fires partway through rather than after the whole response is
buffered in RAM. An error body is bounded the same way.
"""

import contextlib
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from ..net import http, stats
from ..util import config
from . import artifact

# Clears an image-heavy preprint; catches a 10 GB non-PDF.
_DEFAULT_MAX_PDF_BYTES = 200_000_000

# Also bounds how far past the cap a run-away response is *read*.
_CHUNK_SIZE = 64 * 1024
# One decoded read, sliced: enough of an error body to name the failure.
_ERROR_SNIPPET_BYTES = 200


async def _error_snippet(response: httpx.Response) -> str:
    """The head of an error body, without buffering the rest.

    No chunk_size: a size makes httpx materialise every slice first. No
    `aclosing` either — `client.stream`'s own `finally` closes the response.
    """
    async for chunk in response.aiter_bytes():
        return chunk[:_ERROR_SNIPPET_BYTES].decode("utf-8", "replace")
    return ""


def resolve_max_pdf_bytes() -> int | None:
    """Resolve the ``MAX_PDF_BYTES`` env var.

    Returns the cap in bytes, or None when ``config._DISABLE_VALUES`` disables
    it. Everything else — unset, empty, unparseable, or a parsed value ``<= 0``
    — falls back to the default, so a mistyped cap can't silently drop the disk
    guard: ``on_nonpositive="default"`` reads "-1" as a typo, not as the
    unlimited idiom it is elsewhere.
    """
    return config.number(
        "MAX_PDF_BYTES",
        _DEFAULT_MAX_PDF_BYTES,
        cast=int,
        on_nonpositive="default",
    )


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

    Returns ``{path, size_bytes, cached: False}``, or an error dict: a 404 or a
    not-a-PDF rejection → ``retryable: False``; over the cap adds ``max_bytes``;
    an empty body or a disk failure → ``retryable: True``, never a raised
    ``OSError``. Any other non-2xx is ``http.response_error_dict``'s verdict on a
    bounded body prefix, which leaves an unclassified 4xx unflagged; a transport
    failure is ``http.error_dict``'s. A prefix read costs the connection — httpcore
    drops one left unread — which beats an unbounded buffer.
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
            # Not `>= 400`: a Location-less 3xx lands here, and sniffing one for
            # `%PDF-` would negative-cache a redirect as a landing page.
            if not 200 <= response.status_code < 300:
                return http.response_error_dict(
                    provider_label, response, snippet=await _error_snippet(response)
                )
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
                        if not artifact.has_pdf_magic(chunk):
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
    except http.HTTPX_ERRORS as e:
        return http.error_dict(provider_label, e)
    except OSError as e:
        # Retryable, so a full disk is never recorded against the paper.
        stats.incr(namespace, "cache_write_failures")
        return {
            "error": f"{provider_label}: could not write the PDF to {dest}: {e}",
            "retryable": True,
        }
    finally:
        if tmp_path is not None:
            with contextlib.suppress(OSError):
                tmp_path.unlink(missing_ok=True)
