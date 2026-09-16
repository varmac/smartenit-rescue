"""Home Assistant MQTT device-discovery documents."""

import json

from smartenit_rescue import __version__
from smartenit_rescue.backends import (
    CapabilityAddress,
    CapabilityMode,
    RuntimeDevice,
)
from smartenit_rescue.models import CapabilityId

from .topics import TopicLayout


def discovery_document(device: RuntimeDevice, topics: TopicLayout) -> bytes:
    """Build a deterministic device-discovery payload for supported capabilities."""
    device_identifier = f"smartenit_rescue_{device.device.device_id}"
    availability = [
        {"topic": topics.bridge_availability()},
        {"topic": topics.device_availability(device.device.device_id)},
    ]
    components: dict[str, object] = {}

    for capability in device.capabilities:
        if capability.capability is not CapabilityId.ON_OFF:
            continue
        address = CapabilityAddress(
            device_id=device.device.device_id,
            endpoint=capability.endpoint,
            capability=capability.capability,
        )
        component_id = (
            f"{device_identifier}_{capability.endpoint}_{capability.capability}"
        )
        if capability.mode is CapabilityMode.OBSERVABLE:
            components[component_id] = {
                "availability": availability,
                "availability_mode": "all",
                "command_topic": topics.command(address),
                "name": "On/off",
                "optimistic": False,
                "payload_off": "OFF",
                "payload_on": "ON",
                "platform": "switch",
                "qos": 1,
                "state_off": "OFF",
                "state_on": "ON",
                "state_topic": topics.state(address),
                "unique_id": component_id,
            }
            confidence_id = f"{component_id}_confidence"
            components[confidence_id] = {
                "availability": availability,
                "availability_mode": "all",
                "entity_category": "diagnostic",
                "name": "On/off confidence",
                "platform": "sensor",
                "state_topic": topics.confidence(address),
                "unique_id": confidence_id,
            }
            continue

        for action, payload in (("on", "ON"), ("off", "OFF")):
            action_id = f"{component_id}_{action}"
            components[action_id] = {
                "availability": availability,
                "availability_mode": "all",
                "command_topic": topics.command(address),
                "name": f"Turn {action}",
                "payload_press": payload,
                "platform": "button",
                "qos": 1,
                "unique_id": action_id,
            }
        diagnostic_id = f"{component_id}_last_command"
        diagnostic_topic = topics.last_command(address)
        components[diagnostic_id] = {
            "availability": availability,
            "availability_mode": "all",
            "entity_category": "diagnostic",
            "json_attributes_topic": diagnostic_topic,
            "name": "Last command",
            "platform": "sensor",
            "state_topic": diagnostic_topic,
            "unique_id": diagnostic_id,
            "value_template": "{{ value_json.status }}",
        }

    document = {
        "components": components,
        "device": {
            "identifiers": [device_identifier],
            "manufacturer": device.device.manufacturer,
            "model": device.device.model,
            "name": device.name,
        },
        "origin": {
            "name": "smartenit-rescue",
            "sw_version": __version__,
        },
    }
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
