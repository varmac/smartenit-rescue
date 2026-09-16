"""Deterministic MQTT topic and payload codecs."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from smartenit_rescue.backends import CapabilityAddress
from smartenit_rescue.errors import ValidationError
from smartenit_rescue.models import (
    CapabilityId,
    CommandStatus,
    DeviceId,
    EndpointId,
)


@dataclass(frozen=True, slots=True)
class TopicLayout:
    topic_prefix: str
    discovery_prefix: str
    home_assistant_status_topic: str

    def bridge_availability(self) -> str:
        return f"{self.topic_prefix}/bridge/availability"

    def device_availability(self, device_id: DeviceId) -> str:
        return f"{self.topic_prefix}/{device_id}/availability"

    def command(self, address: CapabilityAddress) -> str:
        return self._capability_topic(address, "command")

    def state(self, address: CapabilityAddress) -> str:
        return self._capability_topic(address, "state")

    def confidence(self, address: CapabilityAddress) -> str:
        return self._capability_topic(address, "confidence")

    def last_command(self, address: CapabilityAddress) -> str:
        return self._capability_topic(address, "last-command")

    def discovery(self, device_id: DeviceId) -> str:
        return f"{self.discovery_prefix}/device/smartenit_rescue_{device_id}/config"

    def home_assistant_status(self) -> str:
        return self.home_assistant_status_topic

    def parse_command_topic(self, topic: str) -> CapabilityAddress | None:
        if (
            not isinstance(topic, str)
            or not topic.startswith(f"{self.topic_prefix}/")
            or "+" in topic
            or "#" in topic
            or "\x00" in topic
        ):
            return None
        suffix = topic.removeprefix(f"{self.topic_prefix}/")
        parts = suffix.split("/")
        if len(parts) != 4:
            return None
        raw_device_id, raw_endpoint, raw_capability, kind = parts
        if raw_capability != CapabilityId.ON_OFF or kind != "command":
            return None
        if (
            len(raw_endpoint) > 3
            or not raw_endpoint.isascii()
            or not raw_endpoint.isdecimal()
        ):
            return None
        try:
            device_id = DeviceId.parse(raw_device_id)
            endpoint = EndpointId(int(raw_endpoint))
        except ValidationError:
            return None
        if str(device_id) != raw_device_id or str(endpoint) != raw_endpoint:
            return None
        return CapabilityAddress(
            device_id=device_id,
            endpoint=endpoint,
            capability=CapabilityId.ON_OFF,
        )

    def _capability_topic(self, address: CapabilityAddress, kind: str) -> str:
        return (
            f"{self.topic_prefix}/{address.device_id}/{address.endpoint}/"
            f"{address.capability}/{kind}"
        )


def decode_on_off(payload: bytes) -> bool:
    if not isinstance(payload, bytes):
        raise ValidationError("on/off payload must be bytes containing ON or OFF")
    if payload == b"ON":
        return True
    if payload == b"OFF":
        return False
    raise ValidationError("on/off payload must be exactly ON or OFF")


def encode_on_off(value: bool) -> bytes:
    if not isinstance(value, bool):
        raise ValidationError("on/off value must be boolean")
    return b"ON" if value else b"OFF"


def encode_last_command(
    action: bool,
    status: CommandStatus,
    occurred_at: datetime,
) -> bytes:
    if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
        raise ValidationError("last-command time must be timezone-aware")
    at = occurred_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
    document = {
        "action": "ON" if action else "OFF",
        "at": at,
        "status": status.value,
    }
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
