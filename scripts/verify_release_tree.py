from __future__ import annotations

import argparse
import bz2
import io
import lzma
import re
import stat
import sys
import tarfile
import tomllib
import zipfile
import zlib
from collections.abc import Callable, Iterator, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import IO, Literal, Protocol

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.check_public_tree import Finding, scan_text

_MAX_MEMBER_BYTES = 2 * 1024 * 1024
_MAX_TOTAL_BYTES = 32 * 1024 * 1024
_ARCHIVE_PATH = "<archive>"
_PACKAGE_ROOT = "smartenit_rescue"
_FILE_URL_PREFIX = "file" + "://"

_ZIP_SUFFIXES = (".whl", ".zip")
_TAR_SUFFIXES = (".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz", ".tar")
_ZIP_SIGNATURES = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
_TAR_SIGNATURES: dict[str, tuple[bytes, ...]] = {
    ".tar.gz": (b"\x1f\x8b",),
    ".tgz": (b"\x1f\x8b",),
    ".tar.bz2": (b"BZh",),
    ".tbz2": (b"BZh",),
    ".tar.xz": (b"\xfd7zXZ\x00",),
    ".txz": (b"\xfd7zXZ\x00",),
}

_TEXT_SUFFIXES = {
    ".cfg",
    ".css",
    ".csv",
    ".html",
    ".ini",
    ".json",
    ".lock",
    ".md",
    ".py",
    ".pyi",
    ".rst",
    ".sh",
    ".toml",
    ".tsv",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
_TEXT_NAMES = {
    "METADATA",
    "PKG-INFO",
    "RECORD",
    "WHEEL",
    "entry_points.txt",
    "top_level.txt",
}
_DISALLOWED_NAMES = {
    ".env",
    ".env.local",
    ".env.production",
    ".envrc",
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".state",
    ".aws",
    "credential",
    "credential.json",
    "credentials",
    "credentials.json",
    "gateway.json",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
    "id_rsa.pub",
    "mqtt_password",
    "mqtt_username",
    "secret.json",
    "secrets",
    "secrets.json",
    "service-account.json",
}
_CREDENTIAL_NAME_RE = re.compile(
    r"(?i)(?:credentials?|secrets?|auth(?:entication)?)(?:[-_.](?:config|data|"
    r"store|local|prod(?:uction)?))?\.(?:json|ya?ml|toml|ini|conf|config|env)"
)
_WINDOWS_ABSOLUTE_RE = re.compile(r"(?i)^[a-z]:/")
_DIST_INFO_RE = re.compile(r"^smartenit_rescue-[A-Za-z0-9_.+!]+\.dist-info$")
_EDITABLE_MARKER_RE = re.compile(r"(?i)(?:^|\s)(?:-e|--editable)(?=\s|=)")
_FORBIDDEN_FILE_SUFFIXES = (
    ".bz2",
    ".pcap",
    ".pcapng",
    ".tar",
    ".tar.gz",
    ".tar.xz",
    ".tbz2",
    ".tgz",
    ".txz",
    ".whl",
    ".zip",
)
_FORBIDDEN_DIRECTORY_NAMES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".scratch",
    ".venv",
    ".worktrees",
    "__pycache__",
    "build",
    "dist",
}
_FORBIDDEN_DIRECTORY_SUFFIXES = (".egg-info",)

_ArchiveKind = Literal["wheel", "sdist"]
_MemberKind = Literal["file", "directory"]


@dataclass(frozen=True, slots=True)
class _Member:
    path: str
    size: int
    kind: _MemberKind
    source: zipfile.ZipInfo | tarfile.TarInfo


class _GzipReader(io.RawIOBase):
    def __init__(self, stream: IO[bytes]) -> None:
        self._stream = stream
        self._decoder = zlib.decompressobj(wbits=31)

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            raise ValueError("a bounded read size is required")

        output = bytearray()
        while len(output) < size:
            if self._decoder.eof:
                data = self._decoder.unused_data or self._stream.read(1)
                if not data:
                    break
                self._decoder = zlib.decompressobj(wbits=31)
            elif self._decoder.unconsumed_tail:
                data = self._decoder.unconsumed_tail
            else:
                data = self._stream.read(1)
                if not data:
                    break
            output.extend(self._decoder.decompress(data, size - len(output)))
        return bytes(output)


class _NeedsInputDecompressor(Protocol):
    @property
    def eof(self) -> bool: ...

    @property
    def needs_input(self) -> bool: ...

    @property
    def unused_data(self) -> bytes: ...

    def decompress(self, data: bytes, max_length: int = -1) -> bytes: ...


class _NeedsInputReader(io.RawIOBase):
    def __init__(
        self,
        stream: IO[bytes],
        factory: Callable[[], _NeedsInputDecompressor],
    ) -> None:
        self._stream = stream
        self._factory = factory
        self._decoder = factory()

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            raise ValueError("a bounded read size is required")

        output = bytearray()
        while len(output) < size:
            if self._decoder.eof:
                data = self._decoder.unused_data or self._stream.read(1)
                if not data:
                    break
                self._decoder = self._factory()
            elif self._decoder.needs_input:
                data = self._stream.read(1)
                if not data:
                    break
            else:
                data = b""
            output.extend(self._decoder.decompress(data, size - len(output)))
        return bytes(output)


def _new_bz2_decompressor() -> _NeedsInputDecompressor:
    return bz2.BZ2Decompressor()


def _new_lzma_decompressor() -> _NeedsInputDecompressor:
    return lzma.LZMADecompressor()


def verify_archive(path: Path) -> tuple[Finding, ...]:
    """Inspect one wheel, zip, or tar archive without extracting it."""
    archive_type, error = _archive_type(path)
    if error is not None:
        return (Finding(path=_ARCHIVE_PATH, line=None, rule=error),)
    assert archive_type is not None

    if archive_type == "zip":
        return _verify_zip(
            path, "wheel" if path.name.lower().endswith(".whl") else "sdist"
        )
    return _verify_tar(path)


def main(argv: Sequence[str] | None = None) -> int:
    """Verify one or more archives and return a process status."""
    parser = argparse.ArgumentParser(description="Verify public release archives")
    parser.add_argument("archives", nargs="*")
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 2
    if not args.archives:
        parser.print_usage(sys.stderr)
        return 2

    results: list[tuple[str, Finding]] = []
    status = 0
    for raw_path in args.archives:
        archive_path = Path(raw_path)
        findings = verify_archive(archive_path)
        for finding in findings:
            results.append((str(archive_path), finding))
        if any(finding.rule in _OPERATIONAL_RULES for finding in findings):
            status = 2
        elif findings and status == 0:
            status = 1

    decorated_results = (
        (
            archive_path,
            finding.path,
            finding.line or 0,
            finding.rule,
            finding,
        )
        for archive_path, finding in results
    )
    for output_archive_path, _, _, _, finding in sorted(decorated_results):
        line = finding.line if finding.line is not None else 0
        print(f"{output_archive_path}:{finding.path}:{line}:{finding.rule}")
    return status


def _archive_type(path: Path) -> tuple[Literal["zip", "tar"] | None, str | None]:
    suffix = _matching_suffix(path.name.lower())
    if suffix is None:
        return None, "unsupported-archive"
    try:
        with path.open("rb") as stream:
            signature = stream.read(6)
    except OSError:
        return None, "io-error"

    if suffix in _ZIP_SUFFIXES:
        if not signature.startswith(_ZIP_SIGNATURES):
            return None, "archive-format"
        return "zip", None
    expected_signatures = _TAR_SIGNATURES.get(suffix)
    if expected_signatures is not None and not signature.startswith(
        expected_signatures
    ):
        return None, "archive-format"
    if suffix == ".tar" and signature.startswith(_ZIP_SIGNATURES):
        return None, "archive-format"
    return "tar", None


def _matching_suffix(name: str) -> str | None:
    for suffix in (*_ZIP_SUFFIXES, *_TAR_SUFFIXES):
        if name.endswith(suffix):
            return suffix
    return None


def _verify_zip(path: Path, archive_kind: _ArchiveKind) -> tuple[Finding, ...]:
    try:
        with zipfile.ZipFile(path) as archive:
            members, findings = _zip_members(archive.infolist())
            return _inspect_members(
                archive_kind,
                members,
                findings,
                lambda member: archive.open(_zip_source(member), "r"),
            )
    except (
        EOFError,
        lzma.LZMAError,
        NotImplementedError,
        OSError,
        RuntimeError,
        ValueError,
        zipfile.BadZipFile,
        zlib.error,
    ):
        return (Finding(path=_ARCHIVE_PATH, line=None, rule="archive-format"),)


def _verify_tar(path: Path) -> tuple[Finding, ...]:
    try:
        members, findings = _preflight_tar(path)
        if findings:
            return _sort_findings(findings)
        topology_findings = _topology_findings("sdist", members)
        if topology_findings:
            return _sort_findings(topology_findings)
        return _scan_tar(path, members)
    except (EOFError, lzma.LZMAError, OSError, tarfile.TarError, zlib.error):
        return (Finding(path=_ARCHIVE_PATH, line=None, rule="archive-format"),)


def _zip_members(
    infos: Sequence[zipfile.ZipInfo],
) -> tuple[list[_Member], list[Finding]]:
    members: list[_Member] = []
    findings: list[Finding] = []
    seen: set[str] = set()
    for info in infos:
        normalized, path_finding = _normalize_path(info.orig_filename)
        if path_finding is not None:
            findings.append(path_finding)
            continue
        assert normalized is not None

        mode = (info.external_attr >> 16) & 0xFFFF
        file_type = stat.S_IFMT(mode)
        if file_type == stat.S_IFLNK:
            findings.append(Finding(normalized, None, "link-member"))
            continue
        if info.is_dir() or file_type == stat.S_IFDIR:
            kind: _MemberKind = "directory"
        elif file_type in {0, stat.S_IFREG}:
            kind = "file"
        else:
            findings.append(Finding(normalized, None, "special-member"))
            continue
        _append_member(info, normalized, info.file_size, kind, seen, members, findings)
    return members, findings


def _preflight_tar(path: Path) -> tuple[list[_Member], list[Finding]]:
    members: list[_Member] = []
    findings: list[Finding] = []
    seen: set[str] = set()
    total_file_bytes = 0
    with _open_tar_stream(path) as archive:
        for info in archive:
            normalized, path_finding = _normalize_path(info.name)
            if path_finding is not None:
                findings.append(path_finding)
                return members, findings
            assert normalized is not None

            if info.issym() or info.islnk():
                findings.append(Finding(normalized, None, "link-member"))
                continue
            if info.isdir():
                kind: _MemberKind = "directory"
            elif info.isreg():
                kind = "file"
            else:
                findings.append(Finding(normalized, None, "special-member"))
                continue

            if findings and kind == "file":
                return members, findings
            previous_finding_count = len(findings)
            _append_member(info, normalized, info.size, kind, seen, members, findings)
            if kind != "file":
                continue
            if info.size < 0 or info.size > _MAX_MEMBER_BYTES:
                findings.append(Finding(normalized, None, "member-size-limit"))
            total_file_bytes += info.size
            if total_file_bytes > _MAX_TOTAL_BYTES:
                findings.append(Finding(_ARCHIVE_PATH, None, "aggregate-size-limit"))
            if len(findings) > previous_finding_count:
                return members, findings
    return members, findings


def _scan_tar(path: Path, members: Sequence[_Member]) -> tuple[Finding, ...]:
    text_members = {
        member.path: member
        for member in members
        if member.kind == "file" and _is_text_member(member.path)
    }
    findings: list[Finding] = []
    with _open_tar_stream(path) as archive:
        for info in archive:
            normalized, path_finding = _normalize_path(info.name)
            if path_finding is not None:
                return (Finding(_ARCHIVE_PATH, None, "archive-format"),)
            assert normalized is not None
            member = text_members.get(normalized)
            if member is None:
                continue
            with _open_tar_member(archive, info) as stream:
                content = stream.read(member.size)
            if len(content) != member.size:
                return (Finding(_ARCHIVE_PATH, None, "archive-format"),)
            _scan_member_content("sdist", member.path, content, findings)
    return _sort_findings(findings)


@contextmanager
def _open_tar_stream(path: Path) -> Iterator[tarfile.TarFile]:
    suffix = _matching_suffix(path.name.lower())
    assert suffix is not None
    with ExitStack() as stack:
        raw_stream = stack.enter_context(path.open("rb"))
        stream: IO[bytes] | _GzipReader | _NeedsInputReader
        if suffix in {".tar.gz", ".tgz"}:
            stream = _GzipReader(raw_stream)
        elif suffix in {".tar.bz2", ".tbz2"}:
            stream = _NeedsInputReader(raw_stream, _new_bz2_decompressor)
        elif suffix in {".tar.xz", ".txz"}:
            stream = _NeedsInputReader(raw_stream, _new_lzma_decompressor)
        else:
            stream = raw_stream
        archive = stack.enter_context(
            tarfile.open(fileobj=stream, mode="r|", bufsize=tarfile.BLOCKSIZE)
        )
        yield archive


def _append_member(
    source: zipfile.ZipInfo | tarfile.TarInfo,
    path: str,
    size: int,
    kind: _MemberKind,
    seen: set[str],
    members: list[_Member],
    findings: list[Finding],
) -> None:
    if path in seen:
        findings.append(Finding(path, None, "duplicate-member"))
        return
    seen.add(path)
    if _is_disallowed_path(path, kind):
        findings.append(Finding(path, None, "disallowed-path"))
    members.append(_Member(path=path, size=size, kind=kind, source=source))


def _normalize_path(raw_path: str) -> tuple[str | None, Finding | None]:
    posix_path = raw_path.replace("\\", "/")
    display_path = posix_path.rstrip("/") or _ARCHIVE_PATH
    if (
        not posix_path
        or "\x00" in posix_path
        or posix_path.startswith("/")
        or _WINDOWS_ABSOLUTE_RE.match(posix_path)
    ):
        return None, Finding(display_path, None, "unsafe-path")

    candidate = PurePosixPath(posix_path)
    if not candidate.parts or candidate.as_posix() == "." or ".." in candidate.parts:
        return None, Finding(display_path, None, "unsafe-path")
    return candidate.as_posix(), None


def _is_disallowed_path(path: str, kind: _MemberKind) -> bool:
    lowered = path.lower()
    parts = PurePosixPath(lowered).parts
    name = parts[-1]
    if any(part in _DISALLOWED_NAMES or part.startswith(".state") for part in parts):
        return True
    if any(_CREDENTIAL_NAME_RE.fullmatch(part) for part in parts):
        return True
    directory_parts = parts if kind == "directory" else parts[:-1]
    if any(
        part in _FORBIDDEN_DIRECTORY_NAMES
        or part.endswith(_FORBIDDEN_DIRECTORY_SUFFIXES)
        for part in directory_parts
    ):
        return True
    if name.endswith(_FORBIDDEN_FILE_SUFFIXES):
        return True
    if name.endswith(".log") and any(
        marker in lowered
        for marker in (
            "capture",
            "captures",
            "private",
            "screenshot",
            "screenshots",
            "vendor",
        )
    ):
        return True
    return name.endswith((".gif", ".jpeg", ".jpg", ".png", ".webm")) and any(
        marker in lowered
        for marker in (
            "capture",
            "captures",
            "private",
            "screenshot",
            "screenshots",
            "vendor",
        )
    )


def _inspect_members(
    archive_kind: _ArchiveKind,
    members: Sequence[_Member],
    structural_findings: list[Finding],
    opener: Callable[[_Member], IO[bytes]],
) -> tuple[Finding, ...]:
    findings = list(structural_findings)
    if findings:
        return _sort_findings(findings)
    findings.extend(_topology_findings(archive_kind, members))
    if findings:
        return _sort_findings(findings)

    file_members = [member for member in members if member.kind == "file"]
    text_members = [member for member in file_members if _is_text_member(member.path)]
    for member in file_members:
        if member.size < 0 or member.size > _MAX_MEMBER_BYTES:
            findings.append(Finding(member.path, None, "member-size-limit"))
    if sum(member.size for member in text_members) > _MAX_TOTAL_BYTES:
        findings.append(Finding(_ARCHIVE_PATH, None, "aggregate-size-limit"))
    if findings:
        return _sort_findings(findings)

    for member in text_members:
        try:
            with opener(member) as stream:
                content = stream.read(member.size)
        except (
            EOFError,
            KeyError,
            NotImplementedError,
            OSError,
            RuntimeError,
            ValueError,
            zipfile.BadZipFile,
        ):
            return (Finding(path=_ARCHIVE_PATH, line=None, rule="archive-format"),)
        if len(content) != member.size:
            return (Finding(path=_ARCHIVE_PATH, line=None, rule="archive-format"),)
        _scan_member_content(archive_kind, member.path, content, findings)
    return _sort_findings(findings)


def _scan_member_content(
    archive_kind: _ArchiveKind,
    path: str,
    content: bytes,
    findings: list[Finding],
) -> None:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        findings.append(Finding(path, None, "decode-error"))
        return
    findings.extend(scan_text(path, text))
    if _is_metadata_member(archive_kind, path):
        findings.extend(_extra_metadata_findings(archive_kind, path, text))


def _topology_findings(
    archive_kind: _ArchiveKind, members: Sequence[_Member]
) -> list[Finding]:
    file_paths = [member.path for member in members if member.kind == "file"]
    file_path_set = set(file_paths)
    file_ancestors = sorted(
        {
            parent.as_posix()
            for member in members
            for parent in PurePosixPath(member.path).parents
            if parent.as_posix() in file_path_set
        }
    )
    if file_ancestors:
        return [Finding(path, None, "file-ancestor") for path in file_ancestors]
    if archive_kind == "wheel":
        metadata_paths = [
            path
            for path in file_paths
            if path.endswith(".dist-info/METADATA")
            and len(PurePosixPath(path).parts) == 2
        ]
        if len(metadata_paths) != 1:
            return [Finding(_ARCHIVE_PATH, None, "missing-metadata")]
        dist_info_root = PurePosixPath(metadata_paths[0]).parts[0]
        if not _DIST_INFO_RE.fullmatch(dist_info_root):
            return [Finding(metadata_paths[0], None, "unexpected-root")]
        allowed_roots = {_PACKAGE_ROOT, dist_info_root}
        return [
            Finding(member.path, None, "unexpected-root")
            for member in members
            if PurePosixPath(member.path).parts[0] not in allowed_roots
        ]

    roots = {PurePosixPath(member.path).parts[0] for member in members}
    metadata_paths = [
        path
        for path in file_paths
        if PurePosixPath(path).name == "pyproject.toml"
        and len(PurePosixPath(path).parts) == 2
    ]
    if not metadata_paths:
        return [Finding(_ARCHIVE_PATH, None, "missing-metadata")]
    canonical_root = PurePosixPath(metadata_paths[0]).parts[0]
    findings = [
        Finding(member.path, None, "unexpected-root")
        for member in members
        if PurePosixPath(member.path).parts[0] != canonical_root
    ]
    if len(roots) != 1 and not findings:
        findings.append(Finding(_ARCHIVE_PATH, None, "unexpected-root"))
    return findings


def _is_text_member(path: str) -> bool:
    member_path = PurePosixPath(path)
    return (
        member_path.name in _TEXT_NAMES or member_path.suffix.lower() in _TEXT_SUFFIXES
    )


def _is_metadata_member(archive_kind: _ArchiveKind, path: str) -> bool:
    if archive_kind == "wheel":
        return path.endswith(".dist-info/METADATA")
    return PurePosixPath(path).name in {"PKG-INFO", "pyproject.toml"}


def _extra_metadata_findings(
    archive_kind: _ArchiveKind, path: str, text: str
) -> list[Finding]:
    existing = set(scan_text(path, text))
    findings: list[Finding] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if _FILE_URL_PREFIX in line.lower() and not any(
            finding.line == line_number and finding.rule == "local-dependency"
            for finding in existing
        ):
            findings.append(Finding(path, line_number, "local-dependency"))
        if _EDITABLE_MARKER_RE.search(line) and not any(
            finding.line == line_number and finding.rule == "editable-dependency"
            for finding in existing
        ):
            findings.append(Finding(path, line_number, "editable-dependency"))
    if archive_kind == "sdist" and PurePosixPath(path).name == "pyproject.toml":
        findings.extend(_scan_pyproject(path, text))
    return findings


def _scan_pyproject(path: str, text: str) -> list[Finding]:
    try:
        metadata = tomllib.loads(text)
        findings: list[Finding] = []
        for config in _iter_configs(metadata):
            if "editable" in config:
                findings.append(
                    Finding(
                        path,
                        _assignment_line(text, "editable"),
                        "editable-dependency",
                    )
                )
            if "path" not in config:
                continue
            dependency_path = config["path"]
            if not isinstance(dependency_path, str):
                findings.append(
                    Finding(path, _assignment_line(text, "path"), "metadata-error")
                )
            elif not _is_normalized_relative_path(dependency_path):
                findings.append(
                    Finding(
                        path,
                        _assignment_line(text, "path", dependency_path),
                        "local-dependency",
                    )
                )
        return findings
    except (RecursionError, tomllib.TOMLDecodeError):
        return [Finding(path, None, "metadata-error")]


def _iter_configs(value: object) -> Iterator[dict[str, object]]:
    if isinstance(value, dict):
        config = {str(field): child for field, child in value.items()}
        yield config
        for child in config.values():
            yield from _iter_configs(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_configs(child)


def _is_normalized_relative_path(value: str) -> bool:
    normalized = value.replace("\\", "/")
    if (
        not normalized
        or "\x00" in normalized
        or normalized != value
        or normalized.startswith("/")
        or _WINDOWS_ABSOLUTE_RE.match(normalized)
    ):
        return False
    return all(segment not in {"", ".", ".."} for segment in normalized.split("/"))


def _assignment_line(text: str, field: str, value: str | None = None) -> int | None:
    field_pattern = re.compile(rf"(?i)\b{re.escape(field)}\s*=")
    fallback: int | None = None
    for line_number, line in enumerate(text.splitlines(), start=1):
        if field_pattern.search(line) is None:
            continue
        if fallback is None:
            fallback = line_number
        if value is None or value in line:
            return line_number
    return fallback


def _zip_source(member: _Member) -> zipfile.ZipInfo:
    assert isinstance(member.source, zipfile.ZipInfo)
    return member.source


def _open_tar_member(archive: tarfile.TarFile, member: tarfile.TarInfo) -> IO[bytes]:
    stream = archive.extractfile(member)
    if stream is None:
        raise KeyError(member.name)
    return stream


def _sort_findings(findings: Sequence[Finding]) -> tuple[Finding, ...]:
    unique = {
        (finding.path, finding.line, finding.rule): finding for finding in findings
    }
    decorated = (
        (path, line or 0, rule, finding)
        for (path, line, rule), finding in unique.items()
    )
    return tuple(item[3] for item in sorted(decorated))


_OPERATIONAL_RULES = {
    "archive-format",
    "io-error",
    "metadata-error",
    "unsupported-archive",
}


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
