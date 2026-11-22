from dataclasses import dataclass, field
from typing import Dict, List, Optional, Union

from .release_notes import DEFAULT_RELEASE_NOTES_LABEL

SBOM_FORMATS = ("spdx", "cyclonedx")


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
    manifest_annotations: Optional[Dict[str, str]] = None
    index_annotations: Optional[Dict[str, str]] = None
    release_notes_file: Optional[str] = None
    release_notes_label: str = DEFAULT_RELEASE_NOTES_LABEL

    def __post_init__(self):
        from .release_notes import validate_release_notes_settings
        from .system_packages import validate_system_packages

        self.validate_metadata()
        validate_release_notes_settings(self.release_notes_file, self.release_notes_label)
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

    def validate_metadata(self) -> None:
        from .metadata import validate_metadata

        for name in ("labels", "manifest_annotations", "index_annotations"):
            validate_metadata(
                getattr(self, name),
                name,
                protect_reference=name != "labels",
            )

    def all_tags(self) -> List[str]:
        """Return every full image reference this build is tagged with."""
        from .tags import resolve_tags

        return resolve_tags(self.tag, self.tags, self.semver_tags, self.latest_tag)
