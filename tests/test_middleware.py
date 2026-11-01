"""The ASGI middleware: bearer auth, /health, /ready and the method gate.

Driven directly with a stub app and a captured `send`, so these tests pin
the routing decisions without binding a port or opening a socket.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest

import server

TOKEN = "test-token"
GENESIS_HEADER = (
    "01000000"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "3ba3edfd7a7b12b27ac72c3e67768f617fc81bc3888a51323a9fb8aa4b1e5e4a"
    "29ab5f49ffff001d1dac2b7c"
)


class _StubApp:
    """Stands in for the streamable-HTTP app behind the middleware."""

    def __init__(self) -> None:
        self.called = False

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        self.called = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"app"})


def _scope(path: str, method: str = "GET", token: str | None = None) -> dict[str, Any]:
    headers = []
    if token is not None:
        headers.append((b"authorization", f"Bearer {token}".encode()))
    return {"type": "http", "path": path, "method": method, "headers": headers}


async def _receive() -> dict[str, Any]:
    return {"type": "http.request"}


async def _call(
    app: _StubApp, scope: dict[str, Any]
) -> tuple[int, dict[bytes, bytes], bytes]:
    messages: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    middleware = server.BearerAuthMiddleware(app, TOKEN)
    await middleware(scope, _receive, send)
    start = next(m for m in messages if m["type"] == "http.response.start")
    body = b"".join(
        m.get("body", b"") for m in messages if m["type"] == "http.response.body"
    )
    return start["status"], dict(start.get("headers") or []), body


@pytest.fixture(autouse=True)
def _clear_ready_cache() -> Iterator[None]:
    server._ready_cache = None
    server._ready_lock = None
    yield
    server._ready_cache = None
    server._ready_lock = None


@pytest.mark.anyio
async def test_health_is_open_and_says_only_ok() -> None:
    app = _StubApp()
    status, _, body = await _call(app, _scope("/health"))
    assert status == 200
    assert json.loads(body) == {"status": "ok"}
    assert not app.called


@pytest.mark.anyio
async def test_health_answers_a_head_without_a_body() -> None:
    status, _, body = await _call(_StubApp(), _scope("/health", "HEAD"))
    assert status == 200
    assert body == b""


@pytest.mark.anyio
async def test_a_write_method_on_health_is_refused() -> None:
    status, headers, _ = await _call(_StubApp(), _scope("/health", "POST"))
    assert status == 405
    assert headers[b"allow"] == b"GET, HEAD"


@pytest.mark.anyio
async def test_ready_is_served_from_the_probe(fake_client) -> None:
    fake_client.answers["blockchain.headers.subscribe"] = {
        "height": 0,
        "hex": GENESIS_HEADER,
    }
    status, _, body = await _call(_StubApp(), _scope("/ready"))
    assert status == 200
    assert json.loads(body)["status"] == "ready"
    assert len(fake_client.calls) == 1


@pytest.mark.anyio
async def test_ready_refuses_a_write_method_without_probing(fake_client) -> None:
    status, headers, _ = await _call(_StubApp(), _scope("/ready", "POST"))
    assert status == 405
    assert headers[b"allow"] == b"GET, HEAD"
    assert fake_client.calls == []


@pytest.mark.anyio
async def test_the_mcp_endpoint_requires_a_token() -> None:
    app = _StubApp()
    status, _, body = await _call(app, _scope("/mcp"))
    assert status == 401
    assert json.loads(body) == {"error": "unauthorized"}
    assert not app.called


@pytest.mark.anyio
async def test_the_mcp_endpoint_forwards_a_valid_token() -> None:
    app = _StubApp()
    status, _, body = await _call(app, _scope("/mcp", token=TOKEN))
    assert status == 200
    assert body == b"app"
    assert app.called
