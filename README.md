# fulcrum-mcp

A [Model Context Protocol](https://modelcontextprotocol.io) server that gives
a client read-only access to a [Fulcrum](https://github.com/cculianu/Fulcrum)
Electrum server: address history, balances, UTXOs and transaction lookup from
Fulcrum's own index. No keys, no wallet, no chain writes: nothing here can
move funds or change the chain.

It exists because a node cannot answer the address question. `bitcoind` keeps
no address index, so "which transactions touched this address" needs an
indexer beside it; Fulcrum is that indexer, and this server is its MCP face.

Ten tools, all read-only:

| Tool | What it does |
| --- | --- |
| `get_address_balance` | Confirmed and unconfirmed balance, in satoshis |
| `get_address_history` | The transactions the address touched, paginated, with heights |
| `get_address_utxos` | Unspent outputs with height and value, and their total |
| `get_address_mempool` | Unconfirmed transactions and the fee each pays |
| `get_transaction` | A transaction by id, decoded or hex, from Fulcrum's index |
| `get_transaction_merkle` | A transaction's merkle branch in a block |
| `get_block_header` | A block's hash, time and raw header, by height |
| `get_chain_tip` | The height, hash and time of the block Fulcrum is serving |
| `get_fee_estimate` | Fulcrum's estimate in BTC/kvB and sat/vB |
| `get_server_info` | Fulcrum's version, genesis hash, protocol range and hosts |

Each tool returns text — a scalar as-is, an object as pretty-printed JSON.
They are all marked read-only and open-world, so a well-behaved client skips
the confirmation prompt. A failed lookup comes back as a tool error
(`is_error=True`) with the same readable message, so a client can tell "the
indexer did not answer" from "this address has no history" — the distinction
matters most here, where an empty history is a legitimate answer.

`get_address_history` returns up to `limit` (default 1000) mined entries from
`offset`, with `confirmed_count` for the total and `next_offset` when there
is more; the mempool list is always whole. The cap keeps a heavily used
address from filling a client's context window in one call.

## The indexer

Fulcrum subscribes to a `bitcoind` and builds its own index of every
transaction, output and address. Two consequences shape this server:

- **Scripthashes, not addresses.** Electrum servers index the SHA-256 of an
  output script, byte-reversed and hex-encoded. The address tools do that
  conversion themselves (`address.py`), so a client passes a normal address.
  Supported forms: P2PKH (`1…`), P2SH (`3…`), bech32 P2WPKH/P2WSH (`bc1q…`)
  and taproot (`bc1p…`), plus the testnet and regtest prefixes. The address's
  network is not checked against the server's.
- **Fulcrum's height can lag the node.** `get_chain_tip` is the reference for
  what the index has seen; compare it with `get_blockchain_info` from the
  node server before trusting a "no history" answer for a recent transaction.

The protocol has a subscription model, but this server does not use it: every
call opens a connection, negotiates `server.version` (`1.4`), asks one
question and closes. Nothing is pushed to the client, and no state is kept
between calls.

`get_transaction` reads Fulcrum's index rather than asking the node, so it
resolves a confirmed transaction even when `bitcoind` runs with `txindex=0`
or is still backfilling one.

## Running it

```sh
uv sync                 # installs from uv.lock
FULCRUM_MCP_TOKEN=<a long random token> \
FULCRUM_URL=tcp://127.0.0.1:50001 \
uv run python server.py
```

Or, inside the container, with no uv anywhere:

```sh
podman build -t localhost/fulcrum-mcp:latest .
podman run -e FULCRUM_MCP_TOKEN=<token> \
  -e FULCRUM_URL=tcp://fulcrum:50001 \
  -p 8102:8000 localhost/fulcrum-mcp:latest
```

It listens on `0.0.0.0:8000` and serves streamable HTTP at `/mcp`. `GET
/health` is unauthenticated and answers `{"status":"ok"}` — nothing more, so a
poller can say "up" without holding the token. `GET /ready` is open too and
adds Fulcrum's answer: `200` with `height` and `blockhash`, or `503` with the
reason, probed on a 2-second timeout. The readiness answer is cached for five
seconds, so a poller cannot turn one unauthenticated request into one
connection to the indexer. Both paths answer `GET` and `HEAD`; any other
method is a `405`.

Keep the container healthcheck on `/health`. `/ready` stays `503` while
Fulcrum syncs or is down, so gating podman on it would restart this server in
a loop instead of reporting the indexer's state.

### Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `FULCRUM_URL` | `tcp://fulcrum:50001` | Electrum endpoint; `tcp://` or `ssl://` |
| `FULCRUM_TIMEOUT` | `30` | Call timeout in seconds |
| `FULCRUM_MCP_TOKEN` | – | Bearer token. **Required** unless `MCP_ALLOW_ANONYMOUS=1` |
| `MCP_HOST` / `MCP_PORT` | `0.0.0.0` / `8000` | Listen address |
| `MCP_ALLOW_ANONYMOUS` | – | Set to `1` to serve with no token (loud warning) |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` |
| `FULCRUM_MCP_URL` | `http://127.0.0.1:8000/mcp` | Where `healthcheck.py` probes |

Without `FULCRUM_MCP_TOKEN` the server refuses to start rather than serving
unauthenticated — the port is published on the LAN.

`ssl://` uses the system trust store and verifies the hostname, so it needs a
certificate that matches the name in the URL; the internal `tcp://` default
does not pretend otherwise.

## Security model

This container shares `npm-network` with `bitcoind`, `fulcrum`, `monerod`,
`lightningd` and the rest of the stack, and publishes its port on the LAN. It
speaks only to Fulcrum's Electrum port, and only read methods, so a
prompt-injected client cannot reach a wallet or a spending RPC through it.
Fulcrum itself holds no keys either: it is an indexer, not a wallet.

The bearer token gates every request except `/health` and `/ready`, which
report status only; the comparison is constant-time. Startup fails without a
token unless `MCP_ALLOW_ANONYMOUS=1`.

The Electrum protocol has no authentication of its own, which is fine on
`npm-network`: the trust boundary is the network, and Fulcrum is only reachable
from it.

## Development

Dependencies are managed with [uv](https://docs.astral.sh/uv/) from
`pyproject.toml`; `uv.lock` is the exact closure (versions *and* hashes).

```sh
uv run pytest -q                # 75 tests, ~0.3s, no network
uv run ruff check .             # lint
uv run pyright                  # typecheck
uv run python scripts/smoke.py  # boots the real app on loopback
uv add <package>                # edits pyproject.toml, then re-locks
```

`electrum.py` and `address.py` are stdlib-only on purpose (socket, ssl,
hashlib), so the dependency closure is just `mcp` + `uvicorn` — less to audit
for a server on the indexer's network. `tests/test_electrum.py` drives the
client with a fake socket; `tests/test_address.py` pins the address decoding
to published BIP173/BIP350 and Electrum vectors; `tests/test_tools.py` drives
each tool against a scripted fake indexer.

### The image

A build stage turns `uv.lock` into a hash-pinned requirements file, the
runtime stage installs it with `uv pip install --system --require-hashes`, and
both build tools — uv and pip — are deleted in the same layer.

### Adding a tool

```python
@mcp.tool(structured_output=False, annotations=_INDEX_TOOLS)
def my_tool(address: str) -> str:
    """What the model sees. Args: ..."""
    return _guard("blockchain.scripthash.get_x", lambda: _x(address))
```

`structured_output=False` keeps a text result from being sent twice;
`_INDEX_TOOLS` marks it read-only so clients skip the confirmation prompt; and
`_guard` turns an `AddressError` or `ElectrumError` into a `ToolError`, which
the SDK returns as `is_error=True` with the readable message rather than a
traceback. Adding a tool also means adding its name to `TOOLS` in
`tests/test_schemas.py`; a new top-level module must be added to the
`Containerfile`'s `COPY` lines and pyright's `include`, or it will not ship or
typecheck.

Deployment is owned by the [`stack`](https://github.com/regularnmae/stack)
repo; see `UPSTREAM.md`.
