"""Address decoding, pinned to published vectors.

The reversal in `scripthash()` and the bech32/base58 details are exactly
the kind of thing that silently produces a valid-looking wrong answer, so
the scripts and scripthashes here are constants from BIP173 and the
Electrum protocol, not values computed by the module under test.
"""

from __future__ import annotations

import hashlib

import pytest

from address import AddressError, address_to_script, scripthash, scripthash_for_address

BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _b58check(version: int, payload: bytes) -> str:
    raw = bytes([version]) + payload
    checksum = hashlib.sha256(hashlib.sha256(raw).digest()).digest()[:4]
    number = int.from_bytes(raw + checksum, "big")
    encoded = ""
    while number:
        number, remainder = divmod(number, 58)
        encoded = BASE58_ALPHABET[remainder] + encoded
    return "1" * (len(raw + checksum) - len((raw + checksum).lstrip(b"\x00"))) + encoded


def test_p2pkh_script_and_scripthash() -> None:
    address = "1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN2"
    assert address_to_script(address).hex() == (
        "76a91477bff20c60e522dfaa3350c39b030a5d004e839a88ac"
    )
    assert scripthash_for_address(address) == (
        "eafd9bc024177ba93572c1cc3a83f555dadbb81ca94cd9761ef5211ce794cea9"
    )


def test_p2sh_script_and_scripthash() -> None:
    address = "3J98t1WpEZ73CNmQviecrnyiWrnqRhWNLy"
    assert address_to_script(address).hex() == (
        "a914b472a266d0bd89c13706a4132ccfb16f7c3b9fcb87"
    )
    assert scripthash_for_address(address) == (
        "abe51e78fc13a23889f49922cb5917b9c5f2a8f66122aea0d728524f1493d133"
    )


def test_bech32_p2wpkh_script_and_scripthash() -> None:
    address = "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"
    assert address_to_script(address).hex() == (
        "0014751e76e8199196d454941c45d1b3a323f1433bd6"
    )
    assert scripthash_for_address(address) == (
        "9623df75239b5daa7f5f03042d325b51498c4bb7059c7748b17049bf96f73888"
    )


def test_bech32_accepts_uppercase() -> None:
    lower = "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"
    assert address_to_script(lower.upper()) == address_to_script(lower)


def test_taproot_uses_bech32m_and_a_32_byte_program() -> None:
    # BIP350's first valid bech32m example.
    script = address_to_script(
        "bc1p0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7vqzk5jj0"
    )
    assert script[0] == 0x51  # OP_1
    assert script[1] == 0x20  # push 32 bytes
    assert len(script) == 34


def test_testnet_p2pkh_is_accepted() -> None:
    script = address_to_script("mipcBbFg9gMiCh81Kj8tqqdgoZub1ZJRfn")
    assert script.hex() == "76a914243f1394f44554f4ce3fd68649c19adc483ce92488ac"


def test_scripthash_is_reversed_and_hex() -> None:
    value = scripthash(b"\x51")
    assert len(value) == 64
    assert value == hashlib.sha256(b"\x51").digest()[::-1].hex()


@pytest.mark.parametrize(
    "address,reason",
    [
        ("", "No address"),
        ("bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t5", "checksum"),
        ("1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN3", "checksum"),
        ("bc1Qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4", "mixes upper and lower"),
        ("0BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN2", "base58"),
    ],
)
def test_bad_addresses_are_rejected_with_a_reason(address: str, reason: str) -> None:
    with pytest.raises(AddressError) as excinfo:
        address_to_script(address)
    assert reason in str(excinfo.value)


def test_an_unsupported_base58_version_is_rejected() -> None:
    address = _b58check(0x30, bytes(20))
    with pytest.raises(AddressError) as excinfo:
        address_to_script(address)
    assert "Unsupported address version" in str(excinfo.value)
