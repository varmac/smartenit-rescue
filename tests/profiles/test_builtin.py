from __future__ import annotations

import json
import re
from importlib.resources import files

from smartenit_rescue import profiles
from smartenit_rescue.models.capability import CapabilityId, SupportLevel
from smartenit_rescue.models.identity import EndpointId
from smartenit_rescue.profiles import DeviceEvidence, EvidenceKind, Readback


def test_4040c_profile_is_conservative() -> None:
    (profile,) = profiles.iter_builtin_profiles()
    endpoint = profile.endpoints[EndpointId(1)]

    assert profile.profile_id == "smartenit.4040c"
    assert profile.manufacturer == "Compacta International, Ltd."
    assert profile.models == ("ZBMLCSR",)
    assert profile.match.zigbee_manufacturer_ids == frozenset({0x1075})
    assert profile.match.zigbee_model_ids == frozenset({"ZBMLCSR"})
    assert profile.match.required_endpoint_clusters == {
        EndpointId(1): frozenset({0x0006, 0x0702})
    }
    assert tuple(
        (
            capability.capability,
            capability.driver,
            capability.cluster_id,
            capability.readback,
            capability.support_level,
            capability.unit,
        )
        for capability in endpoint.capabilities
    ) == (
        (
            CapabilityId.ON_OFF,
            "zigbee.on_off",
            0x0006,
            Readback.AVAILABLE,
            SupportLevel.SYNTHETIC,
            None,
        ),
        (
            CapabilityId.ELECTRICAL_POWER,
            "zigbee.metering.power",
            0x0702,
            Readback.AVAILABLE,
            SupportLevel.SYNTHETIC,
            "W",
        ),
        (
            CapabilityId.ELECTRICAL_ENERGY,
            "zigbee.metering.energy",
            0x0702,
            Readback.AVAILABLE,
            SupportLevel.SYNTHETIC,
            "kWh",
        ),
        (
            CapabilityId.VOLTAGE,
            "zigbee.metering.voltage",
            0x0702,
            Readback.AVAILABLE,
            SupportLevel.SYNTHETIC,
            "V",
        ),
        (
            CapabilityId.CURRENT,
            "zigbee.metering.current",
            0x0702,
            Readback.AVAILABLE,
            SupportLevel.SYNTHETIC,
            "A",
        ),
    )
    assert all(
        capability.support_level is SupportLevel.SYNTHETIC
        for endpoint in profile.endpoints.values()
        for capability in endpoint.capabilities
    )
    assert endpoint.capabilities[0].writable is True
    assert all(not item.writable for item in endpoint.capabilities[1:])
    assert tuple((item.kind, item.url) for item in profile.evidence) == (
        (
            EvidenceKind.PUBLIC_DOCUMENTATION,
            "https://docs.smartenit.com/zbmlcsr/zbmlcsr_product-brief.pdf",
        ),
        (
            EvidenceKind.PUBLIC_DOCUMENTATION,
            "https://docs.smartenit.com/zbmlcsr/zbmlcsr_quick-start.pdf",
        ),
    )
    assert all(item.url.startswith("https://") for item in profile.evidence)
    assert tuple(item.description for item in profile.limitations) == (
        "Harmony G2 state readback is not yet publicly verified",
        "Metering values and scaling require hardware verification",
    )


def test_4040c_profile_contains_no_private_or_verified_observations() -> None:
    resource = files("smartenit_rescue.profiles.builtin").joinpath(
        "smartenit-4040c.json"
    )
    raw = resource.read_text(encoding="utf-8")
    data = json.loads(raw)
    user_directory = "/" + "Users/"
    home_directory = "/" + "home/"
    filesystem_path = re.compile(
        rf"(?i)(?:{re.escape(user_directory)}|{re.escape(home_directory)}|[A-Z]:\\\\)"
    )

    assert not re.search(r"(?i)\b(?:[0-9a-f]{2}:){7}[0-9a-f]{2}\b", raw)
    assert not re.search(r"\b(?:10|192\.168)\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", raw)
    assert not re.search(r"\b172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}\b", raw)
    assert not filesystem_path.search(raw)
    assert not re.search(r'(?i)"(?:password|token|secret|api_key)"\s*:', raw)
    assert "node_id" not in raw.lower()
    assert "hardware_verified" not in raw
    assert {
        capability["support_level"]
        for endpoint in data["endpoints"].values()
        for capability in endpoint["capabilities"]
    } == {"synthetic"}


def test_find_matching_profiles_uses_exact_profile_rules() -> None:
    matching = DeviceEvidence(
        manufacturer="Compacta International, Ltd.",
        model="ZBMLCSR",
        zigbee_manufacturer_id=0x1075,
        endpoint_clusters={EndpointId(1): frozenset({0x0006, 0x0702})},
        harmony_processors=frozenset(),
        harmony_components=frozenset(),
    )
    wrong_model = DeviceEvidence(
        manufacturer=matching.manufacturer,
        model="zbmlcsr",
        zigbee_manufacturer_id=matching.zigbee_manufacturer_id,
        endpoint_clusters=matching.endpoint_clusters,
        harmony_processors=matching.harmony_processors,
        harmony_components=matching.harmony_components,
    )

    assert tuple(
        profile.profile_id for profile in profiles.find_matching_profiles(matching)
    ) == ("smartenit.4040c",)
    assert profiles.find_matching_profiles(wrong_model) == ()
