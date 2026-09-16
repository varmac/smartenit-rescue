"""Strict runtime configuration and credential-file loading."""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path
from typing import Literal, TypeAlias

from smartenit_rescue.errors import ValidationError
from smartenit_rescue.models.identity import DeviceId
from smartenit_rescue.profiles import iter_builtin_profiles

_MISSING = object()
_RUNTIME_KEYS = frozenset(
    {
        "client_id",
        "topic_prefix",
        "discovery_prefix",
        "home_assistant_status_topic",
    }
)
_MQTT_REQUIRED_KEYS = frozenset({"host", "port", "keepalive_seconds", "tls"})
_MQTT_CREDENTIAL_KEYS = frozenset({"username_file", "password_file"})
_SIMULATOR_BACKEND_KEYS = frozenset({"type", "devices"})
_HARMONY_BACKEND_KEYS = frozenset(
    {
        "type",
        "host",
        "certificate_sha256_file",
        "session_file",
        "devices",
    }
)
_SIMULATOR_DEVICE_KEYS = frozenset({"device_id", "name", "profile_id", "initial_on"})
_HARMONY_DEVICE_KEYS = frozenset({"device_id", "name", "profile_id", "component_id"})
_SAFE_COMPONENT_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_RFC1918_NETWORKS = (
    IPv4Network((0x0A000000, 8)),
    IPv4Network((0xAC100000, 12)),
    IPv4Network((0xC0A80000, 16)),
)


@dataclass(frozen=True, slots=True)
class RuntimeSettings:
    client_id: str
    topic_prefix: str
    discovery_prefix: str
    home_assistant_status_topic: str


@dataclass(frozen=True, slots=True)
class MqttSettings:
    host: str
    port: int
    keepalive_seconds: int
    tls: bool
    username_file: Path | None
    password_file: Path | None


@dataclass(frozen=True, slots=True)
class SimulatorDeviceSettings:
    device_id: DeviceId
    name: str
    profile_id: str
    initial_on: bool


@dataclass(frozen=True, slots=True)
class SimulatorBackendSettings:
    type: Literal["simulator"]
    devices: tuple[SimulatorDeviceSettings, ...]


@dataclass(frozen=True, slots=True)
class HarmonyG2DeviceSettings:
    device_id: DeviceId
    name: str
    profile_id: str
    component_id: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class HarmonyG2Settings:
    type: Literal["harmony_g2"]
    host: IPv4Address = field(repr=False)
    certificate_sha256_file: Path = field(repr=False)
    session_file: Path = field(repr=False)
    devices: tuple[HarmonyG2DeviceSettings, ...]


BackendSettings: TypeAlias = SimulatorBackendSettings | HarmonyG2Settings


@dataclass(frozen=True, slots=True)
class AppConfig:
    runtime: RuntimeSettings
    mqtt: MqttSettings
    backend: BackendSettings


@dataclass(frozen=True, slots=True)
class MqttCredentials:
    username: str = field(repr=False)
    password: str = field(repr=False)


def _invalid(source: Path, location: str, message: str) -> ValidationError:
    return ValidationError(f"{source}: {location}: {message}")


def _mapping(value: object, *, source: Path, location: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise _invalid(source, location, "must be a table")
    return value


def _closed_mapping(
    value: object,
    *,
    required: frozenset[str],
    allowed: frozenset[str] | None = None,
    source: Path,
    location: str,
) -> Mapping[str, object]:
    item = _mapping(value, source=source, location=location)
    accepted = required if allowed is None else allowed
    if not set(item).issubset(accepted):
        raise _invalid(source, location, "contains an unsupported key")
    if not required.issubset(item):
        raise _invalid(source, location, "is missing a required key")
    return item


def _required(
    value: Mapping[str, object], property_name: str, *, source: Path, location: str
) -> object:
    item = value.get(property_name, _MISSING)
    if item is _MISSING:
        raise _invalid(source, location, "is missing a required key")
    return item


def _nonblank_string(value: object, *, source: Path, location: str) -> str:
    if not isinstance(value, str):
        raise _invalid(source, location, "must be a string")
    if not value.strip():
        raise _invalid(source, location, "must be nonblank")
    return value


def _boolean(value: object, *, source: Path, location: str) -> bool:
    if not isinstance(value, bool):
        raise _invalid(source, location, "must be a boolean")
    return value


def _bounded_integer(
    value: object,
    *,
    minimum: int,
    maximum: int,
    source: Path,
    location: str,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _invalid(source, location, "must be an integer")
    if not minimum <= value <= maximum:
        raise _invalid(source, location, f"must be between {minimum} and {maximum}")
    return value


def _topic(value: object, *, source: Path, location: str) -> str:
    topic = _nonblank_string(value, source=source, location=location)
    if (
        topic.startswith("/")
        or topic.endswith("/")
        or "//" in topic
        or "+" in topic
        or "#" in topic
        or "\x00" in topic
    ):
        raise _invalid(source, location, "must be a valid MQTT topic name")
    return topic


def _path(value: object, *, config_path: Path, source: Path, location: str) -> Path:
    raw = _nonblank_string(value, source=source, location=location)
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = config_path.parent / candidate
    return candidate


def _runtime(value: object, *, source: Path) -> RuntimeSettings:
    item = _closed_mapping(
        value,
        required=_RUNTIME_KEYS,
        source=source,
        location="runtime",
    )
    return RuntimeSettings(
        client_id=_nonblank_string(
            _required(item, "client_id", source=source, location="runtime"),
            source=source,
            location="runtime.client_id",
        ),
        topic_prefix=_topic(
            _required(item, "topic_prefix", source=source, location="runtime"),
            source=source,
            location="runtime.topic_prefix",
        ),
        discovery_prefix=_topic(
            _required(item, "discovery_prefix", source=source, location="runtime"),
            source=source,
            location="runtime.discovery_prefix",
        ),
        home_assistant_status_topic=_topic(
            _required(
                item,
                "home_assistant_status_topic",
                source=source,
                location="runtime",
            ),
            source=source,
            location="runtime.home_assistant_status_topic",
        ),
    )


def _mqtt(value: object, *, config_path: Path, source: Path) -> MqttSettings:
    item = _closed_mapping(
        value,
        required=_MQTT_REQUIRED_KEYS,
        allowed=_MQTT_REQUIRED_KEYS | _MQTT_CREDENTIAL_KEYS,
        source=source,
        location="mqtt",
    )
    credential_keys = _MQTT_CREDENTIAL_KEYS.intersection(item)
    if credential_keys and credential_keys != _MQTT_CREDENTIAL_KEYS:
        raise _invalid(
            source,
            "mqtt",
            "username_file and password_file must be configured together",
        )
    user_path: Path | None = None
    pass_path: Path | None = None
    if credential_keys:
        user_path = _path(
            _required(item, "username_file", source=source, location="mqtt"),
            config_path=config_path,
            source=source,
            location="mqtt.username_file",
        )
        pass_path = _path(
            _required(item, "password_file", source=source, location="mqtt"),
            config_path=config_path,
            source=source,
            location="mqtt.password_file",
        )
    host = _nonblank_string(
        _required(item, "host", source=source, location="mqtt"),
        source=source,
        location="mqtt.host",
    )
    port = _bounded_integer(
        _required(item, "port", source=source, location="mqtt"),
        minimum=1,
        maximum=65535,
        source=source,
        location="mqtt.port",
    )
    keepalive_seconds = _bounded_integer(
        _required(item, "keepalive_seconds", source=source, location="mqtt"),
        minimum=1,
        maximum=65535,
        source=source,
        location="mqtt.keepalive_seconds",
    )
    tls = _boolean(
        _required(item, "tls", source=source, location="mqtt"),
        source=source,
        location="mqtt.tls",
    )
    return MqttSettings(
        host,
        port,
        keepalive_seconds,
        tls,
        user_path,
        pass_path,
    )


def _device_id(
    value: object,
    *,
    synthetic_only: bool,
    source: Path,
    location: str,
) -> DeviceId:
    raw = _nonblank_string(value, source=source, location=location)
    try:
        device_id = DeviceId.parse(raw)
    except ValidationError as error:
        raise _invalid(source, location, "must be a valid device ID") from error
    if synthetic_only and not device_id.value.startswith("02000000000000"):
        raise _invalid(source, location, "must use the reserved synthetic range")
    return device_id


def _raw_devices(backend: Mapping[str, object], *, source: Path) -> list[object]:
    raw_devices = _required(backend, "devices", source=source, location="backend")
    if not isinstance(raw_devices, list):
        raise _invalid(source, "backend.devices", "must be an array of tables")
    if not raw_devices:
        raise _invalid(source, "backend.devices", "must contain at least one device")
    return raw_devices


def _profile_id(item: Mapping[str, object], *, source: Path, location: str) -> str:
    profile_id = _nonblank_string(
        _required(item, "profile_id", source=source, location=location),
        source=source,
        location=f"{location}.profile_id",
    )
    known_profiles = {profile.profile_id for profile in iter_builtin_profiles()}
    if profile_id not in known_profiles:
        raise _invalid(source, f"{location}.profile_id", "is an unknown profile")
    return profile_id


def _simulator_backend(value: object, *, source: Path) -> SimulatorBackendSettings:
    backend = _closed_mapping(
        value,
        required=_SIMULATOR_BACKEND_KEYS,
        source=source,
        location="backend",
    )
    devices: list[SimulatorDeviceSettings] = []
    seen_ids: set[DeviceId] = set()
    for index, raw_device in enumerate(_raw_devices(backend, source=source)):
        location = f"backend.devices[{index}]"
        item = _closed_mapping(
            raw_device,
            required=_SIMULATOR_DEVICE_KEYS,
            source=source,
            location=location,
        )
        device_id = _device_id(
            _required(item, "device_id", source=source, location=location),
            synthetic_only=True,
            source=source,
            location=f"{location}.device_id",
        )
        if device_id in seen_ids:
            raise _invalid(source, f"{location}.device_id", "must not be duplicate")
        seen_ids.add(device_id)
        devices.append(
            SimulatorDeviceSettings(
                device_id=device_id,
                name=_nonblank_string(
                    _required(item, "name", source=source, location=location),
                    source=source,
                    location=f"{location}.name",
                ),
                profile_id=_profile_id(item, source=source, location=location),
                initial_on=_boolean(
                    _required(item, "initial_on", source=source, location=location),
                    source=source,
                    location=f"{location}.initial_on",
                ),
            )
        )
    return SimulatorBackendSettings(type="simulator", devices=tuple(devices))


def _g2_host(value: object, *, source: Path) -> IPv4Address:
    raw = _nonblank_string(value, source=source, location="backend.host")
    try:
        address = IPv4Address(raw)
    except ValueError as error:
        raise _invalid(
            source, "backend.host", "must be an RFC 1918 IPv4 literal"
        ) from error
    if not any(address in network for network in _RFC1918_NETWORKS):
        raise _invalid(source, "backend.host", "must be an RFC 1918 IPv4 literal")
    return address


def _harmony_backend(
    value: object, *, config_path: Path, source: Path
) -> HarmonyG2Settings:
    backend = _closed_mapping(
        value,
        required=_HARMONY_BACKEND_KEYS,
        source=source,
        location="backend",
    )
    devices: list[HarmonyG2DeviceSettings] = []
    seen_ids: set[DeviceId] = set()
    seen_bindings: set[tuple[DeviceId, str]] = set()
    for index, raw_device in enumerate(_raw_devices(backend, source=source)):
        location = f"backend.devices[{index}]"
        item = _closed_mapping(
            raw_device,
            required=_HARMONY_DEVICE_KEYS,
            source=source,
            location=location,
        )
        device_id = _device_id(
            _required(item, "device_id", source=source, location=location),
            synthetic_only=False,
            source=source,
            location=f"{location}.device_id",
        )
        if device_id in seen_ids:
            raise _invalid(source, f"{location}.device_id", "must not be duplicate")
        component_id = _nonblank_string(
            _required(item, "component_id", source=source, location=location),
            source=source,
            location=f"{location}.component_id",
        )
        if _SAFE_COMPONENT_ID.fullmatch(component_id) is None:
            raise _invalid(
                source,
                f"{location}.component_id",
                "must be a safe component identifier",
            )
        binding = (device_id, component_id)
        if binding in seen_bindings:
            raise _invalid(source, location, "must not duplicate a device binding")
        seen_ids.add(device_id)
        seen_bindings.add(binding)
        devices.append(
            HarmonyG2DeviceSettings(
                device_id=device_id,
                name=_nonblank_string(
                    _required(item, "name", source=source, location=location),
                    source=source,
                    location=f"{location}.name",
                ),
                profile_id=_profile_id(item, source=source, location=location),
                component_id=component_id,
            )
        )
    return HarmonyG2Settings(
        type="harmony_g2",
        host=_g2_host(
            _required(backend, "host", source=source, location="backend"),
            source=source,
        ),
        certificate_sha256_file=_path(
            _required(
                backend,
                "certificate_sha256_file",
                source=source,
                location="backend",
            ),
            config_path=config_path,
            source=source,
            location="backend.certificate_sha256_file",
        ),
        session_file=_path(
            _required(backend, "session_file", source=source, location="backend"),
            config_path=config_path,
            source=source,
            location="backend.session_file",
        ),
        devices=tuple(devices),
    )


def _backend(value: object, *, config_path: Path, source: Path) -> BackendSettings:
    item = _mapping(value, source=source, location="backend")
    backend_type = _nonblank_string(
        _required(item, "type", source=source, location="backend"),
        source=source,
        location="backend.type",
    )
    if backend_type == "simulator":
        return _simulator_backend(item, source=source)
    if backend_type == "harmony_g2":
        return _harmony_backend(item, config_path=config_path, source=source)
    raise _invalid(source, "backend.type", "contains an unsupported backend type")


def load_config(path: Path) -> AppConfig:
    """Load and strictly validate one runtime TOML document."""
    source = path
    try:
        raw_toml = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise _invalid(source, "$", "could not read UTF-8 configuration") from error
    try:
        raw = tomllib.loads(raw_toml)
    except (tomllib.TOMLDecodeError, RecursionError) as error:
        raise _invalid(source, "$", "invalid TOML") from error
    root = _closed_mapping(
        raw,
        required=frozenset({"runtime", "mqtt", "backend"}),
        source=source,
        location="$",
    )
    return AppConfig(
        runtime=_runtime(
            _required(root, "runtime", source=source, location="$"),
            source=source,
        ),
        mqtt=_mqtt(
            _required(root, "mqtt", source=source, location="$"),
            config_path=path,
            source=source,
        ),
        backend=_backend(
            _required(root, "backend", source=source, location="$"),
            config_path=path,
            source=source,
        ),
    )


def _read_credential(path: Path, *, field_name: str) -> str:
    try:
        value = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeError) as error:
        raise ValidationError(
            f"{field_name}: could not read UTF-8 credential file"
        ) from error
    if value.endswith("\r\n"):
        value = value[:-2]
    elif value.endswith(("\r", "\n")):
        value = value[:-1]
    if not value.strip():
        raise ValidationError(f"{field_name}: credential must be nonblank")
    return value


def load_credentials(settings: MqttSettings) -> MqttCredentials | None:
    """Read configured MQTT credentials without exposing secret contents."""
    user_path = settings.username_file
    pass_path = settings.password_file
    if user_path is None and pass_path is None:
        return None
    if user_path is None or pass_path is None:
        raise ValidationError(
            "mqtt: username_file and password_file must be configured together"
        )
    return MqttCredentials(
        _read_credential(user_path, field_name="mqtt.username_file"),
        _read_credential(pass_path, field_name="mqtt.password_file"),
    )
