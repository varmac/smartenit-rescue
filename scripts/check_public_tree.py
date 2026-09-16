from __future__ import annotations

import argparse
import ipaddress
import os
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


@dataclass(frozen=True, slots=True)
class Finding:
    path: str
    line: int | None
    rule: str


def scan_text(path: str, text: str) -> tuple[Finding, ...]:
    findings: list[Finding] = []
    findings.extend(_scan_personal_paths(path, text))
    findings.extend(_scan_private_ips(path, text))
    findings.extend(_scan_secret_assignments(path, text))
    findings.extend(_scan_dependencies(path, text))
    findings.extend(_scan_git_urls(path, text))
    findings.extend(_scan_device_identifiers(path, text))
    return _sort_findings(findings)


def scan_tree(root: Path) -> tuple[Finding, ...]:
    if root.is_symlink():
        return (Finding(path=str(root), line=None, rule="symlink"),)

    if not root.exists():
        return _sort_findings([Finding(path=str(root), line=None, rule="missing-root")])

    if not root.is_dir():
        return _sort_findings(
            [Finding(path=str(root), line=None, rule="not-a-directory")]
        )

    findings: list[Finding] = []

    def on_walk_error(error: OSError) -> None:
        error_path = error.filename
        failed_path = Path(error_path) if error_path is not None else root
        relative_error = _relative_path(root, failed_path)
        findings.append(Finding(path=relative_error, line=None, rule="io-error"))

    for dirpath, dirnames, filenames in os.walk(
        root, topdown=True, followlinks=False, onerror=on_walk_error
    ):
        current = Path(dirpath)
        kept_dirnames: list[str] = []

        for dirname in sorted(set(dirnames)):
            child = current / dirname
            relative = _relative_path(root, child)
            if child.is_symlink():
                findings.append(Finding(path=relative, line=None, rule="symlink"))
                continue
            if _is_skipped_dirname(relative, child):
                continue
            kept_dirnames.append(dirname)
        dirnames[:] = kept_dirnames

        for filename in sorted(set(filenames)):
            file_path = current / filename
            relative = _relative_path(root, file_path)
            if _is_skipped_filename(filename):
                continue
            if file_path.is_symlink():
                findings.append(Finding(path=relative, line=None, rule="symlink"))
                continue
            if _is_disallowed_file_name(filename):
                findings.append(
                    Finding(path=relative, line=None, rule="disallowed-path")
                )

            artifact_rule = _private_artifact_rule(relative)
            if artifact_rule is not None:
                findings.append(Finding(path=relative, line=None, rule=artifact_rule))

            if not file_path.is_file():
                continue
            if artifact_rule is not None:
                continue
            if _is_binary_extension(file_path):
                continue
            try:
                text = file_path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                findings.append(Finding(path=relative, line=None, rule="decode-error"))
                continue
            except OSError:
                findings.append(Finding(path=relative, line=None, rule="io-error"))
                continue
            findings.extend(scan_text(relative, text))

    return _sort_findings(findings)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scan repository for private material")
    parser.add_argument("root", nargs="?", default=".")
    args = parser.parse_args(argv)

    findings = scan_tree(Path(args.root))
    for finding in findings:
        line = finding.line if finding.line is not None else 0
        print(f"{finding.path}:{line}:{finding.rule}")
    return 1 if findings else 0


def _scan_personal_paths(path: str, text: str) -> list[Finding]:
    findings: list[Finding] = []
    for idx, line in enumerate(text.splitlines(), start=1):
        if _PERSONAL_PATH_RE.search(line):
            findings.append(Finding(path=path, line=idx, rule="personal-path"))
    return findings


def _scan_private_ips(path: str, text: str) -> list[Finding]:
    findings: list[Finding] = []
    for idx, line in enumerate(text.splitlines(), start=1):
        for match in (*_IP_RE.finditer(line), *_IPV6_RE.finditer(line)):
            candidate = match.group(0)
            try:
                ip = ipaddress.ip_address(candidate)
            except ValueError:
                continue
            if _is_private_lan_ip(ip):
                findings.append(Finding(path=path, line=idx, rule="private-ip"))
    return findings


def _scan_secret_assignments(path: str, text: str) -> list[Finding]:
    findings: list[Finding] = []
    for idx, line in enumerate(text.splitlines(), start=1):
        for match in _ASSIGNMENT_RE.finditer(line):
            field_name = (match.group("key") or match.group("quoted_key") or "").lower()
            if not field_name or not _contains_sensitive_field(field_name):
                continue
            raw_value = match.group("value").strip()
            if _is_python_reference(path, match, raw_value):
                continue
            value = _unquote(raw_value)
            if _is_allowed_value(value):
                continue
            findings.append(Finding(path=path, line=idx, rule="secret-assignment"))
    return findings


def _is_python_reference(path: str, match: re.Match[str], value: str) -> bool:
    return (
        Path(path).suffix.lower() in {".py", ".pyi"}
        and match.group("quoted_key") is None
        and _PYTHON_REFERENCE_RE.fullmatch(value) is not None
    )


def _scan_dependencies(path: str, text: str) -> list[Finding]:
    findings: list[Finding] = []
    for idx, line in enumerate(text.splitlines(), start=1):
        for match in _LOCAL_DEPENDENCY_RE.finditer(line):
            if _is_local_path(match.group("value")):
                findings.append(Finding(path=path, line=idx, rule="local-dependency"))

        editable_match = _EDITABLE_DEPENDENCY_RE.search(line)
        if editable_match is not None and _is_local_path(
            editable_match.group("target")
        ):
            findings.append(Finding(path=path, line=idx, rule="editable-dependency"))

        pep_508_match = _PEP_508_LOCAL_URL_RE.search(line)
        if pep_508_match is not None and _is_local_path(pep_508_match.group("value")):
            findings.append(Finding(path=path, line=idx, rule="local-dependency"))
    return findings


def _scan_git_urls(path: str, text: str) -> list[Finding]:
    findings: list[Finding] = []
    for idx, line in enumerate(text.splitlines(), start=1):
        for match in _PRIVATE_GIT_RE.finditer(line):
            url = match.group(0)
            if _is_private_git_reference(url):
                findings.append(Finding(path=path, line=idx, rule="private-git"))
    return findings


def _scan_device_identifiers(path: str, text: str) -> list[Finding]:
    findings: list[Finding] = []
    for idx, line in enumerate(text.splitlines(), start=1):
        has_private_device_id = any(
            _SYNTHETIC_EUI64_RE.fullmatch(match.group(0)) is None
            for match in _DEVICE_ID_RE.finditer(line)
        )
        if has_private_device_id or (
            _CERT_FINGERPRINT_RE.search(line)
            and _HAS_DEVICE_IDENTIFIER_CONTEXT_RE.search(line)
        ):
            findings.append(Finding(path=path, line=idx, rule="private-device-id"))
    return findings


def _is_skipped_filename(filename: str) -> bool:
    return filename in _SKIP_FILE_NAMES


def _is_disallowed_file_name(name: str) -> bool:
    lowered = name.lower()
    if lowered in _DISALLOWED_FILE_NAMES:
        return True
    if _CREDENTIAL_FILE_RE.fullmatch(lowered):
        return True
    return lowered.startswith(_STATE_PREFIX)


def _private_artifact_rule(relative_path: str) -> str | None:
    file_path = Path(relative_path)
    name = file_path.name.lower()
    text = _path_context(file_path)
    if any(part.lower() == "secrets" for part in file_path.parts) or name in {
        "mqtt_username",
        "mqtt_password",
    }:
        return "private-artifact"

    if name.endswith(_FORBIDDEN_ARCHIVE_SUFFIXES):
        return "private-artifact"

    if name.endswith(_PACKET_CAPTURE_EXTENSIONS):
        return "private-artifact"

    if any(marker in text for marker in _PRIVATE_LOG_MARKERS) and name.endswith(".log"):
        return "private-artifact"

    if any(
        marker in text for marker in _PRIVATE_CAPTURE_MARKERS
    ) and _is_media_extension(file_path):
        return "private-artifact"

    return None


def _path_context(file_path: Path) -> str:
    return str(file_path).lower()


def _is_binary_extension(file_path: Path) -> bool:
    return file_path.suffix.lower() in _BINARY_EXTENSIONS


def _is_media_extension(file_path: Path) -> bool:
    return file_path.suffix.lower() in _MEDIA_EXTENSIONS


def _is_skipped_dirname(relative: str, path: Path) -> bool:
    if relative in {"", "."}:
        return path.name in _SKIP_DIR_NAMES
    return path.name in _SKIP_DIR_NAMES or path.name.endswith(_EGG_SUFFIX)


def _contains_sensitive_field(name: str) -> bool:
    if name in _ALLOWED_METADATA_FIELD_NAMES:
        return False
    return (
        name == _KEY_FIELD
        or name.endswith(f"_{_KEY_FIELD}")
        or any(marker in name for marker in _SENSITIVE_FIELD_MARKERS)
    )


def _is_local_path(value: str) -> bool:
    raw = _unquote(value.strip())
    if _is_allowed_value(raw):
        return False
    if raw.startswith(_FILE_URL_PREFIX):
        return True
    if raw.startswith(("~/", "./", ".\\", "../", "..\\")):
        return True
    if raw.startswith("/") and not _PUBLIC_URL_PREFIX_RE.match(raw):
        return True
    return bool(_WINDOWS_ABSOLUTE_PATH_RE.match(raw))


def _is_allowed_value(value: str) -> bool:
    if _is_allowed_placeholder(value):
        return True
    if _is_synthetic_secret_value(value):
        return True
    if value.startswith(_FILE_URL_PREFIX):
        return False
    return _is_public_url_without_credentials(value)


def _is_public_url_without_credentials(value: str) -> bool:
    if not _ALLOWED_URL_RE.fullmatch(value):
        return False
    try:
        parsed = urlparse(value)
        return (
            parsed.scheme.lower() in {"http", "https"}
            and parsed.hostname is not None
            and parsed.username is None
            and parsed.password is None
        )
    except ValueError:
        return False


def _is_allowed_placeholder(value: str) -> bool:
    return bool(_ALLOWED_PLACEHOLDER_RE.fullmatch(value))


def _is_synthetic_secret_value(value: str) -> bool:
    return bool(_SYNTHETIC_VALUE_RE.fullmatch(value))


def _is_private_git_reference(url: str) -> bool:
    lowered = url.lower()
    if _GIT_PRIVATE_HINT_RE.search(lowered):
        return True
    if _SCP_GIT_RE.fullmatch(lowered):
        host = _extract_git_host(lowered)
        if _is_public_git_host(host):
            return _GIT_PRIVATE_HINT_RE.search(lowered) is not None
        return True

    try:
        parsed = urlparse(lowered)
    except ValueError:
        return False
    host = (parsed.netloc or "").lower()
    if host:
        if _is_public_git_host(host):
            return _GIT_PRIVATE_HINT_RE.search(lowered) is not None
        return True

    return False


def _extract_git_host(value: str) -> str:
    if _SCP_GIT_RE.fullmatch(value):
        return value.split(":", 1)[0].split("@", 1)[1]
    try:
        parsed = urlparse(value)
    except ValueError:
        return ""
    return parsed.hostname or ""


def _is_public_git_host(host: str) -> bool:
    lowered = (host or "").lower()
    return any(
        candidate == lowered or lowered.endswith(f".{candidate}")
        for candidate in _PUBLIC_GIT_HOSTS
    )


def _sort_findings(findings: list[Finding]) -> tuple[Finding, ...]:
    unique = dict.fromkeys(
        (finding.path, finding.line, finding.rule) for finding in findings
    )
    normalized = [
        Finding(path=path, line=line, rule=rule) for path, line, rule in unique
    ]
    decorated = (
        (finding.path, finding.line or 0, finding.rule, finding)
        for finding in normalized
    )
    return tuple(item[3] for item in sorted(decorated))


def _relative_path(root: Path, child: Path) -> str:
    try:
        return child.relative_to(root).as_posix()
    except ValueError:
        return str(child)


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _decode_bytes(values: tuple[int, ...]) -> str:
    return bytes(values).decode("ascii")


def _is_private_lan_ip(ip: ipaddress._BaseAddress) -> bool:
    if ip.version == 6:
        return ip in ipaddress.ip_network(
            "fc" + "00::/7"
        ) or ip in ipaddress.ip_network("fe" + "80::/10")
    return (
        ip in ipaddress.ip_network("10." + "0.0.0/8")
        or ip in ipaddress.ip_network("172." + "16.0.0/12")
        or ip in ipaddress.ip_network("192." + "168.0.0/16")
    )


_USER = _decode_bytes((85, 115, 101, 114, 115))
_HOME = _decode_bytes((104, 111, 109, 101))
_PRIVATE_VAR_FOLDERS = "/private/" + "var/folders/"
_FILE_URL_PREFIX = "file" + "://"

_PERSONAL_PATH_RE = re.compile(
    rf"(?i)(?<![A-Za-z0-9._/-])(?:"
    rf"/{_USER}/[^\s/\\]+(?:/[^\s/\\]+)*/?"
    rf"|/{_HOME}/[^\s/\\]+(?:/[^\s/\\]+)*/?"
    rf"|{re.escape(_PRIVATE_VAR_FOLDERS)}[^\s/\\]+(?:/[^\s/\\]+)*/?"
    rf"|[A-Za-z]:\\{_USER}\\[^\s/\\]+(?:\\[^\s/\\]+)*(?:\\)?"
    rf"|[A-Za-z]:/{_USER}/[^\s/\\]+(?:/[^\s/\\]+)*(?:/)?"
    rf")(?=$|[\s/\]\}}\)>'\"`!?,.;:])"
)
_IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_IPV6_RE = re.compile(r"(?<![A-Za-z0-9:.])[0-9A-Fa-f:]{3,}(?![A-Za-z0-9:.])")

_ASSIGNMENT_RE = re.compile(
    r"(?i)(?:^|[\s,({])(?:export\s+)?(?:(?P<key>[A-Za-z_][A-Za-z0-9_\-]*)|"
    r"['\"](?P<quoted_key>[A-Za-z_][A-Za-z0-9_\-]*)['\"])(?:\s*(?:=|:)\s*"
    r"(?P<value>'[^']*'|\"[^\"]*\"|[^\s#,})\]]+))"
)
_PYTHON_REFERENCE_RE = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*"
)

_LOCAL_DEPENDENCY_RE = re.compile(
    r"(?i)\b(?:path|url)\s*=\s*(?P<value>'[^']*'|\"[^\"]*\"|[^\\s,}]+)"
)
_EDITABLE_DEPENDENCY_RE = re.compile(
    r"(?i)^\s*(?:-e|--editable)\s+(?P<target>'[^']*'|\"[^\"]*\"|[^\s#]+)"
)
_PEP_508_LOCAL_URL_RE = re.compile(
    rf"(?i)\S+\s*@\s*(?P<value>{re.escape(_FILE_URL_PREFIX)}\S+|"
    r"[A-Za-z]:[\\/]\S+|/\S+|\.\./\S+|\.\\\S+|\.\/\S+)"
)

_PRIVATE_GIT_RE = re.compile(
    r"(?i)(?:"
    r"git\+(?:ssh|https?|http)://[^\s]+|(?:ssh|git)://[^\s]+|"
    r"https://[^\s]+\.git(?:/[^\s]*)?|http://[^\s]+\.git(?:/[^\s]*)?|"
    r"[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:[^\s]+"
    r")"
)
_SCP_GIT_RE = re.compile(r"(?i)[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:[^\s]+")
_PUBLIC_GIT_HOSTS = {
    "github.com",
    "gitlab.com",
    "bitbucket.org",
    "sourcehut.org",
}
_GIT_PRIVATE_HINT_RE = re.compile(r"(?i)\b(private|internal|corp|secret|staging)\b")

_DEVICE_ID_RE = re.compile(
    r"\b(?:"
    r"(?:0x)?[0-9a-f]{16}"
    r"|(?<![0-9a-f]:)(?:[0-9a-f]{2}:){7}[0-9a-f]{2}(?!:[0-9a-f]{2})"
    r"|(?<![0-9a-f]-)(?:[0-9a-f]{2}-){7}[0-9a-f]{2}(?!-[0-9a-f]{2})"
    r"|(?<![0-9a-f]:)(?:[0-9a-f]{2}:){5}[0-9a-f]{2}(?!:[0-9a-f]{2})"
    r")\b",
    re.IGNORECASE,
)
_SYNTHETIC_EUI64_RE = re.compile(
    r"\b(?:"
    r"(?:0x)?02000000000000[0-9a-f]{2}"
    r"|02(?::00){6}:[0-9a-f]{2}"
    r"|02(?:-00){6}-[0-9a-f]{2}"
    r")\b",
    re.IGNORECASE,
)
_CERT_FINGERPRINT_RE = re.compile(r"\b[0-9a-f]{40,}\b", re.IGNORECASE)
_HAS_DEVICE_IDENTIFIER_CONTEXT_RE = re.compile(
    r"(?i)\b(?:device[-_ ]?id|fingerprint|certificate|mac|eui|serial)\b"
)

_WINDOWS_ABSOLUTE_PATH_RE = re.compile(r"(?i)^[a-z]:[\\/]")
_PUBLIC_URL_PREFIX_RE = re.compile(r"(?i)^(?:(?:https?|ssh)://|git\+https?://)")
_ALLOWED_URL_RE = re.compile(r"(?i)^[a-z][a-z0-9+.-]*://[^\s]+$")

_ALLOWED_PLACEHOLDER_RE = re.compile(r"<[^>]+>")
_SYNTHETIC_VALUE_RE = re.compile(r"(?i)^<[^>]+>$")

_STATE_PREFIX = _decode_bytes((46, 115, 116, 97, 116, 101))
_KEY_FIELD = _decode_bytes((107, 101, 121))
_ALLOWED_METADATA_FIELD_NAMES = {"token_type"}
_SENSITIVE_FIELD_MARKERS = [
    _decode_bytes((112, 97, 115, 115, 119, 111, 114, 100)),
    _decode_bytes((115, 101, 99, 114, 101, 116)),
    _decode_bytes((116, 111, 107, 101, 110)),
    _decode_bytes((97, 112, 105, 107, 101, 121)),
    _decode_bytes((97, 112, 105, 95, 107, 101, 121)),
    _decode_bytes((97, 99, 99, 101, 115, 115, 95, 107, 101, 121)),
    _decode_bytes((97, 117, 116, 104)),
    _decode_bytes((115, 105, 100, 101)),
    _decode_bytes((115, 115, 104, 95, 107, 101, 121)),
    _decode_bytes((112, 114, 105, 118, 97, 116, 101, 95, 107, 101, 121)),
    _decode_bytes((99, 108, 105, 101, 110, 116, 95, 107, 101, 121)),
]

_BINARY_EXTENSIONS = (".pcap", ".pcapng", ".png", ".jpg", ".jpeg", ".webm", ".gif")
_MEDIA_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webm", ".gif")
_PACKET_CAPTURE_EXTENSIONS = (".pcap", ".pcapng")
_FORBIDDEN_ARCHIVE_SUFFIXES = (
    ".whl",
    ".zip",
    ".tar",
    ".tar.gz",
    ".tgz",
    ".tbz2",
    ".bz2",
)
_PRIVATE_LOG_MARKERS = (
    "screen" + "shot",
    "screen" + "shots",
    "cap" + "ture",
    "cap" + "tures",
    "pri" + "vate",
    "ven" + "dor",
)
_PRIVATE_CAPTURE_MARKERS = _PRIVATE_LOG_MARKERS

_SKIP_DIR_NAMES = {
    ".git",
    ".scratch",
    ".venv",
    "dist",
    "build",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "__pycache__",
}
_EGG_SUFFIX = ".egg-info"

_DISALLOWED_FILE_NAMES = {
    ".env",
    ".env.local",
    ".env.production",
    ".envrc",
    ".state",
    "gateway.json",
    "credentials",
    "credentials.json",
    "credential",
    "credential.json",
    "secrets",
    "secrets.json",
    "secret.json",
    ".netrc",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa.pub",
    ".aws",
    ".npmrc",
    ".pypirc",
    "service-account.json",
}
_CREDENTIAL_FILE_RE = re.compile(
    r"(?i)(?:credentials?|secrets?|auth(?:entication)?)(?:[-_.](?:config|data|"
    r"store|local|prod(?:uction)?))?\.(?:json|ya?ml|toml|ini|conf|config|env)"
)
_SKIP_FILE_NAMES = {
    ".git",
}


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
