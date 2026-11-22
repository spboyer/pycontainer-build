from dataclasses import dataclass, field
import re
from typing import Dict, List, Optional, Union

SBOM_FORMATS = ("spdx", "cyclonedx")
DEPENDENCY_LAYER_STRATEGIES = ("single", "explicit", "history")


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
    requirements_file: Optional[str] = None
    lockfile: Optional[str] = None
    use_lockfile: bool = True
    dependency_layer_strategy: str = "single"
    stable_dependencies: List[str] = field(default_factory=list)
    frequent_dependencies: List[str] = field(default_factory=list)
    dependency_history_ref: Optional[str] = None
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
        if self.lockfile and self.requirements_file:
            raise ValueError("lockfile and requirements_file are mutually exclusive.")
        if self.lockfile and not self.use_lockfile:
            raise ValueError("lockfile cannot be combined with use_lockfile = false.")
        if self.dependency_layer_strategy not in DEPENDENCY_LAYER_STRATEGIES:
            raise ValueError(
                "dependency_layer_strategy must be one of: "
                + ", ".join(DEPENDENCY_LAYER_STRATEGIES)
            )
        groups = []
        for field_name in ("stable_dependencies", "frequent_dependencies"):
            values = getattr(self, field_name)
            if not isinstance(values, list) or not all(
                isinstance(value, str)
                and re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?", value)
                for value in values
            ):
                raise ValueError(f"{field_name} must be a list of distribution names.")
            normalized = [re.sub(r"[-_.]+", "-", value).lower() for value in values]
            if len(normalized) != len(set(normalized)):
                raise ValueError(f"{field_name} contains duplicate distribution names.")
            groups.append(set(normalized))
        if groups[0] & groups[1]:
            raise ValueError("A distribution cannot be both stable and frequent.")
        if self.dependency_layer_strategy == "single" and any(groups):
            raise ValueError("Dependency groups require the explicit or history strategy.")
        if self.dependency_layer_strategy != "single" and not self.include_deps:
            raise ValueError("Grouped dependency layers require include_deps = true.")
        if self.dependency_history_ref is not None and (
            self.dependency_layer_strategy != "history"
            or not isinstance(self.dependency_history_ref, str)
            or not self.dependency_history_ref.strip()
        ):
            raise ValueError("dependency_history_ref requires the history strategy and a non-empty ref.")
        if self.tags or self.semver_tags or self.latest_tag:
            from .tags import resolve_tags

            resolve_tags(self.tag, self.tags, self.semver_tags, self.latest_tag)

    def all_tags(self) -> List[str]:
        """Return every full image reference this build is tagged with."""
        from .tags import resolve_tags

        return resolve_tags(self.tag, self.tags, self.semver_tags, self.latest_tag)
