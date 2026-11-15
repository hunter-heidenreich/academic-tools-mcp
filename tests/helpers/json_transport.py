"""Shared MockTransport JSON response stubs for provider test suites."""

import asyncio
import json
from typing import Any, NamedTuple

import httpx
import pytest

from academic_tools_mcp.net import clients

# Sentinel: a 200 whose body is not valid JSON, simulating a malformed/truncated body.
BAD_JSON = object()


class Resp(NamedTuple):
    """A payload with a non-200 status (and optional headers), for HTTP-error branches."""

    status_code: int
    payload: Any = None
    headers: dict[str, str] | None = None


def stub_json_responses(
    monkeypatch: pytest.MonkeyPatch,
    *payloads: Any,
    slow: bool = False,
) -> list[httpx.Request]:
    """Serve ``payloads`` from successive GETs over a real ``httpx.MockTransport``.

    A payload may be ``BAD_JSON``, a ``Resp`` or ``(status, payload[, headers])`` tuple,
    or an exception to raise; the last payload repeats once the sequence is exhausted.
    ``slow=True`` yields to the event loop before answering so concurrent callers overlap.
    Returns the list of captured ``httpx.Request`` objects.
    """
    requests: list[httpx.Request] = []
    seq = list(payloads)

    def respond(request: httpx.Request) -> httpx.Response:
        payload = seq[len(requests)] if len(requests) < len(seq) else seq[-1]
        requests.append(request)
        if isinstance(payload, Exception):
            raise payload
        status, headers = 200, {"content-type": "application/json"}
        if isinstance(payload, Resp):
            status = payload.status_code
            headers = {**headers, **(payload.headers or {})}
            payload = payload.payload
        elif isinstance(payload, tuple):
            status, payload, *rest = payload
            if rest and rest[0]:
                headers = {**headers, **rest[0]}
        body = b"{ truncated" if payload is BAD_JSON else json.dumps(payload).encode()
        return httpx.Response(status, headers=headers, content=body)

    async def slow_respond(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0)
        return respond(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(slow_respond if slow else respond))
    monkeypatch.setattr(clients, "get_client", lambda *a, **kw: client)
    return requests


def stub_no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install a transport that fails the test if any request reaches it."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected request: {request.url}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(clients, "get_client", lambda *a, **kw: client)
