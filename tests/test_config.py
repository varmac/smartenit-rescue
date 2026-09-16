from __future__ import annotations

from dataclasses import FrozenInstanceError
from ipaddress import IPv4Address
from pathlib import Path

import pytest

from smartenit_rescue.config import (
    HarmonyG2Settings,
    MqttCredentials,
    SimulatorBackendSettings,
    load_config,
    load_credentials,
)
from smartenit_rescue.errors import ValidationError
from smartenit_rescue.models.identity import DeviceId


def valid_config_text(*, credentials: bool = True) -> str:
    credential_fields = ""
    if credentials:
        credential_fields = (
            'username_file = "secrets/mqtt-username"\n'
            'password_file = "secrets/mqtt-password"\n'
        )
    return (
        "[runtime]\n"
        'client_id = "smartenit-rescue"\n'
        'topic_prefix = "smartenit-rescue/v1"\n'
        'discovery_prefix = "homeassistant"\n'
        'home_assistant_status_topic = "homeassistant/status"\n'
        "\n"
        "[mqtt]\n"
        'host = "localhost"\n'
        "port = 1883\n"
        "keepalive_seconds = 60\n"
        "tls = false\n"
        f"{credential_fields}"
        "\n"
        "[backend]\n"
        'type = "simulator"\n'
        "\n"
        "[[backend.devices]]\n"
        'device_id = "0200000000000001"\n'
        'name = "Synthetic Load"\n'
        'profile_id = "smartenit.4040c"\n'
        "initial_on = false\n"
    )


def valid_g2_config_text(*, host: str | None = None) -> str:
    selected_host = host or str(IPv4Address(0x0A172D43))
    return (
        valid_config_text(credentials=False).split("[backend]", maxsplit=1)[0]
        + "[backend]\n"
        + 'type = "harmony_g2"\n'
        + f'host = "{selected_host}"\n'
        + 'certificate_sha256_file = "private/g2-pin"\n'
        + 'session_file = "private/g2-session.json"\n'
        + "\n"
        + "[[backend.devices]]\n"
        + 'device_id = "0200000000000001"\n'
        + 'name = "Example Load"\n'
        + 'profile_id = "smartenit.4040c"\n'
        + 'component_id = "1"\n'
    )


def write_config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_load_config_and_credentials_from_complete_document(tmp_path: Path) -> None:
    credentials_dir = tmp_path / "secrets"
    credentials_dir.mkdir()
    (credentials_dir / "mqtt-username").write_text("mqtt-user\n", encoding="utf-8")
    (credentials_dir / "mqtt-password").write_text("mqtt-pass\r\n", encoding="utf-8")
    path = write_config(tmp_path, valid_config_text())

    config = load_config(path)

    assert config.runtime.client_id == "smartenit-rescue"
    assert config.runtime.topic_prefix == "smartenit-rescue/v1"
    assert config.runtime.discovery_prefix == "homeassistant"
    assert config.runtime.home_assistant_status_topic == "homeassistant/status"
    assert config.mqtt.host == "localhost"
    assert config.mqtt.port == 1883
    assert config.mqtt.keepalive_seconds == 60
    assert config.mqtt.tls is False
    assert config.mqtt.username_file == credentials_dir / "mqtt-username"
    assert config.mqtt.password_file == credentials_dir / "mqtt-password"
    assert isinstance(config.backend, SimulatorBackendSettings)
    assert config.backend.devices[0].device_id == DeviceId.parse("0200000000000001")
    assert config.backend.devices[0].name == "Synthetic Load"
    assert config.backend.devices[0].profile_id == "smartenit.4040c"
    assert config.backend.devices[0].initial_on is False
    assert load_credentials(config.mqtt) == MqttCredentials("mqtt-user", "mqtt-pass")


def test_load_harmony_g2_configuration_from_closed_document(tmp_path: Path) -> None:
    path = write_config(tmp_path, valid_g2_config_text())

    config = load_config(path)

    assert isinstance(config.backend, HarmonyG2Settings)
    assert config.backend.type == "harmony_g2"
    assert config.backend.host == IPv4Address(0x0A172D43)
    assert config.backend.certificate_sha256_file == tmp_path / "private/g2-pin"
    assert config.backend.session_file == tmp_path / "private/g2-session.json"
    assert config.backend.devices[0].device_id == DeviceId.parse("0200000000000001")
    assert config.backend.devices[0].name == "Example Load"
    assert config.backend.devices[0].profile_id == "smartenit.4040c"
    assert config.backend.devices[0].component_id == "1"
    representation = repr(config.backend)
    assert str(config.backend.host) not in representation
    assert str(config.backend.session_file) not in representation


def test_configuration_records_are_immutable_and_credentials_are_redacted(
    tmp_path: Path,
) -> None:
    config = load_config(write_config(tmp_path, valid_config_text(credentials=False)))
    credentials = MqttCredentials("private-user", "private-password")

    with pytest.raises(FrozenInstanceError):
        config.runtime.client_id = "changed"  # type: ignore[misc]

    representation = repr(credentials)
    assert "private-user" not in representation
    assert "private-password" not in representation


@pytest.mark.parametrize(
    ("old", "new", "field"),
    [
        ("[runtime]", "unexpected = true\n[runtime]", "$"),
        ("[runtime]", "[runtime]\nunexpected = true", "runtime"),
        ("[mqtt]", "[mqtt]\nunexpected = true", "mqtt"),
        (
            "[[backend.devices]]",
            "[[backend.devices]]\nunexpected = true",
            "backend.devices[0]",
        ),
        (
            "[backend]",
            "[backend]\nunexpected = true",
            "backend",
        ),
    ],
)
def test_load_config_rejects_unknown_keys(
    tmp_path: Path, old: str, new: str, field: str
) -> None:
    path = write_config(tmp_path, valid_config_text().replace(old, new, 1))

    with pytest.raises(ValidationError) as caught:
        load_config(path)

    assert str(path) in str(caught.value)
    assert field in str(caught.value)


@pytest.mark.parametrize(
    "text",
    [
        valid_config_text().replace("[runtime]\n", "[runtime_missing]\n", 1),
        valid_config_text().replace("[mqtt]\n", "[mqtt_missing]\n", 1),
        valid_config_text().replace("[[backend.devices]]", "[[devices]]", 1),
    ],
)
def test_load_config_requires_all_sections(tmp_path: Path, text: str) -> None:
    with pytest.raises(ValidationError):
        load_config(write_config(tmp_path, text))


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ('client_id = "smartenit-rescue"', 'client_id = "  "'),
        ('host = "localhost"', 'host = "\t"'),
        ('name = "Synthetic Load"', 'name = ""'),
        ('profile_id = "smartenit.4040c"', 'profile_id = " "'),
    ],
)
def test_load_config_rejects_blank_strings(tmp_path: Path, old: str, new: str) -> None:
    path = write_config(tmp_path, valid_config_text().replace(old, new))

    with pytest.raises(ValidationError):
        load_config(path)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("topic_prefix", "smartenit-rescue/#"),
        ("topic_prefix", "smartenit-rescue/+/state"),
        ("topic_prefix", "/smartenit-rescue"),
        ("topic_prefix", "smartenit-rescue/"),
        ("topic_prefix", "smartenit-rescue//v1"),
        ("discovery_prefix", "homeassistant/#"),
        ("home_assistant_status_topic", "homeassistant//status"),
    ],
)
def test_load_config_rejects_wildcard_or_malformed_topics(
    tmp_path: Path, field: str, value: str
) -> None:
    old_values = {
        "topic_prefix": "smartenit-rescue/v1",
        "discovery_prefix": "homeassistant",
        "home_assistant_status_topic": "homeassistant/status",
    }
    text = valid_config_text().replace(
        f'{field} = "{old_values[field]}"', f'{field} = "{value}"'
    )

    with pytest.raises(ValidationError):
        load_config(write_config(tmp_path, text))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("port", "0"),
        ("port", "65536"),
        ("port", "true"),
        ("keepalive_seconds", "0"),
        ("keepalive_seconds", "65536"),
        ("keepalive_seconds", "false"),
    ],
)
def test_load_config_rejects_invalid_mqtt_integer_bounds(
    tmp_path: Path, field: str, value: str
) -> None:
    current = "1883" if field == "port" else "60"
    text = valid_config_text().replace(f"{field} = {current}", f"{field} = {value}")

    with pytest.raises(ValidationError):
        load_config(write_config(tmp_path, text))


@pytest.mark.parametrize("field", ["username_file", "password_file"])
def test_load_config_rejects_one_sided_credential_paths(
    tmp_path: Path, field: str
) -> None:
    line = next(
        item
        for item in valid_config_text().splitlines(keepends=True)
        if item.startswith(f"{field} =")
    )

    with pytest.raises(ValidationError):
        load_config(write_config(tmp_path, valid_config_text().replace(line, "")))


def test_load_config_rejects_duplicate_device_ids(tmp_path: Path) -> None:
    device = valid_config_text().split("[[backend.devices]]", maxsplit=1)[1]
    text = valid_config_text() + "[[backend.devices]]" + device

    with pytest.raises(ValidationError, match="duplicate"):
        load_config(write_config(tmp_path, text))


def test_load_config_rejects_non_reserved_simulator_id(tmp_path: Path) -> None:
    text = valid_config_text().replace(
        "0200000000000001",
        "".join(("00124b00", "00000001")),  # noqa: FLY002
    )

    with pytest.raises(ValidationError, match="reserved synthetic"):
        load_config(write_config(tmp_path, text))


@pytest.mark.parametrize(
    "host",
    [
        "gateway.local",
        str(IPv4Address(0x7F000001)),
        str(IPv4Address(0xCB007108)),
        str(IPv4Address(0)),
        str(IPv4Address(0xE0000001)),
        "::1",
    ],
)
def test_load_config_rejects_non_rfc1918_g2_target(tmp_path: Path, host: str) -> None:
    with pytest.raises(ValidationError, match="RFC 1918"):
        load_config(write_config(tmp_path, valid_g2_config_text(host=host)))


@pytest.mark.parametrize("component_id", ["", "../1", "a/b", "has space"])
def test_load_config_rejects_unsafe_g2_component_id(
    tmp_path: Path, component_id: str
) -> None:
    text = valid_g2_config_text().replace(
        'component_id = "1"', f'component_id = "{component_id}"'
    )

    with pytest.raises(ValidationError, match="component"):
        load_config(write_config(tmp_path, text))


def test_load_config_rejects_mixed_backend_fields(tmp_path: Path) -> None:
    text = valid_g2_config_text().replace(
        'component_id = "1"', 'component_id = "1"\ninitial_on = false'
    )

    with pytest.raises(ValidationError, match="unsupported key"):
        load_config(write_config(tmp_path, text))


def test_load_config_rejects_unknown_backend_type(tmp_path: Path) -> None:
    text = valid_config_text().replace('type = "simulator"', 'type = "unknown"')

    with pytest.raises(ValidationError, match="backend type"):
        load_config(write_config(tmp_path, text))


def test_load_config_rejects_unknown_profile(tmp_path: Path) -> None:
    text = valid_config_text().replace("smartenit.4040c", "example.unknown")

    with pytest.raises(ValidationError, match="unknown profile"):
        load_config(write_config(tmp_path, text))


@pytest.mark.parametrize("value", ["0", "1", '"false"'])
def test_load_config_rejects_non_boolean_initial_state(
    tmp_path: Path, value: str
) -> None:
    text = valid_config_text().replace("initial_on = false", f"initial_on = {value}")

    with pytest.raises(ValidationError):
        load_config(write_config(tmp_path, text))


def test_load_config_rejects_malformed_toml_without_echoing_content(
    tmp_path: Path,
) -> None:
    marker = "private-config-content-7fe2"
    path = write_config(tmp_path, f'[runtime\nsecret = "{marker}"')

    with pytest.raises(ValidationError) as caught:
        load_config(path)

    assert str(path) in str(caught.value)
    assert marker not in str(caught.value)


def test_load_credentials_trims_only_one_final_line_ending(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path, valid_config_text()))
    assert config.mqtt.username_file is not None
    assert config.mqtt.password_file is not None
    config.mqtt.username_file.parent.mkdir()
    config.mqtt.username_file.write_text(" user name \n", encoding="utf-8")
    config.mqtt.password_file.write_text("pass\nword\n\n", encoding="utf-8")

    assert load_credentials(config.mqtt) == MqttCredentials(
        " user name ", "pass\nword\n"
    )


def test_load_credentials_returns_none_without_configured_paths(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path, valid_config_text(credentials=False)))

    assert load_credentials(config.mqtt) is None


@pytest.mark.parametrize("value", ["", "\n", " \t\r\n"])
def test_load_credentials_rejects_blank_secrets_without_exposing_them(
    tmp_path: Path, value: str
) -> None:
    config = load_config(write_config(tmp_path, valid_config_text()))
    assert config.mqtt.username_file is not None
    assert config.mqtt.password_file is not None
    config.mqtt.username_file.parent.mkdir()
    config.mqtt.username_file.write_text(value, encoding="utf-8")
    marker = "private-password-marker-41c9"
    config.mqtt.password_file.write_text(marker, encoding="utf-8")

    with pytest.raises(ValidationError) as caught:
        load_credentials(config.mqtt)

    message = str(caught.value)
    assert marker not in message
    assert len(message) <= 160


def test_load_credentials_does_not_expose_invalid_utf8_bytes(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path, valid_config_text()))
    assert config.mqtt.username_file is not None
    assert config.mqtt.password_file is not None
    config.mqtt.username_file.parent.mkdir()
    config.mqtt.username_file.write_bytes(b"private-user-\xff")
    config.mqtt.password_file.write_text("private-password", encoding="utf-8")

    with pytest.raises(ValidationError) as caught:
        load_credentials(config.mqtt)

    message = str(caught.value)
    assert "private-user" not in message
    assert "private-password" not in message
