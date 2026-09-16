"""Canonical device and endpoint identity types."""

import string
from dataclasses import dataclass

from smartenit_rescue.errors import ValidationError


@dataclass(frozen=True, slots=True, order=True)
class DeviceId:
    value: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.value, str)
            or len(self.value) != 16
            or self.value != self.value.lower()
            or any(char not in string.hexdigits for char in self.value)
        ):
            raise ValidationError(
                "device ID must be canonical lowercase 16 hexadecimal digits"
            )

    @classmethod
    def parse(cls, raw: str) -> "DeviceId":
        if not isinstance(raw, str):
            raise ValidationError("device ID must be a string")
        value = raw
        if value[:2].lower() == "0x":
            value = value[2:]
        if ":" in value or "-" in value:
            separator = ":" if ":" in value else "-"
            groups = value.split(separator)
            if len(groups) != 8 or any(len(group) != 2 for group in groups):
                raise ValidationError("device ID must contain eight two-hex groups")
            if any(
                any(char not in string.hexdigits for char in group) for group in groups
            ):
                raise ValidationError("device ID contains non-hex characters")
            value = "".join(groups)
        if len(value) != 16 or any(char not in string.hexdigits for char in value):
            raise ValidationError("device ID must be exactly 16 hexadecimal digits")
        return cls(value.lower())

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True, order=True)
class EndpointId:
    value: int

    def __post_init__(self) -> None:
        if isinstance(self.value, bool) or not isinstance(self.value, int):
            raise ValidationError("endpoint ID must be an integer")
        if not 1 <= self.value <= 240:
            raise ValidationError("endpoint ID must be between 1 and 240")

    def __str__(self) -> str:
        return str(self.value)
