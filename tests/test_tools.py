"""Tool behaviour, driven through the in-memory client against a fake indexer.

The fake records the Electrum call each tool made, so these tests pin the
shaping that matters: the scripthash sent on the wire, how history splits
confirmed from mempool, the electronics of the tip hash, and that an invalid
address is answered with what is accepted rather than a traceback.
"""

from __future__ import annotations

import json

import pytest

from address import scripthash_for_address
from electrum import ElectrumError

P2WPKH = "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"

# The mainnet genesis header, whose hash is a well-known constant.
GENESIS_HEADER = (
    "01000000"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "3ba3edfd7a7b12b27ac72c3e67768f617fc81bc3888a51323a9fb8aa4b1e5e4a"
    "29ab5f49ffff001d1dac2b7c"
)
GENESIS_HASH = "000000000019d6689c085ae165831e934ff763ae46a2a6c172b3f1b60a8ce26f"


@pytest.mark.anyio
async def test_balance_is_shaped_with_its_scripthash(call, fake_client) -> None:
    fake_client.answers["blockchain.scripthash.get_balance"] = {
        "confirmed": 150000000,
        "unconfirmed": -25000,
    }
    payload = json.loads(await call("get_address_balance", address=P2WPKH))
    assert payload["confirmed"] == 150000000
    assert payload["unconfirmed"] == -25000
    assert payload["unit"] == "satoshi"
    assert fake_client.calls == [
        ("blockchain.scripthash.get_balance", [scripthash_for_address(P2WPKH)])
    ]


@pytest.mark.anyio
async def test_history_splits_confirmed_from_mempool(call, fake_client) -> None:
    fake_client.answers["blockchain.scripthash.get_history"] = [
        {"tx_hash": "aa" * 32, "height": 800000},
        {"tx_hash": "bb" * 32, "height": 0},
        {"tx_hash": "cc" * 32, "height": -1},
    ]
    payload = json.loads(await call("get_address_history", address=P2WPKH))
    assert [row["txid"] for row in payload["confirmed"]] == ["aa" * 32]
    assert [row["height"] for row in payload["mempool"]] == [0, -1]
    assert payload["confirmed_count"] == 1
    assert payload["mempool_count"] == 2


@pytest.mark.anyio
async def test_utxos_are_totalled_in_satoshis(call, fake_client) -> None:
    fake_client.answers["blockchain.scripthash.listunspent"] = [
        {"tx_hash": "aa" * 32, "tx_pos": 0, "height": 800000, "value": 150},
        {"tx_hash": "bb" * 32, "tx_pos": 1, "height": 0, "value": 200},
    ]
    payload = json.loads(await call("get_address_utxos", address=P2WPKH))
    assert payload["count"] == 2
    assert payload["total"] == 350
    assert [utxo["confirmed"] for utxo in payload["utxos"]] == [True, False]
    assert payload["unit"] == "satoshi"


@pytest.mark.anyio
async def test_an_invalid_address_lists_what_is_accepted(call, fake_client) -> None:
    text = await call("get_address_balance", address="1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN3")
    assert "checksum" in text
    assert "Accepted:" in text
    assert fake_client.calls == []  # nothing was asked of the indexer


@pytest.mark.anyio
async def test_transaction_verbose_flag_is_forwarded(call, fake_client) -> None:
    fake_client.answers["blockchain.transaction.get"] = "00ff"
    text = await call("get_transaction", txid="ab" * 32, verbose=False)
    assert text == "00ff"
    assert fake_client.calls == [("blockchain.transaction.get", ["ab" * 32, False])]


@pytest.mark.anyio
async def test_chain_tip_derives_the_block_hash(call, fake_client) -> None:
    fake_client.answers["blockchain.headers.subscribe"] = {
        "height": 0,
        "hex": GENESIS_HEADER,
    }
    payload = json.loads(await call("get_chain_tip"))
    assert payload["blockhash"] == GENESIS_HASH
    assert payload["height"] == 0
    assert payload["time"] == 1231006505


@pytest.mark.anyio
async def test_server_info_is_returned_as_json(call, fake_client) -> None:
    fake_client.answers["server.features"] = {
        "server_version": "Fulcrum 1.11.2",
        "genesis_hash": "00" * 32,
        "protocol_min": "1.4",
    }
    payload = json.loads(await call("get_server_info"))
    assert payload["server_version"] == "Fulcrum 1.11.2"


@pytest.mark.anyio
async def test_an_electrum_error_is_surfaced_with_its_code(call, fake_client) -> None:
    fake_client.answers["blockchain.transaction.get"] = ElectrumError(
        "daemon error: No such mempool or blockchain transaction", 2
    )
    text = await call("get_transaction", txid="ab" * 32)
    assert "No such mempool" in text
    assert "electrum error 2" in text
