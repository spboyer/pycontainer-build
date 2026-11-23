from dataclasses import dataclass, field
from typing import Dict, List, Optional, Union

SBOM_FORMATS = ("spdx", "cyclonedx")
MULTI_PLATFORM_TARGETS = ("linux/amd64", "linux/arm64")
ARM_V7_FOLLOW_UP = "https://github.com/spboyer/pycontainer-build/issues/119"


def normalize_platforms(value: str) -> List[str]:
    """Split a comma-separated platform list, validating and de-duplicating it."""
    from .builder import parse_platform

    if not isinstance(value, str):
        raise ValueError("platform must be a string such as 'linux/amd64'.")
    platforms: List[str] = []
    for entry in value.split(","):
        platform = entry.strip()
        if not platform:
            raise ValueError(
                f"Invalid platform list {value!r}: entries must not be empty."
            )
        parse_platform(platform)
        if platform not in platforms:
            platforms.append(platform)
    return platforms


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
    security: Optional[dict] = None

    def __post_init__(self):
        from .system_packages import validate_system_packages

        self.system_packages = validate_system_packages(self.system_packages)
        if self.security is not None:
            from .security import validate_security

            for platform in normalize_platforms(self.platform):
                self.security = validate_security(self.security, platform)
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
        platforms = normalize_platforms(self.platform)
        if len(platforms) > 1:
            unsupported = [p for p in platforms if p not in MULTI_PLATFORM_TARGETS]
            if unsupported:
                raise ValueError(
                    "Multi-platform builds support only "
                    f"{', '.join(MULTI_PLATFORM_TARGETS)}; unsupported target(s): "
                    f"{', '.join(unsupported)}. linux/arm/v7 support is tracked in "
                    f"{ARM_V7_FOLLOW_UP}."
                )
            if self.generate_sbom:
                raise ValueError(
                    "SBOM generation is not supported for multi-platform builds; "
                    "build each platform separately to generate its SBOM."
                )

    def target_platforms(self) -> List[str]:
        """Return the normalized, de-duplicated target platforms in request order."""
        return normalize_platforms(self.platform)

    def all_tags(self) -> List[str]:
        """Return every full image reference this build is tagged with."""
        from .tags import resolve_tags

        return resolve_tags(self.tag, self.tags, self.semver_tags, self.latest_tag)
