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


class _FrameworkState:
    """Track generated fields across dataclass copies and serialization."""

    def __init__(self, values, generated):
        self.values = values
        self.generated = generated

    def __deepcopy__(self, memo):
        return {
            "ports_configured": _ports_configured(self.values["exposed_ports"]),
            "generated": {
                name: list(value)
                for name, value in self.values.items()
                if self.generated[name]
            },
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
    security: Optional[dict] = None
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
            return _FrameworkState(values, generated)
        return object.__getattribute__(self, name)

    def __post_init__(self):
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

        self.system_packages = validate_system_packages(self.system_packages)
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
        object.__setattr__(self, "_framework_initialized", True)

    def all_tags(self) -> List[str]:
        """Return every full image reference this build is tagged with."""
        from .tags import resolve_tags

        return resolve_tags(self.tag, self.tags, self.semver_tags, self.latest_tag)

    def has_exposed_ports(self) -> bool:
        """Return whether ports were explicitly configured or detected."""
        return _ports_configured(self.exposed_ports)
