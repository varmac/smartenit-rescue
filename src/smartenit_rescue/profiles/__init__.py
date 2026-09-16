"""Declarative device profile contracts."""

from .loader import (
    find_matching_profiles,
    iter_builtin_profiles,
    load_profile,
    load_profile_data,
    match_profile,
)
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

__all__ = [
    "AvailabilityDeviceClass",
    "AvailabilityPolicy",
    "CapabilityProfile",
    "DeviceEvidence",
    "DeviceProfile",
    "EndpointProfile",
    "Evidence",
    "EvidenceKind",
    "Limitation",
    "MatchRules",
    "Normalization",
    "Readback",
    "find_matching_profiles",
    "iter_builtin_profiles",
    "load_profile",
    "load_profile_data",
    "match_profile",
]
