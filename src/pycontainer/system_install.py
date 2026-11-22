"""Fail-closed prerequisites for isolated system package installation (#31).

Nothing here runs a package manager, maintainer script or repository hook.
Package installation stays unavailable until the isolated execution backend
from #22 exists. These helpers cover the parts that run in the builder:

* checking the effective package-manager configuration printed inside the
  sandbox for settings that weaken signature verification;
* turning offline resolution output into a bounded download list relative to
  a local mirror; and
* fetching untrusted repository bytes from explicitly allowed hosts.

The builder is untrusted transport. It never treats downloaded bytes as
authentic: apt verifies ``InRelease`` with the pinned ``Signed-By`` keyring and
checks every ``.deb`` against the signed index, and apk verifies the signed
``APKINDEX`` and package signatures, both offline inside the sandbox.
"""
import hashlib
import http.client
import os
import posixpath
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

DEFAULT_FETCH_TIMEOUT_SECONDS = 30
MAX_FETCH_REDIRECTS = 3
FETCH_CHUNK_SIZE = 1024 * 1024
MAX_PACKAGE_BYTES = 512 * 1024 * 1024
REDIRECT_STATUSES = {301, 302, 303, 307, 308}

DEFAULT_FETCH_DEADLINE_SECONDS = 600

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
    r"^\(\d+/\d+\) Installing ([a-z0-9][a-z0-9+._-]*) \(([0-9A-Za-z.+_~-]+)\)$"
)
_APK_OK_LINE = re.compile(r"^OK: .+$")
_HOST = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


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
    packages: List[Tuple[str, str]] = []
    for line in _reject_apk_diagnostics(text, "apk add --simulate"):
        match = _APK_INSTALL_LINE.match(line)
        if match:
            packages.append((match.group(1), match.group(2)))
        elif not _APK_OK_LINE.match(line):
            raise SystemInstallError(f"Unsupported apk --simulate line: {line[:200]!r}")
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


def normalize_allowed_hosts(hosts: Iterable[str]) -> Set[str]:
    allowed = set()
    for host in hosts:
        if not isinstance(host, str) or not _HOST.match(host):
            raise ValueError(f"Allowed repository host must be a lowercase DNS name: {host!r}")
        allowed.add(host)
    if not allowed:
        raise ValueError("At least one allowed repository host is required.")
    return allowed


def _checked_url(url: str, allowed: Set[str]) -> str:
    try:
        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname
        port = parsed.port
    except ValueError as error:
        raise SystemInstallError(f"Malformed repository URL {url!r}.") from error
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed.query
        or host is None
        or host != parsed.netloc.split(":")[0]
        or host not in allowed
    ):
        raise SystemInstallError(f"Refusing repository URL outside the allowed HTTPS hosts: {url!r}")
    if port not in (None, 443):
        raise SystemInstallError(f"Refusing non-default port in repository URL {url!r}.")
    return url


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _build_opener():
    # No proxy, auth or cookie handlers: environment proxies and their
    # credentials are not inherited by repository transport.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def _open_untrusted(url: str, allowed: Set[str], timeout: float):
    opener = _build_opener()
    current = _checked_url(url, allowed)
    for _ in range(MAX_FETCH_REDIRECTS + 1):
        request = urllib.request.Request(current, headers={"Accept-Encoding": "identity"})
        try:
            response = opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as error:
            if error.code not in REDIRECT_STATUSES:
                error.close()
                raise SystemInstallError(
                    f"Repository fetch of {current} failed with HTTP {error.code}."
                ) from error
            response = error
        if response.status in REDIRECT_STATUSES:
            location = response.headers.get("Location")
            response.close()
            if not location:
                raise SystemInstallError(f"Redirect from {current} has no Location.")
            current = _checked_url(urllib.parse.urljoin(current, location), allowed)
            continue
        if response.status != 200:
            response.close()
            raise SystemInstallError(
                f"Repository fetch of {current} failed with HTTP {response.status}."
            )
        return response
    raise SystemInstallError(f"Too many redirects fetching {url}.")


def fetch_untrusted(
    url: str,
    destination: Path,
    allowed_hosts: Iterable[str],
    max_bytes: int,
    expected_size: Optional[int] = None,
    expected_sha256: Optional[str] = None,
    timeout: float = DEFAULT_FETCH_TIMEOUT_SECONDS,
    deadline: float = DEFAULT_FETCH_DEADLINE_SECONDS,
) -> Tuple[str, int]:
    """Stream ``url`` to ``destination`` within bounds; return (sha256, size).

    ``timeout`` bounds each socket operation and ``deadline`` the whole
    download. A size or hash match proves transport integrity only.
    Authenticity is established later by the package manager in the sandbox.
    """
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer.")
    if expected_size is not None and (type(expected_size) is not int or not 0 < expected_size <= max_bytes):
        raise ValueError("expected_size must be a positive integer no larger than max_bytes.")
    if expected_sha256 is not None and not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ValueError("expected_sha256 must be a lowercase hex SHA-256 digest.")
    if timeout <= 0 or deadline <= 0:
        raise ValueError("timeout and deadline must be greater than zero.")
    expires = time.monotonic() + deadline
    allowed = normalize_allowed_hosts(allowed_hosts)
    destination = Path(destination)
    limit = expected_size if expected_size is not None else max_bytes
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, staged_name = tempfile.mkstemp(
        prefix=f".{destination.name}-", suffix=".tmp", dir=destination.parent
    )
    staged = Path(staged_name)
    hasher = hashlib.sha256()
    size = 0
    try:
        with os.fdopen(descriptor, "wb") as target:
            with _open_untrusted(url, allowed, timeout) as response:
                length = response.headers.get("Content-Length")
                if length is not None and (not length.isdigit() or int(length) > limit):
                    raise SystemInstallError(
                        f"{url} declares Content-Length {length}, limit is {limit}."
                    )
                while True:
                    chunk = response.read(FETCH_CHUNK_SIZE)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > limit:
                        raise SystemInstallError(f"{url} exceeds the {limit}-byte limit.")
                    if time.monotonic() > expires:
                        raise SystemInstallError(f"{url} exceeded the {deadline}-second deadline.")
                    hasher.update(chunk)
                    target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
        digest = hasher.hexdigest()
        if expected_size is not None and size != expected_size:
            raise SystemInstallError(f"{url} has size {size}, expected {expected_size}.")
        if expected_sha256 is not None and digest != expected_sha256:
            raise SystemInstallError(f"{url} hashes to {digest}, expected {expected_sha256}.")
        os.replace(staged, destination)
        return digest, size
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as error:
        raise SystemInstallError(
            f"Repository fetch of {url} failed: {type(error).__name__}."
        ) from error
    finally:
        staged.unlink(missing_ok=True)
