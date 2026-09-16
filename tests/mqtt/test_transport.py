from collections.abc import Callable
from pathlib import Path
from typing import Protocol, runtime_checkable

import pytest
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
from smartenit_rescue.mqtt import (
    Connected,
    Disconnected,
    Message,
    PahoTransport,
    Publish,
    TransportError,
)

CLIENT_ID = "smartenit-rescue-test"


@runtime_checkable
class ConnectProperties(Protocol):
    SessionExpiryInterval: int


class RecordingClient:
    def __init__(self) -> None:
        self.on_connect: Callable[..., None] | None = None
        self.on_disconnect: Callable[..., None] | None = None
        self.on_message: Callable[..., None] | None = None
        self.credentials: tuple[str, str | None] | None = None
        self.tls_calls = 0
        self.will: tuple[str, bytes, int, bool] | None = None
        self.connect_call: tuple[str, int, int, bool, Properties] | None = None
        self.loop_start_calls = 0
        self.subscriptions: list[tuple[str, int]] = []
        self.publications: list[tuple[str, bytes, int, bool]] = []
        self.disconnect_calls = 0
        self.loop_stop_calls = 0
        self.connect_rc = MQTTErrorCode.MQTT_ERR_SUCCESS
        self.loop_start_rc = MQTTErrorCode.MQTT_ERR_SUCCESS
        self.subscribe_rc = MQTTErrorCode.MQTT_ERR_SUCCESS
        self.publish_rc = MQTTErrorCode.MQTT_ERR_SUCCESS
        self.complete_publishes = False
        self.publish_infos: list[MQTTMessageInfo] = []

    def username_pw_set(self, username: str, value: str | None = None) -> None:
        self.credentials = (username, value)

    def tls_set(self) -> None:
        self.tls_calls += 1

    def will_set(self, topic: str, payload: bytes, qos: int, retain: bool) -> None:
        self.will = (topic, payload, qos, retain)

    def connect(
        self,
        host: str,
        port: int,
        keepalive: int,
        *,
        clean_start: bool,
        properties: Properties,
    ) -> MQTTErrorCode:
        self.connect_call = (host, port, keepalive, clean_start, properties)
        return self.connect_rc

    def loop_start(self) -> MQTTErrorCode:
        self.loop_start_calls += 1
        return self.loop_start_rc

    def subscribe(self, topic: str, qos: int) -> tuple[MQTTErrorCode, int | None]:
        self.subscriptions.append((topic, qos))
        return self.subscribe_rc, 1

    def publish(
        self, topic: str, payload: bytes, qos: int, retain: bool
    ) -> MQTTMessageInfo:
        self.publications.append((topic, payload, qos, retain))
        info = MQTTMessageInfo(1)
        info.rc = self.publish_rc
        if self.complete_publishes:
            info._set_as_published()
        self.publish_infos.append(info)
        return info

    def disconnect(self) -> MQTTErrorCode:
        self.disconnect_calls += 1
        return MQTTErrorCode.MQTT_ERR_SUCCESS

    def loop_stop(self) -> MQTTErrorCode:
        self.loop_stop_calls += 1
        return MQTTErrorCode.MQTT_ERR_SUCCESS


class RecordingFactory:
    def __init__(self) -> None:
        self.client = RecordingClient()
        self.arguments: dict[str, object] | None = None

    def __call__(self, **kwargs: object) -> object:
        self.arguments = kwargs
        return self.client


class IncomingMessage:
    topic = "smartenit-rescue/v1/0200000000000001/1/on_off/command"
    payload = b"ON"
    qos = 1
    retain = False


def settings(*, tls: bool = True) -> MqttSettings:
    return MqttSettings(
        "mqtt.local",
        8883,
        45,
        tls,
        Path("/not-read/username"),
        Path("/not-read/password"),
    )


def will() -> Publish:
    return Publish(
        topic="smartenit-rescue/v1/bridge/availability",
        payload=b"offline",
        qos=1,
        retain=True,
    )


def test_transport_error_is_an_expected_rescue_failure() -> None:
    assert issubclass(TransportError, RescueError)


def test_transport_configures_paho_v5_credentials_tls_and_lwt() -> None:
    factory = RecordingFactory()
    credentials = MqttCredentials("mqtt-user", "mqtt-secret")

    transport = PahoTransport(
        settings(), credentials, will(), CLIENT_ID, client_factory=factory
    )
    transport.connect()

    assert factory.arguments == {
        "callback_api_version": CallbackAPIVersion.VERSION2,
        "client_id": CLIENT_ID,
        "protocol": MQTTProtocolVersion.MQTTv5,
    }
    assert factory.client.credentials == ("mqtt-user", "mqtt-secret")
    assert factory.client.tls_calls == 1
    assert factory.client.will == (
        "smartenit-rescue/v1/bridge/availability",
        b"offline",
        1,
        True,
    )
    assert factory.client.connect_call is not None
    host, port, keepalive, clean_start, properties = factory.client.connect_call
    assert (host, port, keepalive, clean_start) == (
        "mqtt.local",
        8883,
        45,
        True,
    )
    assert isinstance(properties, ConnectProperties)
    assert properties.SessionExpiryInterval == 0
    assert factory.client.loop_start_calls == 1


def test_transport_forwards_subscription_and_publish_wire_settings() -> None:
    factory = RecordingFactory()
    transport = PahoTransport(
        settings(tls=False), None, will(), CLIENT_ID, client_factory=factory
    )
    publication = Publish(
        topic="smartenit-rescue/v1/0200000000000001/1/on_off/state",
        payload=b"OFF",
        qos=1,
        retain=False,
    )

    transport.subscribe("smartenit-rescue/v1/0200000000000001/1/on_off/command", qos=1)
    transport.publish(publication)

    assert factory.client.credentials is None
    assert factory.client.tls_calls == 0
    assert factory.client.subscriptions == [
        (
            "smartenit-rescue/v1/0200000000000001/1/on_off/command",
            1,
        )
    ]
    assert factory.client.publications == [
        (
            "smartenit-rescue/v1/0200000000000001/1/on_off/state",
            b"OFF",
            1,
            False,
        )
    ]


def test_connect_raises_bounded_error_for_immediate_paho_failure() -> None:
    factory = RecordingFactory()
    factory.client.connect_rc = MQTTErrorCode.MQTT_ERR_NO_CONN
    transport = PahoTransport(
        settings(tls=False), None, will(), CLIENT_ID, client_factory=factory
    )

    with pytest.raises(TransportError) as error:
        transport.connect()

    assert str(error.value) == (
        "MQTT connect failed: The client is not currently connected."
    )
    assert factory.client.loop_start_calls == 0


def test_connect_raises_bounded_error_when_network_loop_cannot_start() -> None:
    factory = RecordingFactory()
    factory.client.loop_start_rc = MQTTErrorCode.MQTT_ERR_NO_CONN
    transport = PahoTransport(
        settings(tls=False), None, will(), CLIENT_ID, client_factory=factory
    )

    with pytest.raises(TransportError) as error:
        transport.connect()

    assert str(error.value) == (
        "MQTT loop start failed: The client is not currently connected."
    )


def test_subscribe_raises_bounded_error_for_immediate_paho_failure() -> None:
    factory = RecordingFactory()
    factory.client.subscribe_rc = MQTTErrorCode.MQTT_ERR_NO_CONN
    transport = PahoTransport(
        settings(tls=False), None, will(), CLIENT_ID, client_factory=factory
    )

    with pytest.raises(TransportError) as error:
        transport.subscribe(
            "smartenit-rescue/v1/0200000000000001/1/on_off/command", qos=1
        )

    assert str(error.value) == (
        "MQTT subscribe failed: The client is not currently connected."
    )


def test_publish_raises_bounded_error_for_immediate_paho_failure() -> None:
    factory = RecordingFactory()
    factory.client.publish_rc = MQTTErrorCode.MQTT_ERR_NO_CONN
    transport = PahoTransport(
        settings(tls=False), None, will(), CLIENT_ID, client_factory=factory
    )

    with pytest.raises(TransportError) as error:
        transport.publish(
            Publish(
                topic="smartenit-rescue/v1/0200000000000001/1/on_off/state",
                payload=b"OFF",
                qos=1,
                retain=False,
            )
        )

    assert str(error.value) == (
        "MQTT publish failed: The client is not currently connected."
    )


def test_publish_and_wait_returns_after_delivery_completion() -> None:
    factory = RecordingFactory()
    factory.client.complete_publishes = True
    transport = PahoTransport(
        settings(tls=False), None, will(), CLIENT_ID, client_factory=factory
    )
    publication = Publish(
        topic="smartenit-rescue/v1/bridge/availability",
        payload=b"offline",
        qos=1,
        retain=True,
    )

    transport.publish_and_wait(publication, timeout=0.05)

    assert factory.client.publications == [
        (
            "smartenit-rescue/v1/bridge/availability",
            b"offline",
            1,
            True,
        )
    ]
    assert factory.client.publish_infos[0].is_published()


def test_publish_and_wait_times_out_when_delivery_is_withheld() -> None:
    factory = RecordingFactory()
    transport = PahoTransport(
        settings(tls=False), None, will(), CLIENT_ID, client_factory=factory
    )

    with pytest.raises(TransportError, match="^MQTT publish delivery timed out$"):
        transport.publish_and_wait(will(), timeout=0.001)

    assert not factory.client.publish_infos[0].is_published()


def test_callbacks_enqueue_typed_events_without_invoking_a_backend() -> None:
    factory = RecordingFactory()
    transport = PahoTransport(
        settings(tls=False), None, will(), CLIENT_ID, client_factory=factory
    )

    assert factory.client.on_connect is not None
    assert factory.client.on_message is not None
    assert factory.client.on_disconnect is not None
    factory.client.on_connect(
        factory.client,
        None,
        ConnectFlags(session_present=False),
        ReasonCode(PacketTypes.CONNACK, "Success"),
        None,
    )
    factory.client.on_message(factory.client, None, IncomingMessage())
    factory.client.on_disconnect(
        factory.client,
        None,
        DisconnectFlags(is_disconnect_packet_from_server=False),
        ReasonCode(PacketTypes.DISCONNECT),
        None,
    )

    assert transport.next_event(timeout=0.01) == Connected()
    assert transport.next_event(timeout=0.01) == Message(
        topic=IncomingMessage.topic,
        payload=b"ON",
        qos=1,
        retain=False,
    )
    assert transport.next_event(timeout=0.01) == Disconnected()
    assert transport.next_event(timeout=0.0) is None


def test_rejected_connack_enqueues_bounded_failure_instead_of_connected() -> None:
    factory = RecordingFactory()
    transport = PahoTransport(
        settings(tls=False), None, will(), CLIENT_ID, client_factory=factory
    )

    assert factory.client.on_connect is not None
    factory.client.on_connect(
        factory.client,
        None,
        ConnectFlags(session_present=False),
        ReasonCode(PacketTypes.CONNACK, "Not authorized"),
        None,
    )

    event = transport.next_event(timeout=0.01)
    assert isinstance(event, Disconnected)
    assert event.reason == "MQTT connection refused: Not authorized"
    assert transport.next_event(timeout=0.0) is None


@pytest.mark.parametrize("reason", ["Not authorized", "Bad authentication method"])
def test_broker_auth_disconnect_enqueues_bounded_failure(reason: str) -> None:
    factory = RecordingFactory()
    transport = PahoTransport(
        settings(tls=False), None, will(), CLIENT_ID, client_factory=factory
    )

    assert factory.client.on_disconnect is not None
    factory.client.on_disconnect(
        factory.client,
        None,
        DisconnectFlags(is_disconnect_packet_from_server=True),
        ReasonCode(PacketTypes.DISCONNECT, reason),
        None,
    )

    assert transport.next_event(timeout=0.01) == Disconnected(
        reason=f"MQTT disconnected: {reason}"
    )


@pytest.mark.parametrize("reason", ["Unspecified error", "Server shutting down"])
def test_transient_disconnect_remains_retryable(reason: str) -> None:
    factory = RecordingFactory()
    transport = PahoTransport(
        settings(tls=False), None, will(), CLIENT_ID, client_factory=factory
    )

    assert factory.client.on_disconnect is not None
    factory.client.on_disconnect(
        factory.client,
        None,
        DisconnectFlags(is_disconnect_packet_from_server=reason != "Unspecified error"),
        ReasonCode(PacketTypes.DISCONNECT, reason),
        None,
    )

    assert transport.next_event(timeout=0.01) == Disconnected()


def test_disconnect_is_idempotent() -> None:
    factory = RecordingFactory()
    transport = PahoTransport(
        settings(tls=False), None, will(), CLIENT_ID, client_factory=factory
    )

    transport.connect()
    transport.disconnect()
    transport.disconnect()

    assert factory.client.disconnect_calls == 1
    assert factory.client.loop_stop_calls == 1
