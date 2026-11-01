import asyncio
import hashlib
import hmac
import json
import logging
import math
import os
import time
from collections.abc import Callable
from typing import Annotated, Any, Literal, get_args

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field

from address import AddressError, scripthash_for_address
from electrum import ElectrumClient, ElectrumError

logger = logging.getLogger("fulcrum-mcp")

# The SDK types log_level as a closed set, so an unrecognised LOG_LEVEL
# has to be resolved to one of its levels here -- the raw env string is
# not assignable, and an unknown level falling back to INFO beats the
# import-time raise that the SDK would otherwise produce.
_LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
_LEVELS: tuple[_LogLevel, ...] = get_args(_LogLevel)


def _log_level() -> _LogLevel:
    requested = os.environ.get("LOG_LEVEL", "INFO").upper()
    return requested if requested in _LEVELS else "INFO"


def _env_float(name: str, default: float) -> float:
    """A float env var, or the default when unset or unparseable.

    A typo'd timeout should not stop the server from starting, so a bad
    value warns and falls back rather than raising at import.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("ignoring non-numeric %s=%r; using %s", name, raw, default)
        return default


mcp = MCPServer(
    "Fulcrum Electrum",
    instructions=(
        "Read-only access to a Fulcrum Electrum server, which indexes the "
        "chain by address -- something a Bitcoin node cannot do. The address "
        "tools take an ordinary address and decode it to the scripthash "
        "Fulcrum indexes; get_address_history is the one question bitcoind "
        "cannot answer, and it pages a long history. get_address_mempool "
        "adds the fee each unconfirmed transaction pays. get_transaction "
        "reads Fulcrum's own transaction index, so it does not depend on "
        "txindex on the node behind it; get_transaction_merkle proves a "
        "transaction is in a block, and get_block_header dates that block. "
        "get_fee_estimate quotes Fulcrum's fee estimate. Start with "
        "get_chain_tip for the server's height. Nothing here moves funds or "
        "changes the chain."
    ),
    log_level=_log_level(),
)

# Every tool here only reads. Saying so lets a well-behaved client skip a
# confirmation prompt before running one of them. An indexer is open-world:
# its answers depend on the state of the network it is following.
_INDEX_TOOLS = ToolAnnotations(read_only_hint=True, open_world_hint=True)

# Reached over the podman npm-network (container name resolves without any
# host publishing); FULCRUM_URL overrides. Plain TCP is the internal default:
# TLS on that network would need a certificate for the container name.
FULCRUM_URL = os.environ.get("FULCRUM_URL", "tcp://fulcrum:50001")
FULCRUM_TIMEOUT = _env_float("FULCRUM_TIMEOUT", 30.0)

# Set to "1" to serve the endpoint with no token at all. Without it a
# missing token is a startup error, not a warning.
MCP_ALLOW_ANONYMOUS = os.environ.get("MCP_ALLOW_ANONYMOUS", "") == "1"

# Unauthenticated liveness path, for pollers that hold no token.
HEALTH_PATH = "/health"

# Readiness path: /health says the process is up, this says Fulcrum answers.
# Deliberately separate -- an indexer outage must not fail a liveness probe,
# or the container restarts in a loop while Fulcrum catches up.
READY_PATH = "/ready"
READY_TIMEOUT = 2.0
# /ready is unauthenticated, and every probe opens a Fulcrum connection, so
# the answer is cached for a few seconds: a poller hammering the path then
# costs one indexer round trip per TTL instead of one per request.
READY_CACHE_TTL = 5.0
_ready_cache: tuple[float, dict[str, Any], int] | None = None
# Created lazily, inside the running loop: an asyncio.Lock binds to the loop
# that first awaits it, and this server has exactly one in production.
_ready_lock: asyncio.Lock | None = None

# The env var the bearer token is read from (also the name in the quadlet
# unit's EnvironmentFile and in ~/.config/opencode/opencode.json).
TOKEN_ENV = "FULCRUM_MCP_TOKEN"

client = ElectrumClient(FULCRUM_URL, timeout=FULCRUM_TIMEOUT)

_ADDRESS_HELP = (
    "Accepted: P2PKH (1...), P2SH (3...), bech32 P2WPKH/P2WSH (bc1q...) and "
    "taproot (bc1p...), plus the testnet and regtest prefixes."
)

# A txid is 32 bytes of hex; rejecting anything else here keeps an obvious
# typo from becoming a round trip that Fulcrum has to say no to.
_TXID = Annotated[str, Field(pattern=r"^[0-9a-fA-F]{64}$")]


def _render(result: Any) -> str:
    """A tool result as text: a bare string as-is, anything else as JSON."""
    if isinstance(result, str):
        return result
    return json.dumps(result, indent=2)


def _int_field(value: Any, what: str) -> int:
    """A field Fulcrum promised, or an error naming what was missing.

    `bool` is excluded on purpose: it is an `int` subclass, and a JSON
    `true` where a balance belongs is a malformed answer, not a one-satoshi
    balance.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ElectrumError(f"Fulcrum returned an unexpected {what}")
    return value


def _str_field(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value:
        raise ElectrumError(f"Fulcrum returned an unexpected {what}")
    return value


def _error_text(err: ElectrumError) -> str:
    if err.code is None:
        return err.message
    return f"{err.message} (electrum error {err.code})"


def _guard(label: str, action: Callable[[], Any]) -> str:
    """Run a tool body, turning its known failures into a tool error.

    `label` is the Electrum method (or the intent) that failed, so a log line
    names what broke without every tool repeating the same four `except`
    clauses. Failures raise `ToolError`, which the SDK returns as
    `is_error=True` with the same text the model would otherwise have read --
    so a client can tell a failed lookup from an address with no history.
    """
    try:
        return _render(action())
    except AddressError as err:
        raise ToolError(f"{err}. {_ADDRESS_HELP}") from err
    except ElectrumError as err:
        logger.warning("%s failed: %s", label, err.message)
        raise ToolError(_error_text(err)) from err
    except ToolError:
        raise
    except Exception as err:  # noqa: BLE001 - surfaced as text, never hidden
        logger.exception("unexpected error in %s", label)
        raise ToolError(f"Unexpected error calling {label}: {err}") from err


def _header_fields(hexstring: Any) -> tuple[str, int]:
    """A block header's hash and timestamp, from the hex Electrum returns."""
    try:
        raw = bytes.fromhex(hexstring or "")
    except (TypeError, ValueError) as err:
        raise ElectrumError("Fulcrum returned a malformed block header") from err
    if len(raw) != 80:
        raise ElectrumError("Fulcrum returned a malformed block header")
    blockhash = hashlib.sha256(hashlib.sha256(raw[:80]).digest()).digest()[::-1].hex()
    return blockhash, int.from_bytes(raw[68:72], "little")


def _balance(address: str) -> dict[str, Any]:
    script_hash = scripthash_for_address(address)
    result = client.call("blockchain.scripthash.get_balance", [script_hash])
    if not isinstance(result, dict):
        raise ElectrumError("Fulcrum returned an unexpected balance")
    return {
        "address": address,
        "scripthash": script_hash,
        "confirmed": _int_field(result.get("confirmed"), "confirmed balance"),
        "unconfirmed": _int_field(result.get("unconfirmed"), "unconfirmed balance"),
        "unit": "satoshi",
    }


def _history(address: str, limit: int, offset: int) -> dict[str, Any]:
    script_hash = scripthash_for_address(address)
    rows = client.call("blockchain.scripthash.get_history", [script_hash])
    if not isinstance(rows, list):
        raise ElectrumError("Fulcrum returned an unexpected history")
    confirmed: list[dict[str, Any]] = []
    mempool: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            raise ElectrumError("Fulcrum returned an unexpected history entry")
        height = _int_field(row.get("height"), "history height")
        entry = {"txid": _str_field(row.get("tx_hash"), "history txid"), "height": height}
        if height > 0:
            confirmed.append(entry)
        else:
            mempool.append(entry)
    page = confirmed[offset : offset + limit]
    next_offset = offset + len(page)
    return {
        "address": address,
        "scripthash": script_hash,
        "confirmed": page,
        "mempool": mempool,
        "confirmed_count": len(confirmed),
        "mempool_count": len(mempool),
        "returned": len(page),
        "next_offset": next_offset if next_offset < len(confirmed) else None,
    }


def _utxos(address: str) -> dict[str, Any]:
    script_hash = scripthash_for_address(address)
    rows = client.call("blockchain.scripthash.listunspent", [script_hash])
    if not isinstance(rows, list):
        raise ElectrumError("Fulcrum returned an unexpected utxo list")
    utxos = []
    for row in rows:
        if not isinstance(row, dict):
            raise ElectrumError("Fulcrum returned an unexpected utxo entry")
        height = _int_field(row.get("height"), "utxo height")
        utxos.append(
            {
                "txid": _str_field(row.get("tx_hash"), "utxo txid"),
                "vout": _int_field(row.get("tx_pos"), "utxo position"),
                "height": height,
                "value": _int_field(row.get("value"), "utxo value"),
                "confirmed": height > 0,
            }
        )
    return {
        "address": address,
        "scripthash": script_hash,
        "utxos": utxos,
        "count": len(utxos),
        "total": sum(utxo["value"] for utxo in utxos),
        "unit": "satoshi",
    }


def _mempool(address: str) -> dict[str, Any]:
    script_hash = scripthash_for_address(address)
    rows = client.call("blockchain.scripthash.get_mempool", [script_hash])
    if not isinstance(rows, list):
        raise ElectrumError("Fulcrum returned an unexpected mempool")
    transactions = []
    for row in rows:
        if not isinstance(row, dict):
            raise ElectrumError("Fulcrum returned an unexpected mempool entry")
        transactions.append(
            {
                "txid": _str_field(row.get("tx_hash"), "mempool txid"),
                "height": _int_field(row.get("height"), "mempool height"),
                "fee": _int_field(row.get("fee"), "mempool fee"),
            }
        )
    return {
        "address": address,
        "scripthash": script_hash,
        "transactions": transactions,
        "count": len(transactions),
        "total_fee": sum(transaction["fee"] for transaction in transactions),
        "unit": "satoshi",
    }


def _chain_tip() -> dict[str, Any]:
    header = client.call("blockchain.headers.subscribe", [])
    if not isinstance(header, dict):
        raise ElectrumError("Fulcrum returned an unexpected block header")
    height = _int_field(header.get("height"), "tip height")
    blockhash, timestamp = _header_fields(header.get("hex"))
    return {"height": height, "blockhash": blockhash, "time": timestamp}


def _block_header(height: int) -> dict[str, Any]:
    hexstring = client.call("blockchain.block.header", [height])
    blockhash, timestamp = _header_fields(hexstring)
    return {"height": height, "blockhash": blockhash, "time": timestamp, "header": hexstring}


def _transaction_merkle(txid: str, height: int) -> dict[str, Any]:
    result = client.call("blockchain.transaction.get_merkle", [txid, height])
    if not isinstance(result, dict):
        raise ElectrumError("Fulcrum returned an unexpected merkle proof")
    merkle = result.get("merkle")
    if not isinstance(merkle, list):
        raise ElectrumError("Fulcrum returned an unexpected merkle proof")
    return {
        "txid": txid,
        "block_height": _int_field(result.get("block_height"), "merkle block height"),
        "pos": _int_field(result.get("pos"), "merkle position"),
        "merkle": merkle,
    }


def _fee_estimate(blocks: int) -> dict[str, Any]:
    estimate = client.call("blockchain.estimatefee", [blocks])
    if (
        isinstance(estimate, bool)
        or not isinstance(estimate, (int, float))
        or not math.isfinite(estimate)
    ):
        raise ElectrumError("Fulcrum returned an unexpected fee estimate")
    if estimate < 0:
        return {
            "blocks": blocks,
            "btc_per_kvb": None,
            "sat_per_vb": None,
            "note": "Fulcrum has no estimate for this target",
        }
    # Electrum quotes BTC per 1000 virtual bytes; 1e8 sat/BTC / 1000 vB is 1e5.
    return {
        "blocks": blocks,
        "btc_per_kvb": estimate,
        "sat_per_vb": round(estimate * 100_000, 3),
    }


@mcp.tool(structured_output=False, annotations=_INDEX_TOOLS)
def get_address_balance(address: str) -> str:
    """Return an address's balance as Fulcrum sees it, in satoshis.

    `confirmed` counts mined outputs; `unconfirmed` is the mempool delta, so
    a negative unconfirmed means the address has spent coins that are not
    mined yet.

    Args:
        address: A bitcoin address, e.g. bc1q... or 1....
    """
    return _guard("blockchain.scripthash.get_balance", lambda: _balance(address))


@mcp.tool(structured_output=False, annotations=_INDEX_TOOLS)
def get_address_history(
    address: str,
    limit: Annotated[int, Field(ge=1, le=10000)] = 1000,
    offset: Annotated[int, Field(ge=0)] = 0,
) -> str:
    """Return the transactions an address has touched, one page at a time.

    This is the question a plain node cannot answer: bitcoind keeps no
    address index. Entries carry the txid and the height it was mined at;
    height 0 is an unconfirmed mempool transaction and -1 an unconfirmed
    one with unconfirmed parents. `confirmed` is ordered oldest first and
    paginated by `limit`/`offset`; `mempool` is always returned whole.

    `confirmed_count` is the total mined history, not the size of this page.
    When `next_offset` is not null, call again with it as `offset` to read
    the rest.

    Args:
        address: A bitcoin address, e.g. bc1q... or 1....
        limit: Maximum confirmed entries to return (1-10000, default 1000).
        offset: Index of the first confirmed entry to return (default 0).
    """
    return _guard(
        "blockchain.scripthash.get_history",
        lambda: _history(address, limit, offset),
    )


@mcp.tool(structured_output=False, annotations=_INDEX_TOOLS)
def get_address_utxos(address: str) -> str:
    """Return an address's unspent outputs, in satoshis.

    `height` is where the output was created, so a height of 0 is an
    unconfirmed output. `total` sums `value` across the list.

    Args:
        address: A bitcoin address, e.g. bc1q... or 1....
    """
    return _guard("blockchain.scripthash.listunspent", lambda: _utxos(address))


@mcp.tool(structured_output=False, annotations=_INDEX_TOOLS)
def get_address_mempool(address: str) -> str:
    """Return an address's unconfirmed transactions, with the fee each pays.

    `get_address_history` names the mempool transactions an address has
    touched; this adds the fee (in satoshis) Fulcrum attributes to each one,
    which is what tells a stuck payment from a well-priced one. Height 0 is
    an unconfirmed transaction and -1 one with unconfirmed parents.

    Args:
        address: A bitcoin address, e.g. bc1q... or 1....
    """
    return _guard("blockchain.scripthash.get_mempool", lambda: _mempool(address))


@mcp.tool(structured_output=False, annotations=_INDEX_TOOLS)
def get_transaction(txid: _TXID, verbose: bool = True) -> str:
    """Return a transaction by id, from Fulcrum's own index.

    Fulcrum indexes every transaction it has seen, so this resolves a
    confirmed transaction without relying on `txindex` on the node behind
    it. It also serves mempool transactions Fulcrum knows about.

    Args:
        txid: Transaction id (hex).
        verbose: True (default) for the decoded transaction, False for the
            serialised hex.
    """
    return _guard(
        "blockchain.transaction.get",
        lambda: client.call("blockchain.transaction.get", [txid.lower(), verbose]),
    )


@mcp.tool(structured_output=False, annotations=_INDEX_TOOLS)
def get_transaction_merkle(txid: _TXID, height: Annotated[int, Field(ge=0)]) -> str:
    """Return a transaction's merkle branch in the block at `height`.

    This is the proof that a transaction is in a block, and it also confirms
    which block: `pos` is its position in that block's transaction list. The
    protocol has no txid-to-height lookup, so take `height` from
    `get_address_history` (or a block you already know).

    Args:
        txid: Transaction id (hex).
        height: Height of the block the transaction was mined in.
    """
    return _guard(
        "blockchain.transaction.get_merkle",
        lambda: _transaction_merkle(txid.lower(), height),
    )


@mcp.tool(structured_output=False, annotations=_INDEX_TOOLS)
def get_chain_tip() -> str:
    """Return the chain tip Fulcrum is serving: height, hash and block time.

    Use this as the reference height for the address tools: if it lags
    get_blockchain_info from the node server, Fulcrum is still catching up.
    """
    return _guard("blockchain.headers.subscribe", _chain_tip)


@mcp.tool(structured_output=False, annotations=_INDEX_TOOLS)
def get_block_header(height: Annotated[int, Field(ge=0)]) -> str:
    """Return a block's hash, timestamp and raw 80-byte header, by height.

    Use this to date a transaction's block (the height comes from
    `get_address_history`) without trusting the current tip, or to check a
    header's proof of work from the `header` hex it returns.

    Args:
        height: Block height, where 0 is the genesis block.
    """
    return _guard("blockchain.block.header", lambda: _block_header(height))


@mcp.tool(structured_output=False, annotations=_INDEX_TOOLS)
def get_fee_estimate(blocks: Annotated[int, Field(ge=1, le=1008)] = 2) -> str:
    """Return Fulcrum's fee estimate for confirmation within `blocks`.

    The estimate is quoted in both units Fulcrum's protocol and a wallet use:
    `btc_per_kvb` as Electrum returns it, and `sat_per_vb` converted for
    reading. A target with no estimate answers with nulls and a note rather
    than a made-up number.

    Args:
        blocks: Confirmation target in blocks (1-1008, default 2).
    """
    return _guard("blockchain.estimatefee", lambda: _fee_estimate(blocks))


@mcp.tool(structured_output=False, annotations=_INDEX_TOOLS)
def get_server_info() -> str:
    """Return the server's features: version, genesis hash, protocol range.

    Fulcrum's `server.features` also lists the hosts it advertises, which is
    a quick way to confirm which network this endpoint serves.
    """
    return _guard("server.features", lambda: client.call("server.features"))


async def _readiness() -> tuple[dict[str, Any], int]:
    """`/ready`'s answer, cached for READY_CACHE_TTL seconds.

    The path is unauthenticated, so without the cache any LAN host could
    turn one HTTP request into one Fulcrum connection. The cache is a plain
    module global: a stale-by-five-seconds tip is still the right answer for
    a readiness probe. The lock means a burst of concurrent requests produces
    one probe rather than one each; the later callers read its answer.
    """
    global _ready_cache, _ready_lock
    if _ready_lock is None:
        _ready_lock = asyncio.Lock()
    async with _ready_lock:
        cached = _ready_cache
        if cached is not None and time.monotonic() - cached[0] < READY_CACHE_TTL:
            return cached[1], cached[2]
        body, status_code = await _probe_readiness()
        _ready_cache = (time.monotonic(), body, status_code)
        return body, status_code


async def _probe_readiness() -> tuple[dict[str, Any], int]:
    """Ask Fulcrum for its tip, off the event loop with a short timeout.

    Returns the JSON body and status code: 200 with the served tip when
    Fulcrum answers, 503 with the reason when it does not.
    """
    try:
        tip = await asyncio.to_thread(
            client.call, "blockchain.headers.subscribe", [], READY_TIMEOUT
        )
    except ElectrumError as err:
        logger.warning("/ready: Fulcrum is not answering: %s", err.message)
        return {"status": "unready", "error": err.message}, 503
    except Exception as err:  # noqa: BLE001 - reported, never raised at a probe
        logger.exception("/ready: unexpected error")
        return {"status": "unready", "error": str(err)}, 503
    if not isinstance(tip, dict):
        return {"status": "unready", "error": "Fulcrum returned an unexpected body"}, 503
    try:
        blockhash, _ = _header_fields(tip.get("hex"))
    except ElectrumError as err:
        return {"status": "unready", "error": err.message}, 503
    return {"status": "ready", "height": tip.get("height"), "blockhash": blockhash}, 200


async def _json_response(
    scope: Any,
    receive: Any,
    send: Any,
    body: dict[str, Any],
    status_code: int,
    headers: dict[str, str] | None = None,
) -> None:
    """Send one JSON response, with no body for a HEAD request."""
    from starlette.responses import JSONResponse

    response = JSONResponse(body, status_code=status_code, headers=headers)
    if scope.get("method") == "HEAD":
        # JSONResponse has already set Content-Length for the body it would
        # have sent; HEAD promises the headers and no body.
        response.body = b""
    await response(scope, receive, send)


class BearerAuthMiddleware:
    """Pure-ASGI bearer auth in front of the streamable-HTTP app.

    The endpoint is published on the LAN, so a missing token is a hard
    error rather than a warning: serving every tool unauthenticated for
    as long as the env var is absent is not a failure mode worth keeping.
    Set MCP_ALLOW_ANONYMOUS=1 to run it open on purpose, which is what
    debugging from inside npm-network wants.

    GET /health stays unauthenticated so a poller can say "up" without
    holding the token; it says nothing else. GET /ready does the same and
    adds whether Fulcrum answers, on a short timeout, for the same reason.
    Both answer only GET and HEAD -- a write method on a status path is a
    client bug, not something to answer 200 to.
    """

    def __init__(self, app: Any, token: str | None) -> None:
        self.app = app
        self.token = token or ""
        if not self.token:
            if not MCP_ALLOW_ANONYMOUS:
                raise RuntimeError(
                    f"{TOKEN_ENV} is not set, and this endpoint is published "
                    "on the LAN. Set the token, or set MCP_ALLOW_ANONYMOUS=1 "
                    "to serve without authentication."
                )
            logger.warning(
                "%s not set and MCP_ALLOW_ANONYMOUS=1: endpoint is UNAUTHENTICATED",
                TOKEN_ENV,
            )

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if scope.get("path") in (HEALTH_PATH, READY_PATH):
            if scope.get("method") not in ("GET", "HEAD"):
                await _json_response(
                    scope,
                    receive,
                    send,
                    {"error": "method not allowed"},
                    405,
                    {"allow": "GET, HEAD"},
                )
                return
            if scope.get("path") == HEALTH_PATH:
                await _json_response(scope, receive, send, {"status": "ok"}, 200)
                return
            body, status_code = await _readiness()
            await _json_response(scope, receive, send, body, status_code)
            return

        if self.token and not self._authorised(scope):
            await _json_response(
                scope, receive, send, {"error": "unauthorized"}, 401
            )
            return

        await self.app(scope, receive, send)

    def _authorised(self, scope: Any) -> bool:
        """Compare the presented credential in constant time."""
        headers = dict(scope.get("headers") or [])
        header = headers.get(b"authorization", b"").decode("latin-1").strip()
        # RFC 7235 makes the scheme case-insensitive; a client sending
        # "bearer <token>" is not a typo worth a 401, and neither is a
        # stray space inside the header.
        scheme, _, presented = header.partition(" ")
        if scheme.lower() != "bearer":
            return False
        return hmac.compare_digest(presented.strip().encode(), self.token.encode())


if __name__ == "__main__":
    import uvicorn

    # Accept any Host header: DNS-rebinding protection is disabled
    # because the bearer token gates every request.
    app = BearerAuthMiddleware(
        mcp.streamable_http_app(
            transport_security=TransportSecuritySettings(
                enable_dns_rebinding_protection=False
            )
        ),
        os.environ.get(TOKEN_ENV),
    )

    # 0.0.0.0 is what the container publishes; loopback is what running it
    # for a smoke test wants, hence the env overrides.
    uvicorn.run(
        app,
        host=os.environ.get("MCP_HOST", "0.0.0.0"),
        port=int(os.environ.get("MCP_PORT", "8000")),
        log_level=_log_level().lower(),
    )
