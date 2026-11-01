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
async def test_an_invalid_address_lists_what_is_accepted(error_call, fake_client) -> None:
    text = await error_call("get_address_balance", address="1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN3")
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
async def test_an_electrum_error_is_surfaced_with_its_code(error_call, fake_client) -> None:
    fake_client.answers["blockchain.transaction.get"] = ElectrumError(
        "daemon error: No such mempool or blockchain transaction", 2
    )
    text = await error_call("get_transaction", txid="ab" * 32)
    assert "No such mempool" in text
    assert "electrum error 2" in text


@pytest.mark.anyio
async def test_a_balance_without_its_fields_is_an_error(error_call, fake_client) -> None:
    # An unexpected shape must not read as "zero balance": the model would
    # otherwise report a funded address as empty.
    fake_client.answers["blockchain.scripthash.get_balance"] = {"confirmed": 1}
    text = await error_call("get_address_balance", address=P2WPKH)
    assert "unexpected" in text
    assert "unconfirmed balance" in text


@pytest.mark.anyio
async def test_a_history_that_is_not_a_list_is_an_error(error_call, fake_client) -> None:
    fake_client.answers["blockchain.scripthash.get_history"] = None
    text = await error_call("get_address_history", address=P2WPKH)
    assert "unexpected history" in text


@pytest.mark.anyio
async def test_a_utxo_without_its_value_is_an_error(error_call, fake_client) -> None:
    fake_client.answers["blockchain.scripthash.listunspent"] = [
        {"tx_hash": "aa" * 32, "tx_pos": 0, "height": 800000}
    ]
    text = await error_call("get_address_utxos", address=P2WPKH)
    assert "utxo value" in text


@pytest.mark.anyio
async def test_history_is_paginated_with_a_next_offset(call, fake_client) -> None:
    fake_client.answers["blockchain.scripthash.get_history"] = [
        {"tx_hash": f"{n:064x}", "height": 800000 + n} for n in range(3)
    ]
    first = json.loads(await call("get_address_history", address=P2WPKH, limit=2))
    assert first["confirmed_count"] == 3
    assert first["returned"] == 2
    assert first["next_offset"] == 2

    second = json.loads(
        await call("get_address_history", address=P2WPKH, limit=2, offset=2)
    )
    assert second["returned"] == 1
    assert second["next_offset"] is None
    assert second["confirmed"][0]["height"] == 800002


@pytest.mark.anyio
async def test_mempool_transactions_carry_their_fee(call, fake_client) -> None:
    fake_client.answers["blockchain.scripthash.get_mempool"] = [
        {"tx_hash": "aa" * 32, "height": 0, "fee": 1500},
        {"tx_hash": "bb" * 32, "height": -1, "fee": 500},
    ]
    payload = json.loads(await call("get_address_mempool", address=P2WPKH))
    assert payload["count"] == 2
    assert payload["total_fee"] == 2000
    assert payload["unit"] == "satoshi"
    assert fake_client.calls == [
        ("blockchain.scripthash.get_mempool", [scripthash_for_address(P2WPKH)])
    ]


@pytest.mark.anyio
async def test_block_header_is_derived_from_its_hex(call, fake_client) -> None:
    fake_client.answers["blockchain.block.header"] = GENESIS_HEADER
    payload = json.loads(await call("get_block_header", height=0))
    assert payload["blockhash"] == GENESIS_HASH
    assert payload["time"] == 1231006505
    assert payload["header"] == GENESIS_HEADER


@pytest.mark.anyio
async def test_a_header_that_is_not_80_bytes_is_an_error(error_call, fake_client) -> None:
    fake_client.answers["blockchain.block.header"] = "00" * 79
    text = await error_call("get_block_header", height=1)
    assert "malformed block header" in text


@pytest.mark.anyio
async def test_merkle_proof_is_forwarded_with_its_height(call, fake_client) -> None:
    fake_client.answers["blockchain.transaction.get_merkle"] = {
        "block_height": 800000,
        "merkle": ["aa" * 32, "bb" * 32],
        "pos": 7,
    }
    payload = json.loads(
        await call("get_transaction_merkle", txid="AB" * 32, height=800000)
    )
    assert payload["txid"] == "ab" * 32
    assert payload["pos"] == 7
    assert fake_client.calls == [
        ("blockchain.transaction.get_merkle", ["ab" * 32, 800000])
    ]


@pytest.mark.anyio
async def test_fee_estimate_is_converted_to_sat_per_vb(call, fake_client) -> None:
    fake_client.answers["blockchain.estimatefee"] = 0.0001
    payload = json.loads(await call("get_fee_estimate", blocks=6))
    assert payload["btc_per_kvb"] == 0.0001
    assert payload["sat_per_vb"] == 10.0
    assert fake_client.calls == [("blockchain.estimatefee", [6])]


@pytest.mark.anyio
async def test_no_fee_estimate_is_reported_not_invented(call, fake_client) -> None:
    fake_client.answers["blockchain.estimatefee"] = -1
    payload = json.loads(await call("get_fee_estimate"))
    assert payload["sat_per_vb"] is None
    assert "no estimate" in payload["note"]


@pytest.mark.anyio
async def test_a_non_finite_fee_estimate_is_an_error(error_call, fake_client) -> None:
    fake_client.answers["blockchain.estimatefee"] = float("nan")
    text = await error_call("get_fee_estimate")
    assert "unexpected fee estimate" in text


@pytest.mark.anyio
async def test_a_malformed_txid_never_reaches_the_indexer(error_call, fake_client) -> None:
    text = await error_call("get_transaction", txid="not-a-txid")
    assert "txid" in text
    assert fake_client.calls == []
