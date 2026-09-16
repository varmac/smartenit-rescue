from __future__ import annotations

from smartenit_rescue.models.capability import CapabilityId, SupportLevel


def test_capability_vocabulary_is_stable() -> None:
    assert [item.value for item in CapabilityId] == [
        "on_off",
        "electrical_power",
        "electrical_energy",
        "voltage",
        "current",
        "temperature",
        "battery",
        "level",
        "thermostat",
    ]


def test_support_levels_are_evidence_labels() -> None:
    assert [item.value for item in SupportLevel] == [
        "synthetic",
        "experimental",
        "hardware_verified",
    ]
