# What this repo owns, and what it depends on

This repo owns only the **application** — its Python code, tests, CI and
`Containerfile`. It ships no unit and no env file. The runtime is owned by
[`stack`](https://github.com/regularnmae/stack), which holds every quadlet unit
and the `npm-network.network` the MCP servers share, writes this server's env
file(s), and publishes the host port. See that repo for the unit, the env-file
locations and `stack/deploy/examples/fulcrum-mcp/`.

It does **not** own Fulcrum, or the `bitcoind` Fulcrum indexes. Both are owned
by `stack`, together with the rest of the crypto node stack. That is
deliberate: the node server, this server and anything else that needs the
indexer must agree on one endpoint, and one file must have one owner.

## Upstream contract

The server assumes a Fulcrum reachable on the podman network:

| | |
| --- | --- |
| Container / address | `fulcrum:50001` (Electrum protocol, plain TCP) |
| Network | `npm-network` |
| Auth | none — the Electrum protocol has no authentication |
| Protocol | `1.4` requested via `server.version`; Fulcrum negotiates |
| Data | Fulcrum has indexed the chain up to its own tip |

Two properties shape the tools:

- **Fulcrum's tip is the reference, not bitcoind's.** An indexer that is
  catching up answers "no history" for a transaction the node already has.
  `get_chain_tip` exposes the height Fulcrum is serving so a client can
  compare it with the node server's `get_blockchain_info`.
- **`get_transaction` does not need `txindex` on bitcoind.** Fulcrum indexes
  every transaction itself, so the lookup works even while the node's txindex
  is disabled or backfilling. Fulcrum still needs a synced node to build its
  own index in the first place.

TLS is available (`ssl://`) but not used on `npm-network`: it would need a
certificate for the container name, and the network itself is the trust
boundary.

## Deploy

```sh
cd ~/build/stack
./deploy.sh --only fulcrum-mcp   # build the image, restart, wait for "(healthy)"
```

See `stack` for the env-file location and the host port it publishes.
