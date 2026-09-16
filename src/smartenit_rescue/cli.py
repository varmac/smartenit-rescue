"""Command-line access to profiles and the foreground MQTT bridge."""

from __future__ import annotations

import argparse
import os
import re
import signal
import stat
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType
from typing import Protocol
from uuid import uuid4

from smartenit_rescue.backends import Backend, HarmonyG2Backend, SimulatorBackend
from smartenit_rescue.backends.harmony_g2 import (
    G2Transport,
    PinnedG2Transport,
    SessionStore,
)
from smartenit_rescue.config import (
    AppConfig,
    BackendSettings,
    HarmonyG2DeviceSettings,
    HarmonyG2Settings,
    MqttCredentials,
    MqttSettings,
    SimulatorBackendSettings,
    load_config,
    load_credentials,
)
from smartenit_rescue.errors import RescueError, UnsupportedError, ValidationError
from smartenit_rescue.models.capability import SupportLevel
from smartenit_rescue.mqtt import (
    PahoTransport,
    Publish,
    TopicLayout,
    Transport,
)
from smartenit_rescue.profiles import iter_builtin_profiles, load_profile
from smartenit_rescue.profiles.model import DeviceProfile
from smartenit_rescue.runtime import BridgeRuntime

_SUPPORT_ORDER = (
    SupportLevel.SYNTHETIC,
    SupportLevel.EXPERIMENTAL,
    SupportLevel.HARDWARE_VERIFIED,
)


class _ForegroundRuntime(Protocol):
    def run(self, stop_requested: Callable[[], bool]) -> None: ...

    def close(self) -> None: ...


_ConfigLoader = Callable[[Path], AppConfig]
_CredentialsLoader = Callable[[MqttSettings], MqttCredentials | None]
_ProfilesLoader = Callable[[], tuple[DeviceProfile, ...]]
_BackendBuilder = Callable[
    [
        BackendSettings,
        tuple[DeviceProfile, ...],
        Callable[[], datetime],
    ],
    Backend,
]
_TopicsFactory = Callable[[str, str, str], TopicLayout]
_TransportFactory = Callable[
    [MqttSettings, MqttCredentials | None, Publish, str], Transport
]
_RuntimeFactory = Callable[
    [
        Backend,
        Transport,
        TopicLayout,
        str,
        Callable[[], str],
        Callable[[], datetime],
    ],
    _ForegroundRuntime,
]
_RuntimeBuilder = Callable[[Path], _ForegroundRuntime]
_SessionStoreFactory = Callable[[Path], SessionStore]
_G2TransportFactory = Callable[[str, str], G2Transport]
_G2BackendOpener = Callable[
    [
        tuple[HarmonyG2DeviceSettings, ...],
        tuple[DeviceProfile, ...],
        SessionStore,
        G2Transport,
        Callable[[], datetime],
    ],
    Backend,
]

_PIN = re.compile(r"(?:[0-9a-fA-F]{64}|(?:[0-9a-fA-F]{2}:){31}[0-9a-fA-F]{2})")
_MAX_PIN_FILE_BYTES = 256


def _highest_support_level(profile: DeviceProfile) -> SupportLevel:
    rank = max(
        _SUPPORT_ORDER.index(capability.support_level)
        for endpoint in profile.endpoints.values()
        for capability in endpoint.capabilities
    )
    return _SUPPORT_ORDER[rank]


def _list_profiles() -> int:
    for profile in iter_builtin_profiles():
        print(
            profile.profile_id,
            profile.manufacturer,
            ",".join(profile.models),
            _highest_support_level(profile).value,
            sep="\t",
        )
    return 0


def _validate_profile(path: Path) -> int:
    try:
        profile = load_profile(path)
    except (OSError, ValidationError) as error:
        message = str(error)
        print(f"invalid\t{message[:200]}", file=sys.stderr)
        return 2
    print(f"valid\t{profile.profile_id}")
    return 0


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _new_request_id() -> str:
    return str(uuid4())


def _read_certificate_pin(path: Path) -> str:
    try:
        expected = path.lstat()
    except OSError as error:
        raise ValidationError("certificate pin file is required") from error
    if not stat.S_ISREG(expected.st_mode):
        raise ValidationError("certificate pin file is invalid")
    if expected.st_size > _MAX_PIN_FILE_BYTES:
        raise ValidationError("certificate pin file is invalid")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        file_descriptor = os.open(path, flags)
    except OSError as error:
        raise ValidationError("certificate pin file is invalid") from error
    try:
        opened = os.fstat(file_descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != expected.st_dev
            or opened.st_ino != expected.st_ino
        ):
            raise ValidationError("certificate pin file is invalid")
        with os.fdopen(file_descriptor, "rb", closefd=True) as pin_file:
            file_descriptor = -1
            encoded = pin_file.read(_MAX_PIN_FILE_BYTES + 1)
    except OSError as error:
        raise ValidationError("certificate pin file is invalid") from error
    finally:
        if file_descriptor >= 0:
            os.close(file_descriptor)
    if len(encoded) > _MAX_PIN_FILE_BYTES:
        raise ValidationError("certificate pin file is invalid")
    try:
        value = encoded.decode("ascii").strip()
    except UnicodeDecodeError as error:
        raise ValidationError("certificate pin file is invalid") from error
    if _PIN.fullmatch(value) is None:
        raise ValidationError("certificate pin file is invalid")
    return value.replace(":", "").lower()


def _open_harmony_backend(
    devices: tuple[HarmonyG2DeviceSettings, ...],
    profiles: tuple[DeviceProfile, ...],
    session_store: SessionStore,
    transport: G2Transport,
    clock: Callable[[], datetime],
) -> Backend:
    return HarmonyG2Backend.open(
        devices,
        profiles,
        session_store,
        transport,
        clock,
    )


def _build_backend(
    settings: BackendSettings,
    profiles: tuple[DeviceProfile, ...],
    clock: Callable[[], datetime],
    *,
    pin_reader: Callable[[Path], str] = _read_certificate_pin,
    session_store_factory: _SessionStoreFactory = SessionStore,
    g2_transport_factory: _G2TransportFactory = PinnedG2Transport,
    g2_backend_opener: _G2BackendOpener = _open_harmony_backend,
) -> Backend:
    if isinstance(settings, SimulatorBackendSettings):
        return SimulatorBackend(settings.devices, profiles, clock)
    if not isinstance(settings, HarmonyG2Settings):
        raise UnsupportedError("configured backend is not supported")
    pin = pin_reader(settings.certificate_sha256_file)
    try:
        session_store = session_store_factory(settings.session_file)
        transport = g2_transport_factory(str(settings.host), pin)
        return g2_backend_opener(
            settings.devices,
            profiles,
            session_store,
            transport,
            clock,
        )
    except ValidationError as error:
        if str(error) == "local session file is required":
            raise UnsupportedError(
                "externally provisioned local session is required"
            ) from error
        raise ValidationError("Harmony G2 backend initialization failed") from error
    except (OSError, RescueError) as error:
        raise ValidationError("Harmony G2 backend initialization failed") from error


def _build_runtime(
    config_path: Path,
    *,
    config_loader: _ConfigLoader = load_config,
    credentials_loader: _CredentialsLoader = load_credentials,
    profiles_loader: _ProfilesLoader = iter_builtin_profiles,
    backend_builder: _BackendBuilder = _build_backend,
    topics_factory: _TopicsFactory = TopicLayout,
    transport_factory: _TransportFactory = PahoTransport,
    runtime_factory: _RuntimeFactory = BridgeRuntime,
    clock: Callable[[], datetime] = _utc_now,
) -> _ForegroundRuntime:
    config = config_loader(config_path)
    credentials = credentials_loader(config.mqtt)
    profiles = profiles_loader()
    topics = topics_factory(
        config.runtime.topic_prefix,
        config.runtime.discovery_prefix,
        config.runtime.home_assistant_status_topic,
    )
    will = Publish(
        topics.bridge_availability(),
        b"offline",
        qos=1,
        retain=True,
    )
    backend = backend_builder(config.backend, profiles, clock)
    try:
        transport = transport_factory(
            config.mqtt,
            credentials,
            will,
            config.runtime.client_id,
        )
        return runtime_factory(
            backend,
            transport,
            topics,
            config.runtime.home_assistant_status_topic,
            _new_request_id,
            clock,
        )
    except BaseException:
        backend.close()
        raise


def _run_foreground(runtime: _ForegroundRuntime) -> None:
    stop_requested = False

    def request_stop(_signal_number: int, _frame: FrameType | None) -> None:
        nonlocal stop_requested
        stop_requested = True

    previous_int = signal.signal(signal.SIGINT, request_stop)
    previous_term = signal.signal(signal.SIGTERM, request_stop)
    try:
        runtime.run(lambda: stop_requested)
    finally:
        try:
            runtime.close()
        finally:
            signal.signal(signal.SIGINT, previous_int)
            signal.signal(signal.SIGTERM, previous_term)


def _bounded_error(error: BaseException) -> str:
    message = " ".join(str(error).splitlines()).strip()
    if not message:
        message = error.__class__.__name__
    return message[:200]


def _run(config_path: Path, runtime_builder: _RuntimeBuilder) -> int:
    try:
        runtime = runtime_builder(config_path)
        _run_foreground(runtime)
    except (OSError, RescueError) as error:
        print(f"error: {_bounded_error(error)}", file=sys.stderr)
        return 2
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="smartenit-rescue")
    commands = parser.add_subparsers(dest="command", required=True)
    profiles = commands.add_parser("profiles", help="inspect device profiles")
    profile_commands = profiles.add_subparsers(dest="profile_command", required=True)
    profile_commands.add_parser("list", help="list built-in profiles")
    validate = profile_commands.add_parser("validate", help="validate a profile")
    validate.add_argument("path", type=Path)
    run = commands.add_parser("run", help="run the foreground MQTT bridge")
    run.add_argument("--config", required=True, type=Path)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    runtime_builder: _RuntimeBuilder = _build_runtime,
) -> int:
    """Run one profile command or the foreground bridge."""
    args = _parser().parse_args(argv)
    if args.command == "run":
        return _run(args.config, runtime_builder)
    if args.profile_command == "list":
        return _list_profiles()
    return _validate_profile(args.path)
