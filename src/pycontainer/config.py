import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Union

SBOM_FORMATS = ("spdx", "cyclonedx")
_REGISTRY_HOST = re.compile(
    r"(?:\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?)(?::[0-9]{1,5})?"
)


DOCKER_HUB_HOSTS = ("docker.io", "index.docker.io", "registry-1.docker.io")


def _normalize_registry_host(entry, kind: str) -> str:
    """Validate a ``host[:port]`` registry name and return it lowercased."""
    if not isinstance(entry, str) or not _REGISTRY_HOST.fullmatch(entry):
        raise ValueError(
            f"Invalid {kind} {entry!r}: expected host[:port] without "
            "a scheme, path, or credentials."
        )
    try:
        port = urllib.parse.urlsplit(f"//{entry}").port
    except ValueError:
        port = 0
    if port == 0:
        raise ValueError(f"Invalid {kind} port in {entry!r}.")
    return entry.lower()


def canonical_registry(host: str) -> str:
    """Return the lookup key for a registry host, folding Docker Hub aliases."""
    host = host.lower()
    return "docker.io" if host in DOCKER_HUB_HOSTS else host


def validate_insecure_registries(registries: List[str]) -> List[str]:
    """Normalize per-host plain-HTTP registry opt-ins (``host[:port]``)."""
    if not isinstance(registries, list):
        raise ValueError("insecure_registries must be a list of host[:port] strings.")
    normalized = []
    for entry in registries:
        host = _normalize_registry_host(entry, "insecure registry")
        if host not in normalized:
            normalized.append(host)
    return normalized


def validate_registry_mirrors(mirrors: Dict[str, List[str]]) -> Dict[str, List[str]]:
    """Normalize ``{upstream: [mirror, ...]}`` pull-through mirror settings."""
    if not isinstance(mirrors, dict):
        raise ValueError(
            "registry_mirrors must map an upstream host[:port] to a list of mirror hosts."
        )
    normalized: Dict[str, List[str]] = {}
    for upstream, hosts in mirrors.items():
        key = canonical_registry(_normalize_registry_host(upstream, "mirrored registry"))
        if key in normalized:
            raise ValueError(f"Registry {upstream!r} has more than one mirror list.")
        if not isinstance(hosts, list) or not hosts:
            raise ValueError(
                f"Registry mirrors for {upstream!r} must be a non-empty list of "
                "host[:port] strings."
            )
        ordered: List[str] = []
        for entry in hosts:
            host = _normalize_registry_host(entry, "registry mirror")
            if canonical_registry(host) == key:
                raise ValueError(
                    f"Registry mirror {entry!r} is the upstream registry {upstream!r}."
                )
            if host not in ordered:
                ordered.append(host)
        normalized[key] = ordered
    return normalized


@dataclass
class BuildConfig:
    tag: str = "local/test:latest"
    base_image: str = "python:3.11-slim"
    context_dir: str = "."
    workdir: str = "/app"
    entrypoint: Optional[List[str]] = None
    env: Dict[str, str] = field(default_factory=dict)
    exposed_ports: List[int] = field(default_factory=list)
    include_paths: Optional[List[str]] = None
    exclude_paths: List[str] = field(default_factory=list)
    exclude_defaults: bool = True
    output_dir: str = "dist/image"
    use_cache: bool = True
    cache_dir: Optional[str] = None
    max_cache_size_mb: int = 5000
    labels: Optional[Dict[str, str]] = None
    user: Optional[str] = None
    cmd: Optional[List[str]] = None
    include_deps: bool = False
    requirements_file: str = "requirements.txt"
    verbose: bool = False
    dry_run: bool = False
    platform: str = "linux/amd64"
    reproducible: bool = True
    generate_sbom: Union[bool, str] = False
    system_packages: List[str] = field(default_factory=list)
    detect_system_packages: bool = True
    tags: Optional[List[str]] = None
    semver_tags: bool = False
    latest_tag: bool = False
    insecure_registries: List[str] = field(default_factory=list)
    registry_ca_file: Optional[str] = None
    registry_mirrors: Dict[str, List[str]] = field(default_factory=dict)

    def __post_init__(self):
        from .system_packages import validate_system_packages

        self.system_packages = validate_system_packages(self.system_packages)
        self.insecure_registries = validate_insecure_registries(
            self.insecure_registries
        )
        self.registry_mirrors = validate_registry_mirrors(self.registry_mirrors)
        if self.registry_ca_file is not None:
            from .registry_client import create_registry_ssl_context

            if not isinstance(self.registry_ca_file, str) or not self.registry_ca_file:
                raise ValueError("registry_ca_file must be a non-empty path string.")
            create_registry_ssl_context(self.registry_ca_file)
        if not isinstance(self.generate_sbom, (bool, str)):
            raise ValueError(
                "generate_sbom must be a boolean or one of: spdx, cyclonedx"
            )
        if isinstance(self.generate_sbom, str) and self.generate_sbom not in SBOM_FORMATS:
            raise ValueError(
                "generate_sbom must be a boolean or one of: spdx, cyclonedx"
            )
        if self.tags or self.semver_tags or self.latest_tag:
            from .tags import resolve_tags

            resolve_tags(self.tag, self.tags, self.semver_tags, self.latest_tag)

    def all_tags(self) -> List[str]:
        """Return every full image reference this build is tagged with."""
        from .tags import resolve_tags

        return resolve_tags(self.tag, self.tags, self.semver_tags, self.latest_tag)
