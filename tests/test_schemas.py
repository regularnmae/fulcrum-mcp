"""What the server advertises in `tools/list`.

The schema is the contract a client validates against. Every tool here is
text-only and returns `str`, so every tool must pass
`structured_output=False`: a tool returning `str` that does not would
publish an output schema and send its payload twice, once as text and once
as structured content.
"""

from __future__ import annotations

import anyio
import pytest
from mcp import Client

import server

TOOLS = {
    "get_address_balance",
    "get_address_history",
    "get_address_mempool",
    "get_address_utxos",
    "get_transaction",
    "get_transaction_merkle",
    "get_block_header",
    "get_chain_tip",
    "get_fee_estimate",
    "get_server_info",
}


@pytest.fixture
def advertised() -> dict:
    """The tools/list result, fetched once per test through a real client."""

    async def _list():
        async with Client(server.mcp) as c:
            return await c.list_tools()

    listing = anyio.run(_list)
    return {tool.name: tool for tool in listing.tools}


def test_every_tool_is_advertised(advertised: dict) -> None:
    assert set(advertised) == TOOLS


@pytest.mark.parametrize("name", sorted(TOOLS))
def test_text_tools_publish_no_output_schema(advertised: dict, name: str) -> None:
    assert advertised[name].output_schema is None, (
        f"{name} publishes an output schema, which means its str return is "
        "serialised twice; pass structured_output=False to @mcp.tool()"
    )


def test_every_tool_declares_itself_read_only(advertised: dict) -> None:
    for name, tool in advertised.items():
        assert tool.annotations is not None, f"{name} publishes no annotations"
        assert tool.annotations.read_only_hint is True, f"{name} is not marked read-only"
        assert tool.annotations.open_world_hint is True, f"{name} is not marked open-world"


def test_address_arguments_are_required_strings(advertised: dict) -> None:
    for name in (
        "get_address_balance",
        "get_address_history",
        "get_address_mempool",
        "get_address_utxos",
    ):
        schema = advertised[name].input_schema
        assert schema["required"] == ["address"], name
        assert schema["properties"]["address"]["type"] == "string", name


def test_txid_arguments_are_64_hex_characters(advertised: dict) -> None:
    for name in ("get_transaction", "get_transaction_merkle"):
        schema = advertised[name].input_schema
        assert schema["properties"]["txid"]["pattern"] == "^[0-9a-fA-F]{64}$", name


def test_history_pagination_is_bounded(advertised: dict) -> None:
    properties = advertised["get_address_history"].input_schema["properties"]
    assert properties["limit"]["maximum"] == 10000
    assert properties["limit"]["minimum"] == 1
    assert properties["offset"]["minimum"] == 0
