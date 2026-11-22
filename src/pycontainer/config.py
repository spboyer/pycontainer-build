from dataclasses import dataclass, field
from typing import Dict, List, Optional, Union

SBOM_FORMATS = ("spdx", "cyclonedx")


def _track_port_mutation(method):
    def tracked(self, *args, **kwargs):
        result = method(self, *args, **kwargs)
        self.specified = True
        return result
    return tracked


class _DefaultPorts(list):
    """Keep the mutable list API while distinguishing an untouched default."""

    def __init__(self, values=()):
        super().__init__(values)
        self.specified = bool(self)

    append = _track_port_mutation(list.append)
    clear = _track_port_mutation(list.clear)
    extend = _track_port_mutation(list.extend)
    insert = _track_port_mutation(list.insert)
    pop = _track_port_mutation(list.pop)
    remove = _track_port_mutation(list.remove)
    reverse = _track_port_mutation(list.reverse)
    sort = _track_port_mutation(list.sort)
    __delitem__ = _track_port_mutation(list.__delitem__)
    __iadd__ = _track_port_mutation(list.__iadd__)
    __imul__ = _track_port_mutation(list.__imul__)
    __setitem__ = _track_port_mutation(list.__setitem__)


@dataclass
class BuildConfig:
    tag: str = "local/test:latest"
    base_image: str = "python:3.11-slim"
    context_dir: str = "."
    workdir: str = "/app"
    entrypoint: Optional[List[str]] = None
    env: Dict[str, str] = field(default_factory=dict)
    exposed_ports: List[int] = field(default_factory=_DefaultPorts)
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

    def __post_init__(self):
        from .system_packages import validate_system_packages

        self.system_packages = validate_system_packages(self.system_packages)
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

    def has_exposed_ports(self) -> bool:
        """Return whether ports were explicitly configured or detected."""
        return self.exposed_ports is not None and (
            not isinstance(self.exposed_ports, _DefaultPorts)
            or self.exposed_ports.specified
        )
