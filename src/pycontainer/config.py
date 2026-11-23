from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Union
from .build_args import BuildArgument, EXPANDABLE_FIELDS, expand_field, resolve_arguments
from .file_rules import evaluate_file_rules

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
    build_args: Optional[Dict[str, BuildArgument]] = field(default=None, repr=False)
    file_rules: List[Dict[str, Union[str, List[str]]]] = field(default_factory=list)
    _resolved_build_args: Dict[str, str] = field(default_factory=dict, init=False, repr=False)
    _conditional_includes: Tuple[str, ...] = field(default=(), init=False, repr=False)
    _conditional_excludes: Tuple[str, ...] = field(default=(), init=False, repr=False)

    def __post_init__(self):
        if self.build_args is not None:
            self._resolved_build_args = resolve_arguments(self.build_args)
            for name in EXPANDABLE_FIELDS:
                setattr(self, name, expand_field(getattr(self, name), self._resolved_build_args, name))
        try:
            self._validate()
            if self.file_rules:
                self._conditional_includes, self._conditional_excludes = evaluate_file_rules(
                    self.file_rules, self._resolved_build_args, self.platform
                )
        except ValueError as exc:
            raise ValueError(self.redact(str(exc))) from None

    def _validate(self):
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

    def redact(self, message: str) -> str:
        """Keep build input values out of diagnostic output."""
        import json
        from urllib.parse import quote

        representations = set()
        for value in self._resolved_build_args.values():
            if value:
                representations.update((
                    value, repr(value)[1:-1],
                    json.dumps(value, ensure_ascii=False)[1:-1],
                    json.dumps(value)[1:-1], quote(value, safe=""),
                ))
        for value in sorted(representations, key=len, reverse=True):
            message = message.replace(value, "<redacted>")
        return message

    def cache_identity(self, backend: str = "pycontainer-oci-v1") -> str:
        import hashlib
        import json

        storage_fields = {"context_dir", "output_dir", "cache_dir"}
        identity = {
            name: getattr(self, name)
            for name in EXPANDABLE_FIELDS if name not in storage_fields
        }
        identity["backend"] = backend
        if self.file_rules:
            identity["file_rules"] = {
                "include": sorted(self._conditional_includes),
                "exclude": sorted(self._conditional_excludes),
            }
        encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def all_tags(self) -> List[str]:
        """Return every full image reference this build is tagged with."""
        from .tags import resolve_tags

        return resolve_tags(self.tag, self.tags, self.semver_tags, self.latest_tag)
