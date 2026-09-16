from __future__ import annotations

import pytest

from smartenit_rescue.errors import ValidationError
from smartenit_rescue.models.identity import DeviceId, EndpointId


@pytest.mark.parametrize(
    "raw",
    [
        "0200000000000001",
        "0x0200000000000001",
        "02:00:00:00:00:00:00:01",
        "02-00-00-00-00-00-00-01",
    ],
)
def test_device_id_normalizes_eui64(raw: str) -> None:
    assert str(DeviceId.parse(raw)) == "0200000000000001"


def test_device_id_normalization_supports_lowercase_and_equality() -> None:
    assert DeviceId.parse("0X020000000000000A") == DeviceId("020000000000000a")


@pytest.mark.parametrize(
    "value",
    ["invalid", "020000000000000A", "02:00:00:00:00:00:00:01"],
)
def test_device_id_direct_construction_rejects_noncanonical_storage(
    value: str,
) -> None:
    with pytest.raises(ValidationError, match="canonical"):
        DeviceId(value)


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "020000000000001",
        "02000000000000011",
        "020000000000000g",
        "02:00:00:00:00:00:01",
        "02-00-00-00-00-00-00-01-02",
        True,
        0x0200000000000001,
    ],
)
def test_device_id_rejects_invalid_inputs(raw: object) -> None:
    with pytest.raises(ValidationError):
        DeviceId.parse(raw)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [1, 240])
def test_endpoint_id_accepts_bounds(value: int) -> None:
    assert str(EndpointId(value)) == str(value)


@pytest.mark.parametrize("value", [0, 241, True, False, "1"])
def test_endpoint_id_rejects_invalid_values(value: object) -> None:
    with pytest.raises(ValidationError):
        EndpointId(value)  # type: ignore[arg-type]
