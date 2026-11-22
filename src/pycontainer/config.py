from copy import deepcopy
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Union

SBOM_FORMATS = ("spdx", "cyclonedx")


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


class _PortState:
    """Track list identity for dataclass copies while serializing the intent."""

    def __init__(self, ports, configured):
        self.ports = ports
        self.configured = configured

    def __bool__(self):
        return self.configured

    def __deepcopy__(self, memo):
        return self.configured


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
    _exposed_ports_configured: Optional[Union[bool, _PortState]] = field(
        default=None, repr=False, compare=False
    )

    def __getattribute__(self, name):
        if name == "_exposed_ports_configured":
            ports = object.__getattribute__(self, "exposed_ports")
            configured = ports is not None and (
                not isinstance(ports, _DefaultPorts) or ports.specified
            )
            return _PortState(ports, configured)
        return object.__getattribute__(self, name)

    def __post_init__(self):
        from .system_packages import validate_system_packages

        ports = self.exposed_ports
        configured = self.__dict__["_exposed_ports_configured"]
        if isinstance(configured, _PortState):
            configured = configured.configured if ports is configured.ports else None
        if configured is None:
            configured = ports is not None and (
                not isinstance(ports, _DefaultPorts) or ports.specified
            )
        elif not configured and ports:
            configured = True
        if not configured and ports is not None and not isinstance(ports, _DefaultPorts):
            ports = _DefaultPorts()
            object.__setattr__(self, "exposed_ports", ports)
        if isinstance(ports, _DefaultPorts):
            ports.specified = configured
        object.__setattr__(self, "_exposed_ports_configured", configured)

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
        return bool(self._exposed_ports_configured)
