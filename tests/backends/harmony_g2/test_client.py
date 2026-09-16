from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from smartenit_rescue.backends.harmony_g2.client import G2Client, G2Endpoint
from smartenit_rescue.backends.harmony_g2.session import LocalSession
from smartenit_rescue.backends.harmony_g2.transport import G2ResponseError
from smartenit_rescue.errors import ValidationError
from smartenit_rescue.models import DeviceId

NOW = datetime(2026, 9, 15, 20, 40, tzinfo=UTC)
ACCESS_VALUE = "<local-access>"
REFRESH_VALUE = "<local-refresh>"
NEXT_ACCESS_VALUE = "<next-access>"
NEXT_REFRESH_VALUE = "<next-refresh>"


@dataclass(frozen=True, slots=True)
class Request:
    method: str
    path: str
    body: Mapping[str, object] | None
    credential: str | None


class RecordingTransport:
    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.requests: list[Request] = []

    def request(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, object] | None = None,
        access_token: str | None = None,
    ) -> Mapping[str, object]:
        self.requests.append(Request(method, path, body, access_token))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        assert isinstance(response, Mapping)
        return response


class PagingTransport:
    def __init__(self, *, full_pages: int) -> None:
        self.full_pages = full_pages
        self.requests: list[Request] = []

    def request(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, object] | None = None,
        access_token: str | None = None,
    ) -> Mapping[str, object]:
        self.requests.append(Request(method, path, body, access_token))
        page = int(path.rsplit("=", maxsplit=1)[1])
        data: list[object] = []
        if page <= self.full_pages:
            start = (page - 1) * 100
            data = [device(start + offset) for offset in range(100)]
        return inventory(data)


def inventory(data: list[object]) -> dict[str, object]:
    payload: dict[str, object] = {}
    payload["data"] = data
    return payload


def processor(name: object = "OnOff") -> dict[str, object]:
    payload: dict[str, object] = {}
    payload["name"] = name
    return payload


def component(
    component_id: object = "1", processors: object | None = None
) -> dict[str, object]:
    payload: dict[str, object] = {}
    payload["id"] = component_id
    payload["processors"] = [processor()] if processors is None else processors
    return payload


def device(
    number: int = 1,
    *,
    local_id: object | None = None,
    hardware_id: object | None = None,
    components: object | None = None,
    name: object = "Ignored friendly name",
) -> dict[str, object]:
    payload: dict[str, object] = {}
    payload["_id"] = f"device-{number}" if local_id is None else local_id
    payload["hwId"] = f"{number:016x}" if hardware_id is None else hardware_id
    payload["components"] = [component()] if components is None else components
    payload["name"] = name
    return payload


def refresh_response(lifetime: object = 120) -> dict[str, object]:
    payload: dict[str, object] = {}
    payload["access_token"] = NEXT_ACCESS_VALUE
    payload["refresh_token"] = NEXT_REFRESH_VALUE
    payload["expires_in"] = lifetime
    return payload


def acknowledgement(value: object = True) -> dict[str, object]:
    payload: dict[str, object] = {}
    payload["success"] = value
    return payload


def test_endpoint_normalizes_identity_and_redacts_local_segments() -> None:
    endpoint = G2Endpoint(
        "device_example-1",
        DeviceId.parse("02:00:00:00:00:00:00:01"),
        "component-1",
    )

    assert endpoint.device_id == DeviceId.parse("0200000000000001")
    assert endpoint.command_path == (
        "/v2/devices/device_example-1/comps/component-1/procs/OnOff"
    )
    representation = repr(endpoint)
    assert "device_example-1" not in representation
    assert "component-1" not in representation


@pytest.mark.parametrize("value", ["", "../device", "has space", "a/b", "x" * 129])
def test_endpoint_rejects_unsafe_local_path_segments(value: str) -> None:
    with pytest.raises(ValidationError, match="identifier"):
        G2Endpoint(value, DeviceId.parse("0200000000000001"), "1")
    with pytest.raises(ValidationError, match="identifier"):
        G2Endpoint("device-1", DeviceId.parse("0200000000000001"), value)


def test_endpoints_normalize_hardware_id_filter_non_onoff_and_ignore_names() -> None:
    on_off = device(
        hardware_id="02:00:00:00:00:00:00:01",
        components=[
            component("ignored", [processor("Discover")]),
            component("switch_1"),
        ],
        name="Misleading friendly suffix 0001",
    )
    without_on_off = device(
        2,
        hardware_id="0200000000000002",
        components=[component("1", [processor("Discover")])],
    )
    transport = RecordingTransport([inventory([on_off, without_on_off])])

    endpoints = G2Client(transport).endpoints(ACCESS_VALUE)

    assert endpoints == (
        G2Endpoint("device-1", DeviceId.parse("0200000000000001"), "switch_1"),
    )
    assert transport.requests == [
        Request(
            "GET",
            "/v2/devices?limit=100&page=1",
            None,
            ACCESS_VALUE,
        )
    ]


def test_endpoints_return_multiple_onoff_components() -> None:
    transport = RecordingTransport(
        [inventory([device(1, components=[component("1"), component("2")])])]
    )

    endpoints = G2Client(transport).endpoints(ACCESS_VALUE)

    assert [item.component_id for item in endpoints] == ["1", "2"]


def test_endpoints_fetch_next_page_after_exactly_one_hundred_items() -> None:
    transport = PagingTransport(full_pages=1)

    endpoints = G2Client(transport).endpoints(ACCESS_VALUE)

    assert len(endpoints) == 100
    assert [request.path for request in transport.requests] == [
        "/v2/devices?limit=100&page=1",
        "/v2/devices?limit=100&page=2",
    ]


def test_endpoints_reject_inventory_that_exceeds_one_hundred_pages() -> None:
    transport = PagingTransport(full_pages=100)

    with pytest.raises(G2ResponseError, match="100 pages"):
        G2Client(transport).endpoints(ACCESS_VALUE)

    assert len(transport.requests) == 100


@pytest.mark.parametrize(
    "data",
    [
        [device(1), device(2, local_id="device-1")],
        [device(1), device(2, hardware_id="".join(("00000000", "00000001")))],  # noqa: FLY002
        [device(1, components=[component("1"), component("1")])],
    ],
)
def test_endpoints_reject_duplicate_identity_or_binding(data: list[object]) -> None:
    with pytest.raises(G2ResponseError, match="duplicate"):
        G2Client(RecordingTransport([inventory(data)])).endpoints(ACCESS_VALUE)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"data": "not-a-list"},
        inventory(["not-an-object"]),
        inventory([device(1, local_id="../unsafe")]),
        inventory([device(1, hardware_id="not-an-eui")]),
        inventory([device(1, hardware_id=False)]),
        inventory([device(1, components="not-a-list")]),
        inventory([device(1, components=["not-an-object"])]),
        inventory([device(1, components=[component(processors="not-a-list")])]),
        inventory([device(1, components=[component(processors=[{}])])]),
        inventory([device(1, components=[component(component_id=False)])]),
    ],
)
def test_endpoints_reject_malformed_inventory(payload: dict[str, object]) -> None:
    with pytest.raises(G2ResponseError, match="inventory"):
        G2Client(RecordingTransport([payload])).endpoints(ACCESS_VALUE)


def test_renew_sends_exact_exchange_and_parses_rotated_session() -> None:
    old = LocalSession(ACCESS_VALUE, REFRESH_VALUE, NOW + timedelta(seconds=10))
    transport = RecordingTransport([refresh_response()])

    renewed = G2Client(transport).renew(old, NOW)

    assert renewed == LocalSession(
        NEXT_ACCESS_VALUE,
        NEXT_REFRESH_VALUE,
        NOW + timedelta(seconds=120),
    )
    request = transport.requests[0]
    assert request.method == "POST"
    assert request.path == "/v2/oauth2/token"
    assert request.credential == ACCESS_VALUE
    assert request.body is not None
    assert set(request.body) == {"grant_type", "refresh_token"}
    assert request.body["grant_type"] == "refresh_token"
    assert request.body["refresh_token"] == REFRESH_VALUE


@pytest.mark.parametrize("desired", [True, False])
def test_set_on_off_sends_one_exact_method_and_accepts_explicit_success(
    desired: bool,
) -> None:
    transport = RecordingTransport([acknowledgement()])
    endpoint = G2Endpoint("device-1", DeviceId.parse("0200000000000001"), "component_1")

    G2Client(transport).set_on_off(endpoint, desired, ACCESS_VALUE)

    assert transport.requests == [
        Request(
            "POST",
            endpoint.command_path + ("/methods/On" if desired else "/methods/Off"),
            {},
            ACCESS_VALUE,
        )
    ]
    assert all("/attrs/" not in request.path for request in transport.requests)


@pytest.mark.parametrize("payload", [{}, acknowledgement(False), acknowledgement(1)])
def test_set_on_off_rejects_unknown_acknowledgement_without_readback(
    payload: dict[str, object],
) -> None:
    transport = RecordingTransport([payload])
    endpoint = G2Endpoint("device-1", DeviceId.parse("0200000000000001"), "1")

    with pytest.raises(G2ResponseError) as captured:
        G2Client(transport).set_on_off(endpoint, True, ACCESS_VALUE)

    assert captured.value.request_started is True
    assert len(transport.requests) == 1
