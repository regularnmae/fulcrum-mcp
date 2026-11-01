"""Shared fixtures.

Every test talks to the server through the SDK's in-memory client, so no
test opens a socket. Fulcrum itself is replaced by `fake_client`, which
records the Electrum calls a tool made and answers from a per-method script
-- the tools, their address decoding and their error handling are what is
under test, not a live indexer.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest
from mcp import Client

import server


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def client() -> AsyncIterator[Client]:
    """An in-memory client connected to the real server object."""
    async with Client(server.mcp, raise_exceptions=True) as c:
        yield c


class FakeClient:
    """Stands in for `server.client`.

    `answers` maps an Electrum method to the result to return, an exception
    to raise, or a callable taking the params. A method with no entry answers
    with a recognisable echo, so a test that only cares that the call
    happened need not script it.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[Any] | None]] = []
        self.answers: dict[str, Any] = {}
        self.last_timeout: float | None = None

    def call(
        self, method: str, params: list[Any] | None = None, timeout: float | None = None
    ) -> Any:
        self.calls.append((method, params))
        self.last_timeout = timeout
        answer = self.answers.get(method)
        if isinstance(answer, Exception):
            raise answer
        if callable(answer):
            return answer(params)
        if answer is None:
            return {"method": method, "params": params}
        return answer


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch) -> FakeClient:
    """Replace the live indexer with a scripted fake, restoring it after."""
    fake = FakeClient()
    monkeypatch.setattr(server.client, "call", fake.call)
    return fake


@pytest.fixture
def call(client: Client) -> Callable[..., Any]:
    """Call a tool and hand back the text the model would read."""

    async def _call(name: str, **arguments: Any) -> str:
        result = await client.call_tool(name, arguments)
        assert not result.is_error, f"tool {name} failed: {result.content}"
        return result.content[0].text  # type: ignore[union-attr]

    return _call
