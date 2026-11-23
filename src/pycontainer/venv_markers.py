"""Verify that a host-resolved virtualenv is complete for the target's markers.

A virtualenv was resolved by pip on whichever host created it, so PEP 508
markers in root requirements and in installed ``Requires-Dist`` metadata were
evaluated for that host. This module re-evaluates them for the target image and
reports requirements the target needs but the virtualenv lacks, or whose need
cannot be established. Host-only extra packages are tolerated.
"""
import re
import tomllib
from email.parser import Parser
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

from ._pip_target import TargetMarkerError, _variables

# Marker variables whose value can differ between the venv host and the target.
_HOST_MARKER_TEXT = re.compile(
    r"\b(?:sys_platform|platform_machine|platform_system|platform_release|"
    r"platform_version|os_name|implementation_name|implementation_version|"
    r"platform_python_implementation|python_full_version)\b|['\"]platform['\"]\s*[:=]"
)
_UNPARSED_TOOL_TABLES = ("poetry", "pdm", "hatch", "setuptools", "flit")
# Values a staged virtualenv shares with the target: it must be CPython of the target minor.
_SHARED_VARIABLES = {
    "implementation_name",
    "platform_python_implementation",
    "python_version",
}
# Plausible hosts that created the virtualenv; an unrequested extra must be provable on each.
_VENV_HOSTS = (
    ("win32", "Windows", "nt", ("AMD64", "ARM64", "x86")),
    ("linux", "Linux", "posix", ("x86_64", "aarch64")),
    ("darwin", "Darwin", "posix", ("x86_64", "arm64")),
)

_POSIX_LIB_DIR = re.compile(r"python\d+\.\d+t?")

RootGroup = Tuple[str, bool, List[str]]


def venv_layout_os(site_root: Tuple[str, ...]) -> Optional[str]:
    """Infer the creating host's os_name from the site-packages layout, if recognizable."""
    if (
        len(site_root) >= 3
        and _POSIX_LIB_DIR.fullmatch(site_root[-2])
        and site_root[-3] in ("lib", "lib64")
    ):
        return "posix"
    if len(site_root) >= 2 and site_root[-2] == "Lib":
        return "nt"
    return None


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


class _Markers:
    def __init__(self, environment: dict):
        self.environment = environment
        try:
            from pip._vendor.packaging.requirements import (
                InvalidRequirement,
                Requirement,
            )
        except ImportError:
            try:
                from packaging.requirements import InvalidRequirement, Requirement
            except ImportError as exc:
                raise ValueError(
                    "Cannot evaluate virtualenv dependency markers for the target: "
                    "the builder environment provides neither pip nor packaging."
                ) from exc
        self._requirement = Requirement
        self._invalid = InvalidRequirement
        machine = environment["platform_machine"]
        other = "aarch64" if machine == "x86_64" else "x86_64"
        matching = Requirement(f'x; platform_machine == "{machine}"')
        opposite = Requirement(f'x; platform_machine == "{other}"')
        if (
            self.variables(matching) != {"platform_machine"}
            or self.state(matching, "") is not True
            or self.state(opposite, "") is not False
        ):
            raise ValueError("Target marker evaluation self-test failed.")

    def parse(self, text: str, source: str):
        try:
            return self._requirement(text)
        except self._invalid as exc:
            raise ValueError(f"Cannot parse requirement {text!r} in {source}.") from exc

    def variables(self, requirement) -> Set[str]:
        if requirement.marker is None:
            return set()
        try:
            return set(_variables(requirement.marker._markers))
        except (AttributeError, TypeError, TargetMarkerError) as exc:
            raise ValueError(f"Cannot inspect marker of {requirement}.") from exc

    def marker_extras(self, requirement) -> Set[str]:
        return {_normalize(extra) for extra in self._raw_extras(requirement)}

    def _raw_extras(self, requirement) -> Set[str]:
        extras = set()
        pending = [getattr(requirement.marker, "_markers", [])]
        while pending:
            for item in pending.pop():
                if isinstance(item, list):
                    pending.append(item)
                elif isinstance(item, tuple) and len(item) == 3:
                    names = {type(node).__name__: node.value for node in item[::2]}
                    if names.get("Variable") == "extra" and "Value" in names:
                        extras.add(names["Value"])
        return extras

    def state(self, requirement, extra: str, environment: Optional[dict] = None) -> Optional[bool]:
        """True/False in the environment (default: target), or None when a value is unknown."""
        if requirement.marker is None:
            return True
        environment = self.environment if environment is None else environment
        if self.variables(requirement) - set(environment) - {"extra"}:
            return None
        # Older vendored packaging compares extra names verbatim, so also try the
        # marker's own spellings of this normalized extra.
        spellings = {extra} | {
            raw for raw in self._raw_extras(requirement) if extra and _normalize(raw) == extra
        }
        return any(
            bool(requirement.marker.evaluate({**environment, "extra": spelling}))
            for spelling in sorted(spellings)
        )


def _egg_requirements(requires: Path) -> Tuple[List[str], Set[str]]:
    lines, extras, section = [], set(), ""
    for raw in requires.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        extra, _, marker = section.partition(":")
        requirement, _, own_marker = line.partition(";")
        conditions = [f"({value})" for value in (own_marker.strip(), marker) if value]
        if extra:
            extras.add(_normalize(extra))
            conditions.append(f'extra == "{extra}"')
        lines.append(requirement + ("; " + " and ".join(conditions) if conditions else ""))
    return lines, extras


def _installed(site_packages: Path) -> Dict[str, Tuple[str, List[str], Set[str]]]:
    dists = {}
    sources = [(path, None) for path in site_packages.glob("*.dist-info/METADATA")]
    sources.extend(
        (path / "PKG-INFO", path / "requires.txt")
        for path in site_packages.glob("*.egg-info")
        if path.is_dir()
    )
    sources.extend((path, None) for path in site_packages.glob("*.egg-info") if path.is_file())
    for metadata, requires in sorted(sources):
        if not metadata.is_file():
            continue
        with metadata.open(encoding="utf-8", errors="replace") as stream:
            message = Parser().parse(stream, headersonly=True)
        name = (message.get("Name") or "").strip()
        if not name:
            continue
        lines = list(message.get_all("Requires-Dist") or [])
        extras = {_normalize(value) for value in message.get_all("Provides-Extra") or []}
        if requires is not None and requires.is_file():
            egg_lines, egg_extras = _egg_requirements(requires)
            lines.extend(egg_lines)
            extras |= egg_extras
        dists[_normalize(name)] = (name, lines, extras)
    return dists


def _string_list(value) -> List[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _nested_strings(value) -> List[str]:
    if isinstance(value, str):
        return [value]
    items = value.values() if isinstance(value, dict) else value if isinstance(value, list) else []
    return [text for item in items for text in _nested_strings(item)]


def _table(value, *keys) -> dict:
    for key in keys:
        value = value.get(key) if isinstance(value, dict) else None
    return value if isinstance(value, dict) else {}


def _check_dynamic_dependencies(context_dir: Path, project: dict, tool: dict) -> None:
    """Fail closed unless file-based dynamic dependencies are provably marker-free."""
    if not set(_string_list(project.get("dynamic"))) & {"dependencies", "optional-dependencies"}:
        return
    dynamic = _table(tool, "setuptools", "dynamic")
    files = _nested_strings(
        [dynamic.get("dependencies"), dynamic.get("optional-dependencies")]
    ) + _nested_strings(_table(tool, "hatch", "metadata", "hooks", "requirements_txt"))
    if not files:
        raise ValueError(
            "pyproject declares dynamic dependencies that cannot be read to evaluate "
            "their environment markers for a virtualenv; use a requirements file instead."
        )
    root = context_dir.resolve()
    for name in files:
        path = (context_dir / name).resolve()
        inside = path.is_relative_to(root) and path.is_file()
        text = path.read_text(errors="replace") if inside else ""
        if (
            not inside
            or _HOST_MARKER_TEXT.search(text)
            or re.search(r"(?m)^\s*(?:-[rc]|--(?:requirement|constraint))", text)
        ):
            raise ValueError(
                f"Dynamic dependency file {name!r} may declare environment markers that "
                "cannot be evaluated for a virtualenv; use a requirements file instead."
            )


def declared_roots(context_dir: Path, requirement_lines: Iterable[str] = ()) -> List[RootGroup]:
    """Root requirement groups, failing closed on sources whose markers are not parsed."""
    roots = [("requirements file", True, list(requirement_lines))]
    for name in ("setup.py", "setup.cfg", "Pipfile"):
        path = context_dir / name
        if path.is_file() and _HOST_MARKER_TEXT.search(path.read_text(errors="replace")):
            raise ValueError(
                f"{name} may declare environment markers that cannot be evaluated "
                "for a virtualenv; use a requirements file instead."
            )
    pyproject = context_dir / "pyproject.toml"
    if not pyproject.is_file():
        return roots
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"Cannot read dependency declarations from {pyproject}.") from exc
    project = data.get("project", {}) if isinstance(data.get("project"), dict) else {}
    roots.append(("pyproject [project].dependencies", True, _string_list(project.get("dependencies"))))
    optional = project.get("optional-dependencies", {})
    for extra, lines in (optional.items() if isinstance(optional, dict) else []):
        roots.append((f"pyproject optional-dependencies {extra!r}", False, _string_list(lines)))
    groups = data.get("dependency-groups", {})
    for group, lines in (groups.items() if isinstance(groups, dict) else []):
        roots.append((f"pyproject dependency group {group!r}", False, _string_list(lines)))
    tool = data.get("tool", {}) if isinstance(data.get("tool"), dict) else {}
    uv = tool.get("uv", {}) if isinstance(tool.get("uv"), dict) else {}
    roots.append(("tool.uv dev-dependencies", False, _string_list(uv.get("dev-dependencies"))))
    for table in _UNPARSED_TOOL_TABLES:
        if table in tool and _HOST_MARKER_TEXT.search(repr(tool[table])):
            raise ValueError(
                f"pyproject [tool.{table}] may declare environment markers that cannot "
                "be evaluated for a virtualenv; use a requirements file instead."
            )
    _check_dynamic_dependencies(context_dir, project, tool)
    return roots


def verify_virtualenv_markers(
    site_packages: Path,
    environment: dict,
    roots: List[RootGroup],
    shared_python_version: bool = True,
    host_os_name: Optional[str] = None,
) -> List[str]:
    """Return why the staged virtualenv cannot be shown complete for the target."""
    dists = _installed(site_packages)
    if not any(";" in line for _, lines, _ in dists.values() for line in lines) and not any(
        ";" in line for _, _, lines in roots for line in lines
    ):
        return []
    markers = _Markers(environment)
    shared = _SHARED_VARIABLES if shared_python_version else _SHARED_VARIABLES - {"python_version"}
    hosts = [
        {
            **{key: environment[key] for key in shared if key in environment},
            "sys_platform": sys_platform,
            "platform_system": system,
            "os_name": os_name,
            "platform_machine": machine,
        }
        for sys_platform, system, os_name, machines in _VENV_HOSTS
        if host_os_name in (None, os_name)
        for machine in machines
    ]

    def unrequested(missing, extra: str) -> bool:
        """Whether every plausible venv host would have installed one of `missing` for it."""
        return bool(missing) and all(
            any(
                markers.state(requirement, extra, host) is True
                and (not extra or markers.state(requirement, "", host) is False)
                for requirement in missing
            )
            for host in hosts
        )

    definite: Dict[str, Set[str]] = {name: set() for name in dists}
    errors: List[str] = []
    queue: List[str] = []

    def check(owner, requirements, definite_extras, possible_extras, possible_label):
        for requirement in requirements:
            name = _normalize(requirement.name)
            states = [markers.state(requirement, extra) for extra in definite_extras]
            if name in dists:
                new_extras = {_normalize(extra) for extra in requirement.extras} - definite[name]
                if True in states and new_extras:
                    definite[name] |= new_extras
                    queue.append(name)
                continue
            if True in states:
                errors.append(f"{owner} requires {requirement} on the target, but it is not installed")
            elif None in states:
                errors.append(f"{owner}: {requirement} uses a marker value unknown for the target")
            else:
                for extra in possible_extras:
                    if markers.state(requirement, extra) is not False:
                        errors.append(
                            f"{owner}: cannot tell whether {possible_label(extra)} was "
                            f"requested, and its target requirement {requirement} is not installed"
                        )
                        break

    for label, required, lines in roots:
        requirements = []
        for line in lines:
            try:
                requirements.append(markers.parse(line, label))
            except ValueError:
                if ";" in line:
                    raise
        if required:
            check(label, requirements, [""], [], None)
        elif not unrequested(
            [item for item in requirements if _normalize(item.name) not in dists], ""
        ):
            check(label, requirements, [], [""], lambda _extra, label=label: label)

    parsed = {
        name: [markers.parse(line, f"{display} metadata") for line in lines]
        for name, (display, lines, _) in dists.items()
    }
    queue.extend(sorted(dists))
    while queue:
        name = queue.pop(0)
        display, _, provided = dists[name]
        requirements = parsed[name]
        possible = set(provided)
        for requirement in requirements:
            possible |= markers.marker_extras(requirement)
        possible -= definite[name]
        missing = [item for item in requirements if _normalize(item.name) not in dists]
        possible = {extra for extra in possible if not unrequested(missing, extra)}
        check(
            display,
            requirements,
            ["", *sorted(definite[name])],
            sorted(possible),
            lambda extra, display=display: f"extra {extra!r} of {display}",
        )
    return list(dict.fromkeys(errors))
