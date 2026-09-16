"""Strict client for the publicly evidenced Harmony G2 local protocol."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime

from smartenit_rescue.backends.harmony_g2.session import (
    LocalSession,
    parse_refreshed_session,
)
from smartenit_rescue.backends.harmony_g2.transport import (
    G2ResponseError,
    G2Transport,
)
from smartenit_rescue.errors import ValidationError
from smartenit_rescue.models import DeviceId

_SAFE_SEGMENT = re.compile(r"[A-Za-z0-9_-]{1,128}")
_MAX_INVENTORY_PAGES = 100
_PAGE_SIZE = 100


def _response_error(message: str) -> G2ResponseError:
    return G2ResponseError(message, request_started=True)


def _safe_segment(value: object) -> str:
    if not isinstance(value, str) or _SAFE_SEGMENT.fullmatch(value) is None:
        raise ValidationError("G2 local identifier is invalid")
    return value


@dataclass(frozen=True, slots=True)
class G2Endpoint:
    local_device_id: str = field(repr=False)
    device_id: DeviceId
    component_id: str = field(repr=False)

    def __post_init__(self) -> None:
        _safe_segment(self.local_device_id)
        _safe_segment(self.component_id)
        if not isinstance(self.device_id, DeviceId) or self.device_id != DeviceId.parse(
            str(self.device_id)
        ):
            raise ValidationError("G2 endpoint device ID must be canonical")

    @property
    def command_path(self) -> str:
        return (
            f"/v2/devices/{self.local_device_id}/comps/{self.component_id}/procs/OnOff"
        )


def _processors(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise _response_error("G2 inventory processor list is invalid")
    names: list[str] = []
    for processor in value:
        if not isinstance(processor, Mapping):
            raise _response_error("G2 inventory processor is invalid")
        name = processor.get("name")
        if not isinstance(name, str) or not name:
            raise _response_error("G2 inventory processor is invalid")
        names.append(name)
    return tuple(names)


def _on_off_components(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise _response_error("G2 inventory components are invalid")
    result: list[str] = []
    for component in value:
        if not isinstance(component, Mapping):
            raise _response_error("G2 inventory component is invalid")
        if "OnOff" not in _processors(component.get("processors")):
            continue
        try:
            component_id = _safe_segment(component.get("id"))
        except ValidationError as error:
            raise _response_error(
                "G2 inventory component identifier is invalid"
            ) from error
        result.append(component_id)
    return tuple(result)


def _device_id(value: object) -> DeviceId:
    if not isinstance(value, str):
        raise _response_error("G2 inventory hardware identity is invalid")
    try:
        return DeviceId.parse(value)
    except ValidationError as error:
        raise _response_error("G2 inventory hardware identity is invalid") from error


class G2Client:
    def __init__(self, transport: G2Transport) -> None:
        self._transport = transport

    def renew(self, session: LocalSession, now: datetime) -> LocalSession:
        if not isinstance(session, LocalSession):
            raise ValidationError("G2 renewal requires a local session")
        body: dict[str, object] = {}
        body["grant_type"] = "refresh_token"
        body["refresh_token"] = session.refresh_token
        payload = self._transport.request(
            "POST",
            "/v2/oauth2/token",
            body=body,
            access_token=session.access_token,
        )
        try:
            return parse_refreshed_session(payload, now)
        except ValidationError as error:
            raise _response_error("G2 renewal response is invalid") from error

    def endpoints(
        self,
        access_token: str,
    ) -> tuple[G2Endpoint, ...]:
        endpoints: list[G2Endpoint] = []
        seen_local_ids: set[str] = set()
        seen_hardware_ids: set[DeviceId] = set()
        seen_endpoints: set[G2Endpoint] = set()
        for page in range(1, _MAX_INVENTORY_PAGES + 1):
            payload = self._transport.request(
                "GET",
                f"/v2/devices?limit={_PAGE_SIZE}&page={page}",
                access_token=access_token,
            )
            data = payload.get("data")
            if not isinstance(data, list):
                raise _response_error("G2 inventory data array is invalid")
            for raw_device in data:
                if not isinstance(raw_device, Mapping):
                    raise _response_error("G2 inventory device is invalid")
                try:
                    local_id = _safe_segment(raw_device.get("_id"))
                except ValidationError as error:
                    raise _response_error(
                        "G2 inventory local device identifier is invalid"
                    ) from error
                if local_id in seen_local_ids:
                    raise _response_error("G2 inventory contains a duplicate device")
                seen_local_ids.add(local_id)
                component_ids = _on_off_components(raw_device.get("components"))
                if not component_ids:
                    continue
                hardware_id = _device_id(raw_device.get("hwId"))
                if hardware_id in seen_hardware_ids:
                    raise _response_error(
                        "G2 inventory contains a duplicate hardware identity"
                    )
                seen_hardware_ids.add(hardware_id)
                for component_id in component_ids:
                    endpoint = G2Endpoint(local_id, hardware_id, component_id)
                    if endpoint in seen_endpoints:
                        raise _response_error(
                            "G2 inventory contains a duplicate endpoint"
                        )
                    seen_endpoints.add(endpoint)
                    endpoints.append(endpoint)
            if len(data) < _PAGE_SIZE:
                return tuple(endpoints)
        raise _response_error("G2 inventory exceeded 100 pages")

    def set_on_off(
        self,
        endpoint: G2Endpoint,
        desired: bool,
        access_token: str,
    ) -> None:
        if not isinstance(endpoint, G2Endpoint):
            raise ValidationError("G2 command endpoint is invalid")
        if type(desired) is not bool:
            raise ValidationError("G2 OnOff command must be boolean")
        method = "On" if desired else "Off"
        payload = self._transport.request(
            "POST",
            f"{endpoint.command_path}/methods/{method}",
            body={},
            access_token=access_token,
        )
        if payload.get("success") is not True:
            raise _response_error("G2 command acknowledgement is invalid")


__all__ = ["G2Client", "G2Endpoint"]
