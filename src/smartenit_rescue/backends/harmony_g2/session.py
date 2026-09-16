"""Secure persistence for an externally provisioned G2-local session."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from smartenit_rescue.errors import ValidationError

_SESSION_KEYS = frozenset({"access_token", "refresh_token", "expires_at"})
_REFRESH_REQUIRED_KEYS = frozenset({"access_token", "refresh_token", "expires_in"})
_REFRESH_ALLOWED_KEYS = _REFRESH_REQUIRED_KEYS | {"token_type"}
_MAX_SESSION_VALUE_LENGTH = 8192
_MAX_SESSION_BYTES = 32768
_MAX_EXPIRES_IN_SECONDS = 366 * 24 * 60 * 60


def _invalid(message: str) -> ValidationError:
    return ValidationError(message)


def _token(value: object, *, context: str) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= _MAX_SESSION_VALUE_LENGTH
        or not value.isascii()
        or any(character.isspace() for character in value)
        or any(ord(character) < 0x21 or ord(character) > 0x7E for character in value)
    ):
        raise _invalid(f"{context} contains an invalid token")
    return value


def _aware_utc(value: datetime, *, context: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise _invalid(f"{context} clock must be timezone-aware UTC")
    try:
        offset = value.utcoffset()
    except (OverflowError, ValueError) as error:
        raise _invalid(f"{context} clock must be timezone-aware UTC") from error
    if offset != timedelta(0):
        raise _invalid(f"{context} clock must be timezone-aware UTC")
    return value.astimezone(UTC)


def _format_expiry(value: datetime) -> str:
    normalized = _aware_utc(value, context="local session")
    timespec = "microseconds" if normalized.microsecond else "seconds"
    return normalized.isoformat(timespec=timespec).removesuffix("+00:00") + "Z"


def _expiry(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise _invalid("local session expiry must be canonical RFC 3339 UTC")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise _invalid("local session expiry must be canonical RFC 3339 UTC") from error
    normalized = _aware_utc(parsed, context="local session expiry")
    if _format_expiry(normalized) != value:
        raise _invalid("local session expiry must be canonical RFC 3339 UTC")
    return normalized


@dataclass(frozen=True, slots=True)
class LocalSession:
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    expires_at: datetime

    def __post_init__(self) -> None:
        _token(self.access_token, context="local session")
        _token(self.refresh_token, context="local session")
        object.__setattr__(
            self,
            "expires_at",
            _aware_utc(self.expires_at, context="local session"),
        )


def parse_refreshed_session(
    payload: Mapping[str, object], now: datetime
) -> LocalSession:
    """Parse rotated tokens and their positive lifetime from a closed response."""

    if (
        not isinstance(payload, Mapping)
        or not _REFRESH_REQUIRED_KEYS.issubset(payload)
        or not set(payload).issubset(_REFRESH_ALLOWED_KEYS)
        or ("token_type" in payload and payload["token_type"] != "Bearer")
    ):
        raise _invalid("refresh response must contain the exact session fields")
    normalized_now = _aware_utc(now, context="refresh response")
    lifetime = payload["expires_in"]
    if (
        isinstance(lifetime, bool)
        or not isinstance(lifetime, (int, float))
        or not 0 < lifetime <= _MAX_EXPIRES_IN_SECONDS
    ):
        raise _invalid("refresh response contains an invalid session lifetime")
    try:
        expires_at = normalized_now + timedelta(seconds=lifetime)
    except (OverflowError, ValueError) as error:
        raise _invalid(
            "refresh response contains an invalid session lifetime"
        ) from error
    return LocalSession(
        _token(payload["access_token"], context="refresh response"),
        _token(payload["refresh_token"], context="refresh response"),
        expires_at,
    )


def _validate_parent(path: Path) -> None:
    try:
        parent_status = path.parent.lstat()
    except OSError as error:
        raise _invalid("local session parent directory is required") from error
    if stat.S_ISLNK(parent_status.st_mode) or not stat.S_ISDIR(parent_status.st_mode):
        raise _invalid("local session parent must be a real directory")
    if stat.S_IMODE(parent_status.st_mode) != 0o700:
        raise _invalid("local session parent directory must have mode 0700")


def _validate_file_status(file_status: os.stat_result) -> None:
    if not stat.S_ISREG(file_status.st_mode):
        raise _invalid("local session must be a regular file")
    if stat.S_IMODE(file_status.st_mode) != 0o600:
        raise _invalid("local session file must have mode 0600")
    if file_status.st_size > _MAX_SESSION_BYTES:
        raise _invalid("local session file is too large")


def _existing_file_status(path: Path) -> os.stat_result | None:
    try:
        file_status = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise _invalid("cannot inspect local session file") from error
    _validate_file_status(file_status)
    return file_status


def _parse_stored_session(payload: object) -> LocalSession:
    if (
        not isinstance(payload, Mapping)
        or any(not isinstance(key, str) for key in payload)
        or set(payload) != _SESSION_KEYS
    ):
        raise _invalid("local session must contain the exact session fields")
    return LocalSession(
        _token(payload["access_token"], context="local session"),
        _token(payload["refresh_token"], context="local session"),
        _expiry(payload["expires_at"]),
    )


@dataclass(frozen=True, slots=True)
class SessionStore:
    path: Path = field(repr=False)

    def load(self) -> LocalSession:
        _validate_parent(self.path)
        expected_status = _existing_file_status(self.path)
        if expected_status is None:
            raise _invalid("local session file is required")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            file_descriptor = os.open(self.path, flags)
        except FileNotFoundError as error:
            raise _invalid("local session file is required") from error
        except OSError as error:
            raise _invalid("cannot read local session file") from error
        try:
            opened_status = os.fstat(file_descriptor)
            _validate_file_status(opened_status)
            if (
                opened_status.st_dev != expected_status.st_dev
                or opened_status.st_ino != expected_status.st_ino
            ):
                raise _invalid("local session file changed while opening")
            with os.fdopen(file_descriptor, "rb", closefd=True) as session_file:
                file_descriptor = -1
                encoded = session_file.read(_MAX_SESSION_BYTES + 1)
        except OSError as error:
            raise _invalid("cannot read local session file") from error
        finally:
            if file_descriptor >= 0:
                os.close(file_descriptor)
        if len(encoded) > _MAX_SESSION_BYTES:
            raise _invalid("local session file is too large")
        try:
            decoded = encoded.decode("utf-8")
            payload = json.loads(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise _invalid("local session file contains invalid JSON") from error
        return _parse_stored_session(payload)

    def save(self, session: LocalSession) -> None:
        if not isinstance(session, LocalSession):
            raise _invalid("cannot save local session")
        _validate_parent(self.path)
        _existing_file_status(self.path)
        payload: dict[str, object] = {}
        payload["access_token"] = session.access_token
        payload["expires_at"] = _format_expiry(session.expires_at)
        payload["refresh_token"] = session.refresh_token
        encoded = json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        temporary_path: Path | None = None
        committed = False
        try:
            file_descriptor, raw_temporary_path = tempfile.mkstemp(
                prefix=f".{self.path.name}.", dir=self.path.parent
            )
            temporary_path = Path(raw_temporary_path)
            try:
                os.fchmod(file_descriptor, 0o600)
                with os.fdopen(file_descriptor, "wb", closefd=True) as session_file:
                    file_descriptor = -1
                    session_file.write(encoded)
                    session_file.flush()
                    os.fsync(session_file.fileno())
            finally:
                if file_descriptor >= 0:
                    os.close(file_descriptor)
            _validate_parent(self.path)
            _existing_file_status(self.path)
            os.replace(temporary_path, self.path)
            committed = True
            directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            directory_flags |= getattr(os, "O_NOFOLLOW", 0)
            directory_descriptor = os.open(self.path.parent, directory_flags)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except (OSError, ValidationError) as error:
            raise _invalid("cannot save local session") from error
        finally:
            if not committed and temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
