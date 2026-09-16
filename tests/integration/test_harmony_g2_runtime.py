from __future__ import annotations

import hashlib
import http.server
import json
import os
import socket
import ssl
import subprocess
import threading
from collections import deque
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from ipaddress import IPv4Address
from pathlib import Path
from typing import Protocol

import pytest

from smartenit_rescue.backends import Backend, CapabilityAddress
from smartenit_rescue.backends.harmony_g2 import PinnedG2Transport, SessionStore
from smartenit_rescue.cli import _build_backend, _build_runtime
from smartenit_rescue.config import BackendSettings, MqttCredentials, MqttSettings
from smartenit_rescue.models import CapabilityId, DeviceId, EndpointId
from smartenit_rescue.mqtt import (
    Connected,
    Message,
    Publish,
    TopicLayout,
    TransportEvent,
)
from smartenit_rescue.profiles import DeviceProfile

NOW = datetime(2026, 9, 15, 20, 40, tzinfo=UTC)
LOOPBACK = "127.0.0.1"
DEVICE_ID = DeviceId.parse("0200000000000001")
OLD_ACCESS_VALUE = "<old-local-access>"
OLD_REFRESH_VALUE = "<old-local-refresh>"
NEW_ACCESS_VALUE = "<new-local-access>"
NEW_REFRESH_VALUE = "<new-local-refresh>"
ON_OFF = CapabilityAddress(DEVICE_ID, EndpointId(1), CapabilityId.ON_OFF)


class ForegroundRuntime(Protocol):
    def run(self, stop_requested: Callable[[], bool]) -> None: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class G2Request:
    method: str
    path: str
    credential: str | None
    body: Mapping[str, object] | None


@dataclass(slots=True)
class G2State:
    requests: list[G2Request] = field(default_factory=list)
    close_next_command: bool = False


@dataclass(slots=True)
class G2Fixture:
    server: http.server.ThreadingHTTPServer
    thread: threading.Thread
    state: G2State
    certificate_pin: str

    @property
    def port(self) -> int:
        return int(self.server.server_port)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class FakeMqttTransport:
    def __init__(self) -> None:
        self.events: deque[TransportEvent] = deque()
        self.publications: list[Publish] = []
        self.subscriptions: list[tuple[str, int]] = []
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.idle_seen = False

    def connect(self) -> None:
        self.connect_calls += 1
        self.idle_seen = False
        self.events.appendleft(Connected())

    def next_event(self, timeout: float | None = None) -> TransportEvent | None:
        del timeout
        if self.events:
            return self.events.popleft()
        self.idle_seen = True
        return None

    def subscribe(self, topic: str, qos: int) -> None:
        self.subscriptions.append((topic, qos))

    def publish(self, publication: Publish) -> None:
        self.publications.append(publication)

    def publish_and_wait(self, publication: Publish, timeout: float) -> None:
        del timeout
        self.publications.append(publication)

    def disconnect(self) -> None:
        self.disconnect_calls += 1


def response_payload(**values: object) -> dict[str, object]:
    return dict(values)


def inventory_payload() -> dict[str, object]:
    processor: dict[str, object] = {}
    processor["name"] = "OnOff"
    component: dict[str, object] = {}
    component["id"] = "1"
    component["processors"] = [processor]
    device: dict[str, object] = {}
    device["_id"] = "device-example-1"
    device["hwId"] = str(DEVICE_ID)
    device["components"] = [component]
    payload: dict[str, object] = {}
    payload["data"] = [device]
    return payload


@pytest.fixture(scope="module")
def certificate_files(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    directory = tmp_path_factory.mktemp("g2-runtime-tls")
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
def g2_fixture(certificate_files: tuple[Path, Path]) -> Iterator[G2Fixture]:
    certificate, key_file = certificate_files
    state = G2State()

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            self._handle()

        def do_POST(self) -> None:
            self._handle()

        def _handle(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            encoded = self.rfile.read(length)
            body: Mapping[str, object] | None = None
            if encoded:
                decoded = json.loads(encoded.decode("utf-8"))
                assert isinstance(decoded, Mapping)
                body = decoded
            state.requests.append(
                G2Request(
                    self.command,
                    self.path,
                    self.headers.get("Authorization"),
                    body,
                )
            )
            if "/methods/" in self.path and state.close_next_command:
                state.close_next_command = False
                self.close_connection = True
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            if self.path == "/v2/oauth2/token":
                payload = response_payload(expires_in=3600)
                payload["access_token"] = NEW_ACCESS_VALUE
                payload["refresh_token"] = NEW_REFRESH_VALUE
            elif self.path.startswith("/v2/devices?"):
                payload = inventory_payload()
            elif "/methods/" in self.path:
                payload = response_payload(success=True)
            else:
                self.send_error(404)
                return
            response = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(response)

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
    fixture = G2Fixture(
        server,
        thread,
        state,
        hashlib.sha256(der).hexdigest(),
    )
    try:
        yield fixture
    finally:
        fixture.close()


def write_session(path: Path) -> None:
    payload: dict[str, object] = {}
    payload["access_token"] = OLD_ACCESS_VALUE
    payload["expires_at"] = (
        (NOW + timedelta(seconds=60)).isoformat().replace("+00:00", "Z")
    )
    payload["refresh_token"] = OLD_REFRESH_VALUE
    path.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")), encoding="ascii"
    )
    path.chmod(0o600)


def write_config(directory: Path, pin_path: Path, session_path: Path) -> Path:
    config_path = directory / "runtime.toml"
    private_host = str(IPv4Address(0x0A000001))
    config_path.write_text(
        "[runtime]\n"
        'client_id = "g2-integration"\n'
        'topic_prefix = "rescue/g2-test"\n'
        'discovery_prefix = "homeassistant"\n'
        'home_assistant_status_topic = "homeassistant/status"\n'
        "\n"
        "[mqtt]\n"
        'host = "localhost"\n'
        "port = 1883\n"
        "keepalive_seconds = 10\n"
        "tls = false\n"
        "\n"
        "[backend]\n"
        'type = "harmony_g2"\n'
        f"host = {json.dumps(private_host)}\n"
        f"certificate_sha256_file = {json.dumps(str(pin_path))}\n"
        f"session_file = {json.dumps(str(session_path))}\n"
        "\n"
        "[[backend.devices]]\n"
        f'device_id = "{DEVICE_ID}"\n'
        'name = "Synthetic G2 Load"\n'
        'profile_id = "smartenit.4040c"\n'
        'component_id = "1"\n',
        encoding="utf-8",
    )
    return config_path


def build_runtime(
    tmp_path: Path, fixture: G2Fixture
) -> tuple[ForegroundRuntime, FakeMqttTransport, SessionStore, TopicLayout]:
    state_directory = tmp_path / "state"
    state_directory.mkdir(exist_ok=True)
    state_directory.chmod(0o700)
    session_path = state_directory / "g2-session.json"
    if not session_path.exists():
        write_session(session_path)
    pin_path = tmp_path / "g2-pin"
    pin_path.write_text(fixture.certificate_pin, encoding="ascii")
    config_path = write_config(tmp_path, pin_path, session_path)
    mqtt = FakeMqttTransport()

    def backend_builder(
        settings: BackendSettings,
        profiles: tuple[DeviceProfile, ...],
        clock: Callable[[], datetime],
    ) -> Backend:
        return _build_backend(
            settings,
            profiles,
            clock,
            g2_transport_factory=lambda _host, pin: PinnedG2Transport(
                LOOPBACK,
                pin,
                port=fixture.port,
                timeout_seconds=0.5,
            ),
        )

    def mqtt_factory(
        _settings: MqttSettings,
        _credentials: MqttCredentials | None,
        _will: Publish,
        _client_id: str,
    ) -> FakeMqttTransport:
        return mqtt

    runtime = _build_runtime(
        config_path,
        backend_builder=backend_builder,
        transport_factory=mqtt_factory,
        clock=lambda: NOW,
    )
    topics = TopicLayout("rescue/g2-test", "homeassistant", "homeassistant/status")
    return runtime, mqtt, SessionStore(session_path), topics


def run_until_idle(runtime: ForegroundRuntime, mqtt: FakeMqttTransport) -> None:
    runtime.run(lambda: mqtt.idle_seen and not mqtt.events)


def publications_for(mqtt: FakeMqttTransport, topic: str) -> list[Publish]:
    return [
        publication for publication in mqtt.publications if publication.topic == topic
    ]


def command_requests(fixture: G2Fixture) -> list[G2Request]:
    return [
        request for request in fixture.state.requests if "/methods/" in request.path
    ]


def test_generated_tls_runtime_renews_maps_discovers_and_commands_without_replay(
    tmp_path: Path, g2_fixture: G2Fixture
) -> None:
    runtime, mqtt, store, topics = build_runtime(tmp_path, g2_fixture)
    command_topic = topics.command(ON_OFF)
    mqtt.events.extend(
        [
            Message(command_topic, b"ON", qos=1, retain=False),
            Message(command_topic, b"OFF", qos=1, retain=False),
        ]
    )

    run_until_idle(runtime, mqtt)

    assert [request.path for request in g2_fixture.state.requests[:2]] == [
        "/v2/oauth2/token",
        "/v2/devices?limit=100&page=1",
    ]
    renewal = g2_fixture.state.requests[0]
    assert renewal.credential == f"Bearer {OLD_ACCESS_VALUE}"
    assert renewal.body is not None
    assert renewal.body["grant_type"] == "refresh_token"
    assert renewal.body["refresh_token"] == OLD_REFRESH_VALUE
    inventory = g2_fixture.state.requests[1]
    assert inventory.credential == f"Bearer {NEW_ACCESS_VALUE}"
    persisted = store.load()
    assert persisted.access_token == NEW_ACCESS_VALUE
    assert persisted.refresh_token == NEW_REFRESH_VALUE

    discovery = publications_for(mqtt, topics.discovery(DEVICE_ID))[-1]
    document = json.loads(discovery.payload)
    components = document["components"]
    assert {item["name"] for item in components.values()} == {
        "Turn on",
        "Turn off",
        "Last command",
    }
    assert {item["platform"] for item in components.values()} == {"button", "sensor"}
    assert all(
        "state_topic" not in item or item["name"] == "Last command"
        for item in components.values()
    )

    commands = command_requests(g2_fixture)
    assert [request.path.rsplit("/", maxsplit=1)[1] for request in commands] == [
        "On",
        "Off",
    ]
    diagnostics = publications_for(
        mqtt,
        topics.last_command(ON_OFF),
    )
    assert [json.loads(item.payload)["status"] for item in diagnostics] == [
        "accepted",
        "accepted",
    ]
    assert (
        publications_for(mqtt, topics.device_availability(DEVICE_ID))[-1].payload
        == b"online"
    )

    before = len(commands)
    run_until_idle(runtime, mqtt)
    assert len(command_requests(g2_fixture)) == before
    runtime.close()


def test_ambiguous_command_is_not_retried_and_restart_does_not_replay(
    tmp_path: Path, g2_fixture: G2Fixture
) -> None:
    runtime, mqtt, _store, topics = build_runtime(tmp_path, g2_fixture)
    g2_fixture.state.close_next_command = True
    mqtt.events.append(Message(topics.command(ON_OFF), b"ON", qos=1, retain=False))

    run_until_idle(runtime, mqtt)

    assert len(command_requests(g2_fixture)) == 1
    diagnostic = publications_for(mqtt, topics.last_command(ON_OFF))[-1]
    assert json.loads(diagnostic.payload)["status"] == "indeterminate"
    assert (
        publications_for(mqtt, topics.device_availability(DEVICE_ID))[-1].payload
        == b"offline"
    )
    runtime.close()

    restarted, restarted_mqtt, _restarted_store, _topics = build_runtime(
        tmp_path, g2_fixture
    )
    before = len(command_requests(g2_fixture))
    run_until_idle(restarted, restarted_mqtt)
    assert len(command_requests(g2_fixture)) == before
    restarted.close()
