from __future__ import annotations

import json
import os
import stat
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NoReturn

import pytest

from smartenit_rescue.backends.harmony_g2.session import (
    LocalSession,
    SessionStore,
    parse_refreshed_session,
)
from smartenit_rescue.errors import ValidationError

NOW = datetime(2026, 9, 15, 20, 40, tzinfo=UTC)
EXPIRY = NOW + timedelta(minutes=2)
ACCESS_VALUE = "<local-access>"
REFRESH_VALUE = "<local-refresh>"
NEXT_ACCESS_VALUE = "<next-access>"
NEXT_REFRESH_VALUE = "<next-refresh>"
STORED_SESSION: dict[str, object] = {}
STORED_SESSION["access_token"] = ACCESS_VALUE
STORED_SESSION["expires_at"] = "2026-09-15T20:42:00Z"
STORED_SESSION["refresh_token"] = REFRESH_VALUE


def secure_parent(tmp_path: Path) -> Path:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    parent.chmod(0o700)
    return parent


def write_session(path: Path, payload: object = STORED_SESSION) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)


def changed_session(field: str, value: object) -> dict[str, object]:
    payload: dict[str, object] = dict(STORED_SESSION)
    payload[field] = value
    return payload


def refresh_payload(
    lifetime: object = 120,
    *,
    include_refresh: bool = True,
    include_unexpected: bool = False,
) -> dict[str, object]:
    pairs: list[tuple[str, object]] = [
        ("access_token", NEXT_ACCESS_VALUE),
        ("expires_in", lifetime),
    ]
    if include_refresh:
        pairs.append(("refresh_token", NEXT_REFRESH_VALUE))
    if include_unexpected:
        pairs.append(("unexpected", "value"))
    return dict(pairs)


def test_load_accepts_exact_schema_and_redacts_tokens(tmp_path: Path) -> None:
    path = secure_parent(tmp_path) / "session.json"
    write_session(path)

    session = SessionStore(path).load()

    assert session == LocalSession(ACCESS_VALUE, REFRESH_VALUE, EXPIRY)
    assert session.expires_at.tzinfo is UTC
    representation = repr(session)
    assert ACCESS_VALUE not in representation
    assert REFRESH_VALUE not in representation


def test_load_allows_past_expiry_for_attempted_renewal(tmp_path: Path) -> None:
    path = secure_parent(tmp_path) / "session.json"
    payload = changed_session("expires_at", "2020-01-01T00:00:00Z")
    write_session(path, payload)

    session = SessionStore(path).load()

    assert session.expires_at == datetime(2020, 1, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    "payload",
    [
        {
            name: value
            for name, value in STORED_SESSION.items()
            if name != "refresh_token"
        },
        changed_session("unexpected", "value"),
        changed_session("access_token", True),
        changed_session("refresh_token", 3),
        changed_session("expires_at", False),
    ],
)
def test_load_rejects_missing_unknown_or_wrongly_typed_fields(
    tmp_path: Path, payload: object
) -> None:
    path = secure_parent(tmp_path) / "session.json"
    write_session(path, payload)

    with pytest.raises(ValidationError, match="local session"):
        SessionStore(path).load()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("access_token", ""),
        ("access_token", "local access"),
        ("access_token", "local\naccess"),
        ("refresh_token", "caf\N{LATIN SMALL LETTER E WITH ACUTE}"),
        ("refresh_token", "x" * 8193),
    ],
)
def test_load_rejects_invalid_token_without_echoing_it(
    tmp_path: Path, field: str, value: str
) -> None:
    path = secure_parent(tmp_path) / "session.json"
    write_session(path, changed_session(field, value))

    with pytest.raises(ValidationError) as captured:
        SessionStore(path).load()

    if value:
        assert value not in str(captured.value)


@pytest.mark.parametrize(
    "expires_at",
    ["not-a-time", "2026-09-15T20:42:00", "2026-09-15T20:42:00+01:00"],
)
def test_load_requires_canonical_utc_expiry(tmp_path: Path, expires_at: str) -> None:
    path = secure_parent(tmp_path) / "session.json"
    write_session(path, changed_session("expires_at", expires_at))

    with pytest.raises(ValidationError, match="expiry"):
        SessionStore(path).load()


def test_malformed_json_is_rejected_without_echoing_contents(tmp_path: Path) -> None:
    path = secure_parent(tmp_path) / "session.json"
    malformed = json.dumps(changed_session("access_token", "do-not-echo"))[:-1]
    path.write_text(malformed, encoding="utf-8")
    path.chmod(0o600)

    with pytest.raises(ValidationError) as captured:
        SessionStore(path).load()

    assert "do-not-echo" not in str(captured.value)
    assert str(path) not in str(captured.value)


def test_missing_session_has_bounded_error(tmp_path: Path) -> None:
    path = secure_parent(tmp_path) / "session.json"

    with pytest.raises(ValidationError, match=r"^local session file is required$"):
        SessionStore(path).load()


@pytest.mark.skipif(os.name != "posix", reason="POSIX file security contract")
def test_load_rejects_symlink_and_non_regular_session_files(tmp_path: Path) -> None:
    parent = secure_parent(tmp_path)
    target = parent / "target.json"
    write_session(target)
    symlink = parent / "session.json"
    symlink.symlink_to(target)

    with pytest.raises(ValidationError, match="regular file"):
        SessionStore(symlink).load()

    symlink.unlink()
    symlink.mkdir(mode=0o600)
    with pytest.raises(ValidationError, match="regular file"):
        SessionStore(symlink).load()


@pytest.mark.skipif(os.name != "posix", reason="POSIX file security contract")
def test_load_rejects_insecure_session_or_parent_mode(tmp_path: Path) -> None:
    parent = secure_parent(tmp_path)
    path = parent / "session.json"
    write_session(path)
    path.chmod(0o640)

    with pytest.raises(ValidationError, match="0600"):
        SessionStore(path).load()

    path.chmod(0o600)
    parent.chmod(0o750)
    with pytest.raises(ValidationError, match="0700"):
        SessionStore(path).load()


@pytest.mark.skipif(os.name != "posix", reason="POSIX file security contract")
def test_load_rejects_symlink_parent(tmp_path: Path) -> None:
    actual = secure_parent(tmp_path)
    write_session(actual / "session.json")
    alias = tmp_path / "alias"
    alias.symlink_to(actual, target_is_directory=True)

    with pytest.raises(ValidationError, match="parent"):
        SessionStore(alias / "session.json").load()


def test_parse_refreshed_session_uses_rotated_tokens_and_positive_lifetime() -> None:
    session = parse_refreshed_session(
        refresh_payload(),
        NOW,
    )

    assert session == LocalSession(NEXT_ACCESS_VALUE, NEXT_REFRESH_VALUE, EXPIRY)


def test_parse_refreshed_session_accepts_explicit_bearer_token_type() -> None:
    payload = refresh_payload()
    payload["token_type"] = "Bearer"

    session = parse_refreshed_session(payload, NOW)

    assert session == LocalSession(NEXT_ACCESS_VALUE, NEXT_REFRESH_VALUE, EXPIRY)


@pytest.mark.parametrize(
    "payload",
    [
        refresh_payload(include_refresh=False),
        refresh_payload(0),
        refresh_payload(True),
        refresh_payload(float("inf")),
        refresh_payload(10**10000),
        refresh_payload(include_unexpected=True),
        {**refresh_payload(), "token_type": "Basic"},
        {**refresh_payload(), "token_type": True},
    ],
)
def test_parse_refreshed_session_rejects_invalid_payload(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError, match="refresh response"):
        parse_refreshed_session(payload, NOW)


def test_parse_refreshed_session_requires_aware_utc_clock() -> None:
    with pytest.raises(ValidationError, match="clock"):
        parse_refreshed_session(
            refresh_payload(),
            NOW.replace(tzinfo=None),
        )


@pytest.mark.skipif(os.name != "posix", reason="POSIX durability contract")
def test_save_is_compact_atomic_and_durable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = secure_parent(tmp_path)
    path = parent / "session.json"
    real_mkstemp = tempfile.mkstemp
    real_replace = os.replace
    real_fsync = os.fsync
    calls: list[tuple[object, ...]] = []

    def traced_mkstemp(*, prefix: str, dir: Path) -> tuple[int, str]:
        calls.append(("mkstemp", prefix, dir))
        return real_mkstemp(prefix=prefix, dir=dir)

    def traced_replace(source: str | Path, destination: str | Path) -> None:
        calls.append(("replace", Path(source).parent, Path(destination)))
        real_replace(source, destination)

    def traced_fsync(file_descriptor: int) -> None:
        calls.append(("fsync", stat.S_ISDIR(os.fstat(file_descriptor).st_mode)))
        real_fsync(file_descriptor)

    monkeypatch.setattr(tempfile, "mkstemp", traced_mkstemp)
    monkeypatch.setattr(os, "replace", traced_replace)
    monkeypatch.setattr(os, "fsync", traced_fsync)

    SessionStore(path).save(LocalSession(ACCESS_VALUE, REFRESH_VALUE, EXPIRY))

    assert path.read_bytes() == json.dumps(
        STORED_SESSION, separators=(",", ":"), sort_keys=True
    ).encode("ascii")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert ("mkstemp", ".session.json.", parent) in calls
    assert ("replace", parent, path) in calls
    assert ("fsync", False) in calls
    assert ("fsync", True) in calls
    assert list(parent.glob(".session.json.*")) == []


@pytest.mark.skipif(os.name != "posix", reason="POSIX durability contract")
def test_failed_rotation_preserves_previous_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = secure_parent(tmp_path)
    path = parent / "session.json"
    write_session(path)
    previous = path.read_bytes()

    def fail_replace(_source: object, _destination: object) -> NoReturn:
        raise OSError("simulated replacement failure")

    monkeypatch.setattr(os, "replace", fail_replace)

    with pytest.raises(ValidationError, match="save local session"):
        SessionStore(path).save(
            LocalSession(NEXT_ACCESS_VALUE, NEXT_REFRESH_VALUE, EXPIRY)
        )

    assert path.read_bytes() == previous
    assert list(parent.glob(".session.json.*")) == []
