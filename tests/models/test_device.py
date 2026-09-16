from __future__ import annotations

from dataclasses import FrozenInstanceError, dataclass
from datetime import UTC, datetime

import pytest

from smartenit_rescue.errors import ValidationError
from smartenit_rescue.models.device import (
    BackendBinding,
    BridgeAvailability,
    Device,
    DeviceAvailability,
)
from smartenit_rescue.models.identity import DeviceId, EndpointId
from smartenit_rescue.models.state import AvailabilityState

DEVICE_ID = DeviceId.parse("0200000000000001")
AWARE_TIME = datetime(2026, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class AvailabilityFactory:
    availability_type: type[BridgeAvailability | DeviceAvailability]
    includes_device_id: bool


def test_device_is_an_immutable_snapshot_with_sorted_unique_endpoints() -> None:
    device = Device(
        device_id=DEVICE_ID,
        manufacturer="Smartenit",
        model="ZBLC15",
        endpoints=(EndpointId(1), EndpointId(2)),
    )

    assert device.endpoints == (EndpointId(1), EndpointId(2))
    with pytest.raises(FrozenInstanceError):
        device.model = "changed"  # type: ignore[misc]


def test_device_cannot_receive_noncanonical_direct_device_id() -> None:
    with pytest.raises(ValidationError, match="canonical"):
        Device(
            device_id=DeviceId("020000000000000A"),
            manufacturer="Smartenit",
            model="ZBLC15",
            endpoints=(EndpointId(1),),
        )


@pytest.mark.parametrize(
    "endpoints",
    [
        (EndpointId(2), EndpointId(1)),
        (EndpointId(1), EndpointId(1)),
    ],
)
def test_device_rejects_unsorted_or_duplicate_endpoints(
    endpoints: tuple[EndpointId, ...],
) -> None:
    with pytest.raises(ValidationError, match="sorted and unique"):
        Device(
            device_id=DEVICE_ID,
            manufacturer="Smartenit",
            model="ZBLC15",
            endpoints=endpoints,
        )


@pytest.mark.parametrize(
    ("manufacturer", "model"),
    [("", "ZBLC15"), (" ", "ZBLC15"), ("Smartenit", ""), ("Smartenit", " ")],
)
def test_device_requires_nonblank_descriptive_metadata(
    manufacturer: str, model: str
) -> None:
    with pytest.raises(ValidationError, match="manufacturer|model"):
        Device(
            device_id=DEVICE_ID,
            manufacturer=manufacturer,
            model=model,
            endpoints=(EndpointId(1),),
        )


@pytest.mark.parametrize(
    ("backend", "local_id"),
    [("", "abc"), (" ", "abc"), ("zigbee", ""), ("zigbee", " ")],
)
def test_backend_binding_requires_nonblank_identifiers(
    backend: str, local_id: str
) -> None:
    with pytest.raises(ValidationError, match="backend|local ID"):
        BackendBinding(
            device_id=DEVICE_ID,
            backend=backend,
            local_id=local_id,
            bound_at=AWARE_TIME,
        )


def test_backend_binding_requires_aware_time() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        BackendBinding(
            device_id=DEVICE_ID,
            backend="zigbee",
            local_id="abc",
            bound_at=datetime(2026, 1, 1),  # noqa: DTZ001
        )


def test_bridge_and_device_availability_are_distinct_types() -> None:
    bridge = BridgeAvailability(
        state=AvailabilityState.ONLINE,
        observed_at=AWARE_TIME,
        source="zigbee",
    )
    device = DeviceAvailability(
        device_id=DEVICE_ID,
        state=AvailabilityState.ONLINE,
        observed_at=AWARE_TIME,
        source="zigbee",
    )

    assert type(bridge) is BridgeAvailability
    assert type(device) is DeviceAvailability
    assert not isinstance(bridge, DeviceAvailability)


@pytest.mark.parametrize(
    "factory",
    [
        AvailabilityFactory(BridgeAvailability, False),
        AvailabilityFactory(DeviceAvailability, True),
    ],
)
def test_availability_requires_aware_time(
    factory: AvailabilityFactory,
) -> None:
    arguments: dict[str, object] = {
        "state": AvailabilityState.UNKNOWN,
        "observed_at": datetime(2026, 1, 1),  # noqa: DTZ001
        "source": "zigbee",
    }
    if factory.includes_device_id:
        arguments["device_id"] = DEVICE_ID

    with pytest.raises(ValidationError, match="timezone-aware"):
        factory.availability_type(**arguments)  # type: ignore[arg-type]


@pytest.mark.parametrize("source", ["", " ", "\t\n"])
def test_availability_requires_nonblank_source(source: str) -> None:
    with pytest.raises(ValidationError, match="source"):
        BridgeAvailability(
            state=AvailabilityState.UNKNOWN,
            observed_at=AWARE_TIME,
            source=source,
        )


def test_device_availability_requires_canonical_device_identity() -> None:
    with pytest.raises(ValidationError, match="canonical"):
        DeviceAvailability(
            device_id=DeviceId("020000000000000A"),
            state=AvailabilityState.UNKNOWN,
            observed_at=AWARE_TIME,
            source="zigbee",
        )
