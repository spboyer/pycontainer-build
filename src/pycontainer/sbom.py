"""Software Bill of Materials (SBOM) generation."""
import hashlib
import json
import re
import tomllib
import uuid
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Dict, Sequence, Tuple
from urllib.parse import quote

from .config import SBOM_FORMATS


def generate_sbom(
    context_dir: Path,
    output_path: Path,
    format: str = "spdx",
    *,
    package_inventory: Sequence[Tuple[str, str]],
) -> Dict:
    """Generate an SBOM from the exact package inventory staged into the image."""
    if format not in SBOM_FORMATS:
        raise ValueError(f"Unsupported SBOM format: {format}")

    packages = _normalize_inventory(package_inventory)
    if format == "spdx":
        sbom = _generate_spdx(context_dir, packages)
    else:
        sbom = _generate_cyclonedx(packages)

    output_path.write_text(json.dumps(sbom, indent=2), encoding="utf-8")
    return sbom


def _generate_spdx(
    context_dir: Path,
    packages: Sequence[Tuple[str, str]],
) -> Dict:
    """Generate an SPDX 2.3 SBOM."""
    tool_version = _tool_version()
    document_id = uuid.uuid4()
    project_name = context_dir.name or "root"
    sbom = {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": f"pycontainer-{project_name}",
        "documentNamespace": (
            f"https://sbom.pycontainer/{quote(project_name, safe='')}/{document_id}"
        ),
        "creationInfo": {
            "created": _timestamp(),
            "creators": [f"Tool: pycontainer-build-{tool_version}"],
            "licenseListVersion": "3.21",
        },
        "packages": [],
    }

    for package_name, package_version in packages:
        sbom["packages"].append(
            {
                "SPDXID": _spdx_package_id(package_name, package_version),
                "name": package_name,
                "versionInfo": package_version,
                "downloadLocation": "NOASSERTION",
                "filesAnalyzed": False,
                "licenseConcluded": "NOASSERTION",
                "licenseDeclared": "NOASSERTION",
                "copyrightText": "NOASSERTION",
            }
        )

    return sbom


def _generate_cyclonedx(packages: Sequence[Tuple[str, str]]) -> Dict:
    """Generate a CycloneDX 1.4 SBOM."""
    sbom = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.4",
        "serialNumber": f"urn:uuid:{uuid.uuid4()}",
        "version": 1,
        "metadata": {
            "timestamp": _timestamp(),
            "tools": [{"name": "pycontainer-build", "version": _tool_version()}],
        },
        "components": [],
    }

    for package_name, package_version in packages:
        purl_name = _canonical_python_name(package_name)
        purl_version = quote(package_version, safe="._-~")
        sbom["components"].append(
            {
                "type": "library",
                "name": package_name,
                "version": package_version,
                "purl": f"pkg:pypi/{quote(purl_name, safe='-._~')}@{purl_version}",
            }
        )

    return sbom


def _normalize_inventory(
    package_inventory: Sequence[Tuple[str, str]],
) -> list[Tuple[str, str]]:
    if package_inventory is None:
        raise ValueError(
            "package_inventory is required; pass an empty sequence when no packages "
            "were staged"
        )

    packages = set()
    for entry in package_inventory:
        if not isinstance(entry, (tuple, list)) or len(entry) != 2:
            raise ValueError("Each staged package must contain a name and version")
        try:
            name, package_version = entry
        except (TypeError, ValueError) as exc:
            raise ValueError("Each staged package must contain a name and version") from exc
        if (
            not isinstance(name, str)
            or not isinstance(package_version, str)
            or not name.strip()
            or not package_version.strip()
        ):
            raise ValueError(
                "Staged package names and versions must be non-empty text"
            )
        packages.add((name.strip(), package_version.strip()))

    return sorted(packages, key=lambda item: (item[0].casefold(), item[1]))


def _canonical_python_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _spdx_package_id(name: str, package_version: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9.-]+", "-", f"{name}-{package_version}").strip(".-")
    suffix = hashlib.sha256(f"{name}\0{package_version}".encode()).hexdigest()[:12]
    return f"SPDXRef-Package-{slug[:48] or 'package'}-{suffix}"


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _tool_version() -> str:
    try:
        return version("pycontainer-build")
    except PackageNotFoundError:
        pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
        if not pyproject.is_file():
            raise RuntimeError(
                "Cannot determine pycontainer-build version from package metadata"
            )
        with pyproject.open("rb") as stream:
            return tomllib.load(stream)["project"]["version"]
