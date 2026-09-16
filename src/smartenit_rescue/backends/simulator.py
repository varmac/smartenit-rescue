"""Deterministic in-memory backend for runtime proofs and tests."""

from collections.abc import Callable
from datetime import datetime

from smartenit_rescue.config import SimulatorDeviceSettings
from smartenit_rescue.errors import ValidationError
from smartenit_rescue.models import (
    AvailabilityState,
    CapabilityId,
    Command,
    CommandResult,
    CommandStatus,
    Confidence,
    Device,
    DeviceAvailability,
    DeviceId,
    EndpointId,
    Observation,
)
from smartenit_rescue.profiles import CapabilityProfile, DeviceProfile

from .base import (
    CapabilityAddress,
    CapabilityMode,
    RuntimeCapability,
    RuntimeDevice,
    WireValue,
)


class SimulatorBackend:
    name = "simulator"

    def __init__(
        self,
        settings: tuple[SimulatorDeviceSettings, ...],
        profiles: tuple[DeviceProfile, ...],
        clock: Callable[[], datetime],
    ) -> None:
        self._clock = clock
        self._closed = False
        self._observations: dict[CapabilityAddress, Observation[WireValue]] = {}
        self._sequences: dict[CapabilityAddress, int] = {}
        self._capabilities: dict[CapabilityAddress, CapabilityProfile] = {}
        self._availability: dict[DeviceId, DeviceAvailability] = {}
        runtime_devices: list[RuntimeDevice] = []

        for item in settings:
            profile = next(
                (
                    candidate
                    for candidate in profiles
                    if candidate.profile_id == item.profile_id
                ),
                None,
            )
            if profile is None:
                raise ValidationError("unknown simulator profile")

            device = Device(
                device_id=item.device_id,
                manufacturer=profile.manufacturer,
                model=profile.models[0],
                endpoints=tuple(profile.endpoints),
            )
            runtime_capabilities = tuple(
                sorted(
                    RuntimeCapability(
                        endpoint=endpoint_id,
                        capability=capability.capability,
                        mode=CapabilityMode.OBSERVABLE,
                    )
                    for endpoint_id, endpoint in profile.endpoints.items()
                    for capability in endpoint.capabilities
                    if capability.capability is CapabilityId.ON_OFF
                    and capability.writable
                )
            )
            runtime_devices.append(
                RuntimeDevice(
                    device=device,
                    name=item.name,
                    profile=profile,
                    capabilities=runtime_capabilities,
                )
            )
            observed_at = self._clock()
            self._availability[item.device_id] = DeviceAvailability(
                device_id=item.device_id,
                state=AvailabilityState.ONLINE,
                observed_at=observed_at,
                source=self.name,
            )

            for endpoint_id, endpoint in profile.endpoints.items():
                for capability in endpoint.capabilities:
                    address = CapabilityAddress(
                        device_id=item.device_id,
                        endpoint=endpoint_id,
                        capability=capability.capability,
                    )
                    self._capabilities[address] = capability
                    if capability.capability is CapabilityId.ON_OFF:
                        self._sequences[address] = 0
                        self._observations[address] = Observation(
                            value=item.initial_on,
                            source=self.name,
                            confidence=Confidence.OBSERVED,
                            observed_at=observed_at,
                            sequence=0,
                        )

        self._devices = tuple(runtime_devices)

    def devices(self) -> tuple[RuntimeDevice, ...]:
        return self._devices

    def observation(self, address: CapabilityAddress) -> Observation[WireValue] | None:
        return self._observations.get(address)

    def set_desired(self, command: Command[WireValue]) -> CommandResult[WireValue]:
        if self._closed:
            return self._rejected(command, "backend is closed")
        if (
            not isinstance(command.device_id, DeviceId)
            or not isinstance(command.endpoint, EndpointId)
            or not isinstance(command.capability, CapabilityId)
        ):
            return self._rejected(command, "invalid command identity")
        if command.device_id not in self._availability:
            return self._rejected(command, "unknown device")

        runtime_device = next(
            item for item in self._devices if item.device.device_id == command.device_id
        )
        if command.endpoint not in runtime_device.device.endpoints:
            return self._rejected(command, "unknown endpoint")

        address = CapabilityAddress(
            device_id=command.device_id,
            endpoint=command.endpoint,
            capability=command.capability,
        )
        capability = self._capabilities.get(address)
        if capability is None:
            return self._rejected(command, "unsupported capability")
        if not capability.writable:
            return self._rejected(command, "capability is read-only")
        if capability.capability is not CapabilityId.ON_OFF:
            return self._rejected(command, "unsupported writable capability")
        if not isinstance(command.desired, bool):
            return self._rejected(command, "desired value must be boolean")

        sequence = self._sequences[address] + 1
        observation: Observation[WireValue] = Observation(
            value=command.desired,
            source=self.name,
            confidence=Confidence.OBSERVED,
            observed_at=self._clock(),
            sequence=sequence,
        )
        self._sequences[address] = sequence
        self._observations[address] = observation
        return CommandResult(
            request_id=command.request_id,
            status=CommandStatus.CONFIRMED,
            observation=observation,
        )

    def availability(self, device_id: DeviceId) -> DeviceAvailability:
        known = self._availability.get(device_id)
        if known is not None:
            return known
        return DeviceAvailability(
            device_id=device_id,
            state=AvailabilityState.UNKNOWN,
            observed_at=self._clock(),
            source=self.name,
        )

    def close(self) -> None:
        self._closed = True

    @staticmethod
    def _rejected(command: Command[WireValue], reason: str) -> CommandResult[WireValue]:
        return CommandResult(
            request_id=command.request_id,
            status=CommandStatus.REJECTED,
            reason=reason,
        )
