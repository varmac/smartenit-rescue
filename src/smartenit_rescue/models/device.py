"""Immutable device snapshots and separate backend and availability records."""

from dataclasses import dataclass
from datetime import datetime

from smartenit_rescue.errors import ValidationError

from .identity import DeviceId, EndpointId
from .state import AvailabilityState


def _require_nonblank(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{label} must be a nonblank string")


def _require_aware(value: datetime, label: str) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValidationError(f"{label} must be timezone-aware")


def _require_canonical(device_id: DeviceId) -> None:
    if not isinstance(device_id, DeviceId) or device_id != DeviceId.parse(
        str(device_id)
    ):
        raise ValidationError("device ID must be canonical")


@dataclass(frozen=True, slots=True)
class Device:
    device_id: DeviceId
    manufacturer: str
    model: str
    endpoints: tuple[EndpointId, ...]

    def __post_init__(self) -> None:
        _require_nonblank(self.manufacturer, "manufacturer")
        _require_nonblank(self.model, "model")
        if self.endpoints != tuple(sorted(set(self.endpoints))):
            raise ValidationError("endpoints must be sorted and unique")


@dataclass(frozen=True, slots=True)
class BackendBinding:
    device_id: DeviceId
    backend: str
    local_id: str
    bound_at: datetime

    def __post_init__(self) -> None:
        _require_nonblank(self.backend, "backend")
        _require_nonblank(self.local_id, "local ID")
        _require_aware(self.bound_at, "bound_at")


@dataclass(frozen=True, slots=True)
class BridgeAvailability:
    state: AvailabilityState
    observed_at: datetime
    source: str
    detail: str | None = None

    def __post_init__(self) -> None:
        _require_aware(self.observed_at, "observed_at")
        _require_nonblank(self.source, "source")


@dataclass(frozen=True, slots=True)
class DeviceAvailability:
    device_id: DeviceId
    state: AvailabilityState
    observed_at: datetime
    source: str
    detail: str | None = None

    def __post_init__(self) -> None:
        _require_canonical(self.device_id)
        _require_aware(self.observed_at, "observed_at")
        _require_nonblank(self.source, "source")
