from dataclasses import dataclass, field
from typing import Dict, List, Optional, Union

SBOM_FORMATS = ("spdx", "cyclonedx")
SCAN_SEVERITIES = ("negligible", "low", "medium", "high", "critical")


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
    scan: bool = False
    scan_executable: str = "grype"
    scan_threshold: str = "high"
    scan_unknown: str = "fail"
    scan_report_format: str = "json"
    scan_report: Optional[str] = None
    scan_offline: bool = False
    scan_db_dir: Optional[str] = None
    scan_db_max_age_hours: int = 120
    security: Optional[dict] = None

    def __post_init__(self):
        from .system_packages import validate_system_packages

        self.system_packages = validate_system_packages(self.system_packages)
        if self.scan_threshold not in SCAN_SEVERITIES:
            raise ValueError("scan_threshold must be one of: " + ", ".join(SCAN_SEVERITIES))
        if self.scan_unknown not in ("fail", "ignore"):
            raise ValueError("scan_unknown must be fail or ignore")
        if self.scan_report_format not in ("json", "sarif"):
            raise ValueError("scan_report_format must be json or sarif")
        if type(self.scan_db_max_age_hours) is not int or self.scan_db_max_age_hours <= 0:
            raise ValueError("scan_db_max_age_hours must be a positive integer")
        if not isinstance(self.scan_executable, str) or not self.scan_executable.strip():
            raise ValueError("scan_executable must be a nonempty executable path or name")
        if self.security is not None:
            from .security import validate_security

            self.security = validate_security(self.security, self.platform)
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
