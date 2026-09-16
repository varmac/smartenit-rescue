from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest

from smartenit_rescue.backends import (
    Backend,
    CapabilityAddress,
    CapabilityMode,
    RuntimeCapability,
    WireValue,
)
from smartenit_rescue.backends.harmony_g2.backend import HarmonyG2Backend
from smartenit_rescue.backends.harmony_g2.session import LocalSession
from smartenit_rescue.backends.harmony_g2.transport import (
    G2ResponseError,
    G2TransportError,
)
from smartenit_rescue.config import HarmonyG2DeviceSettings
from smartenit_rescue.errors import ValidationError
from smartenit_rescue.models import (
    AvailabilityState,
    CapabilityId,
    Command,
    CommandStatus,
    DeviceId,
    EndpointId,
)
from smartenit_rescue.profiles import DeviceProfile, iter_builtin_profiles

NOW = datetime(2026, 9, 15, 20, 40, tzinfo=UTC)
DEVICE_ID = DeviceId.parse("0200000000000001")
OTHER_DEVICE_ID = DeviceId.parse("0200000000000002")
PRIMARY_ENDPOINT = EndpointId(1)
ACCESS_VALUE = "<local-access>"
REFRESH_VALUE = "<local-refresh>"
NEXT_ACCESS_VALUE = "<next-access>"
NEXT_REFRESH_VALUE = "<next-refresh>"
PROFILE = iter_builtin_profiles()[0]


@dataclass(frozen=True, slots=True)
class Request:
    method: str
    path: str
    body: Mapping[str, object] | None
    credential: str | None


class RecordingTransport:
    def __init__(
        self, responses: list[object], events: list[str] | None = None
    ) -> None:
        self.responses = list(responses)
        self.requests: list[Request] = []
        self.events = events

    def request(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, object] | None = None,
        access_token: str | None = None,
    ) -> Mapping[str, object]:
        self.requests.append(Request(method, path, body, access_token))
        if self.events is not None:
            if path == "/v2/oauth2/token":
                self.events.append("request:renew")
            elif path.startswith("/v2/devices?"):
                self.events.append("request:inventory")
            else:
                self.events.append("request:command")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        assert isinstance(response, Mapping)
        return response


class MemorySessionStore:
    def __init__(self, session: LocalSession, events: list[str] | None = None) -> None:
        self.session = session
        self.events = events
        self.load_count = 0
        self.saved: list[LocalSession] = []

    def load(self) -> LocalSession:
        self.load_count += 1
        return self.session

    def save(self, session: LocalSession) -> None:
        if self.events is not None:
            self.events.append("save")
        self.session = session
        self.saved.append(session)


@dataclass(slots=True)
class MutableClock:
    value: datetime = NOW

    def __call__(self) -> datetime:
        return self.value


def processor(name: object = "OnOff") -> dict[str, object]:
    payload: dict[str, object] = {}
    payload["name"] = name
    return payload


def component(component_id: object = "1") -> dict[str, object]:
    payload: dict[str, object] = {}
    payload["id"] = component_id
    payload["processors"] = [processor()]
    return payload


def device(
    *,
    local_id: object = "device-1",
    hardware_id: object = "0200000000000001",
    components: object | None = None,
    name: object = "Misleading friendly name",
) -> dict[str, object]:
    payload: dict[str, object] = {}
    payload["_id"] = local_id
    payload["hwId"] = hardware_id
    payload["components"] = [component()] if components is None else components
    payload["name"] = name
    return payload


def inventory(devices: list[object] | None = None) -> dict[str, object]:
    payload: dict[str, object] = {}
    payload["data"] = [device()] if devices is None else devices
    return payload


def refresh_response(lifetime: object = 3600) -> dict[str, object]:
    payload: dict[str, object] = {}
    payload["access_token"] = NEXT_ACCESS_VALUE
    payload["refresh_token"] = NEXT_REFRESH_VALUE
    payload["expires_in"] = lifetime
    return payload


def acknowledgement() -> dict[str, object]:
    payload: dict[str, object] = {}
    payload["success"] = True
    return payload


def settings(
    *,
    device_id: DeviceId = DEVICE_ID,
    component_id: str = "1",
    name: str = "Example Load",
    profile_id: str = "smartenit.4040c",
) -> tuple[HarmonyG2DeviceSettings, ...]:
    return (
        HarmonyG2DeviceSettings(
            device_id=device_id,
            name=name,
            profile_id=profile_id,
            component_id=component_id,
        ),
    )


def session(*, expires_in: int = 3600) -> LocalSession:
    return LocalSession(
        ACCESS_VALUE,
        REFRESH_VALUE,
        NOW + timedelta(seconds=expires_in),
    )


def command(
    request_id: str = "request-1",
    desired: WireValue = True,
    *,
    device_id: DeviceId = DEVICE_ID,
    endpoint: EndpointId = PRIMARY_ENDPOINT,
    capability: CapabilityId = CapabilityId.ON_OFF,
) -> Command[WireValue]:
    return Command(request_id, device_id, endpoint, capability, desired)


def open_backend(
    responses: list[object] | None = None,
    *,
    stored_session: LocalSession | None = None,
    device_settings: tuple[HarmonyG2DeviceSettings, ...] | None = None,
    profiles: tuple[DeviceProfile, ...] = (PROFILE,),
    clock: MutableClock | None = None,
    events: list[str] | None = None,
) -> tuple[HarmonyG2Backend, RecordingTransport, MemorySessionStore, MutableClock]:
    selected_clock = clock or MutableClock()
    store = MemorySessionStore(stored_session or session(), events)
    transport = RecordingTransport(
        list(responses) if responses is not None else [inventory()], events
    )
    backend = HarmonyG2Backend.open(
        device_settings or settings(),
        profiles,
        store,
        transport,
        selected_clock,
    )
    return backend, transport, store, selected_clock


def test_open_renews_due_session_and_saves_before_inventory() -> None:
    events: list[str] = []

    backend, transport, store, _clock = open_backend(
        [refresh_response(), inventory()],
        stored_session=session(expires_in=300),
        events=events,
    )

    assert isinstance(backend, Backend)
    assert store.load_count == 1
    assert store.saved == [
        LocalSession(
            NEXT_ACCESS_VALUE,
            NEXT_REFRESH_VALUE,
            NOW + timedelta(seconds=3600),
        )
    ]
    assert events == ["request:renew", "save", "request:inventory"]
    assert transport.requests[-1].credential == NEXT_ACCESS_VALUE


def test_open_skips_renewal_for_fresh_session() -> None:
    _backend, transport, store, _clock = open_backend(
        stored_session=session(expires_in=301)
    )

    assert store.saved == []
    assert [request.path for request in transport.requests] == [
        "/v2/devices?limit=100&page=1"
    ]
    assert transport.requests[0].credential == ACCESS_VALUE


def test_open_maps_exact_identity_component_and_operator_profile() -> None:
    backend, _transport, _store, _clock = open_backend()

    runtime_device = backend.devices()[0]
    assert runtime_device.name == "Example Load"
    assert runtime_device.device.device_id == DEVICE_ID
    assert runtime_device.device.manufacturer == PROFILE.manufacturer
    assert runtime_device.device.model == PROFILE.models[0]
    assert runtime_device.profile is PROFILE
    assert runtime_device.capabilities == (
        RuntimeCapability(
            PRIMARY_ENDPOINT,
            CapabilityId.ON_OFF,
            CapabilityMode.COMMAND_ONLY,
        ),
    )
    address = CapabilityAddress(DEVICE_ID, PRIMARY_ENDPOINT, CapabilityId.ON_OFF)
    assert backend.observation(address) is None
    assert backend.availability(DEVICE_ID).state is AvailabilityState.ONLINE
    assert backend.availability(OTHER_DEVICE_ID).state is AvailabilityState.UNKNOWN


@pytest.mark.parametrize(
    ("configured", "devices"),
    [
        (settings(device_id=OTHER_DEVICE_ID), [device()]),
        (settings(component_id="2"), [device()]),
        (
            settings(),
            [
                device(local_id="device-1"),
                device(local_id="device-2"),
            ],
        ),
        (
            settings(),
            [
                device(
                    hardware_id="0200000000000002",
                    name="Friendly suffix 0001",
                )
            ],
        ),
    ],
)
def test_open_rejects_missing_ambiguous_or_friendly_only_match(
    configured: tuple[HarmonyG2DeviceSettings, ...], devices: list[object]
) -> None:
    with pytest.raises((ValidationError, G2ResponseError), match="G2"):
        open_backend([inventory(devices)], device_settings=configured)


def test_open_rejects_missing_profile() -> None:
    with pytest.raises(ValidationError, match="profile"):
        open_backend(profiles=())


@pytest.mark.parametrize("profile_change", ["missing-endpoint", "read-only"])
def test_open_rejects_profile_without_writable_onoff(
    profile_change: str,
) -> None:
    if profile_change == "missing-endpoint":
        altered = replace(PROFILE, endpoints={})
    else:
        endpoint = PROFILE.endpoints[PRIMARY_ENDPOINT]
        capabilities = tuple(
            replace(candidate, writable=False)
            if candidate.capability is CapabilityId.ON_OFF
            else candidate
            for candidate in endpoint.capabilities
        )
        altered_endpoint = replace(endpoint, capabilities=capabilities)
        altered = replace(PROFILE, endpoints={PRIMARY_ENDPOINT: altered_endpoint})

    with pytest.raises(ValidationError, match="writable OnOff"):
        open_backend(profiles=(altered,))


def test_close_is_idempotent_marks_offline_and_rejects_command() -> None:
    backend, transport, _store, _clock = open_backend()
    before = len(transport.requests)

    backend.close()
    backend.close()
    result = backend.set_desired(command("closed"))

    assert result.status is CommandStatus.REJECTED
    assert result.reason == "backend is closed"
    assert backend.availability(DEVICE_ID).state is AvailabilityState.OFFLINE
    assert len(transport.requests) == before


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (
            G2ResponseError("explicit refusal", status=400, request_started=True),
            CommandStatus.REJECTED,
        ),
        (
            G2ResponseError("authentication refused", status=401, request_started=True),
            CommandStatus.REJECTED,
        ),
        (
            G2ResponseError("authorization refused", status=403, request_started=True),
            CommandStatus.REJECTED,
        ),
        (
            G2TransportError("pin mismatch", request_started=False),
            CommandStatus.REJECTED,
        ),
        (
            G2TransportError("timeout", request_started=True),
            CommandStatus.INDETERMINATE,
        ),
        (
            G2ResponseError("server failure", status=500, request_started=True),
            CommandStatus.INDETERMINATE,
        ),
        ({}, CommandStatus.INDETERMINATE),
    ],
)
def test_command_outcome_is_classified_without_retry(
    failure: object, expected: CommandStatus
) -> None:
    backend, transport, _store, _clock = open_backend([inventory(), failure])

    result = backend.set_desired(command())

    assert result.status is expected
    assert result.observation is None
    assert len(result.reason or "") <= 80
    command_requests = [
        request for request in transport.requests if "/methods/" in request.path
    ]
    assert len(command_requests) == 1


def test_successful_command_is_accepted_without_observation_or_readback() -> None:
    backend, transport, _store, _clock = open_backend([inventory(), acknowledgement()])

    result = backend.set_desired(command(desired=False))

    assert result.status is CommandStatus.ACCEPTED
    assert result.observation is None
    assert result.reason is None
    assert [
        request.path for request in transport.requests if "/methods/" in request.path
    ] == ["/v2/devices/device-1/comps/1/procs/OnOff/methods/Off"]
    assert all("/attrs/" not in request.path for request in transport.requests)


@pytest.mark.parametrize(
    "invalid_command",
    [
        command("unknown-device", device_id=OTHER_DEVICE_ID),
        command("unknown-endpoint", endpoint=EndpointId(2)),
        command("wrong-capability", capability=CapabilityId.TEMPERATURE),
        command("not-boolean", desired=1),
        command(
            "raw-endpoint",
            endpoint=cast(EndpointId, 1),
        ),
        command(
            "raw-capability",
            capability=cast(CapabilityId, "on_off"),
        ),
    ],
)
def test_invalid_command_is_rejected_before_network(
    invalid_command: Command[WireValue],
) -> None:
    backend, transport, _store, _clock = open_backend()
    before = len(transport.requests)

    result = backend.set_desired(invalid_command)

    assert result.status is CommandStatus.REJECTED
    assert result.observation is None
    assert result.reason is not None
    assert len(transport.requests) == before


def test_stale_inventory_is_refreshed_before_command() -> None:
    clock = MutableClock()
    backend, transport, _store, _clock = open_backend(
        [inventory(), inventory(), acknowledgement()], clock=clock
    )
    clock.value += timedelta(seconds=61)

    result = backend.set_desired(command())

    assert result.status is CommandStatus.ACCEPTED
    assert [
        "inventory" if "?" in request.path else "command"
        for request in transport.requests
    ] == ["inventory", "inventory", "command"]


def test_changed_inventory_binding_rejects_command_and_marks_offline() -> None:
    clock = MutableClock()
    backend, transport, _store, _clock = open_backend(
        [inventory(), inventory([device(components=[component("2")])])],
        clock=clock,
    )
    clock.value += timedelta(seconds=61)

    result = backend.set_desired(command())

    assert result.status is CommandStatus.REJECTED
    assert backend.availability(DEVICE_ID).state is AvailabilityState.OFFLINE
    assert all("/methods/" not in request.path for request in transport.requests)


def test_failed_inventory_refresh_rejects_command_and_marks_offline() -> None:
    clock = MutableClock()
    failure = G2TransportError("gateway unavailable", request_started=False)
    backend, transport, _store, _clock = open_backend(
        [inventory(), failure], clock=clock
    )
    clock.value += timedelta(seconds=61)

    result = backend.set_desired(command())

    assert result.status is CommandStatus.REJECTED
    assert result.observation is None
    assert backend.availability(DEVICE_ID).state is AvailabilityState.OFFLINE
    assert all("/methods/" not in request.path for request in transport.requests)


def test_idle_refresh_recovers_after_transient_command_transport_failure() -> None:
    failure = G2TransportError("gateway unavailable", request_started=False)
    backend, transport, _store, clock = open_backend(
        [inventory(), failure, inventory()]
    )

    assert backend.set_desired(command()).status is CommandStatus.REJECTED
    assert backend.availability(DEVICE_ID).state is AvailabilityState.OFFLINE
    clock.value += timedelta(seconds=61)
    backend.refresh()

    assert backend.availability(DEVICE_ID).state is AvailabilityState.ONLINE
    assert (
        len([request for request in transport.requests if "/methods/" in request.path])
        == 1
    )
    assert len([request for request in transport.requests if "?" in request.path]) == 2


def test_idle_refresh_renews_after_auth_refusal_without_replaying_command() -> None:
    refusal = G2ResponseError("access rejected", status=401, request_started=True)
    backend, transport, store, clock = open_backend(
        [inventory(), refusal, refresh_response(), inventory()]
    )

    assert backend.set_desired(command()).status is CommandStatus.REJECTED
    clock.value += timedelta(seconds=61)
    backend.refresh()

    assert backend.availability(DEVICE_ID).state is AvailabilityState.ONLINE
    assert store.saved[-1].access_token == NEXT_ACCESS_VALUE
    assert (
        len([request for request in transport.requests if "/methods/" in request.path])
        == 1
    )


@pytest.mark.parametrize("status", [401, 403])
def test_idle_inventory_auth_refusal_forces_local_renewal(status: int) -> None:
    refusal = G2ResponseError("access rejected", status=status, request_started=True)
    backend, transport, store, clock = open_backend(
        [inventory(), refusal, refresh_response(), inventory()]
    )
    clock.value += timedelta(seconds=61)

    backend.refresh()
    assert backend.availability(DEVICE_ID).state is AvailabilityState.OFFLINE
    clock.value += timedelta(seconds=61)
    backend.refresh()

    assert backend.availability(DEVICE_ID).state is AvailabilityState.ONLINE
    assert store.saved[-1].access_token == NEXT_ACCESS_VALUE
    assert [request.path for request in transport.requests] == [
        "/v2/devices?limit=100&page=1",
        "/v2/devices?limit=100&page=1",
        "/v2/oauth2/token",
        "/v2/devices?limit=100&page=1",
    ]


def test_command_preflight_auth_refusal_recovers_without_command_replay() -> None:
    refusal = G2ResponseError("access rejected", status=401, request_started=True)
    backend, transport, store, clock = open_backend(
        [inventory(), refusal, refresh_response(), inventory()]
    )
    clock.value += timedelta(seconds=61)

    assert backend.set_desired(command()).status is CommandStatus.REJECTED
    backend.refresh()

    assert backend.availability(DEVICE_ID).state is AvailabilityState.ONLINE
    assert store.saved[-1].access_token == NEXT_ACCESS_VALUE
    assert all("/methods/" not in request.path for request in transport.requests)


def test_session_due_before_command_rotates_before_inventory_and_command() -> None:
    clock = MutableClock()
    events: list[str] = []
    backend, _transport, store, _clock = open_backend(
        [inventory(), refresh_response(), inventory(), acknowledgement()],
        stored_session=session(expires_in=362),
        clock=clock,
        events=events,
    )
    events.clear()
    clock.value += timedelta(seconds=62)

    result = backend.set_desired(command())

    assert result.status is CommandStatus.ACCEPTED
    assert events == [
        "request:renew",
        "save",
        "request:inventory",
        "request:command",
    ]
    assert store.saved[-1].access_token == NEXT_ACCESS_VALUE
