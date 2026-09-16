import json
from dataclasses import replace

from smartenit_rescue import __version__
from smartenit_rescue.backends import (
    CapabilityMode,
    RuntimeCapability,
    RuntimeDevice,
)
from smartenit_rescue.models import CapabilityId, Device, DeviceId
from smartenit_rescue.mqtt import TopicLayout, discovery_document
from smartenit_rescue.profiles import DeviceProfile, iter_builtin_profiles

DEVICE_ID = DeviceId.parse("0200000000000001")


def topic_layout() -> TopicLayout:
    return TopicLayout(
        topic_prefix="smartenit-rescue/v1",
        discovery_prefix="homeassistant",
        home_assistant_status_topic="homeassistant/status",
    )


def runtime_device(
    profile: DeviceProfile | None = None,
    *,
    mode: CapabilityMode = CapabilityMode.OBSERVABLE,
) -> RuntimeDevice:
    selected_profile = profile or iter_builtin_profiles()[0]
    on_off = tuple(
        RuntimeCapability(
            endpoint=endpoint_id,
            capability=capability.capability,
            mode=mode,
        )
        for endpoint_id, endpoint in selected_profile.endpoints.items()
        for capability in endpoint.capabilities
        if capability.capability is CapabilityId.ON_OFF and capability.writable
    )
    return RuntimeDevice(
        device=Device(
            device_id=DEVICE_ID,
            manufacturer=selected_profile.manufacturer,
            model=selected_profile.models[0],
            endpoints=tuple(selected_profile.endpoints),
        ),
        name="Synthetic Heater",
        profile=selected_profile,
        capabilities=on_off,
    )


def test_discovery_document_exposes_only_writable_on_off_components() -> None:
    document = discovery_document(runtime_device(), topic_layout())
    decoded = json.loads(document)
    availability = [
        {"topic": "smartenit-rescue/v1/bridge/availability"},
        {"topic": "smartenit-rescue/v1/0200000000000001/availability"},
    ]
    expected = {
        "device": {
            "identifiers": ["smartenit_rescue_0200000000000001"],
            "manufacturer": "Compacta International, Ltd.",
            "model": "ZBMLCSR",
            "name": "Synthetic Heater",
        },
        "origin": {
            "name": "smartenit-rescue",
            "sw_version": __version__,
        },
        "components": {
            "smartenit_rescue_0200000000000001_1_on_off": {
                "availability": availability,
                "availability_mode": "all",
                "command_topic": (
                    "smartenit-rescue/v1/0200000000000001/1/on_off/command"
                ),
                "name": "On/off",
                "optimistic": False,
                "payload_off": "OFF",
                "payload_on": "ON",
                "platform": "switch",
                "qos": 1,
                "state_off": "OFF",
                "state_on": "ON",
                "state_topic": ("smartenit-rescue/v1/0200000000000001/1/on_off/state"),
                "unique_id": "smartenit_rescue_0200000000000001_1_on_off",
            },
            "smartenit_rescue_0200000000000001_1_on_off_confidence": {
                "availability": availability,
                "availability_mode": "all",
                "entity_category": "diagnostic",
                "name": "On/off confidence",
                "platform": "sensor",
                "state_topic": (
                    "smartenit-rescue/v1/0200000000000001/1/on_off/confidence"
                ),
                "unique_id": ("smartenit_rescue_0200000000000001_1_on_off_confidence"),
            },
        },
    }

    assert decoded == expected
    assert set(decoded["components"]) == {
        "smartenit_rescue_0200000000000001_1_on_off",
        "smartenit_rescue_0200000000000001_1_on_off_confidence",
    }


def test_discovery_document_is_compact_and_byte_for_byte_deterministic() -> None:
    device = runtime_device()
    topics = topic_layout()

    first = discovery_document(device, topics)
    second = discovery_document(device, topics)

    assert first == second
    assert b"\n" not in first
    assert b": " not in first
    assert first == json.dumps(
        json.loads(first), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def test_command_only_on_off_uses_buttons_and_last_command_diagnostic() -> None:
    decoded = json.loads(
        discovery_document(
            runtime_device(mode=CapabilityMode.COMMAND_ONLY),
            topic_layout(),
        )
    )
    availability = [
        {"topic": "smartenit-rescue/v1/bridge/availability"},
        {"topic": "smartenit-rescue/v1/0200000000000001/availability"},
    ]
    command_topic = "smartenit-rescue/v1/0200000000000001/1/on_off/command"
    diagnostic_topic = "smartenit-rescue/v1/0200000000000001/1/on_off/last-command"

    assert decoded["components"] == {
        "smartenit_rescue_0200000000000001_1_on_off_on": {
            "availability": availability,
            "availability_mode": "all",
            "command_topic": command_topic,
            "name": "Turn on",
            "payload_press": "ON",
            "platform": "button",
            "qos": 1,
            "unique_id": "smartenit_rescue_0200000000000001_1_on_off_on",
        },
        "smartenit_rescue_0200000000000001_1_on_off_off": {
            "availability": availability,
            "availability_mode": "all",
            "command_topic": command_topic,
            "name": "Turn off",
            "payload_press": "OFF",
            "platform": "button",
            "qos": 1,
            "unique_id": "smartenit_rescue_0200000000000001_1_on_off_off",
        },
        "smartenit_rescue_0200000000000001_1_on_off_last_command": {
            "availability": availability,
            "availability_mode": "all",
            "entity_category": "diagnostic",
            "json_attributes_topic": diagnostic_topic,
            "name": "Last command",
            "platform": "sensor",
            "state_topic": diagnostic_topic,
            "unique_id": ("smartenit_rescue_0200000000000001_1_on_off_last_command"),
            "value_template": "{{ value_json.status }}",
        },
    }
    assert not any(
        component["platform"] == "switch"
        for component in decoded["components"].values()
    )
    assert b"/state" not in discovery_document(
        runtime_device(mode=CapabilityMode.COMMAND_ONLY),
        topic_layout(),
    )
    assert b"confidence" not in discovery_document(
        runtime_device(mode=CapabilityMode.COMMAND_ONLY),
        topic_layout(),
    )


def test_discovery_document_omits_non_writable_on_off_capability() -> None:
    profile = iter_builtin_profiles()[0]
    endpoint_id, endpoint = next(iter(profile.endpoints.items()))
    read_only_capabilities = tuple(
        replace(capability, writable=False)
        if capability.capability is CapabilityId.ON_OFF
        else capability
        for capability in endpoint.capabilities
    )
    read_only_profile = replace(
        profile,
        endpoints={endpoint_id: replace(endpoint, capabilities=read_only_capabilities)},
    )

    document = discovery_document(runtime_device(read_only_profile), topic_layout())
    decoded = json.loads(document)

    assert decoded["components"] == {}
    assert b"command_topic" not in document
