"""Validation, construction, and matching for declarative device profiles."""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping
from enum import StrEnum
from importlib.resources import files
from pathlib import Path
from types import MappingProxyType
from typing import TypeVar

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError

from smartenit_rescue.errors import ValidationError
from smartenit_rescue.models.capability import CapabilityId, SupportLevel
from smartenit_rescue.models.identity import EndpointId

from .model import (
    AvailabilityDeviceClass,
    AvailabilityPolicy,
    CapabilityProfile,
    DeviceEvidence,
    DeviceProfile,
    EndpointProfile,
    Evidence,
    EvidenceKind,
    Limitation,
    MatchRules,
    Normalization,
    Readback,
)

EnumType = TypeVar("EnumType", bound=StrEnum)
_MISSING = object()
_UNKNOWN_PATH_PART = "<unknown>"
_SAFE_PATH_PROPERTIES = frozenset(
    {
        "availability",
        "backend",
        "capabilities",
        "capability",
        "cluster_id",
        "description",
        "device_class",
        "divisor",
        "driver",
        "endpoints",
        "evidence",
        "harmony",
        "id",
        "kind",
        "limitations",
        "manufacturer",
        "match",
        "models",
        "multiplier",
        "normalization",
        "profile_id",
        "readback",
        "required_components",
        "required_endpoint_clusters",
        "required_processors",
        "schema_version",
        "stale_after_seconds",
        "support_level",
        "unit",
        "url",
        "writable",
        "zigbee_manufacturer_ids",
        "zigbee_model_ids",
    }
)
_SCHEMA_ERROR_MESSAGES = MappingProxyType(
    {
        "additionalProperties": "contains an unsupported property",
        "const": "does not equal the required value",
        "contains": "does not contain a required item",
        "enum": "contains an unsupported value",
        "maxContains": "contains too many matching items",
        "maximum": "exceeds the allowed maximum",
        "maxItems": "contains too many items",
        "maxProperties": "contains too many properties",
        "minContains": "does not contain enough matching items",
        "minimum": "is below the allowed minimum",
        "minItems": "does not contain enough items",
        "minLength": "is too short",
        "minProperties": "does not contain enough properties",
        "not": "contains a disallowed value",
        "oneOf": "does not match exactly one allowed shape",
        "pattern": "has an invalid format",
        "required": "is missing a required property",
        "type": "has an invalid type",
        "uniqueItems": "contains duplicate items",
    }
)


def _load_schema() -> Mapping[str, object]:
    raw = (
        files("smartenit_rescue.profiles")
        .joinpath("schema.json")
        .read_text(encoding="utf-8")
    )
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise TypeError("packaged profile schema must be a JSON object")
    return value


_SCHEMA = _load_schema()
Draft202012Validator.check_schema(_SCHEMA)
_VALIDATOR = Draft202012Validator(_SCHEMA)


def _safe_path_part(part: str) -> str:
    if part in _SAFE_PATH_PROPERTIES:
        return part
    if 1 <= len(part) <= 3 and part.isascii() and part.isdigit():
        endpoint = int(part)
        if str(endpoint) == part and 1 <= endpoint <= 240:
            return part
    return _UNKNOWN_PATH_PART


def _error_sort_key(error: JsonSchemaValidationError) -> tuple[tuple[int, object], ...]:
    return tuple(
        (0, part) if isinstance(part, int) else (1, _safe_path_part(str(part)))
        for part in error.absolute_path
    )


def _location(parts: Iterable[str | int]) -> str:
    location = "$"
    for part in parts:
        if isinstance(part, int):
            location += f"[{part}]"
        else:
            location += f"[{json.dumps(_safe_path_part(str(part)))}]"
    return location


def _schema_error_message(error: JsonSchemaValidationError) -> str:
    validator = error.validator
    if isinstance(validator, str):
        message = _SCHEMA_ERROR_MESSAGES.get(validator)
        if message is not None:
            return message
    return "does not satisfy the profile schema"


def _invalid(source: str, location: str, message: str) -> ValidationError:
    return ValidationError(f"{source}: {location}: {message}")


def _validate_schema(data: Mapping[str, object], source: str) -> dict[str, object]:
    document = dict(data)
    try:
        ranked_errors = sorted(
            (_error_sort_key(error), index, error)
            for index, error in enumerate(_VALIDATOR.iter_errors(document))
        )
    except (TypeError, ValueError) as error:
        raise _invalid(source, "$", "value is not valid JSON profile data") from error
    if ranked_errors:
        first = ranked_errors[0][2]
        raise _invalid(
            source,
            _location(first.absolute_path),
            _schema_error_message(first),
        )
    return document


def _required(
    value: Mapping[str, object], property_name: str, *, source: str, location: str
) -> object:
    item = value.get(property_name, _MISSING)
    if item is _MISSING:
        raise _invalid(source, location, f"missing required property {property_name!r}")
    return item


def _mapping(value: object, *, source: str, location: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise _invalid(source, location, "must be an object")
    if any(not isinstance(key, str) for key in value):
        raise _invalid(source, location, "object keys must be strings")
    return value


def _list(value: object, *, source: str, location: str) -> list[object]:
    if not isinstance(value, list):
        raise _invalid(source, location, "must be an array")
    return value


def _string(value: object, *, source: str, location: str) -> str:
    if not isinstance(value, str):
        raise _invalid(source, location, "must be a string")
    return value


def _integer(value: object, *, source: str, location: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _invalid(source, location, "must be an integer")
    return value


def _boolean(value: object, *, source: str, location: str) -> bool:
    if not isinstance(value, bool):
        raise _invalid(source, location, "must be a boolean")
    return value


def _finite_number(value: object, *, source: str, location: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _invalid(source, location, "must be a finite number")
    try:
        result = float(value)
    except OverflowError as error:
        raise _invalid(source, location, "must be a finite number") from error
    if not math.isfinite(result):
        raise _invalid(source, location, "must be a finite number")
    return result


def _enum(
    enum_type: type[EnumType], value: object, *, source: str, location: str
) -> EnumType:
    raw = _string(value, source=source, location=location)
    try:
        return enum_type(raw)
    except ValueError as error:
        raise _invalid(source, location, "contains an unknown value") from error


def _string_tuple(value: object, *, source: str, location: str) -> tuple[str, ...]:
    items = _list(value, source=source, location=location)
    return tuple(
        _string(item, source=source, location=f"{location}[{index}]")
        for index, item in enumerate(items)
    )


def _string_set(value: object, *, source: str, location: str) -> frozenset[str]:
    items = _string_tuple(value, source=source, location=location)
    if len(items) != len(set(items)):
        raise _invalid(source, location, "must contain unique values")
    return frozenset(items)


def _integer_set(value: object, *, source: str, location: str) -> frozenset[int]:
    items = _list(value, source=source, location=location)
    converted = tuple(
        _integer(item, source=source, location=f"{location}[{index}]")
        for index, item in enumerate(items)
    )
    if len(converted) != len(set(converted)):
        raise _invalid(source, location, "must contain unique values")
    return frozenset(converted)


def _normalization(
    value: object, *, source: str, location: str
) -> Normalization | None:
    if value is None:
        return None
    item = _mapping(value, source=source, location=location)
    multiplier = _finite_number(
        _required(item, "multiplier", source=source, location=location),
        source=source,
        location=f'{location}["multiplier"]',
    )
    divisor = _finite_number(
        _required(item, "divisor", source=source, location=location),
        source=source,
        location=f'{location}["divisor"]',
    )
    if divisor == 0:
        raise _invalid(source, f'{location}["divisor"]', "must be nonzero")
    return Normalization(multiplier=multiplier, divisor=divisor)


def _capability(value: object, *, source: str, location: str) -> CapabilityProfile:
    item = _mapping(value, source=source, location=location)
    raw_unit = item.get("unit")
    unit = (
        None
        if raw_unit is None
        else _string(raw_unit, source=source, location=f'{location}["unit"]')
    )
    return CapabilityProfile(
        capability=_enum(
            CapabilityId,
            _required(item, "id", source=source, location=location),
            source=source,
            location=f'{location}["id"]',
        ),
        driver=_string(
            _required(item, "driver", source=source, location=location),
            source=source,
            location=f'{location}["driver"]',
        ),
        cluster_id=_integer(
            _required(item, "cluster_id", source=source, location=location),
            source=source,
            location=f'{location}["cluster_id"]',
        ),
        readback=_enum(
            Readback,
            _required(item, "readback", source=source, location=location),
            source=source,
            location=f'{location}["readback"]',
        ),
        writable=_boolean(
            _required(item, "writable", source=source, location=location),
            source=source,
            location=f'{location}["writable"]',
        ),
        support_level=_enum(
            SupportLevel,
            _required(item, "support_level", source=source, location=location),
            source=source,
            location=f'{location}["support_level"]',
        ),
        unit=unit,
        normalization=_normalization(
            item.get("normalization"),
            source=source,
            location=f'{location}["normalization"]',
        ),
    )


def _endpoint(
    endpoint_text: str, value: object, *, source: str, location: str
) -> EndpointProfile:
    try:
        endpoint = EndpointId(int(endpoint_text))
    except (ValueError, ValidationError) as error:
        raise _invalid(source, location, "must be an endpoint from 1 to 240") from error
    item = _mapping(value, source=source, location=location)
    raw_capabilities = _list(
        _required(item, "capabilities", source=source, location=location),
        source=source,
        location=f'{location}["capabilities"]',
    )
    capabilities = tuple(
        _capability(
            raw,
            source=source,
            location=f'{location}["capabilities"][{index}]',
        )
        for index, raw in enumerate(raw_capabilities)
    )
    identifiers = tuple(capability.capability for capability in capabilities)
    if len(identifiers) != len(set(identifiers)):
        raise _invalid(
            source, f'{location}["capabilities"]', "capabilities must be unique"
        )
    return EndpointProfile(endpoint=endpoint, capabilities=capabilities)


def _match_rules(value: object, *, source: str, location: str) -> MatchRules:
    item = _mapping(value, source=source, location=location)
    raw_clusters = _mapping(
        _required(item, "required_endpoint_clusters", source=source, location=location),
        source=source,
        location=f'{location}["required_endpoint_clusters"]',
    )
    clusters: dict[EndpointId, frozenset[int]] = {}
    for endpoint_key, raw_values in raw_clusters.items():
        endpoint_location = (
            f'{location}["required_endpoint_clusters"][{json.dumps(endpoint_key)}]'
        )
        try:
            endpoint = EndpointId(int(endpoint_key))
        except (ValueError, ValidationError) as error:
            raise _invalid(
                source, endpoint_location, "must be an endpoint from 1 to 240"
            ) from error
        clusters[endpoint] = _integer_set(
            raw_values, source=source, location=endpoint_location
        )

    harmony_location = f'{location}["harmony"]'
    harmony = _mapping(
        _required(item, "harmony", source=source, location=location),
        source=source,
        location=harmony_location,
    )
    return MatchRules(
        zigbee_manufacturer_ids=_integer_set(
            _required(
                item, "zigbee_manufacturer_ids", source=source, location=location
            ),
            source=source,
            location=f'{location}["zigbee_manufacturer_ids"]',
        ),
        zigbee_model_ids=_string_set(
            _required(item, "zigbee_model_ids", source=source, location=location),
            source=source,
            location=f'{location}["zigbee_model_ids"]',
        ),
        required_endpoint_clusters=MappingProxyType(clusters),
        harmony_required_processors=_string_set(
            _required(
                harmony, "required_processors", source=source, location=harmony_location
            ),
            source=source,
            location=f'{harmony_location}["required_processors"]',
        ),
        harmony_required_components=_string_set(
            _required(
                harmony, "required_components", source=source, location=harmony_location
            ),
            source=source,
            location=f'{harmony_location}["required_components"]',
        ),
    )


def _availability(value: object, *, source: str, location: str) -> AvailabilityPolicy:
    item = _mapping(value, source=source, location=location)
    return AvailabilityPolicy(
        device_class=_enum(
            AvailabilityDeviceClass,
            _required(item, "device_class", source=source, location=location),
            source=source,
            location=f'{location}["device_class"]',
        ),
        stale_after_seconds=_integer(
            _required(item, "stale_after_seconds", source=source, location=location),
            source=source,
            location=f'{location}["stale_after_seconds"]',
        ),
    )


def _limitations(
    value: object,
    *,
    declared: frozenset[CapabilityId],
    source: str,
    location: str,
) -> tuple[Limitation, ...]:
    items = _list(value, source=source, location=location)
    limitations: list[Limitation] = []
    for index, raw in enumerate(items):
        item_location = f"{location}[{index}]"
        item = _mapping(raw, source=source, location=item_location)
        capability = _enum(
            CapabilityId,
            _required(item, "capability", source=source, location=item_location),
            source=source,
            location=f'{item_location}["capability"]',
        )
        if capability not in declared:
            raise _invalid(
                source,
                f'{item_location}["capability"]',
                "must reference a declared capability",
            )
        limitations.append(
            Limitation(
                backend=_string(
                    _required(item, "backend", source=source, location=item_location),
                    source=source,
                    location=f'{item_location}["backend"]',
                ),
                capability=capability,
                description=_string(
                    _required(
                        item, "description", source=source, location=item_location
                    ),
                    source=source,
                    location=f'{item_location}["description"]',
                ),
            )
        )
    return tuple(limitations)


def _evidence(value: object, *, source: str, location: str) -> tuple[Evidence, ...]:
    items = _list(value, source=source, location=location)
    result: list[Evidence] = []
    for index, raw in enumerate(items):
        item_location = f"{location}[{index}]"
        item = _mapping(raw, source=source, location=item_location)
        result.append(
            Evidence(
                kind=_enum(
                    EvidenceKind,
                    _required(item, "kind", source=source, location=item_location),
                    source=source,
                    location=f'{item_location}["kind"]',
                ),
                url=_string(
                    _required(item, "url", source=source, location=item_location),
                    source=source,
                    location=f'{item_location}["url"]',
                ),
                description=_string(
                    _required(
                        item, "description", source=source, location=item_location
                    ),
                    source=source,
                    location=f'{item_location}["description"]',
                ),
            )
        )
    return tuple(result)


def load_profile(path: Path) -> DeviceProfile:
    """Load one untrusted JSON profile from disk."""
    source = str(path)
    try:
        raw_json = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise _invalid(source, "$", "must be UTF-8 encoded") from error
    return _load_profile_json(raw_json, source=source)


def _load_profile_json(raw_json: str, *, source: str) -> DeviceProfile:
    try:
        raw = json.loads(
            raw_json,
            parse_constant=_raise_nonfinite_json,
        )
    except json.JSONDecodeError as error:
        raise _invalid(
            source,
            f"line {error.lineno} column {error.colno}",
            "invalid JSON",
        ) from error
    except RecursionError as error:
        raise _invalid(source, "$", "invalid JSON") from error
    except ValueError as error:
        raise _invalid(source, "$", "invalid JSON numeric constant") from error
    if not isinstance(raw, Mapping):
        raise _invalid(source, "$", "must be an object")
    return load_profile_data(raw, source=source)


def iter_builtin_profiles() -> tuple[DeviceProfile, ...]:
    """Load all packaged profiles in deterministic filename order."""
    resources = files("smartenit_rescue.profiles.builtin")
    resources_by_name = {
        resource.name: resource
        for resource in resources.iterdir()
        if resource.is_file() and resource.name.endswith(".json")
    }
    return tuple(
        _load_profile_json(
            resources_by_name[name].read_text(encoding="utf-8"),
            source=f"builtin/{name}",
        )
        for name in sorted(resources_by_name)
    )


def find_matching_profiles(
    evidence: DeviceEvidence,
) -> tuple[DeviceProfile, ...]:
    """Return packaged profiles whose exact rules match the evidence."""
    return tuple(
        profile
        for profile in iter_builtin_profiles()
        if match_profile(profile, evidence)
    )


def _raise_nonfinite_json(value: str) -> object:
    raise ValueError(f"non-finite JSON number: {value}")


def load_profile_data(data: Mapping[str, object], *, source: str) -> DeviceProfile:
    """Validate and explicitly construct one immutable typed profile."""
    document = _validate_schema(data, source)
    endpoints_location = '$["endpoints"]'
    raw_endpoints = _mapping(
        _required(document, "endpoints", source=source, location="$"),
        source=source,
        location=endpoints_location,
    )
    endpoints: dict[EndpointId, EndpointProfile] = {}
    for key, raw_endpoint in raw_endpoints.items():
        location = f"{endpoints_location}[{json.dumps(key)}]"
        endpoint = _endpoint(key, raw_endpoint, source=source, location=location)
        if endpoint.endpoint in endpoints:
            raise _invalid(source, endpoints_location, "endpoint IDs must be unique")
        endpoints[endpoint.endpoint] = endpoint

    declared = frozenset(
        capability.capability
        for endpoint in endpoints.values()
        for capability in endpoint.capabilities
    )
    return DeviceProfile(
        schema_version=_integer(
            _required(document, "schema_version", source=source, location="$"),
            source=source,
            location='$["schema_version"]',
        ),
        profile_id=_string(
            _required(document, "profile_id", source=source, location="$"),
            source=source,
            location='$["profile_id"]',
        ),
        manufacturer=_string(
            _required(document, "manufacturer", source=source, location="$"),
            source=source,
            location='$["manufacturer"]',
        ),
        models=_string_tuple(
            _required(document, "models", source=source, location="$"),
            source=source,
            location='$["models"]',
        ),
        match=_match_rules(
            _required(document, "match", source=source, location="$"),
            source=source,
            location='$["match"]',
        ),
        endpoints=MappingProxyType(endpoints),
        availability=_availability(
            _required(document, "availability", source=source, location="$"),
            source=source,
            location='$["availability"]',
        ),
        limitations=_limitations(
            _required(document, "limitations", source=source, location="$"),
            declared=declared,
            source=source,
            location='$["limitations"]',
        ),
        evidence=_evidence(
            _required(document, "evidence", source=source, location="$"),
            source=source,
            location='$["evidence"]',
        ),
    )


def match_profile(profile: DeviceProfile, evidence: DeviceEvidence) -> bool:
    """Return whether all declared rules match the supplied device evidence."""
    if evidence.manufacturer != profile.manufacturer:
        return False
    if evidence.model not in profile.models:
        return False

    rules = profile.match
    if rules.zigbee_model_ids and evidence.model not in rules.zigbee_model_ids:
        return False
    if (
        rules.zigbee_manufacturer_ids
        and evidence.zigbee_manufacturer_id not in rules.zigbee_manufacturer_ids
    ):
        return False
    for endpoint, required_clusters in rules.required_endpoint_clusters.items():
        observed_clusters = evidence.endpoint_clusters.get(endpoint)
        if observed_clusters is None or not required_clusters.issubset(
            observed_clusters
        ):
            return False
    if not rules.harmony_required_processors.issubset(evidence.harmony_processors):
        return False
    return rules.harmony_required_components.issubset(evidence.harmony_components)
