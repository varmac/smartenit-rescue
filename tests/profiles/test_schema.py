from __future__ import annotations

import json
from copy import deepcopy
from importlib.resources import files
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "minimal-valid.json"


@pytest.fixture
def valid_profile() -> dict[str, Any]:
    value: dict[str, Any] = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    return value


@pytest.fixture(scope="module")
def validator() -> Draft202012Validator:
    schema = json.loads(
        files("smartenit_rescue.profiles")
        .joinpath("schema.json")
        .read_text(encoding="utf-8")
    )
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def assert_rejected(validator: Draft202012Validator, profile: dict[str, Any]) -> None:
    assert list(validator.iter_errors(profile))


def test_minimal_profile_satisfies_schema(
    validator: Draft202012Validator, valid_profile: dict[str, Any]
) -> None:
    validator.validate(valid_profile)


@pytest.mark.parametrize(
    "object_path",
    [
        (),
        ("match",),
        ("match", "harmony"),
        ("endpoints", "1"),
        ("endpoints", "1", "capabilities", 0),
        ("availability",),
        ("limitations", 0),
        ("evidence", 0),
    ],
)
def test_schema_rejects_additional_properties_at_every_object_level(
    validator: Draft202012Validator,
    valid_profile: dict[str, Any],
    object_path: tuple[str | int, ...],
) -> None:
    current: Any = valid_profile
    for part in object_path:
        current = current[part]
    current["unexpected"] = "value"

    assert_rejected(validator, valid_profile)


def test_schema_rejects_additional_normalization_properties(
    validator: Draft202012Validator, valid_profile: dict[str, Any]
) -> None:
    normalization = {"multiplier": 1, "divisor": 1, "unexpected": True}
    valid_profile["endpoints"]["1"]["capabilities"][0]["normalization"] = normalization

    assert_rejected(validator, valid_profile)


@pytest.mark.parametrize("version", [0, 2, True, "1"])
def test_schema_accepts_only_integer_version_one(
    validator: Draft202012Validator,
    valid_profile: dict[str, Any],
    version: object,
) -> None:
    valid_profile["schema_version"] = version

    assert_rejected(validator, valid_profile)


@pytest.mark.parametrize(
    "profile_id",
    [
        "example",
        "Example.switch",
        ".example.switch",
        "example.switch.",
        "example..switch",
        "example.synthetic_switch",
        "example.-switch",
    ],
)
def test_schema_rejects_invalid_profile_ids(
    validator: Draft202012Validator,
    valid_profile: dict[str, Any],
    profile_id: str,
) -> None:
    valid_profile["profile_id"] = profile_id

    assert_rejected(validator, valid_profile)


def test_schema_requires_public_evidence(
    validator: Draft202012Validator, valid_profile: dict[str, Any]
) -> None:
    valid_profile.pop("evidence")

    assert_rejected(validator, valid_profile)


@pytest.mark.parametrize("models", [[], [""], ["EXAMPLE-SWITCH", "EXAMPLE-SWITCH"]])
def test_schema_rejects_invalid_model_lists(
    validator: Draft202012Validator,
    valid_profile: dict[str, Any],
    models: list[str],
) -> None:
    valid_profile["models"] = models

    assert_rejected(validator, valid_profile)


@pytest.mark.parametrize("endpoint", ["0", "00", "01", "241", "endpoint-1"])
def test_schema_rejects_invalid_endpoint_keys(
    validator: Draft202012Validator,
    valid_profile: dict[str, Any],
    endpoint: str,
) -> None:
    valid_profile["endpoints"][endpoint] = valid_profile["endpoints"].pop("1")

    assert_rejected(validator, valid_profile)


def test_schema_requires_an_endpoint_and_capability(
    validator: Draft202012Validator, valid_profile: dict[str, Any]
) -> None:
    no_endpoints = deepcopy(valid_profile)
    no_endpoints["endpoints"] = {}
    valid_profile["endpoints"]["1"]["capabilities"] = []

    assert_rejected(validator, no_endpoints)
    assert_rejected(validator, valid_profile)


def test_schema_requires_boolean_capability_writability(
    validator: Draft202012Validator, valid_profile: dict[str, Any]
) -> None:
    capability = valid_profile["endpoints"]["1"]["capabilities"][0]
    missing = deepcopy(valid_profile)
    missing["endpoints"]["1"]["capabilities"][0].pop("writable")

    capability["writable"] = 1

    assert_rejected(validator, missing)
    assert_rejected(validator, valid_profile)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", "toggle"),
        ("support_level", "verified"),
        ("readback", "sometimes"),
    ],
)
def test_schema_rejects_unknown_capability_vocabulary(
    validator: Draft202012Validator,
    valid_profile: dict[str, Any],
    field: str,
    value: str,
) -> None:
    valid_profile["endpoints"]["1"]["capabilities"][0][field] = value

    assert_rejected(validator, valid_profile)


@pytest.mark.parametrize(
    "driver",
    [
        "__import__('os').system('false')",
        "zigbee.on_off()",
        "zigbee/on_off",
        "Zigbee.on_off",
        "zigbee..on_off",
    ],
)
def test_schema_rejects_executable_looking_driver_strings(
    validator: Draft202012Validator,
    valid_profile: dict[str, Any],
    driver: str,
) -> None:
    valid_profile["endpoints"]["1"]["capabilities"][0]["driver"] = driver

    assert_rejected(validator, valid_profile)


@pytest.mark.parametrize(
    "url",
    ["file:///tmp/device-guide", "ftp://example.invalid/guide", "not a URL"],
)
def test_schema_rejects_non_http_evidence_urls(
    validator: Draft202012Validator,
    valid_profile: dict[str, Any],
    url: str,
) -> None:
    valid_profile["evidence"][0]["url"] = url

    assert_rejected(validator, valid_profile)


def test_schema_rejects_duplicate_capabilities_on_one_endpoint(
    validator: Draft202012Validator, valid_profile: dict[str, Any]
) -> None:
    duplicate = deepcopy(valid_profile["endpoints"]["1"]["capabilities"][0])
    duplicate["driver"] = "zigbee.on_off.alternate"
    valid_profile["endpoints"]["1"]["capabilities"].append(duplicate)

    assert_rejected(validator, valid_profile)


@pytest.mark.parametrize("unit", ["", "   ", 1, {"name": "W"}])
def test_schema_rejects_invalid_units(
    validator: Draft202012Validator,
    valid_profile: dict[str, Any],
    unit: object,
) -> None:
    valid_profile["endpoints"]["1"]["capabilities"][0]["unit"] = unit

    assert_rejected(validator, valid_profile)


@pytest.mark.parametrize(
    "normalization",
    [
        {},
        {"multiplier": 1},
        {"multiplier": "1", "divisor": 1},
        {"multiplier": 1, "divisor": 0},
    ],
)
def test_schema_rejects_invalid_normalization(
    validator: Draft202012Validator,
    valid_profile: dict[str, Any],
    normalization: object,
) -> None:
    valid_profile["endpoints"]["1"]["capabilities"][0]["normalization"] = normalization

    assert_rejected(validator, valid_profile)


@pytest.mark.parametrize("freshness", [0, -1, True])
def test_schema_rejects_invalid_freshness(
    validator: Draft202012Validator,
    valid_profile: dict[str, Any],
    freshness: object,
) -> None:
    valid_profile["availability"]["stale_after_seconds"] = freshness

    assert_rejected(validator, valid_profile)


def test_schema_rejects_unknown_harmony_rules(
    validator: Draft202012Validator, valid_profile: dict[str, Any]
) -> None:
    valid_profile["match"]["harmony"]["expression"] = "processor == 'OnOff'"

    assert_rejected(validator, valid_profile)


def test_schema_rejects_limitation_for_undeclared_capability(
    validator: Draft202012Validator, valid_profile: dict[str, Any]
) -> None:
    valid_profile["limitations"][0]["capability"] = "electrical_power"

    assert_rejected(validator, valid_profile)
