"""System package declaration, auto-detection and base-image verification.

The builder is daemon-free and never executes RUN steps, so it cannot run
``apt-get`` or ``apk add``. Instead this module validates the declared system
packages, infers the ones common Python packages need, inspects the package
database inside the pulled base image layers and reports which required
packages are absent. Nothing is installed or added to the image.
"""
import logging
import re
import tarfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

DEBIAN = "debian"
ALPINE = "alpine"

_PACKAGE_NAME = re.compile(r"^[a-z0-9][a-z0-9+._-]*$")
_REQUIREMENT_NAME = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)")

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


def _requirement_names(context_dir: Path, requirements_file: str) -> Set[str]:
    names: Set[str] = set()
    requirements = context_dir / requirements_file
    if requirements.is_file():
        for line in requirements.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.split("#", 1)[0].strip()
            match = _REQUIREMENT_NAME.match(line)
            if match and not line.startswith("-"):
                names.add(_normalize(match.group(1)))
    pyproject = context_dir / "pyproject.toml"
    if pyproject.is_file():
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError:
            return names
        dependencies = data.get("project", {}).get("dependencies", [])
        for requirement in dependencies if isinstance(dependencies, list) else []:
            match = _REQUIREMENT_NAME.match(str(requirement).strip())
            if match:
                names.add(_normalize(match.group(1)))
    return names


def detect_required_packages(
    context_dir, requirements_file: str, family: str
) -> Dict[str, str]:
    """Map system package -> Python package that implies it."""
    found: Dict[str, str] = {}
    for python_name in sorted(_requirement_names(Path(context_dir), requirements_file)):
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


def _parse_dpkg(text: str) -> Set[str]:
    installed = set()
    for stanza in re.split(r"\n\s*\n", text):
        fields = dict(
            line.split(": ", 1) for line in stanza.splitlines() if ": " in line
        )
        status = fields.get("Status", "install ok installed")
        if "Package" in fields and status.endswith(" installed") and "not-installed" not in status:
            installed.add(fields["Package"].strip())
    return installed


def _parse_apk(text: str) -> Set[str]:
    return {line[2:].strip() for line in text.splitlines() if line.startswith("P:")}


_TRACKED = {
    "etc/os-release",
    "usr/lib/os-release",
    "var/lib/dpkg/status",
    "lib/apk/db/installed",
}


def inspect_base_layers(
    layers: Iterable, *, redact: Optional[Callable[[str], str]] = None
) -> Tuple[Optional[str], Set[str]]:
    """Return (distro family, installed package names) for a base image."""
    files: Dict[str, str] = {}
    status_d: Dict[str, str] = {}
    for layer in layers:
        try:
            with tarfile.open(layer.tar_path, mode="r|*") as tar:
                for member in tar:
                    name = member.name.lstrip("./")
                    if not member.isfile():
                        continue
                    if name in _TRACKED:
                        files[name] = tar.extractfile(member).read().decode("utf-8", "replace")
                    elif name.startswith("var/lib/dpkg/status.d/") and not name.endswith(".md5sums"):
                        status_d[name] = tar.extractfile(member).read().decode("utf-8", "replace")
        except (tarfile.TarError, OSError) as exc:
            message = f"Could not inspect base layer {layer.tar_path}: {exc}"
            logger.debug("%s", redact(message) if redact is not None else message)

    family = None
    for key in ("etc/os-release", "usr/lib/os-release"):
        if key in files and family is None:
            family = _parse_os_release(files[key])
    if family is None:
        if "var/lib/dpkg/status" in files or status_d:
            family = DEBIAN
        elif "lib/apk/db/installed" in files:
            family = ALPINE

    installed: Set[str] = set()
    if family == DEBIAN:
        for text in [files.get("var/lib/dpkg/status", ""), *status_d.values()]:
            installed |= _parse_dpkg(text)
    elif family == ALPINE:
        installed = _parse_apk(files.get("lib/apk/db/installed", ""))
    return family, installed


def report_system_packages(
    config, base_layers, *, redact: Optional[Callable[[str], str]] = None
) -> SystemPackageReport:
    """Compare required system packages with the base image and log findings."""
    redact_message = redact if redact is not None else str
    report = SystemPackageReport()
    explicit = validate_system_packages(config.system_packages)
    if not explicit and not config.detect_system_packages:
        return report

    family, installed = inspect_base_layers(base_layers, redact=redact)
    report.family = family

    required = list(explicit)
    detected = (
        detect_required_packages(config.context_dir, config.requirements_file, family)
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

    if family is None:
        report.unverified = list(required)
        logger.warning("%s", redact_message(
            "Could not determine the base image package manager, so system "
            "packages cannot be verified: %s. pycontainer-build does not "
            "install system packages; use a base image that provides them."
            % ", ".join(required)
        ))
        return report

    for package in required:
        (report.satisfied if package in installed else report.missing).append(package)
    for package in report.missing:
        reason = report.reasons.get(package, "declared in system_packages")
        logger.warning("%s", redact_message(
            "System package %r (%s) is not installed in base image (%s). "
            "pycontainer-build cannot run %s; choose a base image that "
            "includes it."
            % (package, reason, family, "apt-get install" if family == DEBIAN else "apk add")
        ))
    if report.satisfied:
        logger.info(
            "%s", redact_message("System packages present in base image: %s" % ", ".join(report.satisfied))
        )
    return report
