"""The `/ready` probe, driven directly against a fake indexer.

The middleware only routes the path; the probe itself decides between 200
and 503, so it is tested on its own -- including that it uses the short
readiness timeout rather than the endpoint's 30s default.
"""

from __future__ import annotations

import time
from collections.abc import Iterator

import anyio
import pytest

import server
from electrum import ElectrumError

GENESIS_HEADER = (
    "01000000"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "3ba3edfd7a7b12b27ac72c3e67768f617fc81bc3888a51323a9fb8aa4b1e5e4a"
    "29ab5f49ffff001d1dac2b7c"
)


@pytest.fixture(autouse=True)
def _clear_ready_cache() -> Iterator[None]:
    """The cache and its lock are module globals, so tests must not inherit them."""
    server._ready_cache = None
    server._ready_lock = None
    yield
    server._ready_cache = None
    server._ready_lock = None


@pytest.mark.anyio
async def test_ready_reports_the_served_tip(fake_client) -> None:
    fake_client.answers["blockchain.headers.subscribe"] = {"height": 0, "hex": GENESIS_HEADER}
    body, status = await server._readiness()
    assert status == 200
    assert body["status"] == "ready"
    assert body["height"] == 0
    assert body["blockhash"].startswith("000000000019d668")
    assert fake_client.last_timeout == server.READY_TIMEOUT


@pytest.mark.anyio
async def test_ready_is_503_when_fulcrum_is_unreachable(fake_client) -> None:
    fake_client.answers["blockchain.headers.subscribe"] = ElectrumError(
        "cannot reach Fulcrum at tcp://fulcrum:50001: connection refused"
    )
    body, status = await server._readiness()
    assert status == 503
    assert body["status"] == "unready"
    assert "cannot reach Fulcrum" in body["error"]


@pytest.mark.anyio
async def test_ready_is_503_on_an_unexpected_body(fake_client) -> None:
    fake_client.answers["blockchain.headers.subscribe"] = []
    body, status = await server._readiness()
    assert status == 503
    assert body["status"] == "unready"


@pytest.mark.anyio
async def test_ready_is_cached_for_the_ttl(fake_client) -> None:
    # /ready is unauthenticated, so repeated polls must not each open a
    # connection to the indexer.
    fake_client.answers["blockchain.headers.subscribe"] = {"height": 0, "hex": GENESIS_HEADER}
    first, _ = await server._readiness()
    second, _ = await server._readiness()
    assert first == second
    assert len(fake_client.calls) == 1


@pytest.mark.anyio
async def test_ready_probes_again_after_the_ttl(fake_client, monkeypatch) -> None:
    monkeypatch.setattr(server, "READY_CACHE_TTL", 0.0)
    fake_client.answers["blockchain.headers.subscribe"] = {"height": 0, "hex": GENESIS_HEADER}
    await server._readiness()
    await server._readiness()
    assert len(fake_client.calls) == 2


@pytest.mark.anyio
async def test_an_unready_answer_is_cached_then_replaced(fake_client, monkeypatch) -> None:
    fake_client.answers["blockchain.headers.subscribe"] = ElectrumError("down")
    _, status = await server._readiness()
    assert status == 503
    _, status = await server._readiness()
    assert status == 503
    assert len(fake_client.calls) == 1

    monkeypatch.setattr(server, "READY_CACHE_TTL", 0.0)
    fake_client.answers["blockchain.headers.subscribe"] = {"height": 0, "hex": GENESIS_HEADER}
    body, status = await server._readiness()
    assert status == 200
    assert body["status"] == "ready"
    assert len(fake_client.calls) == 2


@pytest.mark.anyio
async def test_concurrent_ready_requests_share_one_probe(fake_client) -> None:
    def slow(_params):
        time.sleep(0.05)
        return {"height": 0, "hex": GENESIS_HEADER}

    fake_client.answers["blockchain.headers.subscribe"] = slow
    results = []

    async def one() -> None:
        results.append(await server._readiness())

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(one)
        tasks.start_soon(one)
    assert len(fake_client.calls) == 1
    assert len(results) == 2
