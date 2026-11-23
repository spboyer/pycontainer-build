"""Collect virtualenv root requirements and reject unreadable marker sources."""
import re
import tomllib
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

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


def host_environments(
    environment: dict, shared_python_version: bool, host_os_name: Optional[str]
) -> List[dict]:
    """Marker environments of the plausible hosts that created the virtualenv."""
    shared = _SHARED_VARIABLES if shared_python_version else _SHARED_VARIABLES - {"python_version"}
    return [
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


def _string_list(value) -> List[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _root_list(value, source: str) -> List[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(
            f"Cannot verify {source}: unsupported dependency declarations; "
            "use a requirements file instead."
        )
    return value


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
    roots.append(("pyproject [project].dependencies", True, _root_list(
        project.get("dependencies", []), "pyproject [project].dependencies"
    )))
    optional = project.get("optional-dependencies", {})
    for extra, lines in (optional.items() if isinstance(optional, dict) else []):
        label = f"pyproject optional-dependencies {extra!r}"
        roots.append((label, False, _root_list(lines, label)))
    groups = data.get("dependency-groups", {})
    for group, lines in (groups.items() if isinstance(groups, dict) else []):
        label = f"pyproject dependency group {group!r}"
        roots.append((label, False, _root_list(lines, label)))
    tool = data.get("tool", {}) if isinstance(data.get("tool"), dict) else {}
    uv = tool.get("uv", {}) if isinstance(tool.get("uv"), dict) else {}
    roots.append(("tool.uv dev-dependencies", False, _root_list(
        uv.get("dev-dependencies", []), "tool.uv dev-dependencies"
    )))
    for table in _UNPARSED_TOOL_TABLES:
        if table in tool and _HOST_MARKER_TEXT.search(repr(tool[table])):
            raise ValueError(
                f"pyproject [tool.{table}] may declare environment markers that cannot "
                "be evaluated for a virtualenv; use a requirements file instead."
            )
    _check_dynamic_dependencies(context_dir, project, tool)
    return roots
