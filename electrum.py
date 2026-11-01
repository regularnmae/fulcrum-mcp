"""Minimal Electrum-protocol client for a Fulcrum server.

Stdlib only, for the same reason `rpc.py` in the node server is: this
process talks to one indexer on the podman network, so its whole transport
is a JSON line over a socket -- no HTTP client, no third-party bitcoin
library. Every call opens a connection, negotiates the protocol version and
closes it again; nothing is subscribed, so there is no stream to keep alive
and every tool body stays synchronous.

One exception type covers every failure -- a JSON-RPC error object, a
closed connection, a timeout -- because that is what a tool body needs to
catch. `ElectrumError.code` carries the server's numeric code when it sent
one.
"""

import json
import socket
import ssl
from typing import Any
from urllib.parse import urlparse

DEFAULT_TIMEOUT = 30.0
CLIENT_NAME = "fulcrum-mcp/0.1.0"
PROTOCOL_VERSION = "1.4"
DEFAULT_PORTS = {"tcp": 50001, "ssl": 50002}


class ElectrumError(Exception):
    """A failed Electrum call.

    `message` is the server's text; `code` is its numeric error code, or
    None for a transport failure (which has no server-side code).
    """

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def parse_url(url: str) -> tuple[str, str, int, bool]:
    """Split `tcp://host:port` or `ssl://host:port` into its parts."""
    parsed = urlparse(url)
    if parsed.scheme not in DEFAULT_PORTS:
        raise ValueError(
            f"FULCRUM_URL must start with tcp:// or ssl://, not {url!r}"
        )
    if not parsed.hostname:
        raise ValueError(f"FULCRUM_URL has no host: {url!r}")
    return (
        url,
        parsed.hostname,
        parsed.port or DEFAULT_PORTS[parsed.scheme],
        parsed.scheme == "ssl",
    )


class ElectrumClient:
    """A client bound to one Fulcrum endpoint."""

    def __init__(self, url: str, timeout: float = DEFAULT_TIMEOUT) -> None:
        self._url, self._host, self._port, self._tls = parse_url(url)
        self._timeout = timeout

    def call(
        self, method: str, params: list[Any] | None = None, timeout: float | None = None
    ) -> Any:
        """Invoke `method`, returning `result` or raising `ElectrumError`.

        `timeout` overrides the endpoint timeout for this one call, which is
        what a readiness probe wants.
        """
        effective_timeout = self._timeout if timeout is None else timeout
        try:
            sock = socket.create_connection((self._host, self._port), timeout=effective_timeout)
        except OSError as err:
            raise ElectrumError(f"cannot reach Fulcrum at {self._url}: {err}") from err
        try:
            if self._tls:
                sock = ssl.create_default_context().wrap_socket(
                    sock, server_hostname=self._host
                )
            sock.settimeout(effective_timeout)
            stream = sock.makefile("rwb")
            try:
                self._request(stream, "server.version", [CLIENT_NAME, PROTOCOL_VERSION])
                return self._request(stream, method, params or [])
            finally:
                stream.close()
        except ElectrumError:
            raise
        except TimeoutError as err:
            raise ElectrumError(
                f"Fulcrum did not answer within {effective_timeout:g}s"
            ) from err
        except OSError as err:
            raise ElectrumError(f"connection to Fulcrum failed: {err}") from err
        finally:
            sock.close()

    def _request(self, stream: Any, method: str, params: list[Any]) -> Any:
        # Ids only have to be unique on this connection, which carries one
        # request before closing, so a fixed id is enough.
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        stream.write(json.dumps(payload).encode() + b"\n")
        stream.flush()
        line = stream.readline()
        if not line:
            raise ElectrumError("Fulcrum closed the connection without answering")
        try:
            body = json.loads(line.decode())
        except (UnicodeDecodeError, json.JSONDecodeError) as err:
            raise ElectrumError("Fulcrum returned a non-JSON line") from err
        if not isinstance(body, dict):
            raise ElectrumError("Fulcrum returned an unexpected message")
        error = body.get("error")
        if error:
            if isinstance(error, dict):
                code = error.get("code")
                raise ElectrumError(
                    str(error.get("message", "electrum error")),
                    code if isinstance(code, int) else None,
                )
            raise ElectrumError(str(error))
        return body.get("result")
