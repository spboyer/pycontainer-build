from copy import deepcopy
import posixpath
import re
import unicodedata
import urllib.parse
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Union

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
        if not isinstance(entry, str) or not _REGISTRY_HOST.fullmatch(entry):
            raise ValueError(
                f"Invalid insecure registry {entry!r}: expected host[:port] without "
                "a scheme, path, or credentials."
            )
        try:
            port = urllib.parse.urlsplit(f"//{entry}").port
        except ValueError:
            port = 0
        if port == 0:
            raise ValueError(f"Invalid insecure registry port in {entry!r}.")
        if entry.lower() not in normalized:
            normalized.append(entry.lower())
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


def _track_list_mutation(method):
    def tracked(self, *args, **kwargs):
        result = method(self, *args, **kwargs)
        self.specified = True
        return result
    return tracked


class _TrackedList(list):
    def __init__(self, values=()):
        super().__init__(values)
        self.specified = False

    def __deepcopy__(self, memo):
        copied = type(self)()
        memo[id(self)] = copied
        list.extend(copied, (deepcopy(value, memo) for value in self))
        copied.__dict__.update(deepcopy(self.__dict__, memo))
        return copied

    append = _track_list_mutation(list.append)
    clear = _track_list_mutation(list.clear)
    extend = _track_list_mutation(list.extend)
    insert = _track_list_mutation(list.insert)
    pop = _track_list_mutation(list.pop)
    remove = _track_list_mutation(list.remove)
    reverse = _track_list_mutation(list.reverse)
    sort = _track_list_mutation(list.sort)
    __delitem__ = _track_list_mutation(list.__delitem__)
    __iadd__ = _track_list_mutation(list.__iadd__)
    __imul__ = _track_list_mutation(list.__imul__)
    __setitem__ = _track_list_mutation(list.__setitem__)


class _DefaultPorts(_TrackedList):
    """Keep the mutable list API while distinguishing an untouched default."""

    def __init__(self, values=()):
        super().__init__(values)
        self.specified = bool(self)


class _GeneratedList(_TrackedList):
    def __init__(self, values=(), *, generated=False):
        super().__init__(values)
        self.generated = generated
        self.snapshot = tuple(self)

    @property
    def untouched(self):
        return self.generated and not self.specified and tuple(self) == self.snapshot


class _GeneratedLabels(dict):
    def __init__(self, values=(), *, generated=False):
        super().__init__(values)
        self.generated = generated
        self.snapshot = self.get("framework")
        self.specified = False

    def __deepcopy__(self, memo):
        copied = type(self)(deepcopy(dict(self), memo), generated=self.generated)
        memo[id(self)] = copied
        copied.snapshot = self.snapshot
        copied.specified = self.specified
        return copied

    @property
    def untouched(self):
        return (
            self.generated and not self.specified
            and self.get("framework") == self.snapshot
        )

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        if key == "framework":
            self.specified = True

    def __delitem__(self, key):
        super().__delitem__(key)
        if key == "framework":
            self.specified = True

    def update(self, *args, **kwargs):
        values = dict(*args, **kwargs)
        super().update(values)
        if "framework" in values:
            self.specified = True

    def clear(self):
        if "framework" in self:
            self.specified = True
        super().clear()

    def pop(self, key, *default):
        present = key in self
        result = super().pop(key, *default)
        if key == "framework" and present:
            self.specified = True
        return result

    def popitem(self):
        key, value = super().popitem()
        if key == "framework":
            self.specified = True
        return key, value

    def setdefault(self, key, default=None):
        if key not in self:
            self[key] = default
        return self[key]

    def __ior__(self, other):
        self.update(other)
        return self


_FRAMEWORK_FIELDS = {
    "entrypoint": "_framework_generated_entrypoint",
    "cmd": "_framework_generated_cmd",
    "exposed_ports": "_framework_generated_ports",
}


def _ports_configured(ports):
    return ports is not None and (
        not isinstance(ports, _DefaultPorts) or ports.specified
    )


def _is_generated_field(config, name):
    value = getattr(config, name)
    previous = getattr(config, _FRAMEWORK_FIELDS[name], None)
    return (
        isinstance(value, _GeneratedList) and value.untouched
        and (previous is None or value is previous)
    )


def _is_generated_label(config):
    labels = config.labels
    previous = getattr(config, "_framework_generated_labels", None)
    return (
        isinstance(labels, _GeneratedLabels) and labels.untouched
        and (previous is None or labels is previous)
    )


class _FrameworkState:
    """Track generated fields across dataclass copies and serialization."""

    def __init__(self, values, generated, labels, generated_label):
        self.values = values
        self.generated = generated
        self.labels = labels
        self.generated_label = generated_label

    def __deepcopy__(self, memo):
        return {
            "ports_configured": _ports_configured(self.values["exposed_ports"]),
            "generated": {
                name: list(value)
                for name, value in self.values.items()
                if self.generated[name]
            },
            "generated_label": self.labels["framework"] if self.generated_label else None,
        }


@dataclass
class BuildConfig:
    tag: str = "local/test:latest"
    base_image: str = "python:3.11-slim"
    context_dir: str = "."
    workdir: str = "/app"
    entrypoint: Optional[List[str]] = None
    env: Dict[str, str] = field(default_factory=dict)
    exposed_ports: Optional[List[int]] = None
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
    security: Optional[dict] = None
    stop_signal: Optional[str] = None
    volumes: List[str] = field(default_factory=list)
    release_notes_file: Optional[str] = None
    release_notes_label: str = DEFAULT_RELEASE_NOTES_LABEL
    _framework_default_env: Dict[str, str] = field(default_factory=dict, repr=False, compare=False)
    _framework_generated_state: Optional[Union[Dict[str, object], _FrameworkState]] = field(
        default=None, repr=False, compare=False
    )

    def __setattr__(self, name, value):
        if (
            name == "exposed_ports" and isinstance(value, _DefaultPorts)
            and self.__dict__.get("_framework_initialized", False)
        ):
            value = list(value)
        object.__setattr__(self, name, value)

    def __getattribute__(self, name):
        if name == "_framework_generated_state":
            values = {
                field_name: object.__getattribute__(self, field_name)
                for field_name in _FRAMEWORK_FIELDS
            }
            generated = {
                field_name: _is_generated_field(self, field_name)
                for field_name in _FRAMEWORK_FIELDS
            }
            return _FrameworkState(values, generated, self.labels, _is_generated_label(self))
        return object.__getattribute__(self, name)

    def __post_init__(self):
        from .release_notes import validate_release_notes_settings
        from .system_packages import validate_system_packages

        generated_state = self.__dict__["_framework_generated_state"]
        configured = None
        if isinstance(generated_state, _FrameworkState):
            if self.exposed_ports is generated_state.values["exposed_ports"]:
                configured = _ports_configured(self.exposed_ports)
            else:
                configured = self.exposed_ports is not None
            for name, previous in generated_state.values.items():
                value = getattr(self, name)
                if isinstance(value, _GeneratedList):
                    if value is not previous or not generated_state.generated[name]:
                        object.__setattr__(self, name, list(value))
                    else:
                        object.__setattr__(self, _FRAMEWORK_FIELDS[name], value)
        elif isinstance(generated_state, dict):
            configured = generated_state.get("ports_configured")
            snapshots = generated_state.get("generated")
            if not isinstance(configured, bool) or not isinstance(snapshots, dict):
                raise ValueError("Invalid serialized framework defaults.")
            for name in _FRAMEWORK_FIELDS:
                snapshot = snapshots.get(name)
                value = getattr(self, name)
                if isinstance(snapshot, list) and isinstance(value, list) and value == snapshot:
                    value = _GeneratedList(value, generated=True)
                    object.__setattr__(self, name, value)
                    object.__setattr__(self, _FRAMEWORK_FIELDS[name], value)
        elif generated_state is not None:
            raise ValueError("Invalid serialized framework defaults.")
        else:
            configured = self.exposed_ports is not None
            for name in _FRAMEWORK_FIELDS:
                value = getattr(self, name)
                if isinstance(value, _GeneratedList):
                    object.__setattr__(self, name, list(value))
            self._framework_default_env = {}

        if isinstance(generated_state, _FrameworkState):
            if (
                isinstance(self.labels, _GeneratedLabels)
                and self.labels is generated_state.labels
                and generated_state.generated_label
            ):
                object.__setattr__(self, "_framework_generated_labels", self.labels)
            elif isinstance(self.labels, _GeneratedLabels):
                self.labels = dict(self.labels)
        elif isinstance(generated_state, dict):
            label = generated_state.get("generated_label")
            if (
                isinstance(label, str) and isinstance(self.labels, dict)
                and self.labels.get("framework") == label
            ):
                self.labels = _GeneratedLabels(self.labels, generated=True)
                object.__setattr__(self, "_framework_generated_labels", self.labels)
            elif isinstance(self.labels, _GeneratedLabels):
                self.labels = dict(self.labels)
        elif isinstance(self.labels, _GeneratedLabels):
            self.labels = dict(self.labels)

        ports = self.exposed_ports
        if configured is None:
            configured = _ports_configured(ports)
        elif not configured and ports:
            configured = True
        if ports is None or (
            not configured and not isinstance(ports, _DefaultPorts)
        ):
            ports = _DefaultPorts()
            object.__setattr__(self, "exposed_ports", ports)
        elif configured and isinstance(ports, _DefaultPorts):
            ports = list(ports)
            object.__setattr__(self, "exposed_ports", ports)
        if isinstance(ports, _DefaultPorts):
            ports.specified = configured

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
        object.__setattr__(self, "_framework_initialized", True)

    def target_platforms(self) -> List[str]:
        """Return the normalized, de-duplicated target platforms in request order."""
        return normalize_platforms(self.platform)

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

    def has_exposed_ports(self) -> bool:
        """Return whether ports were explicitly configured or detected."""
        return _ports_configured(self.exposed_ports)
