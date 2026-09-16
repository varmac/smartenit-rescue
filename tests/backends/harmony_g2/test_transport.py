from __future__ import annotations

import hashlib
import http.server
import os
import socket
import ssl
import subprocess
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from smartenit_rescue.backends.harmony_g2.transport import (
    G2ResponseError,
    G2TransportError,
    PinnedG2Transport,
)
from smartenit_rescue.errors import ValidationError

JSON_TYPE = "application/json"
LOOPBACK = "127.0.0.1"


@dataclass(slots=True)
class ResponseSpec:
    status: int = 200
    content_type: str | None = JSON_TYPE
    body: bytes = b'{"ok":true}'
    delay_seconds: float = 0.0
    send_response: bool = True


@dataclass(slots=True)
class RecordedRequest:
    method: str
    path: str
    header: str | None
    body: bytes


@dataclass(slots=True)
class FixtureState:
    response: ResponseSpec = field(default_factory=ResponseSpec)
    requests: list[RecordedRequest] = field(default_factory=list)


@dataclass(slots=True)
class TlsFixture:
    server: http.server.ThreadingHTTPServer
    thread: threading.Thread
    state: FixtureState
    certificate_pin: str

    @property
    def port(self) -> int:
        return int(self.server.server_port)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


@pytest.fixture(scope="module")
def certificate_files(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    directory = tmp_path_factory.mktemp("g2-tls")
    certificate = directory / "certificate.pem"
    key_file = directory / "test-key.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
            "-keyout",
            os.fspath(key_file),
            "-out",
            os.fspath(certificate),
        ],
        check=True,
        capture_output=True,
    )
    return certificate, key_file


@pytest.fixture
def tls_fixture(certificate_files: tuple[Path, Path]) -> Iterator[TlsFixture]:
    certificate, key_file = certificate_files
    state = FixtureState()

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            self._handle()

        def do_POST(self) -> None:
            self._handle()

        def _handle(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            request_body = self.rfile.read(length)
            header = self.headers.get("Authorization")
            state.requests.append(
                RecordedRequest(self.command, self.path, header, request_body)
            )
            response = state.response
            if response.delay_seconds:
                time.sleep(response.delay_seconds)
            if not response.send_response:
                return
            self.send_response(response.status)
            if response.content_type is not None:
                self.send_header("Content-Type", response.content_type)
            self.send_header("Content-Length", str(len(response.body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(response.body)

        def log_message(self, _format: str, *args: object) -> None:
            del args

    server = http.server.ThreadingHTTPServer((LOOPBACK, 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate, key_file)
    server.socket = context.wrap_socket(server.socket, True)
    thread = threading.Thread(
        target=lambda: server.serve_forever(poll_interval=0.01), daemon=True
    )
    thread.start()
    der = ssl.PEM_cert_to_DER_cert(certificate.read_text(encoding="ascii"))
    fixture = TlsFixture(
        server,
        thread,
        state,
        hashlib.sha256(der).hexdigest(),
    )
    try:
        yield fixture
    finally:
        fixture.close()


def transport(
    fixture: TlsFixture,
    *,
    pin: str | None = None,
    timeout_seconds: float = 1.0,
) -> PinnedG2Transport:
    return PinnedG2Transport(
        LOOPBACK,
        pin or fixture.certificate_pin,
        port=fixture.port,
        timeout_seconds=timeout_seconds,
    )


def test_constructor_accepts_plain_or_colon_separated_pin(
    tls_fixture: TlsFixture,
) -> None:
    colonized = ":".join(
        tls_fixture.certificate_pin[index : index + 2]
        for index in range(0, len(tls_fixture.certificate_pin), 2)
    )

    response = transport(tls_fixture, pin=colonized).request("GET", "/v2/ping")

    assert response == {"ok": True}
    representation = repr(transport(tls_fixture, pin=colonized))
    assert tls_fixture.certificate_pin not in representation
    assert LOOPBACK not in representation


@pytest.mark.parametrize(
    "host",
    [
        "gateway.local",
        "203.0.113.8",
        "169.254.1.1",
        "224.0.0.1",
        "0.0.0.0",
        "::1",
    ],
)
def test_constructor_rejects_non_lan_host(host: str) -> None:
    with pytest.raises(ValidationError, match="LAN IPv4"):
        PinnedG2Transport(host, "0" * 64)


@pytest.mark.parametrize(
    "pin",
    ["", "a" * 63, "g" * 64, "aa:" * 32, "<certificate-pin>"],
)
def test_constructor_rejects_invalid_pin(pin: str) -> None:
    with pytest.raises(ValidationError, match="certificate pin"):
        PinnedG2Transport(LOOPBACK, pin)


@pytest.mark.parametrize("port", [0, -1, 65536, True, 1.5])
def test_constructor_rejects_invalid_port(port: object) -> None:
    with pytest.raises(ValidationError, match="port"):
        PinnedG2Transport(LOOPBACK, "0" * 64, port=port)  # type: ignore[arg-type]


@pytest.mark.parametrize("timeout_seconds", [0, -1, True, float("inf"), float("nan")])
def test_constructor_rejects_invalid_timeout(timeout_seconds: object) -> None:
    with pytest.raises(ValidationError, match="timeout"):
        PinnedG2Transport(
            LOOPBACK,
            "0" * 64,
            timeout_seconds=timeout_seconds,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/v2/ping", None),
        ("POST", "/v2/oauth2/token", {}),
        ("GET", "/v2/devices?limit=100&page=1", None),
        ("GET", "/v2/devices?limit=100&page=99", None),
        ("GET", "/v2/devices?limit=100&page=100", None),
        (
            "POST",
            "/v2/devices/device_1/comps/component-1/procs/OnOff/methods/On",
            {},
        ),
        (
            "POST",
            "/v2/devices/device_1/comps/component-1/procs/OnOff/methods/Off",
            {},
        ),
    ],
)
def test_request_allows_only_proven_routes(
    tls_fixture: TlsFixture,
    method: str,
    path: str,
    body: dict[str, object] | None,
) -> None:
    result = transport(tls_fixture).request(method, path, body=body)

    assert result == {"ok": True}


@pytest.mark.parametrize("method", ["", "get", "PUT", "DELETE"])
def test_request_rejects_unknown_method_before_connecting(
    tls_fixture: TlsFixture, method: str
) -> None:
    before = len(tls_fixture.state.requests)

    with pytest.raises(ValidationError, match="method"):
        transport(tls_fixture).request(method, "/v2/ping")

    assert len(tls_fixture.state.requests) == before


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/v2/devices"),
        ("GET", "/v2/devices?limit=100&page=0"),
        ("GET", "/v2/devices?limit=100&page=101"),
        ("GET", "/v2/devices?limit=99&page=1"),
        ("GET", "/v2/ping#fragment"),
        ("GET", "/v2/ping\r\nX-Test: unsafe"),
        ("POST", "/v2/devices/a/comps/b/procs/OnOff/methods/Toggle"),
        ("POST", "/v2/devices/a/comps/../procs/OnOff/methods/On"),
        ("POST", "/v2/anything"),
    ],
)
def test_request_rejects_unproven_path_before_connecting(
    tls_fixture: TlsFixture, method: str, path: str
) -> None:
    before = len(tls_fixture.state.requests)

    with pytest.raises(ValidationError, match="path"):
        transport(tls_fixture).request(method, path)

    assert len(tls_fixture.state.requests) == before


def test_pin_mismatch_sends_no_http_request_or_credential(
    tls_fixture: TlsFixture,
) -> None:
    before = len(tls_fixture.state.requests)

    with pytest.raises(G2TransportError) as captured:
        transport(tls_fixture, pin="0" * 64).request(
            "GET", "/v2/ping", access_token="<local-access>"
        )

    assert captured.value.request_started is False
    assert captured.value.status is None
    assert len(tls_fixture.state.requests) == before
    assert "<local-access>" not in str(captured.value)
    assert LOOPBACK not in str(captured.value)


def test_matching_pin_sends_authorization_only_after_tls_enrollment(
    tls_fixture: TlsFixture,
) -> None:
    transport(tls_fixture).request("GET", "/v2/ping", access_token="<local-access>")

    recorded = tls_fixture.state.requests[-1]
    assert recorded.header == "Bearer <local-access>"


@pytest.mark.parametrize(
    ("content_type", "body"),
    [
        (None, b"{}"),
        ("text/plain", b"{}"),
        ("application/jsonp", b"{}"),
        ("application/json; charset=latin-1", b"{}"),
        (JSON_TYPE, b"\xff"),
        (JSON_TYPE, b"{"),
        (JSON_TYPE, b"[]"),
        (JSON_TYPE, b"NaN"),
        (JSON_TYPE, b'{"value":NaN}'),
    ],
)
def test_response_rejects_invalid_content_or_json(
    tls_fixture: TlsFixture, content_type: str | None, body: bytes
) -> None:
    tls_fixture.state.response = ResponseSpec(content_type=content_type, body=body)

    with pytest.raises(G2ResponseError) as captured:
        transport(tls_fixture).request("GET", "/v2/ping")

    assert captured.value.request_started is True
    assert captured.value.status == 200
    decoded = body.decode("utf-8", errors="ignore")
    if decoded:
        assert decoded not in str(captured.value)


def test_response_accepts_json_with_utf8_charset(tls_fixture: TlsFixture) -> None:
    tls_fixture.state.response = ResponseSpec(
        content_type="application/json; charset=utf-8",
        body=b'{"ready":true}',
    )

    assert transport(tls_fixture).request("GET", "/v2/ping") == {"ready": True}


def test_response_rejects_oversized_body(tls_fixture: TlsFixture) -> None:
    tls_fixture.state.response = ResponseSpec(body=b"x" * 2_000_001)

    with pytest.raises(G2ResponseError, match="too large") as captured:
        transport(tls_fixture).request("GET", "/v2/ping")

    assert captured.value.request_started is True
    assert captured.value.status == 200


@pytest.mark.parametrize("status", [301, 400, 401, 403, 500])
def test_http_error_preserves_status_without_exposing_body(
    tls_fixture: TlsFixture, status: int
) -> None:
    tls_fixture.state.response = ResponseSpec(
        status=status,
        body=b'{"message":"do-not-echo"}',
    )

    with pytest.raises(G2ResponseError) as captured:
        transport(tls_fixture).request("GET", "/v2/ping")

    assert captured.value.status == status
    assert captured.value.request_started is True
    assert "do-not-echo" not in str(captured.value)


def test_socket_timeout_after_request_is_marked_ambiguous(
    tls_fixture: TlsFixture,
) -> None:
    tls_fixture.state.response = ResponseSpec(
        delay_seconds=0.2,
        send_response=False,
    )

    with pytest.raises(G2TransportError) as captured:
        transport(tls_fixture, timeout_seconds=0.05).request("GET", "/v2/ping")

    assert captured.value.request_started is True
    assert captured.value.status is None
    assert "timeout" in str(captured.value).lower()


def test_connect_failure_is_marked_pre_submission(tls_fixture: TlsFixture) -> None:
    probe = socket.socket()
    probe.bind((LOOPBACK, 0))
    unused_port = int(probe.getsockname()[1])
    probe.close()
    candidate = PinnedG2Transport(
        LOOPBACK,
        tls_fixture.certificate_pin,
        port=unused_port,
        timeout_seconds=0.1,
    )

    with pytest.raises(G2TransportError) as captured:
        candidate.request("GET", "/v2/ping")

    assert captured.value.request_started is False
    assert captured.value.status is None
