"""System package declaration, auto-detection and base-image verification.

The builder is daemon-free and never executes RUN steps, so it cannot run
``apt-get`` or ``apk add``. Instead this module validates the declared system
packages, infers the ones common Python packages need, inspects the package
database inside the pulled base image layers and reports which required
packages are absent. Nothing is installed or added to the image.
"""
import logging
import lzma
import re
import tarfile
import tomllib
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple
from .project import requirement_name

logger = logging.getLogger(__name__)

DEBIAN = "debian"
ALPINE = "alpine"

_PACKAGE_NAME = re.compile(r"^[a-z0-9][a-z0-9+._-]*$")
# Runtime libraries (not -dev packages or compilers: no code is compiled in the
# image) needed by Python packages that dynamically link against system libs.
# Pure wheels that bundle their libraries (psycopg2-binary, pillow) are absent.
PYTHON_PACKAGE_SYSTEM_DEPS: Dict[str, Dict[str, Tuple[str, ...]]] = {
    "psycopg2": {DEBIAN: ("libpq5",), ALPINE: ("libpq",)},
    "mysqlclient": {DEBIAN: ("libmariadb3",), ALPINE: ("mariadb-connector-c",)},
    "lxml": {DEBIAN: ("libxml2", "libxslt1.1"), ALPINE: ("libxml2", "libxslt")},
    "pyodbc": {DEBIAN: ("unixodbc",), ALPINE: ("unixodbc",)},
    "cffi": {DEBIAN: ("libffi8",), ALPINE: ("libffi",)},
}


@dataclass
class SystemPackageReport:
    family: Optional[str] = None
    required: List[str] = field(default_factory=list)
    reasons: Dict[str, str] = field(default_factory=dict)
    satisfied: List[str] = field(default_factory=list)
    missing: List[str] = field(default_factory=list)
    unverified: List[str] = field(default_factory=list)


def validate_system_packages(packages) -> List[str]:
    """Return de-duplicated, order-preserving package names or raise ValueError."""
    if not isinstance(packages, (list, tuple)):
        raise ValueError("system_packages must be a list of package names.")
    result: List[str] = []
    for name in packages:
        if not isinstance(name, str) or not _PACKAGE_NAME.match(name):
            raise ValueError(
                f"Invalid system package name {name!r}: use lowercase letters, "
                "digits and + . _ - only."
            )
        if name not in result:
            result.append(name)
    return result


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _requirement_names(context_dir: Path, requirements_file: Optional[str]) -> Set[str]:
    names: Set[str] = set()
    requirements = context_dir / (requirements_file or "requirements.txt")
    if requirements.is_file():
        for line in requirements.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.split("#", 1)[0].strip()
            name = requirement_name(line)
            if name and not line.startswith("-"):
                names.add(name)
    pyproject = context_dir / "pyproject.toml"
    if pyproject.is_file():
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError:
            return names
        dependencies = data.get("project", {}).get("dependencies", [])
        for requirement in dependencies if isinstance(dependencies, list) else []:
            name = requirement_name(str(requirement))
            if name:
                names.add(name)
    return names


def detect_required_packages(
    context_dir, requirements_file: Optional[str], family: str, extra_names: Iterable[str] = ()
) -> Dict[str, str]:
    """Map system package -> Python package that implies it."""
    found: Dict[str, str] = {}
    names = _requirement_names(Path(context_dir), requirements_file)
    names.update(_normalize(name) for name in extra_names)
    for python_name in sorted(names):
        for package in PYTHON_PACKAGE_SYSTEM_DEPS.get(python_name, {}).get(family, ()):
            found.setdefault(package, python_name)
    return found


def _parse_os_release(text: str) -> Optional[str]:
    values = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            values[key.strip()] = value.strip().strip("\"'")
    ids = [values.get("ID", "")] + values.get("ID_LIKE", "").split()
    if ALPINE in ids:
        return ALPINE
    if DEBIAN in ids or "ubuntu" in ids:
        return DEBIAN
    return None


def _dpkg_states(text: str, default_status: Optional[str]) -> Dict[str, bool]:
    """Map package -> installed for each stanza; conflicting stanzas are False."""
    states: Dict[str, bool] = {}
    for stanza in re.split(r"\n\s*\n", text):
        fields = dict(
            line.split(": ", 1) for line in stanza.splitlines() if ": " in line
        )
        package = fields.get("Package", "").strip()
        status = fields.get("Status", default_status)
        if not package or status is None:
            continue
        words = status.split()
        installed = len(words) == 3 and words[1] == "ok" and words[2] == "installed"
        states[package] = states.get(package, installed) and installed
    return states


def _parse_dpkg(texts: Iterable[Tuple[str, Optional[str]]]) -> Set[str]:
    merged: Dict[str, bool] = {}
    for text, default_status in texts:
        for package, installed in _dpkg_states(text, default_status).items():
            merged[package] = merged.get(package, installed) and installed
    return {package for package, installed in merged.items() if installed}


def _parse_apk(text: str) -> Set[str]:
    return {line[2:].strip() for line in text.splitlines() if line.startswith("P:")}


_OS_RELEASE = ("etc/os-release", "usr/lib/os-release")
_DPKG_STATUS = "var/lib/dpkg/status"
_DPKG_STATUS_D = "var/lib/dpkg/status.d"
_APK_INSTALLED = "lib/apk/db/installed"
_TRACKED = {*_OS_RELEASE, _DPKG_STATUS, _APK_INSTALLED}
_ANCESTORS = {
    "/".join(path.split("/")[:depth])
    for path in (*_TRACKED, _DPKG_STATUS_D + "/x")
    for depth in range(1, path.count("/") + 1)
}
_WHITEOUT = ".wh."
_OPAQUE = ".wh..wh..opq"
_MAX_METADATA_FILE_BYTES = 16 * 1024 * 1024
_MAX_METADATA_TOTAL_BYTES = 64 * 1024 * 1024
_MAX_STATUS_D_RECORDS = 10_000
_DRAIN_CHUNK_BYTES = 1024 * 1024
_DIRECTORY = "directory"
_UNCERTAIN = "uncertain"
_LAYER_READ_ERRORS = (tarfile.TarError, OSError, EOFError, zlib.error, lzma.LZMAError)


class _Unverifiable(Exception):
    """Effective package metadata cannot be established."""


def _member_path(name: str) -> str:
    parts = [part for part in name.split("/") if part not in ("", ".")]
    if ".." in parts:
        raise _Unverifiable(f"unsafe layer entry path {name!r}")
    return "/".join(parts)


def _is_status_d_record(path: str) -> bool:
    parent, _, leaf = path.rpartition("/")
    return parent == _DPKG_STATUS_D and not leaf.endswith(".md5sums")


def _is_relevant(path: str) -> bool:
    return path in _TRACKED or path in _ANCESTORS or _is_status_d_record(path)


def _under(path: str, root: str) -> bool:
    return path.startswith(root + "/")


def _read_layer(tar_path, budget: List[int]):
    """Collect one layer's whiteouts, opaque directories and relevant entries."""
    whiteouts: Set[str] = set()
    opaque: Set[str] = set()
    entries: Dict[str, object] = {}
    with tarfile.open(tar_path, mode="r:*") as tar:
        for member in tar:
            path = _member_path(member.name)
            parent, _, leaf = path.rpartition("/")
            if leaf == _OPAQUE:
                opaque.add(parent)
                continue
            if leaf.startswith(_WHITEOUT + _WHITEOUT):
                continue
            if leaf.startswith(_WHITEOUT):
                target = leaf[len(_WHITEOUT):]
                if target:
                    whiteouts.add(f"{parent}/{target}" if parent else target)
                continue
            if not _is_relevant(path):
                continue
            if member.isdir():
                entries[path] = _DIRECTORY
            elif not member.isfile() or path in _ANCESTORS:
                entries[path] = _UNCERTAIN
            elif member.size > _MAX_METADATA_FILE_BYTES:
                entries[path] = _UNCERTAIN
            else:
                budget[0] -= member.size
                if budget[0] < 0:
                    raise _Unverifiable("package metadata exceeds the inspection limit")
                data = tar.extractfile(member).read(_MAX_METADATA_FILE_BYTES + 1)
                entries[path] = data.decode("utf-8", "replace")
        # Everything after the last header must be end-of-archive padding:
        # tarfile stops silently at a corrupt header, and reading to the end
        # also detects truncated or corrupt compressed tails.
        tar.fileobj.seek(tar.offset)
        while chunk := tar.fileobj.read(_DRAIN_CHUNK_BYTES):
            if chunk.strip(b"\0"):
                raise _Unverifiable("corrupt tar header")
    return whiteouts, opaque, entries


def _apply_layer(state: Dict[str, object], whiteouts, opaque, entries) -> None:
    """Apply one layer on top of lower state; same-layer entries always survive."""
    removed = set()
    for key in state:
        if any(key == target or _under(key, target) for target in whiteouts):
            removed.add(key)
        elif any(_under(key, root) if root else True for root in opaque):
            removed.add(key)
        elif any(
            _under(key, path) for path, entry in entries.items() if entry != _DIRECTORY
        ):
            removed.add(key)
        elif entries.get(key) == _DIRECTORY:
            removed.add(key)
    for key in removed:
        del state[key]
    state.update(
        (path, entry) for path, entry in entries.items() if entry != _DIRECTORY
    )
    if sum(_is_status_d_record(key) for key in state) > _MAX_STATUS_D_RECORDS:
        raise _Unverifiable("too many dpkg status.d records")


def _source(state: Dict[str, object], paths: Iterable[str]):
    """Return (texts, certain) for metadata paths, honoring uncertain ancestors."""
    texts: Dict[str, str] = {}
    for path in paths:
        parts = path.split("/")
        chain = ["/".join(parts[:depth]) for depth in range(1, len(parts) + 1)]
        if any(state.get(item) == _UNCERTAIN for item in chain):
            return {}, False
        if isinstance(state.get(path), str):
            texts[path] = state[path]
    return texts, True


def _inspect(layers: Iterable) -> Tuple[Optional[str], Optional[Set[str]], str]:
    state: Dict[str, object] = {}
    budget = [_MAX_METADATA_TOTAL_BYTES]
    for layer in layers:
        try:
            _apply_layer(state, *_read_layer(layer.tar_path, budget))
        except _Unverifiable as exc:
            return None, None, f"{exc} in base layer {layer.tar_path}"
        except _LAYER_READ_ERRORS as exc:
            return None, None, f"could not read base layer {layer.tar_path}: {exc}"

    family = None
    for path in _OS_RELEASE:
        texts, certain = _source(state, [path])
        if certain and path in texts:
            family = _parse_os_release(texts[path])
            if family:
                break

    status_d = sorted(key for key in state if _is_status_d_record(key))
    dpkg, dpkg_certain = _source(state, [_DPKG_STATUS, _DPKG_STATUS_D, *status_d])
    apk, apk_certain = _source(state, [_APK_INSTALLED])
    if family is None:
        if dpkg and not apk and dpkg_certain and apk_certain:
            family = DEBIAN
        elif apk and not dpkg and dpkg_certain and apk_certain:
            family = ALPINE
        else:
            return None, None, "could not determine the base image distribution"

    if family == DEBIAN:
        if not dpkg_certain:
            return family, None, "dpkg metadata is a link or exceeds inspection limits"
        if not dpkg:
            return family, None, "the base image has no dpkg package database"
        texts = [(dpkg.get(_DPKG_STATUS, ""), None)]
        texts += [(dpkg[key], "install ok installed") for key in status_d]
        return family, _parse_dpkg(texts), ""
    if not apk_certain:
        return family, None, "apk metadata is a link or exceeds inspection limits"
    if not apk:
        return family, None, "the base image has no apk package database"
    return family, _parse_apk(apk[_APK_INSTALLED]), ""


def inspect_base_layers(layers: Iterable) -> Tuple[Optional[str], Optional[Set[str]]]:
    """Return (distro family, installed package names) for a base image.

    Layers are applied in order with OCI whiteouts. Installed packages are
    ``None`` when the effective package database is absent or uncertain.
    """
    family, installed, _ = _inspect(layers)
    return family, installed


def report_system_packages(config, base_layers, locked_names: Iterable[str] = ()) -> SystemPackageReport:
    """Compare required system packages with the base image and log findings."""
    report = SystemPackageReport()
    explicit = validate_system_packages(config.system_packages)
    if not explicit and not config.detect_system_packages:
        return report

    family, installed, reason = _inspect(base_layers)
    report.family = family

    required = list(explicit)
    detected = (
        detect_required_packages(
            config.context_dir, config.requirements_file, family, locked_names
        )
        if family and config.detect_system_packages
        else {}
    )
    for package, python_name in detected.items():
        if package not in required:
            required.append(package)
            report.reasons[package] = f"required by Python package {python_name}"
    report.required = required
    if not required:
        return report

    if installed is None:
        report.unverified = list(required)
        logger.warning(
            "Could not verify system packages in the base image (%s): %s. "
            "pycontainer-build does not install system packages; use a base "
            "image that provides them.",
            reason,
            ", ".join(required),
        )
        return report

    for package in required:
        (report.satisfied if package in installed else report.missing).append(package)
    for package in report.missing:
        reason = report.reasons.get(package, "declared in system_packages")
        logger.warning(
            "System package %r (%s) is not installed in base image (%s). "
            "pycontainer-build cannot run %s; choose a base image that "
            "includes it.",
            package,
            reason,
            family,
            "apt-get install" if family == DEBIAN else "apk add",
        )
    if report.satisfied:
        logger.info("System packages present in base image: %s", ", ".join(report.satisfied))
    return report
