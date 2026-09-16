"""Certificate-pinned, bounded HTTPS transport for proven Harmony G2 routes."""

from __future__ import annotations

import hashlib
import hmac
import http.client
import ipaddress
import json
import math
import re
import ssl
import time
from collections.abc import Mapping
from typing import Protocol, cast, runtime_checkable

from smartenit_rescue.errors import RescueError, ValidationError

_PIN = re.compile(r"(?:[0-9a-fA-F]{64}|(?:[0-9a-fA-F]{2}:){31}[0-9a-fA-F]{2})")
_INVENTORY_PATH = re.compile(r"/v2/devices\?limit=100&page=(?:100|[1-9][0-9]?)")
_COMMAND_PATH = re.compile(
    r"/v2/devices/[A-Za-z0-9_-]{1,128}"
    r"/comps/[A-Za-z0-9_-]{1,128}"
    r"/procs/OnOff/methods/(?:On|Off)"
)
_MAX_RESPONSE_BYTES = 2_000_000
_MAX_REQUEST_BYTES = 65_536
_MAX_JSON_DEPTH = 32
_MAX_JSON_ITEMS = 10_000
_MAX_TIMEOUT_SECONDS = 30.0
_LAN_NETWORKS = (
    ipaddress.IPv4Network((0x0A000000, 8)),
    ipaddress.IPv4Network((0xAC100000, 12)),
    ipaddress.IPv4Network((0xC0A80000, 16)),
    ipaddress.IPv4Network((0x7F000000, 8)),
)


class G2TransportError(RescueError):
    """A bounded transport failure with command-outcome metadata."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        request_started: bool = False,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.request_started = request_started


class G2ResponseError(G2TransportError):
    """The G2 returned an invalid or unsuccessful HTTP response."""


@runtime_checkable
class G2Transport(Protocol):
    def request(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, object] | None = None,
        access_token: str | None = None,
    ) -> Mapping[str, object]: ...


def _validation(message: str) -> ValidationError:
    return ValidationError(message)


def _pin_digest(value: object) -> bytes:
    if not isinstance(value, str) or _PIN.fullmatch(value) is None:
        raise _validation("G2 certificate pin must be a SHA-256 fingerprint")
    return bytes.fromhex(value.replace(":", ""))


def _port(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise _validation("G2 port must be between 1 and 65535")
    return value


def _timeout(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0 < value <= _MAX_TIMEOUT_SECONDS
    ):
        raise _validation("G2 timeout must be positive and bounded")
    return float(value)


def _lan_host(value: object) -> str:
    try:
        address = ipaddress.IPv4Address(value)
    except (ValueError, TypeError) as error:
        raise _validation("G2 host must be a LAN IPv4 literal") from error
    if not any(address in network for network in _LAN_NETWORKS):
        raise _validation("G2 host must be a LAN IPv4 literal")
    return str(address)


def _validate_route(method: object, path: object) -> tuple[str, str]:
    if not isinstance(method, str) or method not in {"GET", "POST"}:
        raise _validation("G2 request method is not allowed")
    if not isinstance(path, str) or any(character in path for character in "\r\n#"):
        raise _validation("G2 request path is not allowed")
    allowed = (
        method == "GET"
        and (path == "/v2/ping" or _INVENTORY_PATH.fullmatch(path) is not None)
    ) or (
        method == "POST"
        and (path == "/v2/oauth2/token" or _COMMAND_PATH.fullmatch(path) is not None)
    )
    if not allowed:
        raise _validation("G2 request path is not allowed")
    return method, path


def _validate_credential(value: object) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 8192
        or not value.isascii()
        or any(ord(character) < 0x21 or ord(character) > 0x7E for character in value)
    ):
        raise _validation("G2 request credential is invalid")
    return value


def _json_value(value: object, *, depth: int = 0) -> int:
    if depth > _MAX_JSON_DEPTH:
        raise _validation("G2 JSON exceeds the nesting limit")
    if value is None or isinstance(value, (bool, str)):
        return 1
    if isinstance(value, int):
        return 1
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _validation("G2 JSON contains a non-finite number")
        return 1
    if isinstance(value, list):
        total = 1
        for item in value:
            total += _json_value(item, depth=depth + 1)
            if total > _MAX_JSON_ITEMS:
                raise _validation("G2 JSON exceeds the item limit")
        return total
    if isinstance(value, Mapping):
        if any(not isinstance(name, str) for name in value):
            raise _validation("G2 JSON object keys must be strings")
        total = 1
        for item in value.values():
            total += _json_value(item, depth=depth + 1)
            if total > _MAX_JSON_ITEMS:
                raise _validation("G2 JSON exceeds the item limit")
        return total
    raise _validation("G2 JSON contains an unsupported value")


def _request_body(method: str, body: Mapping[str, object] | None) -> bytes | None:
    if method == "GET":
        if body is not None:
            raise _validation("G2 GET requests cannot contain a body")
        return None
    if not isinstance(body, Mapping):
        raise _validation("G2 POST requests require a JSON object body")
    _json_value(body)
    try:
        encoded = json.dumps(
            body,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, ValueError) as error:
        raise _validation("G2 request body is invalid") from error
    if len(encoded) > _MAX_REQUEST_BYTES:
        raise _validation("G2 request body is too large")
    return encoded


def _content_type(value: str | None) -> bool:
    if value is None:
        return False
    parts = [part.strip() for part in value.split(";")]
    if parts[0].lower() != "application/json":
        return False
    if len(parts) == 1:
        return True
    if len(parts) != 2:
        return False
    name, separator, charset = parts[1].partition("=")
    return (
        separator == "="
        and name.strip().lower() == "charset"
        and charset.strip().strip('"').lower() == "utf-8"
    )


def _reject_constant(_value: str) -> object:
    raise ValueError("non-finite number")


def _parse_response(
    response: http.client.HTTPResponse,
    connection: http.client.HTTPSConnection,
    deadline: float,
) -> Mapping[str, object]:
    status = response.status
    if not 200 <= status < 300:
        raise G2ResponseError(
            "G2 returned an unsuccessful HTTP status",
            status=status,
            request_started=True,
        )
    if not _content_type(response.getheader("Content-Type")):
        raise G2ResponseError(
            "G2 response content type is invalid",
            status=status,
            request_started=True,
        )
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError
    if connection.sock is not None:
        connection.sock.settimeout(remaining)
    encoded = response.read(_MAX_RESPONSE_BYTES + 1)
    if len(encoded) > _MAX_RESPONSE_BYTES:
        raise G2ResponseError(
            "G2 response is too large",
            status=status,
            request_started=True,
        )
    try:
        decoded = encoded.decode("utf-8")
        payload = json.loads(decoded, parse_constant=_reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise G2ResponseError(
            "G2 response JSON is invalid",
            status=status,
            request_started=True,
        ) from error
    try:
        _json_value(payload)
    except ValidationError as error:
        raise G2ResponseError(
            "G2 response JSON is invalid",
            status=status,
            request_started=True,
        ) from error
    if not isinstance(payload, Mapping):
        raise G2ResponseError(
            "G2 response must be a JSON object",
            status=status,
            request_started=True,
        )
    return cast(Mapping[str, object], payload)


class PinnedG2Transport:
    """Issue one-shot HTTPS requests after exact certificate enrollment."""

    __slots__ = ("_certificate_digest", "_host", "_port", "_timeout_seconds")

    def __init__(
        self,
        host: str,
        certificate_pin: str,
        *,
        port: int = 443,
        timeout_seconds: float = 5.0,
    ) -> None:
        self._host = _lan_host(host)
        self._certificate_digest = _pin_digest(certificate_pin)
        self._port = _port(port)
        self._timeout_seconds = _timeout(timeout_seconds)

    def request(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, object] | None = None,
        access_token: str | None = None,
    ) -> Mapping[str, object]:
        validated_method, validated_path = _validate_route(method, path)
        encoded_body = _request_body(validated_method, body)
        credential = (
            None if access_token is None else _validate_credential(access_token)
        )
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        connection = http.client.HTTPSConnection(
            self._host,
            self._port,
            timeout=self._timeout_seconds,
            context=context,
        )
        request_started = False
        deadline = time.monotonic() + self._timeout_seconds
        try:
            connection.connect()
            connection.auto_open = 0
            if connection.sock is None:
                raise OSError("TLS socket unavailable")
            peer_certificate = connection.sock.getpeercert(binary_form=True)
            if not peer_certificate or not hmac.compare_digest(
                hashlib.sha256(peer_certificate).digest(), self._certificate_digest
            ):
                raise G2TransportError(
                    "G2 certificate pin mismatch", request_started=False
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            connection.sock.settimeout(remaining)
            headers = {"Accept": "application/json"}
            if encoded_body is not None:
                headers["Content-Type"] = "application/json"
            if credential is not None:
                headers["Authorization"] = f"Bearer {credential}"
            request_started = True
            connection.request(
                validated_method,
                validated_path,
                body=encoded_body,
                headers=headers,
            )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            if connection.sock is not None:
                connection.sock.settimeout(remaining)
            response = connection.getresponse()
            return _parse_response(response, connection, deadline)
        except G2TransportError:
            raise
        except TimeoutError as error:
            phase = "after submission" if request_started else "before submission"
            raise G2TransportError(
                f"G2 request timeout {phase}",
                request_started=request_started,
            ) from error
        except (OSError, http.client.HTTPException, ssl.SSLError) as error:
            phase = "after submission" if request_started else "before submission"
            raise G2TransportError(
                f"G2 transport failure {phase}",
                request_started=request_started,
            ) from error
        finally:
            connection.close()


__all__ = [
    "G2ResponseError",
    "G2Transport",
    "G2TransportError",
    "PinnedG2Transport",
]
