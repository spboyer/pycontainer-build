import posixpath
import re
import unicodedata
import urllib.parse
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Union
from .build_args import BuildArgument, EXPANDABLE_FIELDS, expand_field, redact_values, resolve_arguments
from .file_rules import evaluate_file_rules

from .release_notes import DEFAULT_RELEASE_NOTES_LABEL

SBOM_FORMATS = ("spdx", "cyclonedx")
MULTI_PLATFORM_TARGETS = ("linux/amd64", "linux/arm64", "linux/arm/v7")
SCAN_SEVERITIES = ("negligible", "low", "medium", "high", "critical")
MULTI_PLATFORM_SCAN_ERROR = (
    "Vulnerability scanning is not supported for multi-platform images; "
    "build and scan each platform separately."
)
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

# Linux signal names in the OCI "SIGNAME" format, matching Docker's Linux table.
STOP_SIGNALS = frozenset(
    [
        "SIGABRT", "SIGALRM", "SIGBUS", "SIGCHLD", "SIGCONT", "SIGFPE",
        "SIGHUP", "SIGILL", "SIGINT", "SIGIO", "SIGKILL", "SIGPIPE",
        "SIGPROF", "SIGPWR", "SIGQUIT", "SIGSEGV", "SIGSTKFLT",
        "SIGSTOP", "SIGSYS", "SIGTERM", "SIGTRAP", "SIGTSTP", "SIGTTIN",
        "SIGTTOU", "SIGURG", "SIGUSR1", "SIGUSR2", "SIGVTALRM", "SIGWINCH",
        "SIGXCPU", "SIGXFSZ", "SIGRTMIN", "SIGRTMAX",
    ]
    + [f"SIGRTMIN+{n}" for n in range(1, 16)]
    + [f"SIGRTMAX-{n}" for n in range(1, 15)]
)


def validate_stop_signal(value: str) -> str:
    """Return value if it is a supported OCI SIGNAME, else raise ValueError."""
    if not isinstance(value, str) or value not in STOP_SIGNALS:
        raise ValueError(
            f"stop_signal must be a Linux signal name such as SIGTERM, SIGINT or "
            f"SIGRTMIN+3; got {value!r}."
        )
    return value


def validate_volume(path: str) -> str:
    """Return path if it is a normalized absolute container path, else raise ValueError."""
    if (
        not isinstance(path, str)
        or not path.startswith("/")
        or path == "/"
        or path.startswith("//")
        or "\\" in path
        or any(unicodedata.category(ch).startswith("C") for ch in path)
        or posixpath.normpath(path) != path
    ):
        raise ValueError(
            "volumes entries must be normalized absolute container paths other "
            f"than '/', without '.', '..', empty segments, backslashes or control "
            f"characters; got {path!r}."
        )
    return path


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
    scan: bool = False
    scan_executable: str = "grype"
    scan_threshold: str = "high"
    scan_unknown: str = "fail"
    scan_report_format: str = "json"
    scan_report: Optional[str] = None
    scan_offline: bool = False
    scan_db_dir: Optional[str] = None
    scan_db_max_age_hours: int = 120
    record_history: bool = False
    history_dir: Optional[str] = None
    insecure_registries: List[str] = field(default_factory=list)
    registry_ca_file: Optional[str] = None
    registry_mirrors: Dict[str, List[str]] = field(default_factory=dict)
    security: Optional[dict] = None
    stop_signal: Optional[str] = None
    volumes: List[str] = field(default_factory=list)
    release_notes_file: Optional[str] = None
    release_notes_label: str = DEFAULT_RELEASE_NOTES_LABEL
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
        from .release_notes import validate_release_notes_settings
        from .system_packages import validate_system_packages

        self.validate_metadata()
        validate_release_notes_settings(self.release_notes_file, self.release_notes_label)
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
        self.insecure_registries = validate_insecure_registries(
            self.insecure_registries
        )
        self.registry_mirrors = validate_registry_mirrors(self.registry_mirrors)
        if self.registry_ca_file is not None:
            from .registry_client import create_registry_ssl_context

            if not isinstance(self.registry_ca_file, str) or not self.registry_ca_file:
                raise ValueError("registry_ca_file must be a non-empty path string.")
            create_registry_ssl_context(self.registry_ca_file)
        if self.security is not None:
            from .security import validate_security

            for platform in normalize_platforms(self.platform):
                self.security = validate_security(self.security, platform)
        if self.stop_signal is not None:
            validate_stop_signal(self.stop_signal)
        if not isinstance(self.volumes, list):
            raise ValueError("volumes must be a list of absolute container paths.")
        self.volumes = list(dict.fromkeys(validate_volume(p) for p in self.volumes))
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
                    f"{', '.join(unsupported)}."
                )
            if self.generate_sbom:
                raise ValueError(
                    "SBOM generation is not supported for multi-platform builds; "
                    "build each platform separately to generate its SBOM."
                )
            if self.scan:
                raise ValueError(MULTI_PLATFORM_SCAN_ERROR)

    def target_platforms(self) -> List[str]:
        """Return the normalized, de-duplicated target platforms in request order."""
        return normalize_platforms(self.platform)

    def redact(self, message: str) -> str:
        """Keep build input values out of diagnostic output."""
        return redact_values(message, self._resolved_build_args.values())

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
