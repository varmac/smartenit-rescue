"""Broker-independent event loop for the MQTT bridge."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime

from smartenit_rescue.backends import (
    Backend,
    CapabilityAddress,
    CapabilityMode,
    RefreshableBackend,
    RuntimeDevice,
    WireValue,
)
from smartenit_rescue.errors import ValidationError
from smartenit_rescue.models import (
    AvailabilityState,
    CapabilityId,
    Command,
    CommandStatus,
    DeviceId,
    Observation,
)
from smartenit_rescue.mqtt import (
    Connected,
    Disconnected,
    Message,
    Publish,
    TopicLayout,
    Transport,
    TransportError,
    decode_on_off,
    discovery_document,
    encode_last_command,
    encode_on_off,
)
from smartenit_rescue.mqtt.transport import DeliveryTransport

_LOGGER = logging.getLogger(__name__)
_QOS = 1
_SHUTDOWN_DELIVERY_TIMEOUT_SECONDS = 5.0


class BridgeRuntime:
    """Own transport events and translate them into backend operations."""

    def __init__(
        self,
        backend: Backend,
        transport: Transport,
        topics: TopicLayout,
        home_assistant_status_topic: str,
        request_id_factory: Callable[[], str],
        clock: Callable[[], datetime],
    ) -> None:
        self._backend = backend
        self._transport = transport
        self._topics = topics
        self._home_assistant_status_topic = home_assistant_status_topic
        self._request_id_factory = request_id_factory
        self._clock = clock
        self._devices = backend.devices()
        self._capability_modes = self._find_capability_modes(self._devices)
        self._writable_addresses = tuple(self._capability_modes)
        self._validate_status_topic()
        self._connected = False
        self._closed = False
        self._published_availability: dict[DeviceId, AvailabilityState] = {}

    def run(self, stop_requested: Callable[[], bool]) -> None:
        """Connect and process transport events until shutdown is requested."""
        self._transport.connect()
        while not stop_requested():
            event = self._transport.next_event(timeout=0.2)
            if stop_requested():
                if isinstance(event, Connected):
                    self._connected = True
                elif isinstance(event, Disconnected):
                    self._connected = False
                break
            if isinstance(event, Connected):
                self._connected = True
                self._subscribe()
                self._publish_snapshot()
            elif isinstance(event, Disconnected):
                self._connected = False
                if event.reason is not None:
                    raise TransportError(event.reason)
            elif isinstance(event, Message) and self._connected:
                self._handle_message(event)
            if self._connected and isinstance(self._backend, RefreshableBackend):
                self._backend.refresh()
                self._publish_changed_availability()

    def close(self) -> None:
        """Publish bridge shutdown state and release owned resources once."""
        if self._closed:
            return
        self._closed = True
        try:
            if self._connected:
                if not isinstance(self._transport, DeliveryTransport):
                    raise TransportError(
                        "MQTT transport does not support delivery completion"
                    )
                self._transport.publish_and_wait(
                    Publish(
                        self._topics.bridge_availability(),
                        b"offline",
                        qos=_QOS,
                        retain=True,
                    ),
                    timeout=_SHUTDOWN_DELIVERY_TIMEOUT_SECONDS,
                )
        finally:
            try:
                self._backend.close()
            finally:
                self._transport.disconnect()

    @staticmethod
    def _find_capability_modes(
        devices: tuple[RuntimeDevice, ...],
    ) -> dict[CapabilityAddress, CapabilityMode]:
        capabilities: dict[CapabilityAddress, CapabilityMode] = {}
        for item in devices:
            for capability in item.capabilities:
                if capability.capability is not CapabilityId.ON_OFF:
                    continue
                address = CapabilityAddress(
                    item.device.device_id,
                    capability.endpoint,
                    capability.capability,
                )
                capabilities[address] = capability.mode
        return capabilities

    def _validate_status_topic(self) -> None:
        generated_topics = {self._topics.bridge_availability()}
        for item in self._devices:
            device_id = item.device.device_id
            generated_topics.add(self._topics.discovery(device_id))
            generated_topics.add(self._topics.device_availability(device_id))
        for address in self._writable_addresses:
            generated_topics.add(self._topics.command(address))
            if self._capability_modes[address] is CapabilityMode.OBSERVABLE:
                generated_topics.update(
                    {
                        self._topics.state(address),
                        self._topics.confidence(address),
                    }
                )
            else:
                generated_topics.add(self._topics.last_command(address))
        if self._home_assistant_status_topic in generated_topics:
            raise ValidationError(
                "home assistant status topic conflicts with a generated bridge topic"
            )

    def _subscribe(self) -> None:
        for address in self._writable_addresses:
            self._transport.subscribe(self._topics.command(address), qos=_QOS)
        self._transport.subscribe(
            self._home_assistant_status_topic,
            qos=_QOS,
        )

    def _publish_snapshot(self) -> None:
        self._transport.publish(
            Publish(
                self._topics.bridge_availability(),
                b"offline",
                qos=_QOS,
                retain=True,
            )
        )
        for item in self._devices:
            self._transport.publish(
                Publish(
                    self._topics.discovery(item.device.device_id),
                    discovery_document(item, self._topics),
                    qos=_QOS,
                    retain=False,
                )
            )
        for item in self._devices:
            snapshot_complete = True
            for address in self._writable_addresses:
                if address.device_id == item.device.device_id:
                    if self._capability_modes[address] is CapabilityMode.COMMAND_ONLY:
                        continue
                    observation = self._backend.observation(address)
                    if observation is None or not self._publish_observation(
                        address, observation
                    ):
                        snapshot_complete = False
            availability = self._backend.availability(item.device.device_id)
            availability_state = availability.state
            if not snapshot_complete:
                availability_state = AvailabilityState.OFFLINE
            self._transport.publish(
                Publish(
                    self._topics.device_availability(item.device.device_id),
                    availability_state.value.encode("ascii"),
                    qos=_QOS,
                    retain=True,
                )
            )
            self._published_availability[item.device.device_id] = availability_state
        self._transport.publish(
            Publish(
                self._topics.bridge_availability(),
                b"online",
                qos=_QOS,
                retain=True,
            )
        )

    def _handle_message(self, message: Message) -> None:
        if message.topic == self._home_assistant_status_topic:
            if message.payload == b"online":
                self._publish_snapshot()
            return
        if message.retain:
            _LOGGER.warning("ignored retained command topic=%s", message.topic)
            return
        if message.qos != _QOS:
            _LOGGER.warning("ignored command with invalid qos topic=%s", message.topic)
            return
        address = self._topics.parse_command_topic(message.topic)
        if address is None or address not in self._writable_addresses:
            _LOGGER.warning(
                "ignored unknown or non-writable command topic=%s", message.topic
            )
            return
        if (
            self._backend.availability(address.device_id).state
            is not AvailabilityState.ONLINE
        ):
            _LOGGER.warning(
                "ignored command for unavailable device=%s", address.device_id
            )
            return
        try:
            desired = decode_on_off(message.payload)
        except ValidationError:
            _LOGGER.warning("ignored malformed command topic=%s", message.topic)
            return
        command: Command[WireValue] = Command(
            request_id=self._request_id_factory(),
            device_id=address.device_id,
            endpoint=address.endpoint,
            capability=address.capability,
            desired=desired,
        )
        result = self._backend.set_desired(command)
        _LOGGER.info(
            "backend command device=%s endpoint=%s capability=%s status=%s",
            address.device_id,
            address.endpoint,
            address.capability,
            result.status,
        )
        if self._capability_modes[address] is CapabilityMode.COMMAND_ONLY:
            self._transport.publish(
                Publish(
                    self._topics.last_command(address),
                    encode_last_command(desired, result.status, self._clock()),
                    qos=_QOS,
                    retain=True,
                )
            )
            self._publish_changed_availability(force_device=address.device_id)
        elif result.status is CommandStatus.CONFIRMED:
            assert result.observation is not None
            self._publish_observation(address, result.observation)

    def _publish_changed_availability(
        self, *, force_device: DeviceId | None = None
    ) -> None:
        for item in self._devices:
            device_id = item.device.device_id
            state = self._backend.availability(device_id).state
            if self._published_availability.get(device_id) is state and (
                force_device != device_id
            ):
                continue
            self._transport.publish(
                Publish(
                    self._topics.device_availability(device_id),
                    state.value.encode("ascii"),
                    qos=_QOS,
                    retain=True,
                )
            )
            self._published_availability[device_id] = state

    def _publish_observation(
        self,
        address: CapabilityAddress,
        observation: Observation[WireValue],
    ) -> bool:
        if not isinstance(observation.value, bool):
            _LOGGER.warning(
                "ignored non-boolean observation device=%s endpoint=%s capability=%s",
                address.device_id,
                address.endpoint,
                address.capability,
            )
            return False
        self._transport.publish(
            Publish(
                self._topics.state(address),
                encode_on_off(observation.value),
                qos=_QOS,
                retain=True,
            )
        )
        self._transport.publish(
            Publish(
                self._topics.confidence(address),
                observation.confidence.value.encode("ascii"),
                qos=_QOS,
                retain=True,
            )
        )
        return True
