"""Typed boundary shared by runtime backends."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, TypeAlias, runtime_checkable

from smartenit_rescue.errors import ValidationError
from smartenit_rescue.models import (
    CapabilityId,
    Command,
    CommandResult,
    Device,
    DeviceAvailability,
    DeviceId,
    EndpointId,
    Observation,
)
from smartenit_rescue.profiles import DeviceProfile

WireValue: TypeAlias = bool | int | float | str


def _require_canonical(device_id: DeviceId) -> None:
    if not isinstance(device_id, DeviceId) or device_id != DeviceId.parse(
        str(device_id)
    ):
        raise ValidationError("device ID must be canonical")


@dataclass(frozen=True, slots=True)
class CapabilityAddress:
    device_id: DeviceId
    endpoint: EndpointId
    capability: CapabilityId

    def __post_init__(self) -> None:
        _require_canonical(self.device_id)
        if not isinstance(self.endpoint, EndpointId):
            raise ValidationError("endpoint ID must be canonical")
        if not isinstance(self.capability, CapabilityId):
            raise ValidationError("capability ID must be canonical")


class CapabilityMode(StrEnum):
    OBSERVABLE = "observable"
    COMMAND_ONLY = "command_only"


@dataclass(frozen=True, slots=True, order=True)
class RuntimeCapability:
    endpoint: EndpointId
    capability: CapabilityId
    mode: CapabilityMode

    def __post_init__(self) -> None:
        if not isinstance(self.endpoint, EndpointId):
            raise ValidationError("runtime endpoint ID must be canonical")
        if not isinstance(self.capability, CapabilityId):
            raise ValidationError("runtime capability ID must be canonical")
        if not isinstance(self.mode, CapabilityMode):
            raise ValidationError("runtime capability mode must be canonical")


@dataclass(frozen=True, slots=True)
class StateUpdate:
    address: CapabilityAddress
    observation: Observation[WireValue]


@dataclass(frozen=True, slots=True)
class RuntimeDevice:
    device: Device
    name: str
    profile: DeviceProfile
    capabilities: tuple[RuntimeCapability, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.device, Device):
            raise ValidationError("device must be canonical")
        _require_canonical(self.device.device_id)
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValidationError("name must be a nonblank string")
        if not isinstance(self.profile, DeviceProfile):
            raise ValidationError("profile must be validated")
        if self.capabilities != tuple(sorted(set(self.capabilities))):
            raise ValidationError("runtime capabilities must be sorted and unique")
        for runtime_capability in self.capabilities:
            if not isinstance(runtime_capability, RuntimeCapability):
                raise ValidationError("runtime capability must be canonical")
            endpoint = self.profile.endpoints.get(runtime_capability.endpoint)
            if endpoint is None or not any(
                capability.capability is runtime_capability.capability
                and capability.writable
                for capability in endpoint.capabilities
            ):
                raise ValidationError(
                    "runtime capability must be writable and declared by profile"
                )


@runtime_checkable
class Backend(Protocol):
    name: str

    def devices(self) -> tuple[RuntimeDevice, ...]: ...

    def observation(
        self, address: CapabilityAddress
    ) -> Observation[WireValue] | None: ...

    def set_desired(self, command: Command[WireValue]) -> CommandResult[WireValue]: ...

    def availability(self, device_id: DeviceId) -> DeviceAvailability: ...

    def close(self) -> None: ...


@runtime_checkable
class RefreshableBackend(Protocol):
    """Backend that can revalidate availability without issuing a command."""

    def refresh(self) -> None: ...
