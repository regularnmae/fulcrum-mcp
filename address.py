"""Address -> output script -> Electrum scripthash, stdlib only.

Electrum servers do not index addresses; they index the SHA-256 of the
output script, byte-reversed and hex-encoded. Turning a `bc1…` into that
value needs bech32/bech32m (BIP173/BIP350) and base58check decoding, which
is what this module does -- no third-party bitcoin library, matching the
rest of the stack's small dependency closure.

Only the conversions an indexer needs live here: P2PKH, P2SH, and segwit
v0/v1 scripts. Mainnet, testnet and regtest prefixes are all accepted; the
address's own network is not checked against the server's.
"""

import hashlib

BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
BECH32M_CONST = 0x2BC830A3

BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

P2PKH_VERSIONS = {0x00, 0x6F}
P2SH_VERSIONS = {0x05, 0xC4}

_SEGWIT_HRPS = ("bc", "tb", "bcrt")


class AddressError(ValueError):
    """An address that is not one of the supported forms.

    The message is written for the model: it says what was rejected and
    what the tool accepts, not which checksum byte was wrong.
    """


def scripthash(script: bytes) -> str:
    """The Electrum scripthash for an output script: sha256, byte-reversed."""
    return hashlib.sha256(script).digest()[::-1].hex()


def address_to_script(address: str) -> bytes:
    """Decode an address into the output script it pays to."""
    if not address:
        raise AddressError("No address was given")
    lowered = address.lower()
    if lowered.startswith(tuple(hrp + "1" for hrp in _SEGWIT_HRPS)):
        # The original, not the lowered form: _bech32_decode rejects mixed
        # case, which lowering first would silently hide.
        return _segwit_script(address)
    return _base58_script(address)


def scripthash_for_address(address: str) -> str:
    """The Electrum scripthash an address's history is indexed under."""
    return scripthash(address_to_script(address))


def _base58_script(address: str) -> bytes:
    payload = _base58check_decode(address)
    if len(payload) != 21:
        raise AddressError("That address has the wrong length")
    version, h160 = payload[0], payload[1:]
    if version in P2PKH_VERSIONS:
        return b"\x76\xa9\x14" + h160 + b"\x88\xac"
    if version in P2SH_VERSIONS:
        return b"\xa9\x14" + h160 + b"\x87"
    raise AddressError(f"Unsupported address version 0x{version:02x}")


def _base58check_decode(address: str) -> bytes:
    number = 0
    for char in address:
        index = BASE58_ALPHABET.find(char)
        if index < 0:
            raise AddressError("That is not a valid base58 address")
        number = number * 58 + index
    raw = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    # Leading '1's encode leading zero bytes, which big-int decoding drops.
    raw = b"\x00" * (len(address) - len(address.lstrip("1"))) + raw
    if len(raw) < 5:
        raise AddressError("That is not a valid base58 address")
    payload, checksum = raw[:-4], raw[-4:]
    if hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4] != checksum:
        raise AddressError("That address failed its base58 checksum")
    return payload


def _segwit_script(address: str) -> bytes:
    hrp, data, spec = _bech32_decode(address)
    if not data:
        raise AddressError("That segwit address carries no witness program")
    version = data[0]
    if version > 16:
        raise AddressError("That address has an invalid witness version")
    program = bytes(_convertbits(data[1:], 5, 8, pad=False))
    if not 2 <= len(program) <= 40:
        raise AddressError("That address has an invalid witness program length")
    if version == 0 and len(program) not in (20, 32):
        raise AddressError("A version 0 witness program must be 20 or 32 bytes")
    if version == 0 and spec != "bech32":
        raise AddressError("A version 0 witness program must use bech32")
    if version != 0 and spec != "bech32m":
        raise AddressError("A version 1+ witness program must use bech32m")
    # OP_0 is 0x00; OP_1..OP_16 are 0x51..0x60, so version 0 is the exception.
    opcode = 0x00 if version == 0 else 0x50 + version
    return bytes([opcode, len(program)]) + program


def _bech32_decode(address: str) -> tuple[str, list[int], str]:
    """Split an address into its HRP, data part and checksum variant."""
    if address.lower() != address and address.upper() != address:
        raise AddressError("That address mixes upper and lower case")
    address = address.lower()
    if any(ord(char) < 33 or ord(char) > 126 for char in address):
        raise AddressError("That address contains a character outside the bech32 range")
    separator = address.rfind("1")
    if separator < 1 or separator + 7 > len(address):
        raise AddressError("That is not a valid bech32 address")
    hrp, data_part = address[:separator], address[separator + 1 :]
    if any(char not in BECH32_CHARSET for char in data_part):
        raise AddressError("That address contains a character outside the bech32 set")
    data = [BECH32_CHARSET.find(char) for char in data_part]
    checksum = _bech32_polymod(_hrp_expand(hrp) + data)
    if checksum == 1:
        spec = "bech32"
    elif checksum == BECH32M_CONST:
        spec = "bech32m"
    else:
        raise AddressError("That address failed its bech32 checksum")
    return hrp, data[:-6], spec


def _bech32_polymod(values: list[int]) -> int:
    generators = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    checksum = 1
    for value in values:
        top = checksum >> 25
        checksum = ((checksum & 0x1FFFFFF) << 5) ^ value
        for bit in range(5):
            checksum ^= generators[bit] if (top >> bit) & 1 else 0
    return checksum


def _hrp_expand(hrp: str) -> list[int]:
    return [ord(char) >> 5 for char in hrp] + [0] + [ord(char) & 31 for char in hrp]


def _convertbits(values: list[int], from_bits: int, to_bits: int, *, pad: bool) -> list[int]:
    accumulator = 0
    bits = 0
    out: list[int] = []
    max_value = (1 << to_bits) - 1
    for value in values:
        accumulator = (accumulator << from_bits) | value
        bits += from_bits
        while bits >= to_bits:
            bits -= to_bits
            out.append((accumulator >> bits) & max_value)
    if pad:
        if bits:
            out.append((accumulator << (to_bits - bits)) & max_value)
    elif bits >= from_bits or ((accumulator << (to_bits - bits)) & max_value):
        raise AddressError("That address has invalid padding")
    return out
