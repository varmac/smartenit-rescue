"""Broker transport boundary backed by Paho MQTT."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from queue import Empty, Queue
from typing import Protocol, TypeAlias, runtime_checkable

from paho.mqtt import client as mqtt
from paho.mqtt.client import ConnectFlags, DisconnectFlags, MQTTMessageInfo
from paho.mqtt.enums import (
    CallbackAPIVersion,
    MQTTErrorCode,
    MQTTProtocolVersion,
)
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties
from paho.mqtt.reasoncodes import ReasonCode

from smartenit_rescue.config import MqttCredentials, MqttSettings
from smartenit_rescue.errors import RescueError

_properties_factory: Callable[[int], Properties] = Properties


class TransportError(RescueError):
    """Raised when Paho rejects an immediate transport operation."""


@dataclass(frozen=True, slots=True)
class Connected:
    """The Paho client established an MQTT connection."""


@dataclass(frozen=True, slots=True)
class Disconnected:
    """The Paho client lost or closed its MQTT connection."""

    reason: str | None = None


@dataclass(frozen=True, slots=True)
class Message:
    topic: str
    payload: bytes
    qos: int
    retain: bool


TransportEvent: TypeAlias = Connected | Disconnected | Message


@dataclass(frozen=True, slots=True)
class Publish:
    topic: str
    payload: bytes
    qos: int
    retain: bool


@runtime_checkable
class Transport(Protocol):
    def connect(self) -> None: ...

    def next_event(self, timeout: float | None = None) -> TransportEvent | None: ...

    def subscribe(self, topic: str, qos: int) -> None: ...

    def publish(self, publication: Publish) -> None: ...

    def disconnect(self) -> None: ...


@runtime_checkable
class DeliveryTransport(Protocol):
    def publish_and_wait(self, publication: Publish, timeout: float) -> None: ...


@runtime_checkable
class _PahoClient(Protocol):
    on_connect: Callable[..., None] | None
    on_disconnect: Callable[..., None] | None
    on_message: Callable[..., None] | None

    def username_pw_set(self, username: str, value: str | None = None) -> object: ...

    def tls_set(self) -> object: ...

    def will_set(
        self, topic: str, payload: bytes, qos: int, retain: bool
    ) -> object: ...

    def connect(
        self,
        host: str,
        port: int,
        keepalive: int,
        *,
        clean_start: bool,
        properties: object,
    ) -> MQTTErrorCode: ...

    def loop_start(self) -> MQTTErrorCode: ...

    def subscribe(self, topic: str, qos: int) -> tuple[MQTTErrorCode, int | None]: ...

    def publish(
        self, topic: str, payload: bytes, qos: int, retain: bool
    ) -> MQTTMessageInfo: ...

    def disconnect(self) -> MQTTErrorCode: ...

    def loop_stop(self) -> MQTTErrorCode: ...


@runtime_checkable
class _IncomingMessage(Protocol):
    topic: str
    payload: bytes
    qos: int
    retain: bool


class PahoTransport:
    """Translate Paho network callbacks into queued transport events."""

    def __init__(
        self,
        settings: MqttSettings,
        credentials: MqttCredentials | None,
        will: Publish,
        client_id: str,
        *,
        client_factory: Callable[..., object] = mqtt.Client,
    ) -> None:
        self._settings = settings
        self._events: Queue[TransportEvent] = Queue()
        self._loop_started = False
        self._disconnected = False
        client = client_factory(
            callback_api_version=CallbackAPIVersion.VERSION2,
            client_id=client_id,
            protocol=MQTTProtocolVersion.MQTTv5,
        )
        if not isinstance(client, _PahoClient):
            raise TypeError("Paho client factory returned an incompatible client")
        self._client = client
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message

        if credentials is not None:
            self._client.username_pw_set(
                credentials.username,
                credentials.password,
            )
        if settings.tls:
            self._client.tls_set()
        self._client.will_set(
            will.topic,
            will.payload,
            will.qos,
            will.retain,
        )

    def connect(self) -> None:
        properties = _properties_factory(PacketTypes.CONNECT)
        properties.SessionExpiryInterval = 0
        result = self._client.connect(
            self._settings.host,
            self._settings.port,
            self._settings.keepalive_seconds,
            clean_start=True,
            properties=properties,
        )
        _raise_for_error("connect", result)
        result = self._client.loop_start()
        _raise_for_error("loop start", result)
        self._loop_started = True

    def next_event(self, timeout: float | None = None) -> TransportEvent | None:
        try:
            return self._events.get(timeout=timeout)
        except Empty:
            return None

    def subscribe(self, topic: str, qos: int) -> None:
        result, _message_id = self._client.subscribe(topic, qos)
        _raise_for_error("subscribe", result)

    def publish(self, publication: Publish) -> None:
        self._publish(publication)

    def publish_and_wait(self, publication: Publish, timeout: float) -> None:
        result = self._publish(publication)
        try:
            result.wait_for_publish(timeout)
            delivered = result.is_published()
        except (RuntimeError, ValueError) as error:
            raise TransportError("MQTT publish delivery failed") from error
        if not delivered:
            raise TransportError("MQTT publish delivery timed out")

    def _publish(self, publication: Publish) -> MQTTMessageInfo:
        result = self._client.publish(
            publication.topic,
            publication.payload,
            publication.qos,
            publication.retain,
        )
        _raise_for_error("publish", result.rc)
        return result

    def disconnect(self) -> None:
        if self._disconnected:
            return
        self._disconnected = True
        if not self._loop_started:
            return
        try:
            self._client.disconnect()
        finally:
            self._client.loop_stop()

    def _on_connect(
        self,
        client: mqtt.Client,
        userdata: object,
        flags: ConnectFlags,
        reason_code: ReasonCode,
        properties: Properties | None,
    ) -> None:
        if reason_code.is_failure:
            self._events.put(
                Disconnected(reason=f"MQTT connection refused: {reason_code}")
            )
            return
        self._events.put(Connected())

    def _on_disconnect(
        self,
        client: mqtt.Client,
        userdata: object,
        flags: DisconnectFlags,
        reason_code: ReasonCode,
        properties: Properties | None,
    ) -> None:
        if flags.is_disconnect_packet_from_server and str(reason_code) in {
            "Not authorized",
            "Bad authentication method",
        }:
            self._events.put(Disconnected(reason=f"MQTT disconnected: {reason_code}"))
            return
        self._events.put(Disconnected())

    def _on_message(self, client: object, userdata: object, message: object) -> None:
        if not isinstance(message, _IncomingMessage):
            return
        self._events.put(
            Message(
                topic=message.topic,
                payload=message.payload,
                qos=message.qos,
                retain=message.retain,
            )
        )


def _raise_for_error(operation: str, result: MQTTErrorCode) -> None:
    if result is MQTTErrorCode.MQTT_ERR_SUCCESS:
        return
    raise TransportError(f"MQTT {operation} failed: {mqtt.error_string(result)}")
