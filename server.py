import asyncio
import hashlib
import hmac
import json
import logging
import os
from collections.abc import Callable
from typing import Any, Literal, get_args

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

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
        "cannot answer. get_transaction reads Fulcrum's own transaction "
        "index, so it does not depend on txindex on the node behind it. "
        "Start with get_chain_tip for the server's height. Nothing here "
        "moves funds or changes the chain."
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

# The env var the bearer token is read from (also the name in the quadlet
# unit's EnvironmentFile and in ~/.config/opencode/opencode.json).
TOKEN_ENV = "FULCRUM_MCP_TOKEN"

client = ElectrumClient(FULCRUM_URL, timeout=FULCRUM_TIMEOUT)

_ADDRESS_HELP = (
    "Accepted: P2PKH (1...), P2SH (3...), bech32 P2WPKH/P2WSH (bc1q...) and "
    "taproot (bc1p...), plus the testnet and regtest prefixes."
)


def _render(result: Any) -> str:
    """A tool result as text: a bare string as-is, anything else as JSON."""
    if isinstance(result, str):
        return result
    return json.dumps(result, indent=2)


def _error_text(err: ElectrumError) -> str:
    if err.code is None:
        return err.message
    return f"{err.message} (electrum error {err.code})"


def _guard(label: str, action: Callable[[], Any]) -> str:
    """Run a tool body, turning its known failures into text.

    `label` is the Electrum method (or the intent) that failed, so a log line
    names what broke without every tool repeating the same four `except`
    clauses.
    """
    try:
        return _render(action())
    except AddressError as err:
        return f"{err}. {_ADDRESS_HELP}"
    except ElectrumError as err:
        logger.warning("%s failed: %s", label, err.message)
        return _error_text(err)
    except Exception as err:  # noqa: BLE001 - surfaced as text, never hidden
        logger.exception("unexpected error in %s", label)
        return f"Unexpected error calling {label}: {err}"


def _header_fields(hexstring: Any) -> tuple[str, int]:
    """A block header's hash and timestamp, from the hex Electrum returns."""
    try:
        raw = bytes.fromhex(hexstring or "")
    except (TypeError, ValueError) as err:
        raise ElectrumError("Fulcrum returned a malformed block header") from err
    if len(raw) < 80:
        raise ElectrumError("Fulcrum returned a short block header")
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
        "confirmed": result.get("confirmed", 0),
        "unconfirmed": result.get("unconfirmed", 0),
        "unit": "satoshi",
    }


def _history(address: str) -> dict[str, Any]:
    script_hash = scripthash_for_address(address)
    rows = client.call("blockchain.scripthash.get_history", [script_hash])
    confirmed: list[dict[str, Any]] = []
    mempool: list[dict[str, Any]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        height = row.get("height")
        entry = {"txid": row.get("tx_hash"), "height": height}
        if isinstance(height, int) and height > 0:
            confirmed.append(entry)
        else:
            mempool.append(entry)
    return {
        "address": address,
        "scripthash": script_hash,
        "confirmed": confirmed,
        "mempool": mempool,
        "confirmed_count": len(confirmed),
        "mempool_count": len(mempool),
    }


def _utxos(address: str) -> dict[str, Any]:
    script_hash = scripthash_for_address(address)
    rows = client.call("blockchain.scripthash.listunspent", [script_hash])
    utxos = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        height = row.get("height")
        utxos.append(
            {
                "txid": row.get("tx_hash"),
                "vout": row.get("tx_pos"),
                "height": height,
                "value": row.get("value"),
                "confirmed": isinstance(height, int) and height > 0,
            }
        )
    total = sum(u["value"] for u in utxos if isinstance(u["value"], int))
    return {
        "address": address,
        "scripthash": script_hash,
        "utxos": utxos,
        "count": len(utxos),
        "total": total,
        "unit": "satoshi",
    }


def _chain_tip() -> dict[str, Any]:
    header = client.call("blockchain.headers.subscribe", [])
    if not isinstance(header, dict):
        raise ElectrumError("Fulcrum returned an unexpected block header")
    blockhash, timestamp = _header_fields(header.get("hex"))
    return {"height": header.get("height"), "blockhash": blockhash, "time": timestamp}


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
def get_address_history(address: str) -> str:
    """Return every transaction an address has touched.

    This is the question a plain node cannot answer: bitcoind keeps no
    address index. Entries carry the txid and the height it was mined at;
    height 0 is an unconfirmed mempool transaction and -1 an unconfirmed
    one with unconfirmed parents. `confirmed` is ordered oldest first.

    Args:
        address: A bitcoin address, e.g. bc1q... or 1....
    """
    return _guard("blockchain.scripthash.get_history", lambda: _history(address))


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
def get_transaction(txid: str, verbose: bool = True) -> str:
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
        lambda: client.call("blockchain.transaction.get", [txid, verbose]),
    )


@mcp.tool(structured_output=False, annotations=_INDEX_TOOLS)
def get_chain_tip() -> str:
    """Return the chain tip Fulcrum is serving: height, hash and block time.

    Use this as the reference height for the address tools: if it lags
    get_blockchain_info from the node server, Fulcrum is still catching up.
    """
    return _guard("blockchain.headers.subscribe", _chain_tip)


@mcp.tool(structured_output=False, annotations=_INDEX_TOOLS)
def get_server_info() -> str:
    """Return the server's features: version, genesis hash, protocol range.

    Fulcrum's `server.features` also lists the hosts it advertises, which is
    a quick way to confirm which network this endpoint serves.
    """
    return _guard("server.features", lambda: client.call("server.features"))


async def _readiness() -> tuple[dict[str, Any], int]:
    """Probe Fulcrum for `/ready`, off the event loop with a short timeout.

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

        if scope.get("path") == HEALTH_PATH:
            from starlette.responses import JSONResponse

            response = JSONResponse({"status": "ok"})
            await response(scope, receive, send)
            return

        if scope.get("path") == READY_PATH:
            from starlette.responses import JSONResponse

            body, status_code = await _readiness()
            response = JSONResponse(body, status_code=status_code)
            await response(scope, receive, send)
            return

        if self.token and not self._authorised(scope):
            from starlette.responses import JSONResponse

            response = JSONResponse({"error": "unauthorized"}, status_code=401)
            await response(scope, receive, send)
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
