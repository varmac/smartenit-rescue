from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

from smartenit_rescue.errors import ValidationError
from smartenit_rescue.models.capability import CapabilityId, SupportLevel
from smartenit_rescue.models.identity import EndpointId
from smartenit_rescue.profiles import (
    AvailabilityDeviceClass,
    DeviceEvidence,
    EvidenceKind,
    Normalization,
    Readback,
    load_profile,
    load_profile_data,
    match_profile,
)
from smartenit_rescue.profiles.loader import _load_profile_json

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "minimal-valid.json"


@pytest.fixture
def fixture_path() -> Path:
    return FIXTURE_PATH


@pytest.fixture
def valid_data() -> dict[str, Any]:
    value: dict[str, Any] = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    return value


def matching_evidence(**changes: object) -> DeviceEvidence:
    values: dict[str, object] = {
        "manufacturer": "Example Devices",
        "model": "EXAMPLE-SWITCH",
        "zigbee_manufacturer_id": 4660,
        "endpoint_clusters": {EndpointId(1): frozenset({6, 1794})},
        "harmony_processors": frozenset({"OnOff"}),
        "harmony_components": frozenset({"1"}),
    }
    values.update(changes)
    return DeviceEvidence(**values)  # type: ignore[arg-type]


def task_5_profile_data(*, device_class: object = "active") -> dict[str, object]:
    return {
        "schema_version": 1,
        "profile_id": "smartenit.4040c",
        "manufacturer": "Compacta International, Ltd.",
        "models": ["ZBMLCSR"],
        "match": {
            "zigbee_manufacturer_ids": [4213],
            "zigbee_model_ids": ["ZBMLCSR"],
            "required_endpoint_clusters": {"1": [6, 1794]},
            "harmony": {
                "required_processors": [],
                "required_components": [],
            },
        },
        "endpoints": {
            "1": {
                "capabilities": [
                    {
                        "id": "on_off",
                        "driver": "zigbee.on_off",
                        "cluster_id": 6,
                        "readback": "available",
                        "writable": True,
                        "support_level": "synthetic",
                        "unit": None,
                        "normalization": None,
                    },
                    {
                        "id": "electrical_power",
                        "driver": "zigbee.metering.power",
                        "cluster_id": 1794,
                        "readback": "available",
                        "writable": False,
                        "support_level": "synthetic",
                        "unit": "W",
                        "normalization": None,
                    },
                    {
                        "id": "electrical_energy",
                        "driver": "zigbee.metering.energy",
                        "cluster_id": 1794,
                        "readback": "available",
                        "writable": False,
                        "support_level": "synthetic",
                        "unit": "kWh",
                        "normalization": None,
                    },
                    {
                        "id": "voltage",
                        "driver": "zigbee.metering.voltage",
                        "cluster_id": 1794,
                        "readback": "available",
                        "writable": False,
                        "support_level": "synthetic",
                        "unit": "V",
                        "normalization": None,
                    },
                    {
                        "id": "current",
                        "driver": "zigbee.metering.current",
                        "cluster_id": 1794,
                        "readback": "available",
                        "writable": False,
                        "support_level": "synthetic",
                        "unit": "A",
                        "normalization": None,
                    },
                ]
            }
        },
        "availability": {
            "device_class": device_class,
            "stale_after_seconds": 600,
        },
        "limitations": [
            {
                "backend": "harmony_g2",
                "capability": "on_off",
                "description": (
                    "Harmony G2 state readback is not yet publicly verified"
                ),
            },
            {
                "backend": "all",
                "capability": "electrical_power",
                "description": (
                    "Metering values and scaling require hardware verification"
                ),
            },
        ],
        "evidence": [
            {
                "kind": "public_documentation",
                "url": ("https://docs.smartenit.com/zbmlcsr/zbmlcsr_product-brief.pdf"),
                "description": (
                    "4040C identity, manufacturer ID, endpoints, clusters, and metering"
                ),
            },
            {
                "kind": "public_documentation",
                "url": ("https://docs.smartenit.com/zbmlcsr/zbmlcsr_quick-start.pdf"),
                "description": (
                    "4040C operation, indicators, joining, and local control"
                ),
            },
        ],
    }


def test_load_profile_constructs_typed_profile(fixture_path: Path) -> None:
    profile = load_profile(fixture_path)
    endpoint = profile.endpoints[EndpointId(1)]
    capability = endpoint.capabilities[0]

    assert profile.schema_version == 1
    assert profile.profile_id == "example.synthetic-switch"
    assert profile.models == ("EXAMPLE-SWITCH",)
    assert endpoint.endpoint == EndpointId(1)
    assert capability.capability is CapabilityId.ON_OFF
    assert capability.cluster_id == 6
    assert capability.readback is Readback.AVAILABLE
    assert capability.writable is True
    assert capability.support_level is SupportLevel.SYNTHETIC
    assert profile.availability.device_class is AvailabilityDeviceClass.ACTIVE
    assert profile.evidence[0].kind is EvidenceKind.PUBLIC_DOCUMENTATION


def test_task_5_profile_contract_loads_without_schema_extension() -> None:
    profile = load_profile_data(task_5_profile_data(), source="task-5-profile.json")

    assert profile.profile_id == "smartenit.4040c"
    assert tuple(
        capability.capability
        for capability in profile.endpoints[EndpointId(1)].capabilities
    ) == (
        CapabilityId.ON_OFF,
        CapabilityId.ELECTRICAL_POWER,
        CapabilityId.ELECTRICAL_ENERGY,
        CapabilityId.VOLTAGE,
        CapabilityId.CURRENT,
    )
    capabilities = profile.endpoints[EndpointId(1)].capabilities
    assert capabilities[0].writable is True
    assert all(not item.writable for item in capabilities[1:])


@pytest.mark.parametrize("writable", [None, 0, 1, "true"])
def test_loader_rejects_non_boolean_writability(
    valid_data: dict[str, Any], writable: object
) -> None:
    valid_data["endpoints"]["1"]["capabilities"][0]["writable"] = writable

    with pytest.raises(ValidationError, match="invalid type"):
        load_profile_data(valid_data, source="memory")


def test_loader_rejects_missing_writability(valid_data: dict[str, Any]) -> None:
    valid_data["endpoints"]["1"]["capabilities"][0].pop("writable")

    with pytest.raises(ValidationError, match="missing a required property"):
        load_profile_data(valid_data, source="memory")


def test_load_profile_preserves_tuple_order_and_freezes_maps(
    valid_data: dict[str, Any],
) -> None:
    second = deepcopy(valid_data["evidence"][0])
    second["url"] = "https://example.invalid/second-guide"
    second["description"] = "Second synthetic item"
    valid_data["models"].append("EXAMPLE-SWITCH-V2")
    valid_data["evidence"].append(second)

    profile = load_profile_data(valid_data, source="memory")

    assert profile.models == ("EXAMPLE-SWITCH", "EXAMPLE-SWITCH-V2")
    assert tuple(item.url for item in profile.evidence) == (
        "https://example.invalid/device-guide",
        "https://example.invalid/second-guide",
    )
    assert isinstance(profile.endpoints, MappingProxyType)
    assert isinstance(profile.match.required_endpoint_clusters, MappingProxyType)
    with pytest.raises(TypeError):
        profile.endpoints[EndpointId(2)] = profile.endpoints[EndpointId(1)]  # type: ignore[index]
    with pytest.raises(TypeError):
        profile.match.required_endpoint_clusters[EndpointId(1)] = frozenset()  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        profile.profile_id = "changed.profile"  # type: ignore[misc]


def test_load_profile_constructs_normalization(valid_data: dict[str, Any]) -> None:
    capability = valid_data["endpoints"]["1"]["capabilities"][0]
    capability["normalization"] = {"multiplier": 2, "divisor": 5}

    profile = load_profile_data(valid_data, source="memory")

    assert profile.endpoints[EndpointId(1)].capabilities[0].normalization == (
        Normalization(multiplier=2.0, divisor=5.0)
    )


def test_json_parse_error_is_bounded_and_located(tmp_path: Path) -> None:
    path = tmp_path / "invalid.json"
    payload_marker = "do-not-repeat-this-payload"
    path.write_text('{"profile": "' + payload_marker + '"', encoding="utf-8")

    with pytest.raises(ValidationError) as caught:
        load_profile(path)

    message = str(caught.value)
    assert str(path) in message
    assert "line 1 column" in message
    assert payload_marker not in message


def test_deep_json_rejection_is_bounded_and_located() -> None:
    payload_marker = "do-not-echo-recursive-profile-payload"
    raw_json = "[" * 2000 + f'"{payload_marker}"' + "]" * 2000

    with pytest.raises(ValidationError) as caught:
        _load_profile_json(raw_json, source="probe.json")

    message = str(caught.value)
    # JSON parsing may hit a recursion limit or finish before root-type validation.
    assert message in {
        "probe.json: $: invalid JSON",
        "probe.json: $: must be an object",
    }
    assert payload_marker not in message


def test_schema_error_is_bounded_and_located(valid_data: dict[str, Any]) -> None:
    payload_marker = "do-not-repeat-this-payload"
    valid_data["unexpected"] = payload_marker

    with pytest.raises(ValidationError) as caught:
        load_profile_data(valid_data, source="synthetic-profile.json")

    message = str(caught.value)
    assert message.startswith("synthetic-profile.json: $:")
    assert payload_marker not in message
    assert json.dumps(valid_data) not in message


def test_schema_error_does_not_echo_rejected_value() -> None:
    profile = task_5_profile_data()
    payload_marker = "attacker-controlled-value-7d6f3b8c"
    profile["profile_id"] = payload_marker

    with pytest.raises(ValidationError) as caught:
        load_profile_data(profile, source="untrusted-profile.json")

    message = str(caught.value)
    assert message.startswith('untrusted-profile.json: $["profile_id"]:')
    assert payload_marker not in message


def test_schema_error_does_not_echo_unknown_property_name() -> None:
    profile = task_5_profile_data()
    unknown_name = "private-secret-property-6ac9e4"
    profile[unknown_name] = "value"

    with pytest.raises(ValidationError) as caught:
        load_profile_data(profile, source="untrusted-profile.json")

    message = str(caught.value)
    assert message.startswith("untrusted-profile.json: $:")
    assert unknown_name not in message


def test_schema_error_is_bounded_for_oversized_rejected_value() -> None:
    oversized_value = "x" * 100_000
    profile = task_5_profile_data(device_class=oversized_value)

    with pytest.raises(ValidationError) as caught:
        load_profile_data(profile, source="untrusted-profile.json")

    message = str(caught.value)
    assert oversized_value not in message
    assert len(message) <= 160


def test_schema_errors_are_sorted_by_structural_location(
    valid_data: dict[str, Any],
) -> None:
    valid_data["manufacturer"] = ""
    valid_data["models"] = []

    with pytest.raises(ValidationError) as caught:
        load_profile_data(valid_data, source="memory")

    assert str(caught.value).startswith('memory: $["manufacturer"]:')


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("schema_version",), 1.0),
        (("match", "zigbee_manufacturer_ids", 0), 4660.0),
        (("endpoints", "1", "capabilities", 0, "cluster_id"), 6.0),
        (("availability", "stale_after_seconds"), 600.0),
    ],
)
def test_loader_rejects_integral_floats_where_integers_are_expected(
    valid_data: dict[str, Any], path: tuple[str | int, ...], value: float
) -> None:
    current: Any = valid_data
    for part in path[:-1]:
        current = current[part]
    current[path[-1]] = value

    with pytest.raises(ValidationError, match="must be an integer"):
        load_profile_data(valid_data, source="memory")


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan")])
def test_loader_rejects_nonfinite_normalization(
    valid_data: dict[str, Any], value: float
) -> None:
    valid_data["endpoints"]["1"]["capabilities"][0]["normalization"] = {
        "multiplier": value,
        "divisor": 1,
    }

    with pytest.raises(ValidationError, match="finite"):
        load_profile_data(valid_data, source="memory")


def test_loader_rejects_oversized_normalization_integer_as_validation_error(
    valid_data: dict[str, Any],
) -> None:
    oversized_integer = 10**400
    valid_data["endpoints"]["1"]["capabilities"][0]["normalization"] = {
        "multiplier": oversized_integer,
        "divisor": 1,
    }

    with pytest.raises(ValidationError, match="finite") as caught:
        load_profile_data(valid_data, source="oversized-profile.json")

    assert str(oversized_integer) not in str(caught.value)


def test_loader_rejects_duplicate_capabilities_semantically(
    valid_data: dict[str, Any],
) -> None:
    duplicate = deepcopy(valid_data["endpoints"]["1"]["capabilities"][0])
    valid_data["endpoints"]["1"]["capabilities"].append(duplicate)

    with pytest.raises(ValidationError, match="capabilities"):
        load_profile_data(valid_data, source="memory")


def test_loader_never_evaluates_driver_strings(
    valid_data: dict[str, Any], tmp_path: Path
) -> None:
    marker = tmp_path / "executed"
    valid_data["endpoints"]["1"]["capabilities"][0]["driver"] = (
        f"__import__('pathlib').Path({str(marker)!r}).touch()"
    )

    with pytest.raises(ValidationError):
        load_profile_data(valid_data, source="memory")

    assert not marker.exists()


def test_match_profile_requires_exact_model(fixture_path: Path) -> None:
    profile = load_profile(fixture_path)
    evidence = matching_evidence()

    assert match_profile(profile, evidence)
    assert not match_profile(profile, replace(evidence, model="example-switch"))


@pytest.mark.parametrize(
    "changes",
    [
        {"manufacturer": "Other Devices"},
        {"zigbee_manufacturer_id": None},
        {"zigbee_manufacturer_id": 4661},
        {"endpoint_clusters": {}},
        {"endpoint_clusters": {EndpointId(1): frozenset({6})}},
        {"harmony_processors": frozenset()},
        {"harmony_components": frozenset()},
    ],
)
def test_match_profile_rejects_missing_or_conflicting_required_evidence(
    fixture_path: Path, changes: dict[str, object]
) -> None:
    profile = load_profile(fixture_path)

    assert not match_profile(profile, matching_evidence(**changes))


def test_match_profile_allows_additional_device_evidence(fixture_path: Path) -> None:
    profile = load_profile(fixture_path)
    evidence = matching_evidence(
        endpoint_clusters={EndpointId(1): frozenset({6, 8, 1794})},
        harmony_processors=frozenset({"OnOff", "Diagnostics"}),
        harmony_components=frozenset({"1", "status"}),
    )

    assert match_profile(profile, evidence)


def test_empty_optional_match_rules_do_not_require_backend_evidence(
    valid_data: dict[str, Any],
) -> None:
    valid_data["match"]["zigbee_manufacturer_ids"] = []
    valid_data["match"]["zigbee_model_ids"] = []
    valid_data["match"]["required_endpoint_clusters"] = {}
    valid_data["match"]["harmony"]["required_processors"] = []
    valid_data["match"]["harmony"]["required_components"] = []
    profile = load_profile_data(valid_data, source="memory")
    evidence = DeviceEvidence(
        manufacturer="Example Devices",
        model="EXAMPLE-SWITCH",
        zigbee_manufacturer_id=None,
        endpoint_clusters={},
        harmony_processors=frozenset(),
        harmony_components=frozenset(),
    )

    assert match_profile(profile, evidence)
