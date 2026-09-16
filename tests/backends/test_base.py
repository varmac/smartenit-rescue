from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

import pytest

from smartenit_rescue.backends import (
    Backend,
    CapabilityAddress,
    CapabilityMode,
    RuntimeCapability,
    RuntimeDevice,
    WireValue,
)
from smartenit_rescue.errors import ValidationError
from smartenit_rescue.models import (
    AvailabilityState,
    CapabilityId,
    Command,
    CommandResult,
    CommandStatus,
    Device,
    DeviceAvailability,
    DeviceId,
    EndpointId,
    Observation,
)
from smartenit_rescue.profiles import iter_builtin_profiles

DEVICE_ID = DeviceId.parse("0200000000000001")
NOW = datetime(2026, 9, 15, 12, tzinfo=UTC)


class SimulatorStub:
    name = "simulator"

    def devices(self) -> tuple[RuntimeDevice, ...]:
        return ()

    def observation(self, address: CapabilityAddress) -> Observation[WireValue] | None:
        return None

    def set_desired(self, command: Command[WireValue]) -> CommandResult[WireValue]:
        return CommandResult(
            request_id=command.request_id,
            status=CommandStatus.REJECTED,
            reason="not implemented",
        )

    def availability(self, device_id: DeviceId) -> DeviceAvailability:
        return DeviceAvailability(
            device_id=device_id,
            state=AvailabilityState.UNKNOWN,
            observed_at=NOW,
            source=self.name,
        )

    def close(self) -> None:
        return None


def test_capability_address_rejects_noncanonical_device_identity() -> None:
    with pytest.raises(ValidationError, match="canonical"):
        CapabilityAddress(
            device_id=cast(DeviceId, "0200000000000001"),
            endpoint=EndpointId(1),
            capability=CapabilityId.ON_OFF,
        )


@pytest.mark.parametrize(
    ("endpoint", "capability"),
    [
        (cast(EndpointId, 1), CapabilityId.ON_OFF),
        (EndpointId(1), cast(CapabilityId, "on_off")),
    ],
    ids=["raw-endpoint", "raw-capability"],
)
def test_capability_address_requires_canonical_address_types(
    endpoint: EndpointId, capability: CapabilityId
) -> None:
    with pytest.raises(ValidationError, match="endpoint|capability"):
        CapabilityAddress(
            device_id=DEVICE_ID,
            endpoint=endpoint,
            capability=capability,
        )


def test_runtime_device_holds_canonical_device_name_and_validated_profile() -> None:
    profile = next(
        item for item in iter_builtin_profiles() if item.profile_id == "smartenit.4040c"
    )
    device = Device(
        device_id=DEVICE_ID,
        manufacturer=profile.manufacturer,
        model=profile.models[0],
        endpoints=tuple(profile.endpoints),
    )

    runtime_capability = RuntimeCapability(
        endpoint=EndpointId(1),
        capability=CapabilityId.ON_OFF,
        mode=CapabilityMode.OBSERVABLE,
    )
    runtime_device = RuntimeDevice(
        device=device,
        name="Synthetic Heater",
        profile=profile,
        capabilities=(runtime_capability,),
    )

    assert runtime_device.device is device
    assert runtime_device.name == "Synthetic Heater"
    assert runtime_device.profile is profile
    assert runtime_device.capabilities == (runtime_capability,)


def test_runtime_device_rejects_capability_absent_from_profile() -> None:
    profile = next(
        item for item in iter_builtin_profiles() if item.profile_id == "smartenit.4040c"
    )
    device = Device(
        device_id=DEVICE_ID,
        manufacturer=profile.manufacturer,
        model=profile.models[0],
        endpoints=tuple(profile.endpoints),
    )

    with pytest.raises(ValidationError, match="declared by profile"):
        RuntimeDevice(
            device=device,
            name="Synthetic Load",
            profile=profile,
            capabilities=(
                RuntimeCapability(
                    endpoint=EndpointId(1),
                    capability=CapabilityId.TEMPERATURE,
                    mode=CapabilityMode.OBSERVABLE,
                ),
            ),
        )


def test_runtime_device_rejects_duplicate_runtime_capabilities() -> None:
    profile = next(
        item for item in iter_builtin_profiles() if item.profile_id == "smartenit.4040c"
    )
    device = Device(
        device_id=DEVICE_ID,
        manufacturer=profile.manufacturer,
        model=profile.models[0],
        endpoints=tuple(profile.endpoints),
    )
    runtime_capability = RuntimeCapability(
        endpoint=EndpointId(1),
        capability=CapabilityId.ON_OFF,
        mode=CapabilityMode.OBSERVABLE,
    )

    with pytest.raises(ValidationError, match="sorted and unique"):
        RuntimeDevice(
            device=device,
            name="Synthetic Load",
            profile=profile,
            capabilities=(runtime_capability, runtime_capability),
        )


@pytest.mark.parametrize("name", ["", " ", "\t\n"])
def test_runtime_device_requires_nonblank_display_name(name: str) -> None:
    profile = next(
        item for item in iter_builtin_profiles() if item.profile_id == "smartenit.4040c"
    )
    device = Device(
        device_id=DEVICE_ID,
        manufacturer=profile.manufacturer,
        model=profile.models[0],
        endpoints=tuple(profile.endpoints),
    )

    with pytest.raises(ValidationError, match="name"):
        RuntimeDevice(device=device, name=name, profile=profile, capabilities=())


def test_backend_protocol_is_runtime_checkable() -> None:
    simulator = SimulatorStub()

    assert isinstance(simulator, Backend)
