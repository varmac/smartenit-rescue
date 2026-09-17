from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from scripts.check_public_tree import Finding, main, scan_text, scan_tree


def _joined(*parts: str) -> str:
    return "".join(parts)


def test_scan_text_returns_sorted_findings() -> None:
    text = (
        _joined("/", "Users", "/alice/", "private", "/project\n")
        + _joined("pass", "word = 'top-", "secret", "'\n")
        + _joined("host = '", "192", ".168.10.20'\n")
        + _joined("pass", "word = 'another'")
    )
    assert scan_text("sample.txt", text) == (
        Finding(path="sample.txt", line=1, rule="personal-path"),
        Finding(path="sample.txt", line=2, rule="secret-assignment"),
        Finding(path="sample.txt", line=3, rule="private-ip"),
        Finding(path="sample.txt", line=4, rule="secret-assignment"),
    )


@pytest.mark.parametrize(
    "address", [_joined("fd12", ":3456::1"), _joined("fe80", "::abcd")]
)
def test_scan_text_rejects_local_ipv6_addresses(address: str) -> None:
    assert scan_text("notes.txt", f"host = [{address}]") == (
        Finding(path="notes.txt", line=1, rule="private-ip"),
    )


def test_scan_text_allows_documentation_ipv6_address() -> None:
    assert scan_text("notes.txt", "host = [2001:db8::1]") == ()


@pytest.mark.parametrize(
    "personal_path",
    [
        _joined("/", "Users", "/sam/project"),
        _joined("/", "home", "/user/project"),
        _joined("/", "Users", "/sam/project", "."),
        _joined("/", "Users", "/sam"),
        _joined("/", "home", "/user"),
        _joined("/", "private", "/var/folders/7f/"),
    ],
)
def test_scan_text_rejects_personal_paths_with_s_in_username(
    personal_path: str,
) -> None:
    assert scan_text("notes.txt", personal_path) == (
        Finding(path="notes.txt", line=1, rule="personal-path"),
    )


def test_scan_text_rejects_posix_paths_with_no_trailing_boundary() -> None:
    assert scan_text(
        "notes.txt", _joined("logs are under ", "/", "Users", "/alice")
    ) == (Finding(path="notes.txt", line=1, rule="personal-path"),)


def test_scan_text_does_not_reject_root_only_posix_homes() -> None:
    assert scan_text("notes.txt", _joined("/", "Users")) == ()
    assert scan_text("notes.txt", _joined("/", "home")) == ()
    assert scan_text("notes.txt", _joined("/", "home", "/")) == ()


def test_scan_text_does_not_reject_public_urls_containing_users_path_segment() -> None:
    assert (
        scan_text(
            "notes.txt", _joined("visit https://example.com", "/", "Users", "/alice")
        )
        == ()
    )


@pytest.mark.parametrize(
    "raw_id",
    [
        _joined("00112233", "44556677"),
        _joined("0x00112233", "44556677"),
        _joined("00:11:22:33:", "44:55:66:77"),
        _joined("00-11-22-33-", "44-55-66-77"),
    ],
)
def test_scan_text_rejects_supported_eui64_representations(raw_id: str) -> None:
    line = f"device_id = {raw_id}"

    assert scan_text("devices.txt", line) == (
        Finding(path="devices.txt", line=1, rule="private-device-id"),
    )


def test_scan_text_allows_documented_synthetic_eui64_value() -> None:
    line = "device_id = " + _joined("02000000", "00000001")

    assert scan_text("README.md", line) == ()


@pytest.mark.parametrize(
    "raw_id",
    [
        _joined("00112233", "44556677"),
        _joined("00:11:22:33:", "44:55:66:77"),
        _joined("00-11-22-33-", "44-55-66-77"),
        _joined("00:11:22:", "33:44:55"),
    ],
)
def test_scan_text_rejects_device_id_without_identifier_keyword(raw_id: str) -> None:
    line = "Patio load: " + raw_id

    assert scan_text("notes.txt", line) == (
        Finding(path="notes.txt", line=1, rule="private-device-id"),
    )


def test_scan_text_does_not_match_partial_malformed_mac() -> None:
    assert scan_text("notes.txt", "02:00:00:00:00:00:01") == ()


def test_synthetic_eui64_exemption_does_not_hide_other_identifiers() -> None:
    line = (
        "device_id = "
        + _joined("02000000", "00000001")
        + ", serial = "
        + _joined("00112233", "44556677")
    )

    assert scan_text("devices.txt", line) == (
        Finding(path="devices.txt", line=1, rule="private-device-id"),
    )


@pytest.mark.parametrize(
    "line",
    [
        _joined("for example, serial = ", "00112233", "44556677"),
        _joined("sample value: device_id = ", "00:11:22:33", ":44:55:66:77"),
        _joined("placeholder fingerprint = ", "AA" * 20),
    ],
)
def test_scan_text_rejects_identifiers_with_reference_words(line: str) -> None:
    assert scan_text("devices.txt", line) == (
        Finding(path="devices.txt", line=1, rule="private-device-id"),
    )


def test_scan_text_allows_synthetic_eui64_when_reference_words_present() -> None:
    line = "for example, device_id = " + _joined("02000000", "00000001")
    assert scan_text("devices.txt", line) == ()


def test_scan_text_rejects_windows_user_home_with_trailing_separator() -> None:
    assert scan_text(
        "notes.txt",
        _joined("C:", "\\", "Users", "\\", "alice", "\\"),
    ) == (Finding(path="notes.txt", line=1, rule="personal-path"),)


def test_scan_text_rejects_windows_user_home_descendants_with_trailing_separator() -> (
    None
):
    assert scan_text(
        "notes.txt",
        _joined(
            "C:",
            "\\",
            "Users",
            "\\",
            "alice",
            "\\",
            "projects",
            "\\",
        ),
    ) == (Finding(path="notes.txt", line=1, rule="personal-path"),)


def test_scan_text_rejects_non_synthetic_device_id_with_reference_word_and_separator() -> (
    None
):
    line = "for example: device-id: " + _joined("00112233", "44556677")
    assert scan_text("devices.txt", line) == (
        Finding(path="devices.txt", line=1, rule="private-device-id"),
    )


@pytest.mark.parametrize(
    ("relative_path", "content", "rule"),
    [
        (
            "notes.txt",
            _joined("/", "Users", "/alice/", "private", "/project"),
            "personal-path",
        ),
        (
            "notes.txt",
            _joined("/", "home", "/alice/", "private", "/project"),
            "personal-path",
        ),
        (
            "notes.txt",
            _joined("C:\\", "Users", "\\alice\\", "private", "\\project"),
            "personal-path",
        ),
        (
            "config.toml",
            _joined("host = '", "192", ".168.4.20'"),
            "private-ip",
        ),
        ("config.yaml", _joined("api_", "token: ", "secret"), "secret-assignment"),
        (
            "config.toml",
            _joined("export API_", "KEY = 'not-a-placeholder'"),
            "secret-assignment",
        ),
        (
            "config.json",
            _joined('{"', "token", '": "not-a-placeholder"}'),
            "secret-assignment",
        ),
        (
            "script.sh",
            _joined("export SSH_", "KEY='not-a-placeholder'"),
            "secret-assignment",
        ),
        (
            "pyproject.toml",
            _joined('dependency = {path = "..', "/private", '"}'),
            "local-dependency",
        ),
        ("requirements.txt", _joined("-e ..", "/private"), "editable-dependency"),
        (
            "requirements.txt",
            _joined("--editable ..", "/private"),
            "editable-dependency",
        ),
        (
            "requirements.txt",
            _joined("pkg @ file", ":///tmp/private/pkg.whl"),
            "local-dependency",
        ),
        (
            "requirements.txt",
            _joined("pkg @ file", "://./tmp/private/pkg"),
            "local-dependency",
        ),
        (
            "requirements.txt",
            _joined("pkg @ ..", "/tmp/private/pkg"),
            "local-dependency",
        ),
        (
            "requirements.txt",
            _joined("pkg ", "@", " .", "/local"),
            "local-dependency",
        ),
        (
            "pyproject.toml",
            _joined("dependency = {pa", 'th = "', "./local", '"}'),
            "local-dependency",
        ),
        (
            "pyproject.toml",
            _joined("dependency = {pa", "th = 'C:\\", "work\\local'}"),
            "local-dependency",
        ),
        (
            "pyproject.toml",
            _joined("dependency = {pa", "th = 'C:/", "work/local'}"),
            "local-dependency",
        ),
        (
            "pyproject.toml",
            _joined("dependency = {pa", "th = '..\\", "local'}"),
            "local-dependency",
        ),
        (
            "requirements.txt",
            _joined("pkg ", "@", " ..", "/../dependencies/local"),
            "local-dependency",
        ),
        (
            "requirements.txt",
            _joined("git+", "ssh", "://private.example/pkg@v1"),
            "private-git",
        ),
        (
            "requirements.txt",
            _joined("git+", "https", "://example.com/private/pkg"),
            "private-git",
        ),
        (
            "requirements.txt",
            _joined("git", "@github.com:example/private.git"),
            "private-git",
        ),
        (
            "requirements.txt",
            _joined("pkg @ git", "://github.com/private/repo"),
            "private-git",
        ),
        (
            "requirements.txt",
            _joined("pkg @ ssh", "://git@private.example.org/internal"),
            "private-git",
        ),
        (
            "requirements.txt",
            _joined("pkg @ https", "://github.com/private/repo.git"),
            "private-git",
        ),
        (
            "requirements.txt",
            _joined("pkg @ deploy", "@private.example.org:internal/repo.git"),
            "private-git",
        ),
        (
            "devices.txt",
            _joined("device_id = AA:BB:CC:", "DD:EE:FF"),
            "private-device-id",
        ),
        (
            "cert.fingerprint",
            _joined(
                "fingerprint = AAABBBCCCDDDEEEFF0011223344556677",
                "8899AABBCCDDEEFF0011223344556677",
            ),
            "private-device-id",
        ),
        (".env", "EXAMPLE=value", "disallowed-path"),
        ("gateway.json", "{}", "disallowed-path"),
        ("credentials", "{}", "disallowed-path"),
        (_joined("logs/pri", "vate-session.log"), "safe", "private-artifact"),
        (_joined("evidence/screen", "shots/session.png"), "safe", "private-artifact"),
        ("vendor/bundle.tar.gz", "safe", "private-artifact"),
        (_joined("logs/cap", "ture.log"), "safe", "private-artifact"),
        (
            "docs/notes.txt",
            _joined("pass", "word = 'not-a-placeholder'"),
            "secret-assignment",
        ),
        (
            "tests/notes.txt",
            _joined("api_k", "ey = 'not-a-placeholder'"),
            "secret-assignment",
        ),
        (
            "scripts/check_public_tree.py",
            _joined("api_k", "ey = 'not-a-placeholder'"),
            "secret-assignment",
        ),
        (
            "secrets.lock",
            _joined("api_k", "ey = 'not-a-placeholder'"),
            "secret-assignment",
        ),
    ],
)
def test_scan_tree_rejects_private_material(
    tmp_path: Path, relative_path: str, content: str, rule: str
) -> None:
    target = tmp_path / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    assert rule in {finding.rule for finding in scan_tree(tmp_path)}


def test_scan_tree_rejects_disallowed_files_and_file_symlink(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("EXAMPLE=value", encoding="utf-8")
    target = tmp_path / "safe.txt"
    target.write_text("safe", encoding="utf-8")
    linked = tmp_path / "linked.txt"
    linked.symlink_to(target)

    rules = {finding.rule for finding in scan_tree(tmp_path)}
    paths = {finding.path for finding in scan_tree(tmp_path)}

    assert "disallowed-path" in rules
    assert "symlink" in rules
    assert "linked.txt" in paths


@pytest.mark.parametrize(
    "relative_path",
    (
        "secrets/mqtt_username",
        "secrets/mqtt_password",
        "nested/secrets/broker-credential",
        "nested/Secrets/broker-credential",
        "mqtt_username",
        "mqtt_password",
    ),
)
def test_scan_tree_rejects_bare_credential_files(
    tmp_path: Path, relative_path: str
) -> None:
    target = tmp_path / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("synthetic-placeholder\n", encoding="utf-8")

    assert Finding(path=relative_path, line=None, rule="private-artifact") in scan_tree(
        tmp_path
    )


def test_git_ignores_local_secrets_directory(tmp_path: Path) -> None:
    root = Path(__file__).parents[1]
    (tmp_path / ".gitignore").write_text(
        (root / ".gitignore").read_text(encoding="utf-8"), encoding="utf-8"
    )
    subprocess.run(
        ["git", "init", "--quiet"], cwd=tmp_path, check=True, capture_output=True
    )
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "--verbose", "secrets/mqtt_username"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert result.stdout.split(":", 1)[0] == ".gitignore"


def test_scan_tree_checks_nested_worktree_content(tmp_path: Path) -> None:
    nested = tmp_path / ".worktrees" / "other-checkout"
    nested.mkdir(parents=True)
    (nested / ".env").write_text("EXAMPLE=value", encoding="utf-8")

    assert Finding(
        path=".worktrees/other-checkout/.env", line=None, rule="disallowed-path"
    ) in scan_tree(tmp_path)


@pytest.mark.skipif(os.name == "nt", reason="directory symlink is platform-specific")
def test_scan_tree_reports_directory_symlinks(tmp_path: Path) -> None:
    (tmp_path / "source").mkdir()
    (tmp_path / "source" / "safe.txt").write_text("safe", encoding="utf-8")
    (tmp_path / "linked-dir").symlink_to(tmp_path / "source")

    findings = scan_tree(tmp_path)
    assert "symlink" in {finding.rule for finding in findings}
    assert "linked-dir" in {finding.path for finding in findings}


@pytest.mark.skipif(os.name == "nt", reason="directory symlink is platform-specific")
def test_scan_tree_reports_skipped_name_directory_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    (target / "safe.txt").write_text("safe", encoding="utf-8")
    (tmp_path / ".venv").symlink_to(target)

    findings = scan_tree(tmp_path)
    paths = {finding.path for finding in findings}
    assert ".venv" in paths
    assert {finding.rule for finding in findings if finding.path == ".venv"} == {
        "symlink"
    }


def test_scan_tree_includes_docs_and_tests_violations(tmp_path: Path) -> None:
    (tmp_path / "docs" / ".env").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs" / ".env").write_text("value=1", encoding="utf-8")
    (tmp_path / "tests" / "gateway.json").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "tests" / "gateway.json").write_text("{}", encoding="utf-8")

    findings = scan_tree(tmp_path)
    rules = {finding.path: finding.rule for finding in findings}
    assert rules["docs/.env"] == "disallowed-path"
    assert rules["tests/gateway.json"] == "disallowed-path"


def test_scan_tree_checks_every_public_file_without_exemptions(tmp_path: Path) -> None:
    trigger = _joined("pass", "word = 'not-a-placeholder'")
    paths = (
        "scripts/check_public_tree.py",
        "tests/test_public_tree.py",
        "docs/superpowers/plans/public-foundation.md",
    )
    for relative in paths:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(trigger, encoding="utf-8")

    findings = scan_tree(tmp_path)
    assert {finding.path for finding in findings} == set(paths)
    assert {finding.rule for finding in findings} == {"secret-assignment"}


def test_scan_tree_orders_findings_deterministically(tmp_path: Path) -> None:
    (tmp_path / "z.txt").write_text(
        _joined("192", ".168.0.1\n", "pass", "word='x'"), encoding="utf-8"
    )
    (tmp_path / "a.txt").write_text(
        _joined(
            "/",
            "Users",
            "/bob/private\n",
            "192",
            ".168.1.1\n",
            "pass",
            "word='x'",
        ),
        encoding="utf-8",
    )

    findings = scan_tree(tmp_path)
    assert findings == (
        Finding(path="a.txt", line=1, rule="personal-path"),
        Finding(path="a.txt", line=2, rule="private-ip"),
        Finding(path="a.txt", line=3, rule="secret-assignment"),
        Finding(path="z.txt", line=1, rule="private-ip"),
        Finding(path="z.txt", line=2, rule="secret-assignment"),
    )


def test_scan_tree_rejects_invalid_roots(tmp_path: Path) -> None:
    missing = tmp_path / "missing-root"
    file_root = tmp_path / "not-dir.txt"
    file_root.write_text("safe", encoding="utf-8")

    assert scan_tree(missing)[0] == Finding(
        path=str(missing), line=None, rule="missing-root"
    )
    assert scan_tree(file_root)[0] == Finding(
        path=str(file_root), line=None, rule="not-a-directory"
    )


def test_scan_tree_non_utf8_and_io_errors(tmp_path: Path) -> None:
    binary = tmp_path / "binary.bin"
    binary.write_bytes(b"\xff\xfe\xfd")
    rules = {finding.rule for finding in scan_tree(tmp_path)}
    assert "decode-error" in rules

    if os.name != "nt":
        unreadable = tmp_path / "unreadable.txt"
        unreadable.write_text("value='abc'", encoding="utf-8")
        unreadable.chmod(0o000)
        assert "io-error" in {finding.rule for finding in scan_tree(tmp_path)}


def test_scan_tree_reports_walk_enumeration_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    original_root = tmp_path

    def fake_walk(
        top: str | os.PathLike[str],
        topdown: bool = True,
        onerror: Callable[[OSError], object] | None = None,
        followlinks: bool = False,
    ) -> Iterator[tuple[str, list[str], list[str]]]:
        del top, topdown, followlinks
        if onerror is not None:
            error = OSError("permission denied")
            error.filename = str(original_root / "blocked")
            onerror(error)
        yield (str(original_root), [], [])

    monkeypatch.setattr("scripts.check_public_tree.os.walk", fake_walk)
    findings = scan_tree(tmp_path)
    assert findings == (Finding(path="blocked", line=None, rule="io-error"),)


def test_scan_tree_distinguishes_public_assets_from_private_captures(
    tmp_path: Path,
) -> None:
    assets = tmp_path / "assets"
    assets.mkdir()
    for extension in (".png", ".jpg", ".jpeg", ".webm", ".gif"):
        (assets / f"public{extension}").write_bytes(b"\xffsynthetic")

    captures = (
        _joined("captures/screen", "shot.png"),
        _joined("captures/cap", "ture.jpg"),
        _joined("assets/ven", "dor-bundle.webm"),
        _joined("assets/pri", "vate-image.gif"),
    )
    for relative in captures:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"\xffmust-not-be-read")

    (tmp_path / "logs" / "public.log").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "logs" / "public.log").write_text("safe", encoding="utf-8")
    suspicious_log = tmp_path / _joined("logs/ven", "dor.log")
    suspicious_log.write_bytes(b"\xffmust-not-be-read")

    findings = scan_tree(tmp_path)
    findings_by_path = {finding.path: finding.rule for finding in findings}
    assert all(
        f"assets/public{extension}" not in findings_by_path
        for extension in (
            ".png",
            ".jpg",
            ".jpeg",
            ".webm",
            ".gif",
        )
    )
    assert all(
        findings_by_path[relative] == "private-artifact" for relative in captures
    )
    assert findings_by_path[_joined("logs/ven", "dor.log")] == "private-artifact"


def test_scan_tree_scans_lockfiles(tmp_path: Path) -> None:
    (tmp_path / "secrets.lock").write_text(
        _joined("api_k", "ey = 'not-a-placeholder'"), encoding="utf-8"
    )
    (tmp_path / "uv.lock").write_text(
        "version = 1\n",
        encoding="utf-8",
    )
    findings = scan_tree(tmp_path)

    assert (Finding(path="secrets.lock", line=1, rule="secret-assignment")) in findings
    assert all(finding.path != "uv.lock" for finding in findings)


def test_main_returns_expected_status_and_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)

    (tmp_path / "README.md").write_text("safe", encoding="utf-8")
    assert main(["."]) == 0
    assert capsys.readouterr().out == ""

    (tmp_path / ".env").write_text("EXAMPLE=1", encoding="utf-8")
    assert main(["."]) == 1
    output = capsys.readouterr().out.strip().splitlines()
    assert output == [".env:0:disallowed-path"]


def test_main_reports_io_and_decode_errors_without_printing_contents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)

    (tmp_path / "corrupt.bin").write_bytes(b"\xff\xfe")
    assert main(["."]) == 1
    output = capsys.readouterr().out.strip().splitlines()
    assert output == ["corrupt.bin:0:decode-error"]


@pytest.mark.skipif(os.name == "nt", reason="permission change is platform-specific")
def test_main_reports_io_error_without_content_leak(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    locked = tmp_path / "locked.txt"
    locked.write_text("value='secret'", encoding="utf-8")
    locked.chmod(0)

    assert main([str(tmp_path)]) == 1
    output = capsys.readouterr().out.strip()
    assert output == "locked.txt:0:io-error"


def test_main_reports_invalid_root(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    invalid = tmp_path / "missing"
    assert main([str(invalid)]) == 1
    output = capsys.readouterr().out.strip()
    assert output == f"{invalid}:0:missing-root"


def test_main_reports_not_a_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    file_root = tmp_path / "not-dir.txt"
    file_root.write_text("safe", encoding="utf-8")
    assert main([str(file_root)]) == 1
    output = capsys.readouterr().out.strip()
    assert output == f"{file_root}:0:not-a-directory"


@pytest.mark.parametrize(
    ("relative_path", "content"),
    [
        ("README.md", "Example broker: 192.0.2.10; token = '<user-supplied>'"),
        (
            "README.md",
            "Example certificate fingerprint: <example-certificate-fingerprint>",
        ),
        ("README.md", "api_key = '<your-api-key>'; password = '<your-password>'"),
    ],
)
def test_scan_tree_allows_public_examples(
    tmp_path: Path, relative_path: str, content: str
) -> None:
    target = tmp_path / relative_path
    target.write_text(content, encoding="utf-8")

    assert scan_tree(tmp_path) == ()


def test_scan_tree_allows_public_urls_in_key_like_assignments(tmp_path: Path) -> None:
    (tmp_path / "config.txt").write_text(
        _joined("api_k", 'ey = "https://example.com/keys"\n')
        + _joined("api_", "secret = https://docs.example.com/reference\n")
        + _joined("pass", "word = http://public.example.org/guide\n"),
        encoding="utf-8",
    )

    assert scan_tree(tmp_path) == ()


@pytest.mark.parametrize(
    "content",
    [
        "def request(access_token: str | None = None) -> None: ...",
        "request(access_token=session.access_token)",
    ],
)
def test_scan_text_allows_python_types_and_credential_references(content: str) -> None:
    assert scan_text("client.py", content) == ()


def test_scan_text_rejects_python_secret_string_assignment() -> None:
    content = _joined("access_", "token = 'not-a-placeholder'")

    assert scan_text("client.py", content) == (
        Finding(path="client.py", line=1, rule="secret-assignment"),
    )


def test_scan_text_allows_noncredential_token_type_metadata() -> None:
    content = _joined('{"token', '_type": "Bearer"}')

    assert scan_text("response.json", content) == ()


@pytest.mark.parametrize(
    "field_name",
    ["key", "signing_key", "encryption_key"],
)
def test_scan_tree_rejects_generic_secret_key_assignments(
    tmp_path: Path, field_name: str
) -> None:
    (tmp_path / "config.toml").write_text(
        f"{field_name} = 'not-a-placeholder'",
        encoding="utf-8",
    )

    assert scan_tree(tmp_path) == (
        Finding(path="config.toml", line=1, rule="secret-assignment"),
    )


def test_scan_tree_rejects_credential_bearing_url_without_leaking_value(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    credential_url = _joined(
        "https://user:",
        "not-a-placeholder",
        "@example.com/reference",
    )
    (tmp_path / "config.toml").write_text(
        _joined("pass", "word=") + credential_url,
        encoding="utf-8",
    )

    assert main([str(tmp_path)]) == 1
    assert capsys.readouterr().out.strip() == "config.toml:1:secret-assignment"


def test_scan_tree_allows_noncredentialed_public_url_for_generic_key(
    tmp_path: Path,
) -> None:
    (tmp_path / "config.toml").write_text(
        _joined("documentation_", "key=https://example.com/reference"),
        encoding="utf-8",
    )

    assert scan_tree(tmp_path) == ()


@pytest.mark.parametrize(
    "filename",
    [
        "credentials.yaml",
        "credential.toml",
        "secrets.env",
        "auth-config.json",
        "service-account.json",
        ".npmrc",
        ".pypirc",
    ],
)
def test_scan_tree_rejects_credential_filenames(tmp_path: Path, filename: str) -> None:
    (tmp_path / filename).write_text("placeholder", encoding="utf-8")
    assert scan_tree(tmp_path) == (
        Finding(path=filename, line=None, rule="disallowed-path"),
    )


def test_repository_passes_its_own_scanner() -> None:
    root = Path(__file__).resolve().parents[1]
    assert scan_tree(root) == ()


def test_harmony_guide_uses_sanitized_markers_and_no_cloud_bootstrap() -> None:
    root = Path(__file__).resolve().parents[1]
    text = (root / "docs/harmony-g2.md").read_text(encoding="utf-8")

    assert "<private-ip-address>" in text
    assert "<local-access-token>" in text
    assert "<local-refresh-token>" in text
    assert "non-runnable" in text
    assert "/v2/oauth2/token" not in text
    assert "client secret" not in text.lower()
