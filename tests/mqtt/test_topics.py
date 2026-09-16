import json
from datetime import UTC, datetime
from typing import cast

import pytest

from smartenit_rescue.backends import CapabilityAddress
from smartenit_rescue.errors import ValidationError
from smartenit_rescue.models import CapabilityId, CommandStatus, DeviceId, EndpointId
from smartenit_rescue.mqtt import (
    TopicLayout,
    decode_on_off,
    encode_last_command,
    encode_on_off,
)

DEVICE_ID = DeviceId.parse("0200000000000001")
ADDRESS = CapabilityAddress(DEVICE_ID, EndpointId(1), CapabilityId.ON_OFF)


def topic_layout() -> TopicLayout:
    return TopicLayout(
        topic_prefix="smartenit-rescue/v1",
        discovery_prefix="homeassistant",
        home_assistant_status_topic="homeassistant/status",
    )


def test_topic_layout_builds_exact_backend_neutral_topics() -> None:
    topics = topic_layout()

    assert topics.bridge_availability() == ("smartenit-rescue/v1/bridge/availability")
    assert topics.device_availability(DEVICE_ID) == (
        "smartenit-rescue/v1/0200000000000001/availability"
    )
    assert topics.command(ADDRESS) == (
        "smartenit-rescue/v1/0200000000000001/1/on_off/command"
    )
    assert topics.state(ADDRESS) == (
        "smartenit-rescue/v1/0200000000000001/1/on_off/state"
    )
    assert topics.confidence(ADDRESS) == (
        "smartenit-rescue/v1/0200000000000001/1/on_off/confidence"
    )
    assert topics.last_command(ADDRESS) == (
        "smartenit-rescue/v1/0200000000000001/1/on_off/last-command"
    )
    assert topics.discovery(DEVICE_ID) == (
        "homeassistant/device/smartenit_rescue_0200000000000001/config"
    )
    assert topics.home_assistant_status() == "homeassistant/status"


def test_parse_command_topic_returns_the_canonical_address() -> None:
    topics = topic_layout()

    assert (
        topics.parse_command_topic(
            "smartenit-rescue/v1/0200000000000001/1/on_off/command"
        )
        == ADDRESS
    )


@pytest.mark.parametrize("endpoint", [2, 240])
def test_parse_command_topic_round_trips_other_valid_endpoints(endpoint: int) -> None:
    topics = topic_layout()
    address = CapabilityAddress(DEVICE_ID, EndpointId(endpoint), CapabilityId.ON_OFF)

    assert topics.parse_command_topic(topics.command(address)) == address


@pytest.mark.parametrize(
    "topic",
    [
        "",
        "smartenit-rescue/v1",
        "smartenit-rescue/v1/0200000000000001/1/on_off/state",
        "smartenit-rescue/v1/0200000000000001/1/on_off/command/extra",
        "smartenit-rescue/v1/0200000000000001/1//command",
        "smartenit-rescue/v1/0200000000000001/0/on_off/command",
        "smartenit-rescue/v1/0200000000000001/241/on_off/command",
        "smartenit-rescue/v1/0200000000000001/01/on_off/command",
        "smartenit-rescue/v1/0200000000000001/1/electrical_power/command",
        "smartenit-rescue/v1/0200000000000001/1/+/command",
        "smartenit-rescue/v1/+/1/on_off/command",
        "smartenit-rescue/v1/#",
        "smartenit-rescue/v1/020000000000000g/1/on_off/command",
        "smartenit-rescue/v1/020000000000000A/1/on_off/command",
        "other/v1/0200000000000001/1/on_off/command",
        ("smartenit-rescue/v1/smartenit-rescue/v1/0200000000000001/1/on_off/command"),
    ],
)
def test_parse_command_topic_rejects_non_command_and_malformed_topics(
    topic: str,
) -> None:
    assert topic_layout().parse_command_topic(topic) is None


@pytest.mark.parametrize(("value", "payload"), [(True, b"ON"), (False, b"OFF")])
def test_on_off_codec_round_trips_boolean_state(value: bool, payload: bytes) -> None:
    assert encode_on_off(value) == payload
    assert decode_on_off(payload) is value


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"on",
        b"off",
        b"ON\n",
        b" OFF",
        b"TRUE",
        cast(bytes, "ON"),
        cast(bytes, bytearray(b"ON")),
    ],
)
def test_decode_on_off_rejects_every_payload_except_exact_bytes(payload: bytes) -> None:
    with pytest.raises(ValidationError, match="ON or OFF"):
        decode_on_off(payload)


def test_encode_on_off_rejects_non_boolean_values() -> None:
    with pytest.raises(ValidationError, match="boolean"):
        encode_on_off(cast(bool, 1))


def test_last_command_codec_is_compact_deterministic_and_utc() -> None:
    payload = encode_last_command(
        True,
        CommandStatus.ACCEPTED,
        datetime(2026, 9, 15, 12, 42, tzinfo=UTC),
    )

    assert payload == (
        b'{"action":"ON","at":"2026-09-15T12:42:00Z","status":"accepted"}'
    )
    assert json.loads(payload) == {
        "action": "ON",
        "at": "2026-09-15T12:42:00Z",
        "status": "accepted",
    }


def test_last_command_codec_rejects_naive_time() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        encode_last_command(
            False,
            CommandStatus.REJECTED,
            datetime(2026, 9, 15, 12, 42, tzinfo=UTC).replace(tzinfo=None),
        )
