from __future__ import annotations

import importlib
import signal
import socket
from collections import deque
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from ipaddress import IPv4Address
from pathlib import Path
from types import FrameType
from typing import Protocol, cast

import pytest

from smartenit_rescue.backends import Backend, SimulatorBackend
from smartenit_rescue.config import (
    AppConfig,
    BackendSettings,
    HarmonyG2DeviceSettings,
    HarmonyG2Settings,
    MqttCredentials,
    MqttSettings,
    RuntimeSettings,
    SimulatorBackendSettings,
    SimulatorDeviceSettings,
)
from smartenit_rescue.errors import ValidationError
from smartenit_rescue.models import DeviceId
from smartenit_rescue.mqtt import (
    Publish,
    TopicLayout,
    Transport,
    TransportError,
    TransportEvent,
)
from smartenit_rescue.profiles import DeviceProfile, iter_builtin_profiles


class ForegroundRuntime(Protocol):
    def run(self, stop_requested: Callable[[], bool]) -> None: ...

    def close(self) -> None: ...


class IdleTransport:
    def __init__(self) -> None:
        self.events: deque[TransportEvent] = deque()

    def connect(self) -> None:
        pass

    def next_event(self, timeout: float | None = None) -> TransportEvent | None:
        del timeout
        return self.events.popleft() if self.events else None

    def subscribe(self, topic: str, qos: int) -> None:
        pass

    def publish(self, publication: Publish) -> None:
        pass

    def disconnect(self) -> None:
        pass


class RecordingRuntime:
    def __init__(
        self,
        *,
        signal_to_request: signal.Signals | None = None,
        run_error: Exception | None = None,
    ) -> None:
        self.signal_to_request = signal_to_request
        self.run_error = run_error
        self.stop_states: list[bool] = []
        self.close_calls = 0
        self.installed_handlers: dict[int, object] | None = None

    def run(self, stop_requested: Callable[[], bool]) -> None:
        self.stop_states.append(stop_requested())
        if self.signal_to_request is not None:
            assert self.installed_handlers is not None
            handler = self.installed_handlers[int(self.signal_to_request)]
            assert callable(handler)
            handler(int(self.signal_to_request), cast(FrameType | None, None))
            self.stop_states.append(stop_requested())
        if self.run_error is not None:
            raise self.run_error

    def close(self) -> None:
        self.close_calls += 1


def app_config() -> AppConfig:
    return AppConfig(
        runtime=RuntimeSettings(
            client_id="smartenit-rescue",
            topic_prefix="rescue/v1",
            discovery_prefix="homeassistant",
            home_assistant_status_topic="homeassistant/status",
        ),
        mqtt=MqttSettings("localhost", 1883, 60, False, None, None),
        backend=SimulatorBackendSettings(
            type="simulator",
            devices=(
                SimulatorDeviceSettings(
                    device_id=DeviceId.parse("0200000000000001"),
                    name="Synthetic Load",
                    profile_id="smartenit.4040c",
                    initial_on=False,
                ),
            ),
        ),
    )


def g2_settings(tmp_path: Path) -> HarmonyG2Settings:
    return HarmonyG2Settings(
        type="harmony_g2",
        host=IPv4Address(0x0A000001),
        certificate_sha256_file=tmp_path / "g2-pin",
        session_file=tmp_path / "g2-session.json",
        devices=(
            HarmonyG2DeviceSettings(
                device_id=DeviceId.parse("0200000000000001"),
                name="Example Load",
                profile_id="smartenit.4040c",
                component_id="1",
            ),
        ),
    )


def fixed_clock() -> datetime:
    return datetime(2026, 9, 15, 12, tzinfo=UTC)


def run_main(argv: Sequence[str]) -> int:
    cli = importlib.import_module("smartenit_rescue.cli")
    result: object = cli.main(argv)
    assert isinstance(result, int)
    return result


def test_parser_accepts_explicit_run_config_path() -> None:
    cli = importlib.import_module("smartenit_rescue.cli")

    args = cli._parser().parse_args(["run", "--config", "bridge.toml"])

    assert args.command == "run"
    assert args.config == Path("bridge.toml")


def test_runtime_construction_wires_config_credentials_backend_topics_and_lwt() -> None:
    cli = importlib.import_module("smartenit_rescue.cli")
    config = app_config()
    config_path = Path("/operator/bridge.toml")
    profiles = iter_builtin_profiles()
    calls: dict[str, object] = {}
    profile_loads = 0
    transport = IdleTransport()
    runtime = RecordingRuntime()

    def load_config(path: Path) -> AppConfig:
        calls["config_path"] = path
        return config

    def load_credentials(settings: MqttSettings) -> MqttCredentials | None:
        calls["credential_settings"] = settings
        return None

    def load_profiles() -> tuple[DeviceProfile, ...]:
        nonlocal profile_loads
        profile_loads += 1
        return profiles

    def build_backend(
        settings: BackendSettings,
        loaded_profiles: tuple[DeviceProfile, ...],
        clock: Callable[[], datetime],
    ) -> Backend:
        calls["backend_args"] = (settings, loaded_profiles, clock)
        assert isinstance(settings, SimulatorBackendSettings)
        return SimulatorBackend(settings.devices, loaded_profiles, clock)

    def make_topics(
        topic_prefix: str,
        discovery_prefix: str,
        home_assistant_status_topic: str,
    ) -> TopicLayout:
        calls["topic_args"] = (
            topic_prefix,
            discovery_prefix,
            home_assistant_status_topic,
        )
        return TopicLayout(topic_prefix, discovery_prefix, home_assistant_status_topic)

    def make_transport(
        settings: MqttSettings,
        credentials: MqttCredentials | None,
        will: Publish,
        client_id: str,
    ) -> Transport:
        calls["transport_args"] = (settings, credentials, will, client_id)
        return transport

    def make_runtime(
        backend: Backend,
        selected_transport: Transport,
        topics: TopicLayout,
        home_assistant_status_topic: str,
        request_id_factory: Callable[[], str],
        runtime_clock: Callable[[], datetime],
    ) -> ForegroundRuntime:
        calls["runtime_args"] = (
            backend,
            selected_transport,
            topics,
            home_assistant_status_topic,
            request_id_factory,
            runtime_clock,
        )
        return runtime

    result = cli._build_runtime(
        config_path,
        config_loader=load_config,
        credentials_loader=load_credentials,
        profiles_loader=load_profiles,
        backend_builder=build_backend,
        topics_factory=make_topics,
        transport_factory=make_transport,
        runtime_factory=make_runtime,
        clock=fixed_clock,
    )

    assert result is runtime
    assert calls["config_path"] == config_path
    assert calls["credential_settings"] == config.mqtt
    assert profile_loads == 1
    assert calls["backend_args"] == (
        config.backend,
        profiles,
        fixed_clock,
    )
    assert calls["topic_args"] == (
        "rescue/v1",
        "homeassistant",
        "homeassistant/status",
    )
    settings, credentials, will, client_id = cast(
        tuple[MqttSettings, MqttCredentials | None, Publish, str],
        calls["transport_args"],
    )
    assert settings == config.mqtt
    assert credentials is None
    assert will == Publish(
        "rescue/v1/bridge/availability", b"offline", qos=1, retain=True
    )
    assert client_id == "smartenit-rescue"
    (
        _backend,
        selected_transport,
        topics,
        status_topic,
        request_id_factory,
        runtime_clock,
    ) = cast(
        tuple[
            Backend,
            Transport,
            TopicLayout,
            str,
            Callable[[], str],
            Callable[[], datetime],
        ],
        calls["runtime_args"],
    )
    assert selected_transport is transport
    assert topics == TopicLayout("rescue/v1", "homeassistant", "homeassistant/status")
    assert status_topic == "homeassistant/status"
    assert request_id_factory() != request_id_factory()
    assert runtime_clock is fixed_clock


def test_build_backend_constructs_simulator_from_tagged_settings() -> None:
    cli = importlib.import_module("smartenit_rescue.cli")
    config = app_config()

    backend = cli._build_backend(
        config.backend,
        iter_builtin_profiles(),
        fixed_clock,
    )

    assert isinstance(backend, SimulatorBackend)
    assert backend.devices()[0].name == "Synthetic Load"


def test_build_backend_validates_pin_before_constructing_g2_dependencies(
    tmp_path: Path,
) -> None:
    cli = importlib.import_module("smartenit_rescue.cli")
    selected = g2_settings(tmp_path)
    colonized = ":".join("ab" for _ in range(32))
    selected.certificate_sha256_file.write_text(colonized + "\n", encoding="ascii")
    calls: list[tuple[object, ...]] = []
    sentinel_store = object()
    sentinel_transport = object()
    simulator = app_config().backend
    assert isinstance(simulator, SimulatorBackendSettings)
    sentinel_backend = SimulatorBackend(
        simulator.devices, iter_builtin_profiles(), fixed_clock
    )

    def make_store(path: Path) -> object:
        calls.append(("store", path))
        return sentinel_store

    def make_g2_transport(host: str, pin: str) -> object:
        calls.append(("transport", host, pin))
        return sentinel_transport

    def open_backend(
        devices: tuple[HarmonyG2DeviceSettings, ...],
        profiles: tuple[DeviceProfile, ...],
        store: object,
        transport: object,
        clock: Callable[[], datetime],
    ) -> Backend:
        calls.append(("open", devices, profiles, store, transport, clock))
        return sentinel_backend

    result = cli._build_backend(
        selected,
        iter_builtin_profiles(),
        fixed_clock,
        session_store_factory=make_store,
        g2_transport_factory=make_g2_transport,
        g2_backend_opener=open_backend,
    )

    assert result is sentinel_backend
    assert calls[0] == ("store", selected.session_file)
    assert calls[1] == (
        "transport",
        str(selected.host),
        "ab" * 32,
    )
    assert calls[2][0] == "open"


@pytest.mark.parametrize("unsafe_session", [False, True])
def test_cli_reports_missing_or_unsafe_g2_session_without_private_values(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    unsafe_session: bool,
) -> None:
    cli = importlib.import_module("smartenit_rescue.cli")
    tmp_path.chmod(0o700)
    selected = g2_settings(tmp_path)
    selected.certificate_sha256_file.write_text("ab" * 32, encoding="ascii")
    if unsafe_session:
        selected.session_file.write_text("{}", encoding="ascii")
        selected.session_file.chmod(0o644)

    def build(_path: Path) -> ForegroundRuntime:
        cli._build_backend(selected, iter_builtin_profiles(), fixed_clock)
        raise AssertionError("backend construction unexpectedly succeeded")

    assert cli.main(["run", "--config", "bridge.toml"], runtime_builder=build) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    expected = (
        "Harmony G2 backend initialization failed"
        if unsafe_session
        else "externally provisioned local session is required"
    )
    assert captured.err.startswith(f"error: {expected}")
    for marker in (
        str(selected.host),
        str(selected.session_file),
        str(selected.certificate_sha256_file),
        selected.devices[0].component_id,
        "ab" * 32,
    ):
        assert marker not in captured.err


def test_cli_reports_malformed_pin_without_echoing_file_or_contents(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli = importlib.import_module("smartenit_rescue.cli")
    selected = g2_settings(tmp_path)
    marker = "do-not-echo-pin-contents"
    selected.certificate_sha256_file.write_text(marker, encoding="ascii")

    def build(_path: Path) -> ForegroundRuntime:
        cli._build_backend(selected, iter_builtin_profiles(), fixed_clock)
        raise AssertionError("backend construction unexpectedly succeeded")

    assert cli.main(["run", "--config", "bridge.toml"], runtime_builder=build) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "certificate pin file is invalid" in captured.err
    assert marker not in captured.err
    assert str(selected.certificate_sha256_file) not in captured.err


def test_cli_redacts_g2_startup_failure_details(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli = importlib.import_module("smartenit_rescue.cli")
    selected = g2_settings(tmp_path)
    selected.certificate_sha256_file.write_text("ab" * 32, encoding="ascii")
    markers = (
        "<local-access>",
        str(selected.host),
        selected.devices[0].component_id,
        str(selected.session_file),
    )

    def fail_open(*_args: object) -> Backend:
        raise ValidationError(" ".join(markers))

    def build(_path: Path) -> ForegroundRuntime:
        cli._build_backend(
            selected,
            iter_builtin_profiles(),
            fixed_clock,
            session_store_factory=lambda _path: object(),
            g2_transport_factory=lambda _host, _pin: object(),
            g2_backend_opener=fail_open,
        )
        raise AssertionError("backend construction unexpectedly succeeded")

    assert cli.main(["run", "--config", "bridge.toml"], runtime_builder=build) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "error: Harmony G2 backend initialization failed\n"
    assert all(marker not in captured.err for marker in markers)


def test_runtime_construction_closes_backend_when_mqtt_construction_fails() -> None:
    cli = importlib.import_module("smartenit_rescue.cli")
    config = app_config()
    profiles = iter_builtin_profiles()

    class TrackingBackend(SimulatorBackend):
        def __init__(self) -> None:
            assert isinstance(config.backend, SimulatorBackendSettings)
            super().__init__(config.backend.devices, profiles, fixed_clock)
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1
            super().close()

    backend = TrackingBackend()

    def fail_transport(*_args: object) -> Transport:
        raise ValidationError("MQTT construction failed")

    with pytest.raises(ValidationError, match="MQTT construction failed"):
        cli._build_runtime(
            Path("bridge.toml"),
            config_loader=lambda _path: config,
            credentials_loader=lambda _settings: None,
            profiles_loader=lambda: profiles,
            backend_builder=lambda _settings, _profiles, _clock: backend,
            transport_factory=fail_transport,
            clock=fixed_clock,
        )

    assert backend.close_calls == 1


@pytest.mark.parametrize("requested_signal", [signal.SIGINT, signal.SIGTERM])
def test_run_signal_requests_stop_restores_handlers_and_closes_once(
    requested_signal: signal.Signals,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = importlib.import_module("smartenit_rescue.cli")
    runtime = RecordingRuntime(signal_to_request=requested_signal)
    current: dict[int, object] = {
        int(signal.SIGINT): signal.SIG_DFL,
        int(signal.SIGTERM): signal.SIG_IGN,
    }
    calls: list[tuple[int, object]] = []

    def record_signal(signal_number: int, handler: object) -> object:
        previous = current[signal_number]
        current[signal_number] = handler
        calls.append((signal_number, handler))
        return previous

    monkeypatch.setattr(cli.signal, "signal", record_signal)
    runtime.installed_handlers = current

    result = cli.main(
        ["run", "--config", "bridge.toml"],
        runtime_builder=lambda _path: runtime,
    )

    assert result == 0
    assert runtime.stop_states == [False, True]
    assert runtime.close_calls == 1
    assert calls[-2:] == [
        (int(signal.SIGINT), signal.SIG_DFL),
        (int(signal.SIGTERM), signal.SIG_IGN),
    ]


@pytest.mark.parametrize(
    "error",
    [
        OSError("configuration unavailable"),
        ValidationError("invalid\nconfiguration"),
    ],
)
def test_run_reports_bounded_construction_errors_without_traceback(
    error: Exception,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = importlib.import_module("smartenit_rescue.cli")

    def fail_build(_path: Path) -> ForegroundRuntime:
        raise error

    assert cli.main(["run", "--config", "bridge.toml"], runtime_builder=fail_build) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.count("\n") == 1
    assert "Traceback" not in captured.err
    assert len(captured.err) <= 240


def test_run_reports_connection_failure_and_closes_once(
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = importlib.import_module("smartenit_rescue.cli")
    transport_error = TransportError("MQTT connection refused: Not authorized")
    runtime = RecordingRuntime(run_error=transport_error)

    assert (
        cli.main(
            ["run", "--config", "bridge.toml"],
            runtime_builder=lambda _path: runtime,
        )
        == 2
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "error: MQTT connection refused: Not authorized\n"
    assert runtime.close_calls == 1


def test_run_leaves_unexpected_programmer_failures_visible() -> None:
    cli = importlib.import_module("smartenit_rescue.cli")
    runtime = RecordingRuntime(run_error=RuntimeError("programmer failure"))

    with pytest.raises(RuntimeError, match="programmer failure"):
        cli.main(
            ["run", "--config", "bridge.toml"],
            runtime_builder=lambda _path: runtime,
        )

    assert runtime.close_calls == 1


def test_profiles_list_prints_deterministic_summary(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def reject_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("profile listing attempted a network connection")

    monkeypatch.setattr(socket, "create_connection", reject_network)
    monkeypatch.setattr(socket.socket, "connect", reject_network)

    assert run_main(["profiles", "list"]) == 0
    captured = capsys.readouterr()
    assert captured.out == (
        "smartenit.4040c\tCompacta International, Ltd.\tZBMLCSR\tsynthetic\n"
    )
    assert captured.err == ""


def test_profiles_validate_prints_only_valid_profile_id(
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = Path(__file__).parent / "profiles" / "fixtures" / "minimal-valid.json"

    assert run_main(["profiles", "validate", str(path)]) == 0
    captured = capsys.readouterr()
    assert captured.out == "valid\texample.synthetic-switch\n"
    assert captured.err == ""


def test_profiles_validate_reports_bounded_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    payload_marker = "do-not-echo-profile-payload"
    path = tmp_path / "invalid.json"
    path.write_text('{"profile": "' + payload_marker * 10_000, encoding="utf-8")

    assert run_main(["profiles", "validate", str(path)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err
    assert payload_marker not in captured.err
    assert len(captured.err) <= 240


def test_profiles_validate_reports_bounded_failure_for_non_utf8_input(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    payload_marker = "do-not-echo-binary-profile-payload"
    path = tmp_path / "binary-profile.json"
    path.write_bytes(b'{"profile":"' + payload_marker.encode() + b'\xff"}')

    assert run_main(["profiles", "validate", str(path)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err
    assert "Traceback" not in captured.err
    assert payload_marker not in captured.err
    assert len(captured.err) <= 240


def test_profiles_validate_maps_json_recursion_to_bounded_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    payload_marker = "do-not-echo-recursive-profile-payload"
    recursion_depth = 1800
    path = tmp_path / "nested.json"
    path.write_text(
        ("[" * recursion_depth) + f'"{payload_marker}"' + ("]" * recursion_depth),
        encoding="utf-8",
    )

    assert run_main(["profiles", "validate", str(path)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err
    assert "Traceback" not in captured.err
    assert payload_marker not in captured.err
    assert len(captured.err) <= 240


def test_profiles_validate_bounds_oversized_normalization_integer(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    oversized_integer = "9" * 400
    fixture = Path(__file__).parent / "profiles" / "fixtures" / "minimal-valid.json"
    profile_text = fixture.read_text(encoding="utf-8").replace(
        '"normalization": null',
        '"normalization": {"multiplier": ' + oversized_integer + ', "divisor": 1}',
        1,
    )
    path = tmp_path / "oversized-profile.json"
    path.write_text(profile_text, encoding="utf-8")

    assert run_main(["profiles", "validate", str(path)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Traceback" not in captured.err
    assert oversized_integer not in captured.err
    assert len(captured.err) <= 240


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["profiles"],
        ["profiles", "validate"],
        ["profiles", "unknown"],
        ["devices", "list"],
    ],
)
def test_usage_errors_preserve_argparse_system_exit(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as caught:
        run_main(argv)

    assert caught.value.code == 2
