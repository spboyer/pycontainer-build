"""Fail-closed prerequisites for isolated system package installation (#31).

Nothing here runs a package manager, maintainer script or repository hook.
Package installation stays unavailable until the isolated execution backend
from #22 exists. These helpers cover the parts that run in the builder:

* checking the effective package-manager configuration printed inside the
  sandbox for settings that weaken signature verification;
* turning offline resolution output into a bounded download list relative to
  a local mirror.

Transport and installation are not implemented. Eventually apt must verify
``InRelease`` with the pinned ``Signed-By`` keyring and check every ``.deb``
against the signed index, and apk must verify the signed ``APKINDEX`` and
package signatures, both offline inside the sandbox.
"""
import posixpath
import re
import urllib.parse
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Set, Tuple

MAX_PACKAGE_BYTES = 512 * 1024 * 1024

APT_SANDBOX_USER = "_apt"
# apt's StringToBool false spellings; any other value counts as enabled.
_APT_FALSE = {"no", "false", "without", "off", "disable"}
# StringToBool first tries strtol(value, base 0), so "00" and "0x0" are false.
_C_INTEGER = re.compile(r"^[+-]?(0[xX][0-9a-fA-F]+|0[0-7]*|[1-9][0-9]*)$")
# apt-secure(8) overrides that accept unsigned, weak or downgraded repositories.
_APT_INSECURE_TRUE_KEYS = (
    "APT::Get::AllowUnauthenticated",
    "Acquire::AllowInsecureRepositories",
    "Acquire::AllowWeakRepositories",
    "Acquire::AllowDowngradeToInsecureRepositories",
)
_APT_FRESHNESS_KEYS = ("Acquire::Check-Valid-Until", "Acquire::Check-Date")
# Settings, including their named children, that replace or loosen the
# signature verifier itself.
_APT_VERIFIER_KEYS = (
    "Acquire::gpgv::Options",
    "APT::Key::gpgvcommand",
    "Dir::Bin::apt-key",
    "Dir::Bin::gpg",
    "Dir::Bin::gpgv",
)
# Only named children are overrides here; the parent is checked as a fixed path.
_APT_VERIFIER_CHILDREN = ("Dir::Bin::methods",)
# Paths whose override would swap the acquire methods, including gpgv.
_APT_FIXED_PATHS = {"dir": "/", "dir::bin": "", "dir::bin::methods": "/usr/lib/apt/methods"}

_APT_DUMP_LINE = re.compile(r'^([^\s"]+) "((?:[^"\\]|\\.)*)";$')
_APT_URI_LINE = re.compile(r"^'([^']+)' (\S+) (\d+) ?(\S*)$")
_APK_INSTALL_LINE = re.compile(
    r"^\((\d+)/(\d+)\) Installing ([a-z0-9][a-z0-9+._-]*) \(([0-9A-Za-z.+_~-]+)\)$"
)
_APK_OK_LINE = re.compile(r"^OK: .+$")


class SystemInstallError(RuntimeError):
    """Raised when package-manager state or transport cannot be trusted."""


@dataclass(frozen=True)
class MirrorFile:
    """A package file the builder must place at ``path`` under the mirror."""

    path: str
    size: Optional[int] = None


def _strip_binary_scope(key: str) -> str:
    if key.lower().startswith("binary::"):
        parts = key.split("::", 2)
        if len(parts) == 3:
            return parts[2]
    return key


def parse_apt_config_dump(text: str) -> Dict[str, List[str]]:
    """Parse ``apt-config dump`` output into key -> values, rejecting noise."""
    values: Dict[str, List[str]] = {}
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        match = _APT_DUMP_LINE.match(line)
        if not match:
            raise SystemInstallError(
                f"Unrecognized apt-config dump line {number}: {line[:120]!r}"
            )
        key = match.group(1)
        if key.endswith("::"):
            key = key[:-2]
        values.setdefault(key, []).append(match.group(2))
    return values


def _apt_false(value: str) -> bool:
    match = _C_INTEGER.match(value.lstrip())
    if match:
        digits = match.group(1)
        if digits[:2].lower() == "0x":
            return int(digits[2:], 16) == 0
        return int(digits, 8 if digits.startswith("0") else 10) == 0
    return value.strip().lower() in _APT_FALSE


def _is_verifier_key(lowered: str) -> bool:
    for key in _APT_VERIFIER_KEYS:
        base = key.lower()
        if lowered == base or lowered.startswith(base + "::"):
            return True
    return any(lowered.startswith(key.lower() + "::") for key in _APT_VERIFIER_CHILDREN)


def check_apt_config(text: str, allow_expired_release: bool = False) -> None:
    """Fail if the sandbox's effective apt config weakens repository trust.

    ``allow_expired_release`` permits disabling ``Valid-Until``/date checks,
    which pinned snapshot repositories need; signatures are still required.
    Base-image hooks such as ``DPkg::Post-Invoke`` are base-image code that
    runs inside the sandbox and are not rejected here.
    """
    values = parse_apt_config_dump(text)
    problems: List[str] = []
    for raw_key, entries in values.items():
        key = _strip_binary_scope(raw_key)
        lowered = key.lower()
        enabled = [v for v in entries if not _apt_false(v)]
        if any(lowered == k.lower() for k in _APT_INSECURE_TRUE_KEYS) and enabled:
            problems.append(f"{raw_key}={enabled[0]!r}")
        elif any(lowered == k.lower() for k in _APT_FRESHNESS_KEYS):
            if not allow_expired_release and len(enabled) != len(entries):
                problems.append(f"{raw_key} is disabled")
        elif _is_verifier_key(lowered):
            if any(v for v in entries):
                problems.append(f"{raw_key} overrides the signature verifier")
        elif lowered in _APT_FIXED_PATHS:
            if any(v != _APT_FIXED_PATHS[lowered] for v in entries):
                problems.append(f"{raw_key}={entries[0]!r} relocates apt's verifier methods")
        elif lowered == "apt::sandbox::user":
            if any(v != APT_SANDBOX_USER for v in entries):
                problems.append(
                    f"{raw_key}={entries[0]!r}; downloads must run as {APT_SANDBOX_USER}"
                )
    if problems:
        raise SystemInstallError(
            "apt configuration weakens repository verification: "
            + "; ".join(problems)
        )


def _mirror_relative(uri_path: str, mirror_root: str) -> str:
    if "%2f" in uri_path.lower() or "%5c" in uri_path.lower() or "%00" in uri_path:
        raise SystemInstallError(f"Encoded separator in mirror path {uri_path!r}.")
    decoded = urllib.parse.unquote(uri_path, errors="strict")
    root = mirror_root.rstrip("/") + "/"
    if "\\" in decoded or "\x00" in decoded or not decoded.startswith(root):
        raise SystemInstallError(f"Path {decoded!r} is outside mirror {mirror_root!r}.")
    relative = decoded[len(root):]
    parts = relative.split("/")
    # ":" would let a drive-qualified part reset a Windows destination path.
    if not relative or ":" in relative or any(p in ("", ".", "..") for p in parts):
        raise SystemInstallError(f"Unsafe mirror path {decoded!r}.")
    if posixpath.normpath(relative) != relative:
        raise SystemInstallError(f"Non-canonical mirror path {decoded!r}.")
    return relative


def _check_mirror_root(mirror_root: str) -> None:
    if not mirror_root.startswith("/") or posixpath.normpath(mirror_root) != mirror_root.rstrip("/"):
        raise ValueError(f"Mirror root must be an absolute canonical POSIX path: {mirror_root!r}")


def parse_apt_print_uris(
    text: str, mirror_root: str, max_bytes: int = MAX_PACKAGE_BYTES
) -> List[MirrorFile]:
    """Parse ``apt-get install --print-uris`` run against a ``file:`` mirror.

    The printed hash is ignored: apt prints MD5 or nothing for some archives.
    apt checks each ``.deb`` against the SHA-256 in the signed index when it
    installs offline, so only paths and sizes are needed for transport.
    """
    _check_mirror_root(mirror_root)
    files: List[MirrorFile] = []
    seen: Set[str] = set()
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        match = _APT_URI_LINE.match(line)
        if not match:
            raise SystemInstallError(
                f"Unrecognized apt --print-uris line {number}: {line[:120]!r}"
            )
        uri, filename, size_text, _hash = match.groups()
        try:
            parsed = urllib.parse.urlsplit(uri)
        except ValueError as error:
            raise SystemInstallError(f"Malformed apt URI {uri!r}.") from error
        if parsed.scheme != "file" or parsed.netloc not in ("", "localhost") or parsed.query or parsed.fragment:
            raise SystemInstallError(f"apt resolved a non-mirror URI {uri!r}.")
        relative = _mirror_relative(parsed.path, mirror_root)
        if posixpath.basename(relative) != filename or not filename.endswith(".deb"):
            raise SystemInstallError(f"apt filename {filename!r} does not match {uri!r}.")
        size = int(size_text)
        if size <= 0 or size > max_bytes:
            raise SystemInstallError(f"Package {filename} has unsupported size {size}.")
        if relative in seen:
            raise SystemInstallError(f"Duplicate package path {relative!r}.")
        seen.add(relative)
        files.append(MirrorFile(relative, size))
    return files


def _reject_apk_diagnostics(text: str, command: str) -> List[str]:
    lines = [line for line in text.splitlines() if line.strip()]
    for line in lines:
        # apk only warns and skips a repository whose index is untrusted or
        # corrupt, so every warning is fatal.
        if line.startswith(("WARNING:", "ERROR:")):
            raise SystemInstallError(f"{command} reported: {line[:200]}")
    return lines


def parse_apk_simulate(text: str) -> List[Tuple[str, str]]:
    """Return ``(name, version)`` pairs from ``apk add --simulate`` output.

    Only new installs are accepted; upgrades, downgrades, replacements and
    removals of base-image packages fail closed for now.
    """
    lines = _reject_apk_diagnostics(text, "apk add --simulate")
    if not lines or not _APK_OK_LINE.fullmatch(lines[-1]):
        raise SystemInstallError("apk --simulate did not finish with an OK line.")
    packages: List[Tuple[str, str]] = []
    total = None
    for line in lines[:-1]:
        match = _APK_INSTALL_LINE.match(line)
        if not match:
            raise SystemInstallError(f"Unsupported apk --simulate line: {line[:200]!r}")
        index, count = int(match.group(1)), int(match.group(2))
        if total is None:
            total = count
        if count != total or index != len(packages) + 1 or index > count:
            raise SystemInstallError("Incomplete or inconsistent apk --simulate output.")
        packages.append((match.group(3), match.group(4)))
    if total is not None and len(packages) != total:
        raise SystemInstallError("Incomplete apk --simulate output.")
    return packages


def parse_apk_fetch_urls(text: str, mirror_root: str) -> List[str]:
    """Parse ``apk fetch --simulate --recursive --url`` against a local mirror."""
    _check_mirror_root(mirror_root)
    paths: List[str] = []
    for line in _reject_apk_diagnostics(text, "apk fetch --simulate"):
        if "://" in line:
            raise SystemInstallError(f"apk resolved a non-mirror URL {line!r}.")
        relative = _mirror_relative(line, mirror_root)
        if not relative.endswith(".apk"):
            raise SystemInstallError(f"apk resolved a non-package path {line!r}.")
        if relative not in paths:
            paths.append(relative)
    return paths


def select_apk_downloads(
    simulated: Iterable[Tuple[str, str]], fetch_paths: Iterable[str]
) -> List[MirrorFile]:
    """Keep the fetch paths for packages ``apk add --simulate`` would install."""
    by_name: Dict[str, str] = {}
    for path in fetch_paths:
        name = posixpath.basename(path)
        if name in by_name:
            raise SystemInstallError(f"Package {name} resolves to several mirror paths.")
        by_name[name] = path
    selected = []
    for name, version in simulated:
        filename = f"{name}-{version}.apk"
        if filename not in by_name:
            raise SystemInstallError(
                f"apk would install {name} {version}, but fetch resolution has no {filename}."
            )
        selected.append(MirrorFile(by_name[filename]))
    return selected
