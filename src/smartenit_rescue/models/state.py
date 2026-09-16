"""Observed state and explicit command outcome contracts."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Generic, TypeVar

from smartenit_rescue.errors import ValidationError

from .capability import CapabilityId
from .identity import DeviceId, EndpointId

T = TypeVar("T")
T_co = TypeVar("T_co", covariant=True)


class Confidence(StrEnum):
    OBSERVED = "observed"
    ASSUMED = "assumed"
    STALE = "stale"
    UNKNOWN = "unknown"


class AvailabilityState(StrEnum):
    ONLINE = "online"
    OFFLINE = "offline"
    UNKNOWN = "unknown"


class CommandStatus(StrEnum):
    REJECTED = "rejected"
    ACCEPTED = "accepted"
    CONFIRMED = "confirmed"
    INDETERMINATE = "indeterminate"


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


@dataclass(frozen=True, slots=True)
class Observation(Generic[T_co]):
    value: T_co
    source: str
    confidence: Confidence
    observed_at: datetime
    sequence: int

    def __post_init__(self) -> None:
        _require_nonblank(self.source, "source")
        _require_aware(self.observed_at, "observed_at")
        if (
            isinstance(self.sequence, bool)
            or not isinstance(self.sequence, int)
            or self.sequence < 0
        ):
            raise ValidationError("sequence must be a nonnegative integer")


@dataclass(frozen=True, slots=True)
class Command(Generic[T]):
    request_id: str
    device_id: DeviceId
    endpoint: EndpointId
    capability: CapabilityId
    desired: T

    def __post_init__(self) -> None:
        _require_nonblank(self.request_id, "request ID")
        if self.desired is None:
            raise ValidationError("desired value must not be None")


@dataclass(frozen=True, slots=True)
class CommandResult(Generic[T_co]):
    request_id: str
    status: CommandStatus
    observation: Observation[T_co] | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        _require_nonblank(self.request_id, "request ID")
        if self.status is CommandStatus.CONFIRMED and self.observation is None:
            raise ValidationError("confirmed command result requires an observation")
        if (
            self.status is CommandStatus.CONFIRMED
            and self.observation is not None
            and self.observation.confidence is not Confidence.OBSERVED
        ):
            raise ValidationError(
                "confirmed command result requires observed confidence"
            )
        if self.status in {
            CommandStatus.REJECTED,
            CommandStatus.INDETERMINATE,
        } and (not isinstance(self.reason, str) or not self.reason.strip()):
            raise ValidationError(
                "rejected or indeterminate command result requires a nonblank reason"
            )
