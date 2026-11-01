# fulcrum-mcp — agent notes

Flat Python MCP server (not a package). `server.py` is the app — config,
tools, auth middleware; `electrum.py` is its **stdlib-only** Electrum client
(socket, ssl, json — no HTTP, no bitcoin library); `address.py` decodes
addresses to output scripts and scripthashes; `healthcheck.py` is the
container healthcheck probe. Shipped as a rootless podman/quadlet container;
the runtime — its unit, the `npm-network` it shares and the env wiring — is
owned by the `stack` repo (https://github.com/regularnmae/stack), not here.
Toolchain is uv only — no pip in CI, in the build, or in the image (CI
asserts it). `mcp` is the 2.x line: the app object is `MCPServer` (the v2
rename of `FastMCP`).

This is a sibling of `bitcoin-node-mcp`, `monero-node-mcp`,
`monero-wallet-mcp` and `lightning-mcp`, modelled on the pre-existing
`searxng-mcp`. Follow that repo's structure and conventions. Env vars, tool
list and the security model are documented in `README.md`; don't re-derive
them from the code.

## Commands

```sh
uv run ruff check .            # lint (CI gate). No `ruff format`; line-length 100, rules E,F,I,UP,B,SIM
uv run pyright                 # typecheck (CI gate); include list in [tool.pyright]
uv run pytest -q               # 82 tests, ~0.4s, no network
uv run python scripts/smoke.py # boots the real app on loopback: /health, /ready, 401s, handshake
uv add <pkg>                   # edits pyproject.toml AND re-locks — commit the uv.lock change
uv lock --check                # CI drift gate
podman build -t localhost/fulcrum-mcp:latest .   # single-stage Containerfile
```

Deployment is owned by the `stack` repo: `cd ~/build/stack && ./deploy.sh
--only fulcrum-mcp` rebuilds the image, restarts `fulcrum-mcp.service` and
waits for "(healthy)"; `./deploy.sh` deploys the whole stack. The unit and
`npm-network` live there, not here.

## Facts that are easy to get wrong

- **A scripthash is not an address hash.** It is `sha256(output_script)`,
  **byte-reversed**, hex-encoded. Every address tool goes through
  `address.scripthash_for_address`; nothing should hand-build it.
- **Witness opcodes:** version 0 is `OP_0` = `0x00`, and versions 1–16 are
  `0x51`–`0x60`. Writing `0x50 + version` for version 0 produces a script that
  looks plausible and indexes nothing — that bug was caught by the BIP173
  vector in `tests/test_address.py`, which is why those constants are pinned
  rather than computed by the module under test.
- **One connection per call, handshake first.** `ElectrumClient.call` opens a
  socket, sends `server.version` (`fulcrum-mcp/<__version__>`, protocol `1.4`),
  asks one question and closes. There are no subscriptions: MCP is
  request/response, so `blockchain.scripthash.subscribe` and friends are out
  of scope, and no state is kept between calls. A response line is capped at
  `MAX_RESPONSE_BYTES` (8 MiB), only a reset/aborted connect is retried, and at
  most `MAX_CONCURRENT_CALLS` (8) calls are in flight at once.
- Tool bodies are synchronous (`def`, not `async def`); anyio runs them on
  worker threads. `ElectrumClient` keeps no protocol state between calls (only
  a semaphore bounding concurrent sockets), so that is safe.
- Every `@mcp.tool` passes `structured_output=False` and
  `annotations=_INDEX_TOOLS`; `tests/test_schemas.py` asserts this and the
  exact set of tool names — adding a tool means updating `TOOLS` there.
- `_guard` raises `ToolError` rather than returning error text, so a failure
  is `is_error=True` with the same message. A Fulcrum shape that is not what
  the protocol promises is an error, never an empty history or a zero balance.
- Address decoding is stdlib-only on purpose (`hashlib`, no `embit`/
  `bitcoinlib`). It validates bech32/bech32m checksums and the base58check
  checksum, and accepts mainnet/testnet/regtest prefixes without checking the
  address's network against the server's.
- A new top-level module must be added to the `Containerfile`'s `COPY` lines
  **and** pyright's `include`, or it won't ship or typecheck.
- `.containerignore` excludes `tests/`, `scripts/`, `*.md`, `.git`.
- The image declares no `HEALTHCHECK`; the `stack` unit supplies
  `HealthCmd=python /app/healthcheck.py`. `stack` writes this server's env
  file(s); see that repo for the unit, the env-file location and the host
  port.
- The server binds `0.0.0.0:8000` and serves `/mcp` (token-gated); `/health` is
  open and answers exactly `{"status":"ok"}`. `/ready` is also open and probes
  Fulcrum (`200` with height and blockhash, `503` when it does not answer, 2s
  timeout), cached for `READY_CACHE_TTL` (5s) because the path is open. Both
  answer `GET`/`HEAD` only and `405` anything else. The podman healthcheck is
  `healthcheck.py` against `/mcp` (an authenticated initialize) and must not be
  moved to `/ready`, or a syncing indexer restart-loops the container.
- The bearer-token env var is **`FULCRUM_MCP_TOKEN`**, not a shared name: each
  sibling server has its own token.

## Testing quirks

- Tests talk to the server through the SDK's in-memory `Client(mcp)` — no test
  opens a socket to Fulcrum. `fake_client` (in `tests/conftest.py`) replaces
  `server.client.call` with a scripted fake that records calls; use it to pin
  the scripthash sent on the wire and the error handling.
- `tests/test_electrum.py` does fake a socket, but by monkeypatching
  `electrum.socket.create_connection`, so the transport contract (handshake
  first, one JSON line, error codes, timeouts) is covered without a server.
- `pytest` runs with `pythonpath = ["."]`, so `import server` / `import
  electrum` / `import address` work as top-level modules. Do **not** `from
  conftest import ...` in a test — `tests` is a package and that import fails;
  use the fixtures.
