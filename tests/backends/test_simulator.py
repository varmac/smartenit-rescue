from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

import pytest

from smartenit_rescue.backends import (
    Backend,
    CapabilityAddress,
    CapabilityMode,
    RuntimeCapability,
    SimulatorBackend,
    WireValue,
)
from smartenit_rescue.config import SimulatorDeviceSettings
from smartenit_rescue.models import (
    AvailabilityState,
    CapabilityId,
    Command,
    CommandStatus,
    Confidence,
    DeviceId,
    EndpointId,
)
from smartenit_rescue.profiles import iter_builtin_profiles

NOW = datetime(2026, 9, 15, 12, tzinfo=UTC)
DEVICE_ID = DeviceId.parse("0200000000000001")
SECOND_DEVICE_ID = DeviceId.parse("0200000000000002")
UNKNOWN_DEVICE_ID = DeviceId.parse("02000000000000ff")
PRIMARY_ENDPOINT = EndpointId(1)
ON_OFF = CapabilityAddress(DEVICE_ID, PRIMARY_ENDPOINT, CapabilityId.ON_OFF)
BUILTIN_PROFILES = iter_builtin_profiles()


def fixed_clock() -> datetime:
    return NOW


def device_settings(
    device_id: DeviceId = DEVICE_ID,
    *,
    name: str = "Synthetic Heater",
    initial_on: bool = False,
) -> SimulatorDeviceSettings:
    return SimulatorDeviceSettings(
        device_id=device_id,
        name=name,
        profile_id="smartenit.4040c",
        initial_on=initial_on,
    )


def desired_command(
    request_id: str,
    desired: WireValue,
    *,
    device_id: DeviceId = DEVICE_ID,
    endpoint: EndpointId = PRIMARY_ENDPOINT,
    capability: CapabilityId = CapabilityId.ON_OFF,
) -> Command[WireValue]:
    return Command(
        request_id=request_id,
        device_id=device_id,
        endpoint=endpoint,
        capability=capability,
        desired=desired,
    )


def test_construction_creates_runtime_device_without_issuing_a_command() -> None:
    backend = SimulatorBackend(
        (device_settings(initial_on=True),), BUILTIN_PROFILES, fixed_clock
    )

    assert isinstance(backend, Backend)
    assert backend.name == "simulator"
    assert [item.device.device_id for item in backend.devices()] == [DEVICE_ID]
    assert backend.devices()[0].capabilities == (
        RuntimeCapability(
            endpoint=PRIMARY_ENDPOINT,
            capability=CapabilityId.ON_OFF,
            mode=CapabilityMode.OBSERVABLE,
        ),
    )
    initial = backend.observation(ON_OFF)
    assert initial is not None
    assert initial.value is True
    assert initial.sequence == 0


def test_construction_uses_only_injected_profiles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_resource_access(_package: object) -> object:
        raise AssertionError("simulator constructor attempted resource access")

    monkeypatch.setattr("smartenit_rescue.profiles.loader.files", fail_resource_access)

    backend = SimulatorBackend((device_settings(),), BUILTIN_PROFILES, fixed_clock)

    assert backend.devices()[0].profile is BUILTIN_PROFILES[0]


def test_each_configured_device_starts_online() -> None:
    backend = SimulatorBackend(
        (
            device_settings(),
            device_settings(SECOND_DEVICE_ID, name="Synthetic Lamp", initial_on=True),
        ),
        BUILTIN_PROFILES,
        fixed_clock,
    )

    for device_id in (DEVICE_ID, SECOND_DEVICE_ID):
        availability = backend.availability(device_id)
        assert availability.state is AvailabilityState.ONLINE
        assert availability.observed_at == NOW
        assert availability.source == "simulator"


def test_initial_on_off_observation_is_authoritative_sequence_zero() -> None:
    backend = SimulatorBackend((device_settings(),), BUILTIN_PROFILES, fixed_clock)

    observation = backend.observation(ON_OFF)

    assert observation is not None
    assert observation.value is False
    assert observation.confidence is Confidence.OBSERVED
    assert observation.observed_at == NOW
    assert observation.source == "simulator"
    assert observation.sequence == 0


@pytest.mark.parametrize("desired", [True, False])
def test_on_and_off_return_confirmed_observed_results(desired: bool) -> None:
    backend = SimulatorBackend(
        (device_settings(initial_on=not desired),), BUILTIN_PROFILES, fixed_clock
    )

    result = backend.set_desired(desired_command("req-1", desired))

    assert result.status is CommandStatus.CONFIRMED
    assert result.reason is None
    assert result.observation is not None
    assert result.observation.value is desired
    assert result.observation.confidence is Confidence.OBSERVED
    assert backend.observation(ON_OFF) == result.observation


def test_repeated_desired_values_remain_correct_with_monotonic_sequences() -> None:
    backend = SimulatorBackend((device_settings(),), BUILTIN_PROFILES, fixed_clock)

    first = backend.set_desired(desired_command("req-1", True))
    repeated = backend.set_desired(desired_command("req-2", True))
    final = backend.set_desired(desired_command("req-3", False))

    assert first.observation is not None
    assert repeated.observation is not None
    assert final.observation is not None
    assert [
        first.observation.sequence,
        repeated.observation.sequence,
        final.observation.sequence,
    ] == [1, 2, 3]
    assert [
        first.observation.value,
        repeated.observation.value,
        final.observation.value,
    ] == [True, True, False]


@pytest.mark.parametrize(
    "command",
    [
        desired_command("wrong-device", True, device_id=UNKNOWN_DEVICE_ID),
        desired_command("wrong-endpoint", True, endpoint=EndpointId(2)),
        desired_command("wrong-capability", True, capability=CapabilityId.TEMPERATURE),
        desired_command("read-only", 12.5, capability=CapabilityId.ELECTRICAL_POWER),
        desired_command("not-boolean", "ON"),
    ],
    ids=[
        "wrong-device",
        "wrong-endpoint",
        "wrong-capability",
        "read-only-capability",
        "non-boolean-value",
    ],
)
def test_invalid_commands_are_rejected_without_state_change(
    command: Command[WireValue],
) -> None:
    backend = SimulatorBackend((device_settings(),), BUILTIN_PROFILES, fixed_clock)
    before = backend.observation(ON_OFF)

    result = backend.set_desired(command)

    assert result.status is CommandStatus.REJECTED
    assert result.observation is None
    assert result.reason is not None
    assert len(result.reason) <= 80
    assert repr(command.desired) not in result.reason
    assert backend.observation(ON_OFF) == before


@pytest.mark.parametrize(
    ("endpoint", "capability"),
    [
        (cast(EndpointId, 1), CapabilityId.ON_OFF),
        (PRIMARY_ENDPOINT, cast(CapabilityId, "on_off")),
    ],
    ids=["raw-endpoint", "raw-capability"],
)
def test_malformed_command_identity_is_rejected_without_state_change(
    endpoint: EndpointId, capability: CapabilityId
) -> None:
    backend = SimulatorBackend((device_settings(),), BUILTIN_PROFILES, fixed_clock)
    before = backend.observation(ON_OFF)
    command = desired_command(
        "malformed-identity",
        True,
        endpoint=endpoint,
        capability=capability,
    )

    result = backend.set_desired(command)

    assert result.status is CommandStatus.REJECTED
    assert result.observation is None
    assert result.reason == "invalid command identity"
    assert backend.observation(ON_OFF) == before


def test_close_is_idempotent_and_rejects_later_commands() -> None:
    backend = SimulatorBackend((device_settings(),), BUILTIN_PROFILES, fixed_clock)
    before = backend.observation(ON_OFF)

    backend.close()
    backend.close()
    result = backend.set_desired(desired_command("after-close", True))

    assert result.status is CommandStatus.REJECTED
    assert result.observation is None
    assert result.reason == "backend is closed"
    assert backend.observation(ON_OFF) == before
