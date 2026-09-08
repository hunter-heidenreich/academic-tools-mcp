"""Shared fakes for the PDF-download tests.

Four test modules drive ``_pdf_download.stream_to_file`` — directly
(``test_pdf_download``) or through a provider's ``download_pdf``
(``test_oa_download``, ``test_force_refresh_pdf``, ``test_download_singleflight``).
They used to carry near-copies of the same stubs, and the copies had drifted:
only one set ``response.headers``, so the OA path's Content-Type guard had
nothing to read anywhere else.

Two fidelities are offered, and the choice matters. ``mock_stream_response`` +
``install_stream`` build a ``MagicMock`` whose ``.stream(*args, **kwargs)``
swallows its arguments — cheap, but it cannot observe wiring such as the
``timeout`` each provider passes. ``streaming_client`` returns a real
``httpx.AsyncClient`` over ``MockTransport``, so anything the production code
puts on the wire is inspectable; reach for it when the assertion is about the
request rather than the response.
"""

from __future__ import annotations

import contextlib
from typing import Any
from unittest.mock import MagicMock

import httpx

# stream_to_file takes an explicit timeout — no default, so every caller states
# one. Short here: nothing in these tests reaches a real socket.
TIMEOUT = 5.0


@contextlib.asynccontextmanager
async def passthrough_slot(*_args: Any, **_kwargs: Any):
    """Skip the rate-limit gating (and its sleeps).

    Signature covers both call shapes: ``slot_factory=passthrough_slot`` takes
    none, a provider's ``_request_slot`` takes the URL.
    """
    yield


def mock_stream_response(
    status_code: int = 200,
    chunks: list[bytes] | None = None,
    content_type: str = "application/pdf",
):
    """Mock async-context-manager yielding a streaming response.

    ``chunks=[]`` really means an empty body — the 0-byte 200 that
    ``stream_to_file`` classifies retryable — so the default is keyed on
    ``None``, not on falsiness.
    """
    chunks = [b"%PDF-1.4 fresh bytes"] if chunks is None else chunks

    async def aiter_bytes(_chunk_size):
        for c in chunks:
            yield c

    response = MagicMock()
    response.status_code = status_code
    response.raise_for_status = MagicMock()
    response.aiter_bytes = aiter_bytes
    response.headers = {"content-type": content_type}

    @contextlib.asynccontextmanager
    async def stream_cm():
        yield response

    return stream_cm


def install_stream(monkeypatch, stream_cm_or_obj) -> None:
    """Point every pooled client at a stub whose ``.stream`` returns the given
    context manager (called fresh per stream invocation)."""

    class StubClient:
        def stream(self, *_args, **_kwargs):
            return stream_cm_or_obj() if callable(stream_cm_or_obj) else stream_cm_or_obj

    from academic_tools_mcp import _clients

    monkeypatch.setattr(_clients, "get_client", lambda *a, **kw: StubClient())


class UnreadStream(httpx.AsyncByteStream):
    """A response body that is genuinely streamed.

    ``httpx.MockTransport`` with ``text=``/``content=`` hands back a response
    whose content is already buffered, so ``.text`` works and the streaming
    paths under test cannot reproduce. A real stream is required.
    """

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    async def __aiter__(self):
        yield self._payload


def streaming_client(
    status_code: int,
    body: bytes,
    content_type: str = "text/html",
    *,
    requests: list[httpx.Request] | None = None,
) -> httpx.AsyncClient:
    """A real AsyncClient over MockTransport, streaming ``body``.

    Pass ``requests`` to capture what actually went out — the seam for
    asserting on headers, timeouts and URLs a MagicMock stub would swallow.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        return httpx.Response(
            status_code,
            headers={"content-type": content_type},
            stream=UnreadStream(body),
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))
