from __future__ import annotations

from datetime import UTC, datetime

import pytest

from smartenit_rescue.errors import ValidationError
from smartenit_rescue.models.capability import CapabilityId
from smartenit_rescue.models.identity import DeviceId, EndpointId
from smartenit_rescue.models.state import (
    AvailabilityState,
    Command,
    CommandResult,
    CommandStatus,
    Confidence,
    Observation,
)


def test_state_vocabularies_are_stable() -> None:
    assert [item.value for item in Confidence] == [
        "observed",
        "assumed",
        "stale",
        "unknown",
    ]
    assert [item.value for item in AvailabilityState] == [
        "online",
        "offline",
        "unknown",
    ]
    assert [item.value for item in CommandStatus] == [
        "rejected",
        "accepted",
        "confirmed",
        "indeterminate",
    ]


def test_command_status_does_not_collapse_acceptance_into_confirmation() -> None:
    assert CommandStatus.ACCEPTED is not CommandStatus.CONFIRMED  # type: ignore[comparison-overlap]
    assert CommandStatus.INDETERMINATE.value == "indeterminate"


def test_observation_requires_aware_time() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        Observation(
            value=True,
            source="simulator",
            confidence=Confidence.OBSERVED,
            observed_at=datetime(2026, 1, 1),  # noqa: DTZ001
            sequence=1,
        )


@pytest.mark.parametrize("source", ["", " ", "\t\n"])
def test_observation_requires_nonblank_source(source: str) -> None:
    with pytest.raises(ValidationError, match="source"):
        Observation(
            value=True,
            source=source,
            confidence=Confidence.OBSERVED,
            observed_at=datetime(2026, 1, 1, tzinfo=UTC),
            sequence=1,
        )


@pytest.mark.parametrize("sequence", [-1, True, False])
def test_observation_rejects_invalid_sequence(sequence: int) -> None:
    with pytest.raises(ValidationError, match="sequence"):
        Observation(
            value=True,
            source="simulator",
            confidence=Confidence.OBSERVED,
            observed_at=datetime(2026, 1, 1, tzinfo=UTC),
            sequence=sequence,
        )


def test_observation_is_immutable_and_covariant() -> None:
    observation: Observation[bool] = Observation(
        value=True,
        source="simulator",
        confidence=Confidence.OBSERVED,
        observed_at=datetime(2026, 1, 1, tzinfo=UTC),
        sequence=0,
    )
    broader_observation: Observation[object] = observation

    assert broader_observation.value is True
    with pytest.raises(AttributeError):
        observation.value = False  # type: ignore[misc]


def test_command_targets_one_explicit_capability() -> None:
    command = Command(
        request_id="req-0001",
        device_id=DeviceId.parse("0200000000000001"),
        endpoint=EndpointId(1),
        capability=CapabilityId.ON_OFF,
        desired=True,
    )
    assert command.desired is True


def test_command_cannot_receive_invalid_direct_device_id() -> None:
    with pytest.raises(ValidationError, match="canonical"):
        Command(
            request_id="req-0001",
            device_id=DeviceId("invalid"),
            endpoint=EndpointId(1),
            capability=CapabilityId.ON_OFF,
            desired=True,
        )


@pytest.mark.parametrize("request_id", ["", " ", "\t\n"])
def test_command_requires_nonblank_request_id(request_id: str) -> None:
    with pytest.raises(ValidationError, match="request ID"):
        Command(
            request_id=request_id,
            device_id=DeviceId.parse("0200000000000001"),
            endpoint=EndpointId(1),
            capability=CapabilityId.ON_OFF,
            desired=True,
        )


def test_command_rejects_none_as_a_desired_value() -> None:
    with pytest.raises(ValidationError, match="desired"):
        Command(
            request_id="req-0001",
            device_id=DeviceId.parse("0200000000000001"),
            endpoint=EndpointId(1),
            capability=CapabilityId.ON_OFF,
            desired=None,
        )


def test_confirmed_command_result_requires_observation() -> None:
    with pytest.raises(ValidationError, match="confirmed"):
        CommandResult[bool](
            request_id="req-0001",
            status=CommandStatus.CONFIRMED,
            observation=None,
        )


@pytest.mark.parametrize(
    "confidence",
    [Confidence.ASSUMED, Confidence.STALE, Confidence.UNKNOWN],
)
def test_confirmed_command_result_requires_observed_confidence(
    confidence: Confidence,
) -> None:
    observation = Observation(
        value=True,
        source="simulator",
        confidence=confidence,
        observed_at=datetime(2026, 1, 1, tzinfo=UTC),
        sequence=1,
    )

    with pytest.raises(ValidationError, match="observed confidence"):
        CommandResult(
            request_id="req-0001",
            status=CommandStatus.CONFIRMED,
            observation=observation,
        )


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (CommandStatus.REJECTED, None),
        (CommandStatus.REJECTED, " "),
        (CommandStatus.INDETERMINATE, None),
        (CommandStatus.INDETERMINATE, " "),
    ],
)
def test_unsuccessful_command_result_requires_nonblank_reason(
    status: CommandStatus, reason: str | None
) -> None:
    with pytest.raises(ValidationError, match="reason"):
        CommandResult[bool](
            request_id="req-0001",
            status=status,
            reason=reason,
        )


def test_accepted_command_result_does_not_require_observation() -> None:
    result = CommandResult[bool](
        request_id="req-0001",
        status=CommandStatus.ACCEPTED,
    )

    assert result.observation is None


def test_command_result_requires_nonblank_request_id() -> None:
    with pytest.raises(ValidationError, match="request ID"):
        CommandResult[bool](request_id=" ", status=CommandStatus.ACCEPTED)
