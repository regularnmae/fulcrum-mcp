"""The Electrum transport, driven with a fake socket.

What is pinned here is the wire contract: a handshake first, one JSON line
per request, an error object named with its code, and every failure mode
(closed connection, garbage, timeout, refused connect) surfacing as the one
`ElectrumError` a tool body catches.
"""

from __future__ import annotations

import json

import pytest

import electrum as electrum_module
from electrum import ElectrumClient, ElectrumError, parse_url

URL = "tcp://fulcrum:50001"
HANDSHAKE = b'{"id":1,"result":["Fulcrum 1.11.2","1.4"]}\n'


class _Stream:
    def __init__(self, responses: list[bytes], read_error: Exception | None = None) -> None:
        self.responses = list(responses)
        self.read_error = read_error
        self.writes: list[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> int:
        self.writes.append(data)
        return len(data)

    def flush(self) -> None:
        pass

    def readline(self, limit: int = -1) -> bytes:
        if self.read_error is not None:
            raise self.read_error
        if not self.responses:
            return b""
        line = self.responses.pop(0)
        if limit >= 0 and len(line) > limit:
            # Emulate a buffered reader: hand back the prefix and keep the
            # rest for a next read the client will not make.
            self.responses.insert(0, line[limit:])
            return line[:limit]
        return line

    def close(self) -> None:
        self.closed = True


class _Sock:
    def __init__(self, stream: _Stream) -> None:
        self.stream = stream
        self.closed = False

    def makefile(self, _mode: str) -> _Stream:
        return self.stream

    def settimeout(self, _timeout: float) -> None:
        pass

    def close(self) -> None:
        self.closed = True


def _install(
    monkeypatch,
    responses: list[bytes],
    *,
    connect_error: Exception | None = None,
    read_error: Exception | None = None,
):
    stream = _Stream(responses, read_error=read_error)

    def fake_create_connection(address, timeout=None):
        if connect_error is not None:
            raise connect_error
        return _Sock(stream)

    monkeypatch.setattr(electrum_module.socket, "create_connection", fake_create_connection)
    return stream


def test_a_call_sends_the_handshake_first(monkeypatch) -> None:
    stream = _install(monkeypatch, [HANDSHAKE, b'{"id":1,"result":{"confirmed":7}}\n'])
    assert ElectrumClient(URL).call("blockchain.scripthash.get_balance", ["ab"]) == {
        "confirmed": 7
    }
    handshake = json.loads(stream.writes[0])
    assert handshake["method"] == "server.version"
    assert handshake["params"][0].startswith("fulcrum-mcp/")
    request = json.loads(stream.writes[1])
    assert request["method"] == "blockchain.scripthash.get_balance"
    assert request["params"] == ["ab"]


def test_an_error_object_keeps_its_code(monkeypatch) -> None:
    _install(
        monkeypatch,
        [HANDSHAKE, b'{"id":1,"error":{"code":2,"message":"daemon error: no such tx"}}\n'],
    )
    with pytest.raises(ElectrumError) as excinfo:
        ElectrumClient(URL).call("blockchain.transaction.get", ["ab"])
    assert excinfo.value.code == 2
    assert excinfo.value.message == "daemon error: no such tx"


def test_a_closed_connection_is_named(monkeypatch) -> None:
    _install(monkeypatch, [HANDSHAKE])
    with pytest.raises(ElectrumError) as excinfo:
        ElectrumClient(URL).call("server.features")
    assert "closed the connection" in excinfo.value.message


def test_a_non_json_line_is_named(monkeypatch) -> None:
    _install(monkeypatch, [HANDSHAKE, b"<html>nope</html>\n"])
    with pytest.raises(ElectrumError) as excinfo:
        ElectrumClient(URL).call("server.features")
    assert "non-JSON" in excinfo.value.message


def test_a_refused_connection_is_named(monkeypatch) -> None:
    _install(monkeypatch, [], connect_error=ConnectionRefusedError(111, "refused"))
    with pytest.raises(ElectrumError) as excinfo:
        ElectrumClient(URL).call("server.features")
    assert "cannot reach Fulcrum at tcp://fulcrum:50001" in excinfo.value.message


def test_a_read_timeout_is_named(monkeypatch) -> None:
    _install(monkeypatch, [], read_error=TimeoutError("timed out"))
    with pytest.raises(ElectrumError) as excinfo:
        ElectrumClient(URL, timeout=2.5).call("server.features")
    assert "did not answer within 2.5s" in excinfo.value.message


def test_url_parsing_covers_both_schemes() -> None:
    assert parse_url("tcp://fulcrum:50001")[2:] == (50001, False)
    assert parse_url("ssl://fulcrum:50002")[2:] == (50002, True)
    with pytest.raises(ValueError):
        parse_url("http://fulcrum:50001")
    with pytest.raises(ValueError):
        parse_url("tcp://")


def test_url_parsing_rejects_parts_the_socket_would_drop() -> None:
    for url in (
        "tcp://fulcrum:50001/electrum",
        "tcp://fulcrum:50001?timeout=1",
        "tcp://fulcrum:50001#fragment",
        "ssl://user:pass@fulcrum:50002",
    ):
        with pytest.raises(ValueError):
            parse_url(url)


def test_a_line_over_the_cap_is_refused(monkeypatch) -> None:
    # 64 is above the handshake line and below the oversized answer, so the
    # cap is exercised on the answer rather than on server.version.
    monkeypatch.setattr(electrum_module, "MAX_RESPONSE_BYTES", 64)
    _install(monkeypatch, [HANDSHAKE, b'{"result":"' + b"x" * 64 + b'"}\n'])
    with pytest.raises(ElectrumError) as excinfo:
        ElectrumClient(URL).call("server.features")
    assert "exceeded 64 bytes" in excinfo.value.message


def test_a_line_without_a_newline_is_refused(monkeypatch) -> None:
    _install(monkeypatch, [HANDSHAKE, b'{"id":1,"result":1}'])
    with pytest.raises(ElectrumError) as excinfo:
        ElectrumClient(URL).call("server.features")
    assert "not newline-terminated" in excinfo.value.message


def test_a_reset_connect_is_retried_once(monkeypatch) -> None:
    stream = _Stream([HANDSHAKE, b'{"id":1,"result":1}\n'])
    attempts: list[int] = []

    def fake_create_connection(address, timeout=None):
        attempts.append(1)
        if len(attempts) == 1:
            raise ConnectionResetError(104, "reset")
        return _Sock(stream)

    monkeypatch.setattr(electrum_module.socket, "create_connection", fake_create_connection)
    monkeypatch.setattr(electrum_module.time, "sleep", lambda _seconds: None)
    assert ElectrumClient(URL).call("server.features") == 1
    assert len(attempts) == 2


def test_a_refused_connect_is_not_retried(monkeypatch) -> None:
    attempts: list[int] = []

    def fake_create_connection(address, timeout=None):
        attempts.append(1)
        raise ConnectionRefusedError(111, "refused")

    monkeypatch.setattr(electrum_module.socket, "create_connection", fake_create_connection)
    with pytest.raises(ElectrumError):
        ElectrumClient(URL).call("server.features")
    assert len(attempts) == 1


def test_the_client_name_tracks_the_manifest_version() -> None:
    import tomllib
    from pathlib import Path

    version = tomllib.loads(Path("pyproject.toml").read_text())["project"]["version"]
    assert electrum_module.__version__ == version
    assert f"fulcrum-mcp/{version}" == electrum_module.CLIENT_NAME


def test_the_per_call_timeout_overrides_the_default(monkeypatch) -> None:
    seen: list[float | None] = []

    def fake_create_connection(address, timeout=None):
        seen.append(timeout)
        return _Sock(_Stream([HANDSHAKE, b'{"id":1,"result":1}\n']))

    monkeypatch.setattr(electrum_module.socket, "create_connection", fake_create_connection)
    ElectrumClient(URL, timeout=30).call("server.features", timeout=2)
    assert seen == [2]
