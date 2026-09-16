from __future__ import annotations

import gzip
import io
import stat
import struct
import subprocess
import sys
import tarfile
import zipfile
from collections.abc import Iterable
from pathlib import Path

import pytest

from scripts.check_public_tree import Finding
from scripts.verify_release_tree import main, verify_archive


def _joined(*parts: str) -> str:
    return "".join(parts)


def _safe_sdist_members() -> dict[str, bytes]:
    root = "smartenit_rescue-0.1.0"
    return {
        f"{root}/pyproject.toml": (
            b'[project]\nname = "smartenit-rescue"\n'
            b'dependencies = ["jsonschema>=4.23,<5"]\n'
        ),
        f"{root}/src/smartenit_rescue/__init__.py": b'__version__ = "0.1.0"\n',
    }


def _safe_wheel_members() -> dict[str, bytes]:
    return {
        "smartenit_rescue/__init__.py": b'__version__ = "0.1.0"\n',
        "smartenit_rescue-0.1.0.dist-info/METADATA": (
            b"Metadata-Version: 2.3\n"
            b"Name: smartenit-rescue\n"
            b"Version: 0.1.0\n"
            b"Requires-Dist: jsonschema<5,>=4.23\n"
        ),
        "smartenit_rescue-0.1.0.dist-info/WHEEL": (
            b"Wheel-Version: 1.0\nTag: py3-none-any\n"
        ),
    }


def _write_tar(
    path: Path,
    members: dict[str, bytes],
    *,
    directories: Iterable[str] = (),
    links: Iterable[tuple[str, str]] = (),
    special_names: Iterable[str] = (),
) -> Path:
    if path.name.endswith((".tar.gz", ".tgz")):
        with tarfile.open(path, "w:gz") as archive:
            _populate_tar(archive, members, directories, links, special_names)
    else:
        with tarfile.open(path, "w") as archive:
            _populate_tar(archive, members, directories, links, special_names)
    return path


def _populate_tar(
    archive: tarfile.TarFile,
    members: dict[str, bytes],
    directories: Iterable[str],
    links: Iterable[tuple[str, str]],
    special_names: Iterable[str],
) -> None:
    for name, content in members.items():
        info = tarfile.TarInfo(name)
        info.size = len(content)
        archive.addfile(info, io.BytesIO(content))
    for name in directories:
        info = tarfile.TarInfo(name)
        info.type = tarfile.DIRTYPE
        archive.addfile(info)
    for name, target in links:
        info = tarfile.TarInfo(name)
        info.type = tarfile.SYMTYPE
        info.linkname = target
        archive.addfile(info)
    for name in special_names:
        info = tarfile.TarInfo(name)
        info.type = tarfile.FIFOTYPE
        archive.addfile(info)


def _write_zip(
    path: Path,
    members: dict[str, bytes],
    *,
    directories: Iterable[str] = (),
    links: Iterable[tuple[str, str]] = (),
) -> Path:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in members.items():
            archive.writestr(name, content)
        for name in directories:
            archive.writestr(f"{name.rstrip('/')}/", b"")
        for name, target in links:
            info = zipfile.ZipInfo(name)
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, target)
    return path


def _write_header_only_zip(path: Path, entries: Iterable[tuple[bytes, int]]) -> Path:
    local_parts: list[bytes] = []
    central_parts: list[bytes] = []
    offset = 0
    entry_count = 0
    for name, declared_size in entries:
        local = (
            struct.pack(
                "<IHHHHHIIIHH",
                0x04034B50,
                20,
                0,
                0,
                0,
                0,
                0,
                declared_size,
                declared_size,
                len(name),
                0,
            )
            + name
        )
        central = (
            struct.pack(
                "<IHHHHHHIIIHHHHHII",
                0x02014B50,
                20,
                20,
                0,
                0,
                0,
                0,
                0,
                declared_size,
                declared_size,
                len(name),
                0,
                0,
                0,
                0,
                0,
                offset,
            )
            + name
        )
        local_parts.append(local)
        central_parts.append(central)
        offset += len(local)
        entry_count += 1

    local_data = b"".join(local_parts)
    central_data = b"".join(central_parts)
    end = struct.pack(
        "<IHHHHIIH",
        0x06054B50,
        0,
        0,
        entry_count,
        entry_count,
        len(central_data),
        len(local_data),
        0,
    )
    path.write_bytes(local_data + central_data + end)
    return path


def _damage_deflate_body(path: Path, member_name: str) -> None:
    with zipfile.ZipFile(path) as archive:
        info = archive.getinfo(member_name)
    payload = bytearray(path.read_bytes())
    header = struct.unpack(
        "<IHHHHHIIIHH", payload[info.header_offset : info.header_offset + 30]
    )
    name_length = header[-2]
    extra_length = header[-1]
    body_start = info.header_offset + 30 + name_length + extra_length
    payload[body_start : body_start + info.compress_size] = b"\xff" * info.compress_size
    path.write_bytes(payload)


def _write_truncated_oversized_tar(path: Path, member_name: str) -> Path:
    info = tarfile.TarInfo(member_name)
    info.size = 2 * 1024 * 1024 + 1
    header = info.tobuf(format=tarfile.USTAR_FORMAT)
    path.write_bytes(gzip.compress(header) + b"damaged compressed body")
    return path


def _write_link_before_damaged_body_tar(path: Path, root: str) -> Path:
    linked = tarfile.TarInfo(f"{root}/linked.txt")
    linked.type = tarfile.SYMTYPE
    linked.linkname = "notes.txt"
    regular = tarfile.TarInfo(f"{root}/notes.txt")
    regular.size = 1
    headers = linked.tobuf(format=tarfile.USTAR_FORMAT) + regular.tobuf(
        format=tarfile.USTAR_FORMAT
    )
    path.write_bytes(gzip.compress(headers) + b"damaged compressed body")
    return path


@pytest.mark.parametrize("kind", ["tar", "zip"])
def test_verify_archive_accepts_minimal_safe_artifacts(
    tmp_path: Path, kind: str
) -> None:
    if kind == "tar":
        archive = _write_tar(
            tmp_path / "smartenit_rescue-0.1.0.tar.gz", _safe_sdist_members()
        )
    else:
        archive = _write_zip(
            tmp_path / "smartenit_rescue-0.1.0-py3-none-any.whl",
            _safe_wheel_members(),
        )

    assert verify_archive(archive) == ()


@pytest.mark.parametrize(
    ("member_name", "expected_path"),
    [
        ("/absolute.txt", "/absolute.txt"),
        (
            "smartenit_rescue-0.1.0/../../escape.txt",
            "smartenit_rescue-0.1.0/../../escape.txt",
        ),
        (r"C:\escape.txt", "C:/escape.txt"),
    ],
)
def test_verify_archive_rejects_unsafe_tar_paths_without_extracting(
    tmp_path: Path, member_name: str, expected_path: str
) -> None:
    escape_target = tmp_path.parent / "escape.txt"
    archive = _write_tar(tmp_path / "unsafe.tar", {member_name: b"unsafe"})

    assert verify_archive(archive) == (
        Finding(path=expected_path, line=None, rule="unsafe-path"),
    )
    assert not escape_target.exists()


@pytest.mark.parametrize("name", [b"", b"bad\0name.txt"])
def test_verify_archive_rejects_empty_and_nul_zip_paths(
    tmp_path: Path, name: bytes
) -> None:
    archive = _write_header_only_zip(tmp_path / "unsafe.zip", [(name, 0)])

    findings = verify_archive(archive)

    assert len(findings) == 1
    assert findings[0].rule == "unsafe-path"


def test_verify_archive_rejects_tar_links_and_special_members(tmp_path: Path) -> None:
    members = _safe_sdist_members()
    root = "smartenit_rescue-0.1.0"
    archive = _write_tar(
        tmp_path / "smartenit_rescue-0.1.0.tar.gz",
        members,
        links=[(f"{root}/linked.txt", "pyproject.toml")],
        special_names=[f"{root}/pipe"],
    )

    assert verify_archive(archive) == (
        Finding(path=f"{root}/linked.txt", line=None, rule="link-member"),
        Finding(path=f"{root}/pipe", line=None, rule="special-member"),
    )


def test_verify_archive_rejects_zip_symlinks(tmp_path: Path) -> None:
    members = _safe_wheel_members()
    archive = _write_zip(
        tmp_path / "smartenit_rescue-0.1.0-py3-none-any.whl",
        members,
        links=[("smartenit_rescue/linked.py", "__init__.py")],
    )

    assert verify_archive(archive) == (
        Finding(path="smartenit_rescue/linked.py", line=None, rule="link-member"),
    )


@pytest.mark.parametrize(
    "member_name",
    [
        "smartenit_rescue-0.1.0/.env",
        "smartenit_rescue-0.1.0/config/credentials.json",
        "smartenit_rescue-0.1.0/mqtt_username",
        "smartenit_rescue-0.1.0/mqtt_password",
        "smartenit_rescue-0.1.0/captures/session.pcap",
        "smartenit_rescue-0.1.0/vendor/bundle.zip",
        "smartenit_rescue-0.1.0/vendor/bundle.tar.xz",
        "smartenit_rescue-0.1.0/vendor/bundle.txz",
    ],
)
def test_verify_archive_rejects_forbidden_filenames(
    tmp_path: Path, member_name: str
) -> None:
    members = _safe_sdist_members() | {member_name: b"not read"}
    archive = _write_tar(tmp_path / "smartenit_rescue-0.1.0.tar.gz", members)

    assert "disallowed-path" in {finding.rule for finding in verify_archive(archive)}


@pytest.mark.parametrize("kind", ["sdist", "wheel"])
@pytest.mark.parametrize(
    "forbidden_directory",
    [
        ".git",
        ".scratch",
        ".worktrees",
        ".venv",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "__pycache__",
        "build",
        "dist",
        "generated.egg-info",
    ],
)
def test_verify_archive_rejects_forbidden_directory_components(
    tmp_path: Path, kind: str, forbidden_directory: str
) -> None:
    if kind == "sdist":
        root = "smartenit_rescue-0.1.0"
        member_name = f"{root}/{forbidden_directory}/notes.txt"
        archive = _write_tar(
            tmp_path / "smartenit_rescue-0.1.0.tar.gz",
            _safe_sdist_members() | {member_name: b"not read"},
        )
    else:
        member_name = f"smartenit_rescue/{forbidden_directory}/notes.txt"
        archive = _write_zip(
            tmp_path / "smartenit_rescue-0.1.0-py3-none-any.whl",
            _safe_wheel_members() | {member_name: b"not read"},
        )

    assert verify_archive(archive) == (
        Finding(path=member_name, line=None, rule="disallowed-path"),
    )


@pytest.mark.parametrize(
    ("kind", "forbidden_directory"),
    [("sdist", ".scratch"), ("wheel", ".git")],
)
def test_verify_archive_rejects_forbidden_directory_entries(
    tmp_path: Path, kind: str, forbidden_directory: str
) -> None:
    if kind == "sdist":
        member_name = f"smartenit_rescue-0.1.0/{forbidden_directory}"
        archive = _write_tar(
            tmp_path / "smartenit_rescue-0.1.0.tar.gz",
            _safe_sdist_members(),
            directories=[member_name],
        )
    else:
        member_name = f"smartenit_rescue/{forbidden_directory}"
        archive = _write_zip(
            tmp_path / "smartenit_rescue-0.1.0-py3-none-any.whl",
            _safe_wheel_members(),
            directories=[member_name],
        )

    assert verify_archive(archive) == (
        Finding(path=member_name, line=None, rule="disallowed-path"),
    )


@pytest.mark.parametrize("kind", ["sdist", "wheel"])
@pytest.mark.parametrize("ancestor_first", [True, False])
def test_verify_archive_rejects_file_as_ancestor_in_either_member_order(
    tmp_path: Path, kind: str, ancestor_first: bool
) -> None:
    if kind == "sdist":
        ancestor = "smartenit_rescue-0.1.0/data"
        archive_path = tmp_path / "smartenit_rescue-0.1.0.tar.gz"
        safe_members = _safe_sdist_members()
    else:
        ancestor = "smartenit_rescue/data"
        archive_path = tmp_path / "smartenit_rescue-0.1.0-py3-none-any.whl"
        safe_members = _safe_wheel_members()
    conflicting_members = [
        (ancestor, b"regular file"),
        (f"{ancestor}/notes.txt", b"descendant"),
    ]
    if not ancestor_first:
        conflicting_members.reverse()
    members = dict([*safe_members.items(), *conflicting_members])
    archive = (
        _write_tar(archive_path, members)
        if kind == "sdist"
        else _write_zip(archive_path, members)
    )

    assert verify_archive(archive) == (
        Finding(path=ancestor, line=None, rule="file-ancestor"),
    )


def test_verify_archive_reports_private_content_without_echoing_it(
    tmp_path: Path,
) -> None:
    member_name = "smartenit_rescue-0.1.0/notes.txt"
    private_value = _joined("pass", "word = 'release-value'")
    members = _safe_sdist_members() | {member_name: private_value.encode()}
    archive = _write_tar(tmp_path / "smartenit_rescue-0.1.0.tar.gz", members)

    assert verify_archive(archive) == (
        Finding(path=member_name, line=1, rule="secret-assignment"),
    )


def test_verify_archive_scans_sdist_pkg_info(tmp_path: Path) -> None:
    member_name = "smartenit_rescue-0.1.0/PKG-INFO"
    private_value = _joined("api_", "key = 'release-value'")
    members = _safe_sdist_members() | {member_name: private_value.encode()}
    archive = _write_tar(tmp_path / "smartenit_rescue-0.1.0.tar.gz", members)

    assert verify_archive(archive) == (
        Finding(path=member_name, line=1, rule="secret-assignment"),
    )


@pytest.mark.parametrize(
    ("archive_kind", "metadata", "expected_rule"),
    [
        (
            "sdist",
            _joined('[project]\ndependencies = ["pkg @ file', ':///tmp/pkg"]\n'),
            "local-dependency",
        ),
        (
            "sdist",
            _joined('[tool.uv.sources]\npkg = { path = "..', '/pkg" }\n'),
            "local-dependency",
        ),
        (
            "sdist",
            _joined("-e ..", "/pkg\n"),
            "editable-dependency",
        ),
        (
            "wheel",
            _joined(
                "Metadata-Version: 2.3\nRequires-Dist: pkg @ git+",
                _joined("s", "sh://internal.example/pkg\n"),
            ),
            "private-git",
        ),
        (
            "wheel",
            _joined("Home-page: FILE", ":///tmp/pkg\n"),
            "local-dependency",
        ),
        (
            "wheel",
            _joined("Description: --edit", "able https://github.com/example/pkg\n"),
            "editable-dependency",
        ),
    ],
)
def test_verify_archive_rejects_local_or_private_dependency_metadata(
    tmp_path: Path, archive_kind: str, metadata: str, expected_rule: str
) -> None:
    if archive_kind == "sdist":
        members = _safe_sdist_members()
        members["smartenit_rescue-0.1.0/pyproject.toml"] = metadata.encode()
        archive = _write_tar(tmp_path / "smartenit_rescue-0.1.0.tar.gz", members)
    else:
        members = _safe_wheel_members()
        members["smartenit_rescue-0.1.0.dist-info/METADATA"] = metadata.encode()
        archive = _write_zip(
            tmp_path / "smartenit_rescue-0.1.0-py3-none-any.whl", members
        )

    assert expected_rule in {finding.rule for finding in verify_archive(archive)}


def test_verify_archive_rejects_sdist_dependency_path_that_escapes_root(
    tmp_path: Path,
) -> None:
    metadata_path = "smartenit_rescue-0.1.0/pyproject.toml"
    metadata = _joined(
        "[tool.uv.sources]\n",
        'pkg = { path = "vendor/../..',
        '/outside" }\n',
    )
    members = _safe_sdist_members()
    members[metadata_path] = metadata.encode()
    archive = _write_tar(tmp_path / "smartenit_rescue-0.1.0.tar.gz", members)

    assert verify_archive(archive) == (
        Finding(path=metadata_path, line=2, rule="local-dependency"),
    )


def test_verify_archive_rejects_non_posix_sdist_dependency_path(
    tmp_path: Path,
) -> None:
    metadata_path = "smartenit_rescue-0.1.0/pyproject.toml"
    metadata = b"[tool.uv.sources]\npkg = { path = 'vendor\\pkg' }\n"
    members = _safe_sdist_members()
    members[metadata_path] = metadata
    archive = _write_tar(tmp_path / "smartenit_rescue-0.1.0.tar.gz", members)

    assert verify_archive(archive) == (
        Finding(path=metadata_path, line=2, rule="local-dependency"),
    )


def test_verify_archive_rejects_sdist_editable_dependency_config(
    tmp_path: Path,
) -> None:
    metadata_path = "smartenit_rescue-0.1.0/pyproject.toml"
    metadata = b'[tool.uv.sources]\npkg = { path = "vendor/pkg", editable = true }\n'
    members = _safe_sdist_members()
    members[metadata_path] = metadata
    archive = _write_tar(tmp_path / "smartenit_rescue-0.1.0.tar.gz", members)

    assert verify_archive(archive) == (
        Finding(path=metadata_path, line=2, rule="editable-dependency"),
    )


def test_verify_archive_rejects_malformed_sdist_pyproject(tmp_path: Path) -> None:
    metadata_path = "smartenit_rescue-0.1.0/pyproject.toml"
    members = _safe_sdist_members()
    members[metadata_path] = b"[project\n"
    archive = _write_tar(tmp_path / "smartenit_rescue-0.1.0.tar.gz", members)

    assert verify_archive(archive) == (
        Finding(path=metadata_path, line=None, rule="metadata-error"),
    )


def test_verify_archive_allows_normalized_vendored_dependency_path(
    tmp_path: Path,
) -> None:
    metadata_path = "smartenit_rescue-0.1.0/pyproject.toml"
    metadata = b'[tool.uv.sources]\npkg = { path = "vendor/pkg" }\n'
    members = _safe_sdist_members() | {
        metadata_path: metadata,
        "smartenit_rescue-0.1.0/vendor/pkg/__init__.py": b"",
    }
    archive = _write_tar(tmp_path / "smartenit_rescue-0.1.0.tar.gz", members)

    assert verify_archive(archive) == ()


@pytest.mark.parametrize("kind", ["sdist", "wheel"])
def test_verify_archive_rejects_unexpected_top_level_roots(
    tmp_path: Path, kind: str
) -> None:
    if kind == "sdist":
        members = _safe_sdist_members() | {"other-root/notes.txt": b"safe"}
        archive = _write_tar(tmp_path / "smartenit_rescue-0.1.0.tar.gz", members)
        unexpected_path = "other-root/notes.txt"
    else:
        members = _safe_wheel_members() | {"other_package/data.txt": b"safe"}
        archive = _write_zip(
            tmp_path / "smartenit_rescue-0.1.0-py3-none-any.whl", members
        )
        unexpected_path = "other_package/data.txt"

    assert Finding(
        path=unexpected_path, line=None, rule="unexpected-root"
    ) in verify_archive(archive)


@pytest.mark.parametrize("kind", ["sdist", "wheel"])
def test_verify_archive_requires_distribution_metadata(
    tmp_path: Path, kind: str
) -> None:
    if kind == "sdist":
        archive = _write_tar(
            tmp_path / "smartenit_rescue-0.1.0.tar.gz",
            {"smartenit_rescue-0.1.0/README.md": b"safe\n"},
        )
    else:
        archive = _write_zip(
            tmp_path / "smartenit_rescue-0.1.0-py3-none-any.whl",
            {"smartenit_rescue/__init__.py": b""},
        )

    assert verify_archive(archive) == (
        Finding(path="<archive>", line=None, rule="missing-metadata"),
    )


def test_verify_archive_bounds_individual_and_aggregate_text_sizes(
    tmp_path: Path,
) -> None:
    root = b"smartenit_rescue-0.1.0/"
    too_large = _write_header_only_zip(
        tmp_path / "smartenit_rescue-0.1.0.zip",
        [
            (root + b"pyproject.toml", 2 * 1024 * 1024 + 1),
        ],
    )
    aggregate = _write_header_only_zip(
        tmp_path / "smartenit_rescue-0.1.0-aggregate.zip",
        [(root + f"part-{index}.txt".encode(), 2 * 1024 * 1024) for index in range(17)]
        + [(root + b"pyproject.toml", 1)],
    )

    assert verify_archive(too_large) == (
        Finding(
            path="smartenit_rescue-0.1.0/pyproject.toml",
            line=None,
            rule="member-size-limit",
        ),
    )
    assert Finding(
        path="<archive>", line=None, rule="aggregate-size-limit"
    ) in verify_archive(aggregate)


def test_verify_compressed_tar_rejects_oversized_member_before_body(
    tmp_path: Path,
) -> None:
    member_name = "smartenit_rescue-0.1.0/notes.txt"
    archive = _write_truncated_oversized_tar(
        tmp_path / "smartenit_rescue-0.1.0.tar.gz", member_name
    )

    assert verify_archive(archive) == (
        Finding(path=member_name, line=None, rule="member-size-limit"),
    )


def test_verify_compressed_tar_bounds_aggregate_binary_sizes(tmp_path: Path) -> None:
    root = "smartenit_rescue-0.1.0"
    members = _safe_sdist_members() | {
        f"{root}/payload-{index}.bin": b"\0" * (2 * 1024 * 1024) for index in range(17)
    }
    archive = _write_tar(tmp_path / "smartenit_rescue-0.1.0.tar.gz", members)

    assert Finding(
        path="<archive>", line=None, rule="aggregate-size-limit"
    ) in verify_archive(archive)


def test_verify_compressed_tar_stops_before_body_after_structural_finding(
    tmp_path: Path,
) -> None:
    root = "smartenit_rescue-0.1.0"
    archive = _write_link_before_damaged_body_tar(
        tmp_path / "smartenit_rescue-0.1.0.tar.gz", root
    )

    assert verify_archive(archive) == (
        Finding(path=f"{root}/linked.txt", line=None, rule="link-member"),
    )


def test_verify_archive_reports_invalid_utf8_text(tmp_path: Path) -> None:
    members = _safe_sdist_members() | {"smartenit_rescue-0.1.0/notes.txt": b"\xff\xfe"}
    archive = _write_tar(tmp_path / "smartenit_rescue-0.1.0.tar.gz", members)

    assert verify_archive(archive) == (
        Finding(
            path="smartenit_rescue-0.1.0/notes.txt",
            line=None,
            rule="decode-error",
        ),
    )


@pytest.mark.parametrize(
    ("filename", "content", "rule"),
    [
        ("release.bin", b"PK\x05\x06" + b"\0" * 18, "unsupported-archive"),
        ("release.zip", b"not an archive", "archive-format"),
        ("release.tar.gz", b"PK\x05\x06" + b"\0" * 18, "archive-format"),
        ("release.tar", gzip.compress(b"\0" * 1024), "archive-format"),
    ],
)
def test_verify_archive_reports_bounded_archive_errors(
    tmp_path: Path, filename: str, content: bytes, rule: str
) -> None:
    archive = tmp_path / filename
    archive.write_bytes(content)

    assert verify_archive(archive) == (Finding(path="<archive>", line=None, rule=rule),)


def test_main_verifies_all_archives_sorts_output_and_hides_payload(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    private_value = _joined("pass", "word = 'must-not-print'")
    unsafe_name = "smartenit_rescue-0.1.0/z-notes.txt"
    unsafe_members = _safe_sdist_members() | {unsafe_name: private_value.encode()}
    unsafe = _write_tar(tmp_path / "z-release.tar.gz", unsafe_members)
    safe = _write_zip(tmp_path / "a-release.whl", _safe_wheel_members())

    assert main([str(unsafe), str(safe)]) == 1
    output = capsys.readouterr()
    assert output.out.splitlines() == [f"{unsafe}:{unsafe_name}:1:secret-assignment"]
    assert "must-not-print" not in output.out
    assert output.err == ""


def test_main_returns_two_for_invalid_or_unreadable_inputs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "missing.whl"
    corrupt = tmp_path / "corrupt.zip"
    corrupt.write_bytes(b"not an archive")

    assert main([]) == 2
    assert main([str(missing)]) == 2
    assert main([str(corrupt)]) == 2
    output = capsys.readouterr()
    assert "not an archive" not in output.out
    assert "not an archive" not in output.err


def test_main_bounds_damaged_deflate_and_verifies_later_archives(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    damaged = _write_zip(
        tmp_path / "a-damaged.whl",
        _safe_wheel_members(),
    )
    _damage_deflate_body(damaged, "smartenit_rescue/__init__.py")

    private_value = _joined("pass", "word = 'must-not-print'")
    later_member = "smartenit_rescue/later.py"
    later_members = _safe_wheel_members() | {later_member: private_value.encode()}
    later = _write_zip(tmp_path / "z-later.whl", later_members)

    assert main([str(damaged), str(later)]) == 2
    output = capsys.readouterr()
    assert output.out.splitlines() == [
        f"{damaged}:<archive>:0:archive-format",
        f"{later}:{later_member}:1:secret-assignment",
    ]
    assert "must-not-print" not in output.out
    assert output.err == ""


def test_main_bounds_recursive_metadata_and_verifies_later_archives(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    metadata_path = "smartenit_rescue-0.1.0/pyproject.toml"
    nesting_depth = sys.getrecursionlimit() + 10
    nested_value = "[" * nesting_depth + "0" + "]" * nesting_depth
    recursive_members = _safe_sdist_members()
    recursive_members[metadata_path] = f"value = {nested_value}\n".encode()
    recursive = _write_zip(tmp_path / "a-recursive.zip", recursive_members)

    private_value = _joined("pass", "word = 'must-not-print'")
    later_member = "smartenit_rescue/later.py"
    later_members = _safe_wheel_members() | {later_member: private_value.encode()}
    later = _write_zip(tmp_path / "z-later.whl", later_members)

    assert main([str(recursive), str(later)]) == 2
    output = capsys.readouterr()
    assert output.out.splitlines() == [
        f"{recursive}:{metadata_path}:0:metadata-error",
        f"{later}:{later_member}:1:secret-assignment",
    ]
    assert nested_value not in output.out
    assert "must-not-print" not in output.out
    assert output.err == ""


def test_verifier_runs_as_a_direct_script(tmp_path: Path) -> None:
    archive = _write_zip(
        tmp_path / "smartenit_rescue-0.1.0-py3-none-any.whl",
        _safe_wheel_members(),
    )
    root = Path(__file__).resolve().parents[1]

    result = subprocess.run(
        [sys.executable, "scripts/verify_release_tree.py", str(archive)],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_release_verifier_accepts_sanitized_harmony_guide(tmp_path: Path) -> None:
    repository = Path(__file__).resolve().parents[1]
    guide = (repository / "docs/harmony-g2.md").read_bytes()
    root = "smartenit_rescue-0.1.0"
    members = _safe_sdist_members()
    members[f"{root}/docs/harmony-g2.md"] = guide
    archive = _write_tar(tmp_path / "smartenit_rescue-0.1.0.tar.gz", members)

    assert verify_archive(archive) == ()


def test_sdist_excludes_ignored_scratch_files(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    output = tmp_path / "artifacts"
    scratch_file = root / ".scratch" / "__release_gate_synthetic_fixture__.txt"
    scratch_file.parent.mkdir(exist_ok=True)
    scratch_file.write_text("synthetic fixture\n", encoding="utf-8")
    try:
        result = subprocess.run(
            ["uv", "build", "--sdist", "--out-dir", str(output)],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
        )
    finally:
        scratch_file.unlink()

    assert result.returncode == 0, result.stderr
    [sdist] = output.glob("*.tar.gz")
    with tarfile.open(sdist, "r:gz") as archive:
        member_names = [member.name for member in archive.getmembers()]
    assert not any("/.scratch/" in name for name in member_names)
