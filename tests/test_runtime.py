from __future__ import annotations

import json
from collections import deque
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from smartenit_rescue.backends import (
    Backend,
    CapabilityAddress,
    CapabilityMode,
    RuntimeCapability,
    RuntimeDevice,
    SimulatorBackend,
    WireValue,
)
from smartenit_rescue.config import SimulatorDeviceSettings
from smartenit_rescue.errors import ValidationError
from smartenit_rescue.models import (
    AvailabilityState,
    CapabilityId,
    Command,
    CommandResult,
    CommandStatus,
    Confidence,
    DeviceAvailability,
    DeviceId,
    EndpointId,
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
    TransportEvent,
    discovery_document,
)
from smartenit_rescue.profiles import iter_builtin_profiles
from smartenit_rescue.runtime import BridgeRuntime

NOW = datetime(2026, 9, 15, 12, tzinfo=UTC)
DEVICE_ID = DeviceId.parse("0200000000000001")
UNKNOWN_DEVICE_ID = DeviceId.parse("02000000000000ff")
ENDPOINT = EndpointId(1)
ADDRESS = CapabilityAddress(DEVICE_ID, ENDPOINT, CapabilityId.ON_OFF)
COMMAND_TOPIC = "rescue/v1/0200000000000001/1/on_off/command"
HA_STATUS_TOPIC = "homeassistant/status"


def fixed_clock() -> datetime:
    return NOW


class FakeTransport:
    def __init__(self, events: list[TransportEvent]) -> None:
        self.events = deque(events)
        self.subscriptions: list[tuple[str, int]] = []
        self.publications: list[Publish] = []
        self.operations: list[str] = []
        self.connect_error: TransportError | None = None
        self.delivery_error: TransportError | None = None
        self.delivery_timeouts: list[float] = []

    @property
    def exhausted(self) -> bool:
        return not self.events

    def connect(self) -> None:
        self.operations.append("connect")
        if self.connect_error is not None:
            raise self.connect_error

    def next_event(self, timeout: float | None = None) -> TransportEvent | None:
        del timeout
        if not self.events:
            return None
        return self.events.popleft()

    def subscribe(self, topic: str, qos: int) -> None:
        self.operations.append(f"subscribe:{topic}")
        self.subscriptions.append((topic, qos))

    def publish(self, publication: Publish) -> None:
        self.operations.append(
            f"publish:{publication.topic}:{publication.payload.decode()}"
        )
        self.publications.append(publication)

    def publish_and_wait(self, publication: Publish, timeout: float) -> None:
        self.publish(publication)
        self.operations.append("wait-for-delivery")
        self.delivery_timeouts.append(timeout)
        if self.delivery_error is not None:
            raise self.delivery_error

    def disconnect(self) -> None:
        self.operations.append("disconnect")


class RecordingBackend:
    name = "recording"

    def __init__(
        self,
        *,
        command_status: CommandStatus = CommandStatus.CONFIRMED,
        available: bool = True,
        writable: bool = True,
        operations: list[str] | None = None,
    ) -> None:
        settings = SimulatorDeviceSettings(
            device_id=DEVICE_ID,
            name="Synthetic Heater",
            profile_id="smartenit.4040c",
            initial_on=False,
        )
        self._delegate = SimulatorBackend(
            (settings,), iter_builtin_profiles(), fixed_clock
        )
        self._command_status = command_status
        self._available = available
        self._writable = writable
        self._operations = operations
        self.commands: list[Command[WireValue]] = []
        self.close_calls = 0

    def devices(self) -> tuple[RuntimeDevice, ...]:
        devices = self._delegate.devices()
        if self._writable:
            return devices
        device = devices[0]
        profile = device.profile
        endpoint = profile.endpoints[ENDPOINT]
        capabilities = tuple(
            replace(item, writable=False)
            if item.capability is CapabilityId.ON_OFF
            else item
            for item in endpoint.capabilities
        )
        updated_endpoint = replace(endpoint, capabilities=capabilities)
        updated_profile = replace(profile, endpoints={ENDPOINT: updated_endpoint})
        return (replace(device, profile=updated_profile, capabilities=()),)

    def observation(self, address: CapabilityAddress) -> Observation[WireValue] | None:
        return self._delegate.observation(address)

    def set_desired(self, command: Command[WireValue]) -> CommandResult[WireValue]:
        self.commands.append(command)
        if self._command_status is CommandStatus.CONFIRMED:
            return self._delegate.set_desired(command)
        if self._command_status is CommandStatus.INDETERMINATE:
            return CommandResult(
                request_id=command.request_id,
                status=CommandStatus.INDETERMINATE,
                reason="outcome unknown",
            )
        if self._command_status is CommandStatus.REJECTED:
            return CommandResult(
                request_id=command.request_id,
                status=CommandStatus.REJECTED,
                reason="command rejected",
            )
        return CommandResult(
            request_id=command.request_id,
            status=self._command_status,
        )

    def availability(self, device_id: DeviceId) -> DeviceAvailability:
        availability = self._delegate.availability(device_id)
        if self._available:
            return availability
        return replace(availability, state=AvailabilityState.OFFLINE)

    def close(self) -> None:
        self.close_calls += 1
        if self._operations is not None:
            self._operations.append("backend-close")
        self._delegate.close()


class FixedObservationBackend(RecordingBackend):
    def __init__(self, observation: Observation[WireValue] | None) -> None:
        super().__init__()
        self._observation = observation

    def observation(self, address: CapabilityAddress) -> Observation[WireValue] | None:
        del address
        return self._observation


class CommandOnlyBackend(RecordingBackend):
    def devices(self) -> tuple[RuntimeDevice, ...]:
        device = super().devices()[0]
        return (
            replace(
                device,
                capabilities=(
                    RuntimeCapability(
                        endpoint=ENDPOINT,
                        capability=CapabilityId.ON_OFF,
                        mode=CapabilityMode.COMMAND_ONLY,
                    ),
                ),
            ),
        )

    def observation(self, address: CapabilityAddress) -> Observation[WireValue] | None:
        del address
        raise AssertionError("command-only snapshot requested an observation")


class EndpointTwoCommandOnlyBackend(CommandOnlyBackend):
    def devices(self) -> tuple[RuntimeDevice, ...]:
        device = super().devices()[0]
        endpoint = EndpointId(2)
        capability = replace(device.capabilities[0], endpoint=endpoint)
        endpoint_profile = replace(
            device.profile.endpoints[ENDPOINT], endpoint=endpoint
        )
        profile = replace(device.profile, endpoints={endpoint: endpoint_profile})
        return (replace(device, profile=profile, capabilities=(capability,)),)

    def set_desired(self, command: Command[WireValue]) -> CommandResult[WireValue]:
        self.commands.append(command)
        return CommandResult(
            request_id=command.request_id, status=CommandStatus.ACCEPTED
        )


class RecoveringCommandOnlyBackend(CommandOnlyBackend):
    def __init__(self, *, can_recover: bool = False) -> None:
        super().__init__()
        self._state = AvailabilityState.ONLINE
        self._can_recover = can_recover
        self.refresh_calls = 0

    def devices(self) -> tuple[RuntimeDevice, ...]:
        first = super().devices()[0]
        second = replace(
            first,
            device=replace(first.device, device_id=UNKNOWN_DEVICE_ID),
            name="Second synthetic load",
        )
        return first, second

    def availability(self, device_id: DeviceId) -> DeviceAvailability:
        return DeviceAvailability(device_id, self._state, NOW, self.name)

    def set_desired(self, command: Command[WireValue]) -> CommandResult[WireValue]:
        self.commands.append(command)
        self._state = AvailabilityState.OFFLINE
        return CommandResult(
            request_id=command.request_id,
            status=CommandStatus.INDETERMINATE,
            reason="outcome unknown",
        )

    def refresh(self) -> None:
        self.refresh_calls += 1
        if self._can_recover:
            self._state = AvailabilityState.ONLINE


def request_ids() -> Callable[[], str]:
    values = iter(("request-1", "request-2", "request-3"))
    return lambda: next(values)


def make_runtime(
    events: list[TransportEvent],
    *,
    backend: RecordingBackend | None = None,
) -> tuple[BridgeRuntime, RecordingBackend, FakeTransport]:
    selected_backend = backend or RecordingBackend()
    transport = FakeTransport(events)
    topics = TopicLayout("rescue/v1", "homeassistant", HA_STATUS_TOPIC)
    runtime = BridgeRuntime(
        selected_backend,
        transport,
        topics,
        HA_STATUS_TOPIC,
        request_ids(),
        fixed_clock,
    )
    assert isinstance(selected_backend, Backend)
    assert isinstance(transport, Transport)
    return runtime, selected_backend, transport


def run_until_events_exhausted(
    runtime: BridgeRuntime, transport: FakeTransport
) -> None:
    checks = 0

    def stop_before_wait() -> bool:
        nonlocal checks
        checks += 1
        is_pre_wait_check = checks % 2 == 1
        return is_pre_wait_check and transport.exhausted

    runtime.run(stop_before_wait)


@pytest.mark.parametrize(
    "status_topic",
    [COMMAND_TOPIC, "rescue/v1/bridge/availability"],
    ids=["command-topic", "bridge-availability-topic"],
)
def test_status_topic_collision_is_rejected_before_transport_side_effects(
    status_topic: str,
) -> None:
    backend = RecordingBackend()
    transport = FakeTransport([Connected()])
    topics = TopicLayout("rescue/v1", "homeassistant", status_topic)

    with pytest.raises(ValidationError, match="status topic conflicts"):
        BridgeRuntime(
            backend,
            transport,
            topics,
            status_topic,
            request_ids(),
            fixed_clock,
        )

    assert transport.operations == []
    assert transport.publications == []
    assert backend.commands == []


def test_connect_subscribes_and_republishes_without_backend_command() -> None:
    runtime, backend, transport = make_runtime([Connected()])

    run_until_events_exhausted(runtime, transport)

    assert transport.subscriptions == [
        (COMMAND_TOPIC, 1),
        (HA_STATUS_TOPIC, 1),
    ]
    assert [
        (item.topic, item.payload, item.qos, item.retain)
        for item in transport.publications
    ] == [
        ("rescue/v1/bridge/availability", b"offline", 1, True),
        (
            "homeassistant/device/smartenit_rescue_0200000000000001/config",
            transport.publications[1].payload,
            1,
            False,
        ),
        (
            "rescue/v1/0200000000000001/1/on_off/state",
            b"OFF",
            1,
            True,
        ),
        (
            "rescue/v1/0200000000000001/1/on_off/confidence",
            b"observed",
            1,
            True,
        ),
        (
            "rescue/v1/0200000000000001/availability",
            b"online",
            1,
            True,
        ),
        ("rescue/v1/bridge/availability", b"online", 1, True),
    ]
    assert transport.publications[1].payload.startswith(b'{"components":')
    assert backend.commands == []


def test_snapshot_keeps_availability_offline_until_observation_is_refreshed() -> None:
    runtime, _backend, transport = make_runtime([Connected()])

    run_until_events_exhausted(runtime, transport)

    assert [(item.topic, item.qos, item.retain) for item in transport.publications] == [
        ("rescue/v1/bridge/availability", 1, True),
        (
            "homeassistant/device/smartenit_rescue_0200000000000001/config",
            1,
            False,
        ),
        ("rescue/v1/0200000000000001/1/on_off/state", 1, True),
        ("rescue/v1/0200000000000001/1/on_off/confidence", 1, True),
        ("rescue/v1/0200000000000001/availability", 1, True),
        ("rescue/v1/bridge/availability", 1, True),
    ]
    assert [
        item.payload
        for item in transport.publications
        if not item.topic.startswith("homeassistant/device/")
    ] == [
        b"offline",
        b"OFF",
        b"observed",
        b"online",
        b"online",
    ]


@pytest.mark.parametrize(
    "observation",
    [
        None,
        Observation(
            value=1,
            source="test",
            confidence=Confidence.OBSERVED,
            observed_at=NOW,
            sequence=1,
        ),
    ],
    ids=["missing", "non-boolean"],
)
def test_incomplete_snapshot_keeps_stale_retained_observation_unavailable(
    observation: Observation[WireValue] | None,
) -> None:
    fixed_backend = FixedObservationBackend(observation)
    runtime, backend, transport = make_runtime([Connected()], backend=fixed_backend)
    stale_publications = [
        Publish(
            "rescue/v1/0200000000000001/1/on_off/state",
            b"ON",
            qos=1,
            retain=True,
        ),
        Publish(
            "rescue/v1/0200000000000001/1/on_off/confidence",
            b"observed",
            qos=1,
            retain=True,
        ),
        Publish(
            "rescue/v1/0200000000000001/availability",
            b"online",
            qos=1,
            retain=True,
        ),
        Publish(
            "rescue/v1/bridge/availability",
            b"online",
            qos=1,
            retain=True,
        ),
    ]
    transport.publications.extend(stale_publications)

    run_until_events_exhausted(runtime, transport)

    snapshot = transport.publications[len(stale_publications) :]
    assert [(item.topic, item.qos, item.retain) for item in snapshot] == [
        ("rescue/v1/bridge/availability", 1, True),
        (
            "homeassistant/device/smartenit_rescue_0200000000000001/config",
            1,
            False,
        ),
        ("rescue/v1/0200000000000001/availability", 1, True),
        ("rescue/v1/bridge/availability", 1, True),
    ]
    assert [
        (item.topic, item.payload)
        for item in snapshot
        if not item.topic.startswith("homeassistant/device/")
    ] == [
        ("rescue/v1/bridge/availability", b"offline"),
        ("rescue/v1/0200000000000001/availability", b"offline"),
        ("rescue/v1/bridge/availability", b"online"),
    ]
    retained = {item.topic: item.payload for item in stale_publications}
    retained.update((item.topic, item.payload) for item in snapshot if item.retain)
    assert retained == {
        "rescue/v1/0200000000000001/1/on_off/state": b"ON",
        "rescue/v1/0200000000000001/1/on_off/confidence": b"observed",
        "rescue/v1/0200000000000001/availability": b"offline",
        "rescue/v1/bridge/availability": b"online",
    }
    assert backend.commands == []


def test_confirmed_command_replaces_retained_observation_at_qos_one() -> None:
    runtime, backend, transport = make_runtime(
        [
            Connected(),
            Message(COMMAND_TOPIC, b"ON", qos=1, retain=False),
        ]
    )

    run_until_events_exhausted(runtime, transport)

    assert len(backend.commands) == 1
    assert [
        (item.topic, item.payload, item.qos, item.retain)
        for item in transport.publications[-2:]
    ] == [
        (
            "rescue/v1/0200000000000001/1/on_off/state",
            b"ON",
            1,
            True,
        ),
        (
            "rescue/v1/0200000000000001/1/on_off/confidence",
            b"observed",
            1,
            True,
        ),
    ]


def test_reconnect_repeats_subscriptions_and_publications_without_command() -> None:
    runtime, backend, transport = make_runtime(
        [Connected(), Disconnected(), Connected()]
    )

    run_until_events_exhausted(runtime, transport)

    assert transport.subscriptions == [
        (COMMAND_TOPIC, 1),
        (HA_STATUS_TOPIC, 1),
        (COMMAND_TOPIC, 1),
        (HA_STATUS_TOPIC, 1),
    ]
    assert transport.publications[6:] == transport.publications[:6]
    assert backend.commands == []


def test_home_assistant_online_republishes_without_resubscribing_or_command() -> None:
    runtime, backend, transport = make_runtime(
        [
            Connected(),
            Message(HA_STATUS_TOPIC, b"offline", qos=1, retain=True),
            Message(HA_STATUS_TOPIC, b"online", qos=1, retain=True),
        ]
    )

    run_until_events_exhausted(runtime, transport)

    assert len(transport.subscriptions) == 2
    assert transport.publications[6:] == transport.publications[:6]
    assert backend.commands == []


def test_exact_on_and_off_each_submit_once_and_publish_returned_observation() -> None:
    runtime, backend, transport = make_runtime(
        [
            Connected(),
            Message(COMMAND_TOPIC, b"ON", qos=1, retain=False),
            Message(COMMAND_TOPIC, b"OFF", qos=1, retain=False),
        ]
    )

    run_until_events_exhausted(runtime, transport)

    assert [command.desired for command in backend.commands] == [True, False]
    assert [command.request_id for command in backend.commands] == [
        "request-1",
        "request-2",
    ]
    assert [
        (item.topic, item.payload, item.qos, item.retain)
        for item in transport.publications[6:]
    ] == [
        (
            "rescue/v1/0200000000000001/1/on_off/state",
            b"ON",
            1,
            True,
        ),
        (
            "rescue/v1/0200000000000001/1/on_off/confidence",
            b"observed",
            1,
            True,
        ),
        (
            "rescue/v1/0200000000000001/1/on_off/state",
            b"OFF",
            1,
            True,
        ),
        (
            "rescue/v1/0200000000000001/1/on_off/confidence",
            b"observed",
            1,
            True,
        ),
    ]


def test_discovery_qos_command_is_accepted_by_runtime() -> None:
    backend = RecordingBackend()
    topics = TopicLayout("rescue/v1", "homeassistant", HA_STATUS_TOPIC)
    document = json.loads(discovery_document(backend.devices()[0], topics))
    component = document["components"]["smartenit_rescue_0200000000000001_1_on_off"]
    command_topic = component["command_topic"]
    qos = component["qos"]
    assert isinstance(command_topic, str)
    assert qos == 1
    transport = FakeTransport(
        [
            Connected(),
            Message(
                command_topic,
                b"ON",
                qos=qos,
                retain=False,
            ),
        ]
    )
    runtime = BridgeRuntime(
        backend,
        transport,
        topics,
        HA_STATUS_TOPIC,
        request_ids(),
        fixed_clock,
    )

    run_until_events_exhausted(runtime, transport)

    assert [command.desired for command in backend.commands] == [True]


@pytest.mark.parametrize(
    "status", [CommandStatus.ACCEPTED, CommandStatus.INDETERMINATE]
)
def test_unconfirmed_results_do_not_publish_invented_state(
    status: CommandStatus,
) -> None:
    backend = RecordingBackend(command_status=status)
    runtime, backend, transport = make_runtime(
        [
            Connected(),
            Message(COMMAND_TOPIC, b"ON", qos=1, retain=False),
        ],
        backend=backend,
    )

    run_until_events_exhausted(runtime, transport)

    assert len(backend.commands) == 1
    assert len(transport.publications) == 6


def test_command_only_snapshot_never_requests_or_publishes_state() -> None:
    backend = CommandOnlyBackend()
    runtime, selected_backend, transport = make_runtime([Connected()], backend=backend)

    run_until_events_exhausted(runtime, transport)

    assert [
        (item.topic, item.payload, item.qos, item.retain)
        for item in transport.publications
    ] == [
        ("rescue/v1/bridge/availability", b"offline", 1, True),
        (
            "homeassistant/device/smartenit_rescue_0200000000000001/config",
            transport.publications[1].payload,
            1,
            False,
        ),
        (
            "rescue/v1/0200000000000001/availability",
            b"online",
            1,
            True,
        ),
        ("rescue/v1/bridge/availability", b"online", 1, True),
    ]
    assert b'"platform":"button"' in transport.publications[1].payload
    assert b'"platform":"switch"' not in transport.publications[1].payload
    assert selected_backend.commands == []


def test_command_only_endpoint_two_routes_subscription_to_backend() -> None:
    backend = EndpointTwoCommandOnlyBackend()
    address = CapabilityAddress(DEVICE_ID, EndpointId(2), CapabilityId.ON_OFF)
    topics = TopicLayout("rescue/v1", "homeassistant", HA_STATUS_TOPIC)
    command_topic = topics.command(address)
    runtime, selected_backend, transport = make_runtime(
        [Connected(), Message(command_topic, b"ON", qos=1, retain=False)],
        backend=backend,
    )

    run_until_events_exhausted(runtime, transport)

    assert (command_topic, 1) in transport.subscriptions
    assert len(selected_backend.commands) == 1
    assert selected_backend.commands[0].endpoint == EndpointId(2)


@pytest.mark.parametrize(
    "status",
    [
        CommandStatus.ACCEPTED,
        CommandStatus.REJECTED,
        CommandStatus.INDETERMINATE,
    ],
)
def test_command_only_result_publishes_diagnostic_and_current_availability(
    status: CommandStatus,
) -> None:
    backend = CommandOnlyBackend(command_status=status)
    runtime, selected_backend, transport = make_runtime(
        [Connected(), Message(COMMAND_TOPIC, b"ON", qos=1, retain=False)],
        backend=backend,
    )

    run_until_events_exhausted(runtime, transport)

    assert len(selected_backend.commands) == 1
    expected_payload = (
        b'{"action":"ON","at":"2026-09-15T12:00:00Z",'
        + f'"status":"{status.value}"}}'.encode()
    )
    assert [
        (item.topic, item.payload, item.qos, item.retain)
        for item in transport.publications[-2:]
    ] == [
        (
            "rescue/v1/0200000000000001/1/on_off/last-command",
            expected_payload,
            1,
            True,
        ),
        (
            "rescue/v1/0200000000000001/availability",
            b"online",
            1,
            True,
        ),
    ]
    assert all(
        not item.topic.endswith(("/state", "/confidence"))
        for item in transport.publications
    )


def test_command_only_reconnect_republishes_without_command() -> None:
    backend = CommandOnlyBackend()
    runtime, selected_backend, transport = make_runtime(
        [Connected(), Disconnected(), Connected()], backend=backend
    )

    run_until_events_exhausted(runtime, transport)

    assert transport.publications[4:] == transport.publications[:4]
    assert selected_backend.commands == []


def test_command_only_failure_publishes_all_affected_device_availability() -> None:
    backend = RecoveringCommandOnlyBackend()
    runtime, selected_backend, transport = make_runtime(
        [Connected(), Message(COMMAND_TOPIC, b"ON", qos=1, retain=False)],
        backend=backend,
    )

    run_until_events_exhausted(runtime, transport)

    assert len(selected_backend.commands) == 1
    assert [
        item.payload
        for item in transport.publications
        if item.topic == "rescue/v1/02000000000000ff/availability"
    ] == [b"online", b"offline"]


def test_command_only_refresh_recovers_during_busy_message_stream_without_replay() -> (
    None
):
    backend = RecoveringCommandOnlyBackend(can_recover=True)
    runtime, selected_backend, transport = make_runtime(
        [
            Connected(),
            Message(COMMAND_TOPIC, b"ON", qos=1, retain=False),
            Message(HA_STATUS_TOPIC, b"offline", qos=1, retain=False),
        ],
        backend=backend,
    )

    run_until_events_exhausted(runtime, transport)

    assert backend.refresh_calls >= 2
    assert len(selected_backend.commands) == 1
    assert [
        item.payload
        for item in transport.publications
        if item.topic == "rescue/v1/0200000000000001/availability"
    ] == [b"online", b"offline", b"online"]
    assert [
        item.payload
        for item in transport.publications
        if item.topic == "rescue/v1/02000000000000ff/availability"
    ] == [b"online", b"offline", b"online"]


def test_command_only_last_command_topic_cannot_be_ha_status_topic() -> None:
    backend = CommandOnlyBackend()
    status_topic = "rescue/v1/0200000000000001/1/on_off/last-command"
    topics = TopicLayout("rescue/v1", "homeassistant", status_topic)

    with pytest.raises(ValidationError, match="status topic conflicts"):
        BridgeRuntime(
            backend,
            FakeTransport([]),
            topics,
            status_topic,
            request_ids(),
            fixed_clock,
        )


@pytest.mark.parametrize(
    ("event", "backend", "expected_publications"),
    [
        (Message(COMMAND_TOPIC, b"TOGGLE", qos=1, retain=False), None, 6),
        (Message(COMMAND_TOPIC, b"ON", qos=1, retain=True), None, 6),
        (
            Message(
                "rescue/v1/02000000000000ff/1/on_off/command",
                b"ON",
                qos=1,
                retain=False,
            ),
            None,
            6,
        ),
        (Message(COMMAND_TOPIC, b"ON", qos=0, retain=False), None, 6),
        (
            Message(COMMAND_TOPIC, b"ON", qos=1, retain=False),
            RecordingBackend(writable=False),
            4,
        ),
        (
            Message(COMMAND_TOPIC, b"ON", qos=1, retain=False),
            RecordingBackend(available=False),
            6,
        ),
    ],
    ids=[
        "malformed-payload",
        "retained-command",
        "unknown-device",
        "wrong-qos",
        "non-writable-capability",
        "unavailable-device",
    ],
)
def test_invalid_commands_do_not_reach_backend(
    event: Message,
    backend: RecordingBackend | None,
    expected_publications: int,
) -> None:
    runtime, selected_backend, transport = make_runtime(
        [Connected(), event], backend=backend
    )

    run_until_events_exhausted(runtime, transport)

    assert selected_backend.commands == []
    assert len(transport.publications) == expected_publications


def test_repeated_qos_delivery_submits_once_per_delivery_without_retry() -> None:
    event = Message(COMMAND_TOPIC, b"ON", qos=1, retain=False)
    runtime, backend, transport = make_runtime([Connected(), event, event])

    run_until_events_exhausted(runtime, transport)

    assert [command.desired for command in backend.commands] == [True, True]
    assert [command.request_id for command in backend.commands] == [
        "request-1",
        "request-2",
    ]


def test_disconnected_event_does_not_issue_command_or_publish() -> None:
    runtime, backend, transport = make_runtime([Disconnected()])

    run_until_events_exhausted(runtime, transport)

    assert backend.commands == []
    assert transport.subscriptions == []
    assert transport.publications == []


def test_stop_requested_after_event_wait_discards_returned_command() -> None:
    runtime, backend, transport = make_runtime(
        [
            Connected(),
            Message(COMMAND_TOPIC, b"ON", qos=1, retain=False),
        ]
    )
    stop_states = iter((False, False, False, True))

    runtime.run(lambda: next(stop_states))

    assert transport.exhausted
    assert backend.commands == []
    assert len(transport.publications) == 6


def test_stop_during_wait_records_connected_without_starting_snapshot() -> None:
    operations: list[str] = []
    backend = RecordingBackend(operations=operations)
    runtime, backend, transport = make_runtime([Connected()], backend=backend)
    transport.operations = operations
    stop_states = iter((False, True))

    runtime.run(lambda: next(stop_states))

    assert transport.subscriptions == []
    assert transport.publications == []
    assert backend.commands == []

    runtime.close()

    assert operations == [
        "connect",
        "publish:rescue/v1/bridge/availability:offline",
        "wait-for-delivery",
        "backend-close",
        "disconnect",
    ]


def test_stop_during_wait_records_disconnected_before_close() -> None:
    operations: list[str] = []
    backend = RecordingBackend(operations=operations)
    runtime, backend, transport = make_runtime([Connected()], backend=backend)
    transport.operations = operations
    run_until_events_exhausted(runtime, transport)
    transport.operations.clear()
    transport.subscriptions.clear()
    transport.publications.clear()
    transport.events.append(Disconnected())
    stop_states = iter((False, True))

    runtime.run(lambda: next(stop_states))

    assert transport.subscriptions == []
    assert transport.publications == []
    assert backend.commands == []

    runtime.close()

    assert operations == ["connect", "backend-close", "disconnect"]


def test_connection_refusal_is_a_bounded_expected_failure() -> None:
    runtime, backend, transport = make_runtime(
        [Disconnected(reason="MQTT connection refused: Not authorized")]
    )

    with pytest.raises(TransportError, match="^MQTT connection refused"):
        run_until_events_exhausted(runtime, transport)

    assert backend.commands == []
    assert transport.subscriptions == []
    assert transport.publications == []


def test_close_publishes_bridge_offline_then_closes_once_and_disconnects() -> None:
    operations: list[str] = []
    backend = RecordingBackend(operations=operations)
    runtime, backend, transport = make_runtime([Connected()], backend=backend)
    transport.operations = operations
    run_until_events_exhausted(runtime, transport)

    runtime.close()
    runtime.close()

    assert operations[-4:] == [
        "publish:rescue/v1/bridge/availability:offline",
        "wait-for-delivery",
        "backend-close",
        "disconnect",
    ]
    assert transport.publications[-1] == Publish(
        "rescue/v1/bridge/availability", b"offline", qos=1, retain=True
    )
    assert backend.close_calls == 1
    assert transport.delivery_timeouts == [5.0]
    assert operations.count("disconnect") == 1


def test_close_delivery_timeout_is_explicit_and_still_cleans_up_once() -> None:
    operations: list[str] = []
    backend = RecordingBackend(operations=operations)
    runtime, backend, transport = make_runtime([Connected()], backend=backend)
    transport.operations = operations
    run_until_events_exhausted(runtime, transport)
    transport.delivery_error = TransportError("MQTT publish delivery timed out")

    with pytest.raises(TransportError, match="^MQTT publish delivery timed out$"):
        runtime.close()
    runtime.close()

    assert operations[-4:] == [
        "publish:rescue/v1/bridge/availability:offline",
        "wait-for-delivery",
        "backend-close",
        "disconnect",
    ]
    assert transport.delivery_timeouts == [5.0]
    assert backend.close_calls == 1
    assert operations.count("disconnect") == 1


def test_immediate_connect_failure_propagates_without_publish() -> None:
    runtime, backend, transport = make_runtime([])
    transport.connect_error = TransportError("MQTT connect failed: unavailable")

    with pytest.raises(TransportError, match="unavailable"):
        runtime.run(lambda: False)
    runtime.close()

    assert transport.publications == []
    assert backend.close_calls == 1
    assert transport.operations == ["connect", "disconnect"]
