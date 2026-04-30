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
from pathlib import Path
from typing import Any, Callable

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

    Returns ``{path, size_bytes, cached: False}`` on success or a
    structured error dict on failure (transport error, 404, size cap
    exceeded). 404 → ``{error}``. Cap exceeded →
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
        async with slot_factory():
            async with client.stream("GET", url, timeout=timeout) as response:
                if response.status_code == 404:
                    return {
                        "error": (
                            not_found_message
                            or f"{provider_label}: PDF not found at {url}"
                        )
                    }
                response.raise_for_status()
                with os.fdopen(fd, "wb") as f:
                    fd_handed_off = True
                    async for chunk in response.aiter_bytes(_CHUNK_SIZE):
                        if (
                            max_bytes is not None
                            and written + len(chunk) > max_bytes
                        ):
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
