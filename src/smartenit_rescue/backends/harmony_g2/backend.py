"""Command-only backend for exact Harmony G2 controller bindings."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Protocol

from smartenit_rescue.backends.base import (
    CapabilityAddress,
    CapabilityMode,
    RuntimeCapability,
    RuntimeDevice,
    WireValue,
)
from smartenit_rescue.backends.harmony_g2.client import G2Client, G2Endpoint
from smartenit_rescue.backends.harmony_g2.session import LocalSession
from smartenit_rescue.backends.harmony_g2.transport import (
    G2ResponseError,
    G2Transport,
    G2TransportError,
)
from smartenit_rescue.config import HarmonyG2DeviceSettings
from smartenit_rescue.errors import RescueError, ValidationError
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
from smartenit_rescue.profiles import DeviceProfile

_SESSION_REFRESH_MARGIN = timedelta(minutes=5)
_INVENTORY_FRESHNESS = timedelta(seconds=60)
_PRIMARY_ENDPOINT = EndpointId(1)


class _SessionStorage(Protocol):
    def load(self) -> LocalSession: ...

    def save(self, session: LocalSession) -> None: ...


def _aware_now(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValidationError("Harmony G2 clock must be timezone-aware")
    return value


def _profile(profile_id: str, profiles: tuple[DeviceProfile, ...]) -> DeviceProfile:
    matches = tuple(
        candidate for candidate in profiles if candidate.profile_id == profile_id
    )
    if len(matches) != 1:
        raise ValidationError("Harmony G2 configuration requires one exact profile")
    profile = matches[0]
    endpoint = profile.endpoints.get(_PRIMARY_ENDPOINT)
    if endpoint is None or not any(
        candidate.capability is CapabilityId.ON_OFF and candidate.writable
        for candidate in endpoint.capabilities
    ):
        raise ValidationError(
            "Harmony G2 profile must declare writable OnOff at endpoint 1"
        )
    if not profile.models:
        raise ValidationError("Harmony G2 profile must declare a model")
    return profile


def _snapshot(
    settings: tuple[HarmonyG2DeviceSettings, ...],
    profiles: tuple[DeviceProfile, ...],
    endpoints: tuple[G2Endpoint, ...],
) -> tuple[tuple[RuntimeDevice, ...], dict[DeviceId, G2Endpoint]]:
    if not settings:
        raise ValidationError("Harmony G2 requires at least one configured device")
    devices: list[RuntimeDevice] = []
    bindings: dict[DeviceId, G2Endpoint] = {}
    seen_pairs: set[tuple[DeviceId, str]] = set()
    for configured in settings:
        if not isinstance(configured, HarmonyG2DeviceSettings):
            raise ValidationError("Harmony G2 device configuration is invalid")
        pair = (configured.device_id, configured.component_id)
        if configured.device_id in bindings or pair in seen_pairs:
            raise ValidationError("Harmony G2 configured binding is duplicated")
        seen_pairs.add(pair)
        matches = tuple(
            endpoint
            for endpoint in endpoints
            if endpoint.device_id == configured.device_id
            and endpoint.component_id == configured.component_id
        )
        if len(matches) != 1:
            raise ValidationError(
                "Harmony G2 configured binding must match inventory exactly once"
            )
        profile = _profile(configured.profile_id, profiles)
        operator_asserted_device = Device(
            device_id=configured.device_id,
            manufacturer=profile.manufacturer,
            model=profile.models[0],
            endpoints=tuple(sorted(profile.endpoints)),
        )
        devices.append(
            RuntimeDevice(
                device=operator_asserted_device,
                name=configured.name,
                profile=profile,
                capabilities=(
                    RuntimeCapability(
                        endpoint=_PRIMARY_ENDPOINT,
                        capability=CapabilityId.ON_OFF,
                        mode=CapabilityMode.COMMAND_ONLY,
                    ),
                ),
            )
        )
        bindings[configured.device_id] = matches[0]
    return tuple(devices), bindings


class HarmonyG2Backend:
    name = "harmony_g2"

    def __init__(
        self,
        settings: tuple[HarmonyG2DeviceSettings, ...],
        profiles: tuple[DeviceProfile, ...],
        session_store: _SessionStorage,
        client: G2Client,
        clock: Callable[[], datetime],
        session: LocalSession,
        devices: tuple[RuntimeDevice, ...],
        bindings: dict[DeviceId, G2Endpoint],
        discovered_at: datetime,
    ) -> None:
        self._settings = settings
        self._profiles = profiles
        self._session_store = session_store
        self._client = client
        self._clock = clock
        self._session = session
        self._devices = devices
        self._bindings = bindings
        self._inventory_at = discovered_at
        self._next_refresh_at = discovered_at + _INVENTORY_FRESHNESS
        self._force_renew = False
        self._closed = False
        self._availability = {
            device.device.device_id: DeviceAvailability(
                device_id=device.device.device_id,
                state=AvailabilityState.ONLINE,
                observed_at=discovered_at,
                source=self.name,
            )
            for device in devices
        }

    @classmethod
    def open(
        cls,
        settings: tuple[HarmonyG2DeviceSettings, ...],
        profiles: tuple[DeviceProfile, ...],
        session_store: _SessionStorage,
        transport: G2Transport,
        clock: Callable[[], datetime],
    ) -> HarmonyG2Backend:
        now = _aware_now(clock)
        session = session_store.load()
        client = G2Client(transport)
        if session.expires_at <= now + _SESSION_REFRESH_MARGIN:
            renewed = client.renew(session, now)
            session_store.save(renewed)
            session = renewed
        endpoints = client.endpoints(session.access_token)
        devices, bindings = _snapshot(settings, profiles, endpoints)
        return cls(
            settings,
            profiles,
            session_store,
            client,
            clock,
            session,
            devices,
            bindings,
            now,
        )

    def devices(self) -> tuple[RuntimeDevice, ...]:
        return self._devices

    def observation(self, address: CapabilityAddress) -> Observation[WireValue] | None:
        del address
        return None

    def availability(self, device_id: DeviceId) -> DeviceAvailability:
        known = self._availability.get(device_id)
        if known is not None:
            return known
        return DeviceAvailability(
            device_id=device_id,
            state=AvailabilityState.UNKNOWN,
            observed_at=_aware_now(self._clock),
            source=self.name,
        )

    def refresh(self) -> None:
        if self._closed:
            return
        now = _aware_now(self._clock)
        if now < self._next_refresh_at:
            return
        self._next_refresh_at = now + _INVENTORY_FRESHNESS
        try:
            self._maintain()
        except RescueError as error:
            if isinstance(error, G2ResponseError) and error.status in {401, 403}:
                self._force_renew = True
            self._mark_all(AvailabilityState.OFFLINE)

    def set_desired(self, command: Command[WireValue]) -> CommandResult[WireValue]:
        problem = self._command_problem(command)
        if problem is not None:
            return self._rejected(command, problem)
        try:
            self._maintain()
        except RescueError as error:
            if isinstance(error, G2ResponseError) and error.status in {401, 403}:
                self._force_renew = True
            self._mark_all(AvailabilityState.OFFLINE)
            return self._rejected(command, "G2 maintenance failed before command")
        endpoint = self._bindings.get(command.device_id)
        if endpoint is None:
            self._mark_all(AvailabilityState.OFFLINE)
            return self._rejected(command, "G2 binding is unavailable")
        desired = command.desired
        if not isinstance(desired, bool):
            return self._rejected(command, "desired value must be boolean")
        try:
            self._client.set_on_off(
                endpoint,
                desired,
                self._session.access_token,
            )
        except G2ResponseError as error:
            if error.status in {400, 401, 403}:
                if error.status in {401, 403}:
                    self._force_renew = True
                    self._mark_all(AvailabilityState.OFFLINE)
                return self._rejected(command, "G2 refused command")
            self._mark_all(AvailabilityState.OFFLINE)
            return self._indeterminate(command)
        except G2TransportError as error:
            self._mark_all(AvailabilityState.OFFLINE)
            if error.request_started:
                return self._indeterminate(command)
            return self._rejected(command, "G2 command was not submitted")
        self._mark_device(command.device_id, AvailabilityState.ONLINE)
        return CommandResult(
            request_id=command.request_id,
            status=CommandStatus.ACCEPTED,
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._mark_all(AvailabilityState.OFFLINE)

    def _command_problem(self, command: Command[WireValue]) -> str | None:
        if self._closed:
            return "backend is closed"
        if (
            not isinstance(command.device_id, DeviceId)
            or not isinstance(command.endpoint, EndpointId)
            or not isinstance(command.capability, CapabilityId)
        ):
            return "invalid command identity"
        if command.device_id not in self._bindings:
            return "unknown device"
        if command.endpoint != _PRIMARY_ENDPOINT:
            return "unknown endpoint"
        if command.capability is not CapabilityId.ON_OFF:
            return "unsupported capability"
        if type(command.desired) is not bool:
            return "desired value must be boolean"
        return None

    def _maintain(self) -> None:
        now = _aware_now(self._clock)
        if (
            self._force_renew
            or self._session.expires_at <= now + _SESSION_REFRESH_MARGIN
        ):
            renewed = self._client.renew(self._session, now)
            self._session_store.save(renewed)
            self._session = renewed
            self._force_renew = False
        inventory_stale = now - self._inventory_at >= _INVENTORY_FRESHNESS
        any_offline = any(
            item.state is not AvailabilityState.ONLINE
            for item in self._availability.values()
        )
        if not inventory_stale and not any_offline:
            return
        endpoints = self._client.endpoints(self._session.access_token)
        devices, bindings = _snapshot(self._settings, self._profiles, endpoints)
        self._devices = devices
        self._bindings = bindings
        self._inventory_at = now
        self._mark_all(AvailabilityState.ONLINE)

    def _mark_all(self, state: AvailabilityState) -> None:
        for device_id in self._availability:
            self._mark_device(device_id, state)

    def _mark_device(self, device_id: DeviceId, state: AvailabilityState) -> None:
        self._availability[device_id] = DeviceAvailability(
            device_id=device_id,
            state=state,
            observed_at=_aware_now(self._clock),
            source=self.name,
        )

    @staticmethod
    def _rejected(command: Command[WireValue], reason: str) -> CommandResult[WireValue]:
        return CommandResult(
            request_id=command.request_id,
            status=CommandStatus.REJECTED,
            reason=reason,
        )

    @staticmethod
    def _indeterminate(command: Command[WireValue]) -> CommandResult[WireValue]:
        return CommandResult(
            request_id=command.request_id,
            status=CommandStatus.INDETERMINATE,
            reason="G2 command outcome is unknown",
        )


__all__ = ["HarmonyG2Backend"]
