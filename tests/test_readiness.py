"""The `/ready` probe, driven directly against a fake indexer.

The middleware only routes the path; the probe itself decides between 200
and 503, so it is tested on its own -- including that it uses the short
readiness timeout rather than the endpoint's 30s default.
"""

from __future__ import annotations

import pytest

import server
from electrum import ElectrumError

GENESIS_HEADER = (
    "01000000"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "3ba3edfd7a7b12b27ac72c3e67768f617fc81bc3888a51323a9fb8aa4b1e5e4a"
    "29ab5f49ffff001d1dac2b7c"
)


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
