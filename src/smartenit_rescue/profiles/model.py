"""Immutable typed representations of validated device profiles."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from smartenit_rescue.models.capability import CapabilityId, SupportLevel
from smartenit_rescue.models.identity import EndpointId


class EvidenceKind(StrEnum):
    PUBLIC_DOCUMENTATION = "public_documentation"


class Readback(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    REPORT_ONLY = "report_only"


class AvailabilityDeviceClass(StrEnum):
    ACTIVE = "active"
    PASSIVE = "passive"


@dataclass(frozen=True, slots=True)
class Evidence:
    kind: EvidenceKind
    url: str
    description: str


@dataclass(frozen=True, slots=True)
class Normalization:
    multiplier: float
    divisor: float


@dataclass(frozen=True, slots=True)
class CapabilityProfile:
    capability: CapabilityId
    driver: str
    cluster_id: int
    readback: Readback
    writable: bool
    support_level: SupportLevel
    unit: str | None
    normalization: Normalization | None


@dataclass(frozen=True, slots=True)
class EndpointProfile:
    endpoint: EndpointId
    capabilities: tuple[CapabilityProfile, ...]


@dataclass(frozen=True, slots=True)
class AvailabilityPolicy:
    device_class: AvailabilityDeviceClass
    stale_after_seconds: int


@dataclass(frozen=True, slots=True)
class Limitation:
    backend: str
    capability: CapabilityId
    description: str


@dataclass(frozen=True, slots=True)
class MatchRules:
    zigbee_manufacturer_ids: frozenset[int]
    zigbee_model_ids: frozenset[str]
    required_endpoint_clusters: Mapping[EndpointId, frozenset[int]]
    harmony_required_processors: frozenset[str]
    harmony_required_components: frozenset[str]

    def __post_init__(self) -> None:
        clusters = {
            endpoint: frozenset(values)
            for endpoint, values in self.required_endpoint_clusters.items()
        }
        object.__setattr__(
            self, "required_endpoint_clusters", MappingProxyType(clusters)
        )


@dataclass(frozen=True, slots=True)
class DeviceEvidence:
    manufacturer: str
    model: str
    zigbee_manufacturer_id: int | None
    endpoint_clusters: Mapping[EndpointId, frozenset[int]]
    harmony_processors: frozenset[str]
    harmony_components: frozenset[str]

    def __post_init__(self) -> None:
        clusters = {
            endpoint: frozenset(values)
            for endpoint, values in self.endpoint_clusters.items()
        }
        object.__setattr__(self, "endpoint_clusters", MappingProxyType(clusters))


@dataclass(frozen=True, slots=True)
class DeviceProfile:
    schema_version: int
    profile_id: str
    manufacturer: str
    models: tuple[str, ...]
    match: MatchRules
    endpoints: Mapping[EndpointId, EndpointProfile]
    availability: AvailabilityPolicy
    limitations: tuple[Limitation, ...]
    evidence: tuple[Evidence, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "endpoints", MappingProxyType(dict(self.endpoints)))
