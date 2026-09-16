"""Closed capability and support vocabularies."""

from enum import StrEnum


class CapabilityId(StrEnum):
    ON_OFF = "on_off"
    ELECTRICAL_POWER = "electrical_power"
    ELECTRICAL_ENERGY = "electrical_energy"
    VOLTAGE = "voltage"
    CURRENT = "current"
    TEMPERATURE = "temperature"
    BATTERY = "battery"
    LEVEL = "level"
    THERMOSTAT = "thermostat"


class SupportLevel(StrEnum):
    SYNTHETIC = "synthetic"
    EXPERIMENTAL = "experimental"
    HARDWARE_VERIFIED = "hardware_verified"
