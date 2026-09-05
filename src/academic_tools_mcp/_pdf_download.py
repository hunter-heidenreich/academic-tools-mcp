"""Shared streaming PDF download helper.

The three PDF-providing modules (arxiv, biorxiv, acl_anthology) all want
the same shape: acquire the rate-limit slot, open a streaming GET, write
chunks to a sibling temp file, atomic-rename into place, and cap the
total bytes so a misrouted URL can't fill the disk. The slot acquisition
is per-provider (different gap / concurrency caps), but the streaming +
size-capping + atomic-rename logic is identical, so it lives here.

Streaming (vs. the previous ``response.content`` + ``write_bytes`` path)
matters for two reasons: peak memory stays at one chunk size instead of
2× the PDF size, and the size cap fires partway through rather than
after the entire response is already buffered in RAM.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from . import _http, config

# Default cap. 200 MB is large enough for legitimate physics surveys and
# image-heavy biology preprints while still short-circuiting "10 GB book
# disguised as PDF" footguns. Tunable via MAX_PDF_BYTES env var.
_DEFAULT_MAX_PDF_BYTES = 200_000_000

# Streamed write chunk size. 64 KiB is large enough to amortise per-call
# overhead and small enough that the cap-check fires within a fraction
# of a second of the limit being passed.
_CHUNK_SIZE = 64 * 1024


def is_usable_pdf(path: Path) -> bool:
    """Whether an existing cached PDF should be trusted as a cache hit.

    Rejects the leftovers an interrupted or degenerate download can leave
    behind: a 0-byte file (an empty 200, a killed copy) and anything whose
    first bytes are not the ``%PDF-`` magic number (an HTML landing page
    saved under a .pdf name).

    Every `dest.exists()` short-circuit in the PDF providers should route
    through this instead. `exists()` alone is what let a 0-byte file be
    served as ``cached: True`` forever and handed to the converter.

    Not a validity proof — a file truncated after the header still passes.
    That case is caught downstream by the converter failing, which is
    recoverable; serving an empty file silently is not.
    """
    try:
        if path.stat().st_size == 0:
            return False
        with path.open("rb") as f:
            return f.read(5) == b"%PDF-"
    except OSError:
        return False


def cached_hit(dest: Path) -> dict[str, Any] | None:
    """Return the standard cache-hit payload for ``dest``, or None.

    ``None`` means "treat as a miss and re-download" — either the file is
    absent or it failed ``is_usable_pdf``. Collapses the six copies of this
    block across the three PDF providers (each had it twice: once up front
    and once in the post-single-flight re-check).
    """
    try:
        if not is_usable_pdf(dest):
            return None
        return {"path": str(dest), "size_bytes": dest.stat().st_size, "cached": True}
    except OSError:
        # Raced with an unlink between the check and the stat.
        return None


def resolve_max_pdf_bytes() -> int | None:
    """Resolve the MAX_PDF_BYTES env var.

    Returns the cap in bytes, or None to disable the cap. Unset / empty /
    garbage falls back to the default; explicit "none" / "off" /
    "disabled" / "0" disables.
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
    if value <= 0:
        return None
    return value


async def stream_to_file(
    client: httpx.AsyncClient,
    url: str,
    dest: Path,
    *,
    slot_factory: Callable[[], Any],
    provider_label: str,
    timeout: float = 60.0,
    not_found_message: str | None = None,
    require_pdf: bool = False,
) -> dict[str, Any]:
    """Stream a GET response to ``dest``, atomically, with a size cap.

    ``slot_factory()`` returns an async context manager that, on entry,
    has acquired the provider's rate-limit slot. The slot is held for
    the lifetime of the streaming download — open connections count
    toward the concurrency cap, and releasing earlier would let a
    fan-out exceed documented limits while slow streams are still
    flushing.

    Lands in a sibling ``*.tmp`` file via ``mkstemp`` and is moved into
    place with ``os.replace`` so a crash mid-download cannot leave a
    half-written canonical file. The temp is unlinked on every failure
    path, including the size-cap abort.

    ``require_pdf=True`` validates that the response is actually a PDF
    before writing — needed for the open-access download path, where the
    URL may resolve to an HTML landing/paywall page rather than a PDF. An
    obvious ``text/html`` / ``text/plain`` Content-Type, or a first chunk
    lacking the ``%PDF-`` magic bytes, is rejected with
    ``{error, retryable: False}`` and no file is written. Content-Type is
    only an advisory early-out (publishers mislabel both ways); the magic
    bytes are authoritative, so a real PDF served as ``octet-stream`` or
    with no Content-Type still passes. Default ``False`` skips the check
    entirely (the native providers fetch known-PDF CDN URLs).

    Returns ``{path, size_bytes, cached: False}`` on success or a
    structured error dict on failure (transport error, 404, size cap
    exceeded, not-a-PDF). 404 → ``{error}``. Cap exceeded →
    ``{error, retryable: False, max_bytes}``.
    """
    max_bytes = resolve_max_pdf_bytes()

    dest.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_str = tempfile.mkstemp(
        prefix=dest.name + ".",
        suffix=".tmp",
        dir=str(dest.parent),
    )
    tmp_path = Path(tmp_str)
    written = 0
    fd_handed_off = False

    try:
        async with slot_factory(), client.stream("GET", url, timeout=timeout) as response:
            if response.status_code == 404:
                return {"error": (not_found_message or f"{provider_label}: PDF not found at {url}")}
            if response.status_code >= 400:
                # Read the body before raising. This is a streaming response, so
                # its content is not available until aread(); `error_dict` wants
                # a snippet for the 4xx message and would otherwise raise
                # httpx.ResponseNotRead, which is a RuntimeError and therefore
                # escapes the `except HTTPX_ERRORS` below rather than being
                # converted. Only error responses are buffered, so the streaming
                # guarantee that matters (a 200 PDF is never held in memory) is
                # untouched, and an error body is small.
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
            checked_pdf = not require_pdf
            with os.fdopen(fd, "wb") as f:
                fd_handed_off = True
                async for chunk in response.aiter_bytes(_CHUNK_SIZE):
                    if not checked_pdf:
                        # Authoritative content check: the first bytes of
                        # a PDF are the "%PDF-" magic number. Reject a
                        # mislabeled HTML page that slipped past the
                        # Content-Type guard before anything is written.
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
            # A 200 with an empty body. Without this guard os.replace would
            # install a 0-byte file as a successful download, every
            # `dest.exists()` check downstream would treat it as cached
            # forever, and convert_paper would hand it to the converter.
            # (The %PDF- sniff above cannot catch this: with no chunks the
            # loop body never runs.)
            return {
                "error": (
                    f"{provider_label}: {url} returned an empty body "
                    "(0 bytes) — nothing was cached."
                ),
                "retryable": True,
            }
        os.replace(tmp_path, dest)
        return {"path": str(dest), "size_bytes": written, "cached": False}
    except _http.HTTPX_ERRORS as e:
        return _http.error_dict(provider_label, e)
    finally:
        # If we never handed fd to a file object (early-return or
        # exception before os.fdopen), close it ourselves. Always nuke
        # the temp on any non-success path; on success os.replace
        # already moved it so the unlink is a no-op.
        if not fd_handed_off:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
