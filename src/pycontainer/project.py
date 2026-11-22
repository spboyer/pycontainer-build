import configparser, logging, shlex, tomllib, re
from pathlib import Path, PureWindowsPath
from typing import List, Tuple
from urllib.parse import unquote
from .fs_utils import (
    DEFAULT_CONTEXT_EXCLUDES,
    VIRTUALENV_EXCLUDES,
    iter_files,
    path_is_excluded,
    relative_context_path,
)

logger = logging.getLogger(__name__)
_REQUIREMENT_NAME = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)")
_VCS_REQUIREMENT = re.compile(r"^(?:git|hg|svn|bzr)\+")


def requirement_name(requirement):
    requirement = requirement.strip()
    if _VCS_REQUIREMENT.match(requirement):
        egg = re.search(r"[#&]egg=([^&\s]+)", requirement)
        if not egg:
            return None
        requirement = unquote(egg.group(1))
    match = _REQUIREMENT_NAME.match(requirement)
    return re.sub(r"[-_.]+", "-", match.group(1)).lower() if match else None


def declared_dependency_names(
    context_dir,
    requirements_file="requirements.txt",
    *,
    exclude_paths=None,
    exclude_defaults=True,
):
    """Read declared runtime dependency names without resolving or installing them."""
    from .dependencies import (
        MAX_REQUIREMENTS_BYTES,
        _REQUIREMENT_VARIABLE,
        _expand_requirement_variables,
        _logical_requirement_lines,
    )

    ctx = Path(context_dir)
    root = ctx.resolve()
    excludes = list(DEFAULT_CONTEXT_EXCLUDES) if exclude_defaults else []
    if exclude_paths is not None:
        excludes.extend(exclude_paths)
    names = set()
    visited = set()
    total_bytes = 0

    def selected_file(relative, *, display=None):
        relative = relative_context_path(relative)
        if path_is_excluded(relative, excludes):
            return None
        path = ctx / relative
        if not path.is_file():
            return None
        if not path.resolve().is_relative_to(root):
            raise ValueError(f"Dependency file is outside the build context: {display or relative}")
        return path

    def read_requirements(relative, *, required=False, environment_path=False):
        nonlocal total_bytes
        relative = relative_context_path(relative)
        display = "<environment-based requirements file>" if environment_path else relative
        path = selected_file(relative, display=display)
        if path is None:
            if required and not path_is_excluded(relative, excludes):
                raise FileNotFoundError(f"Requirements file not found: {display}")
            return
        resolved = path.resolve()
        if resolved in visited:
            return
        visited.add(resolved)
        total_bytes += path.stat().st_size
        if total_bytes > MAX_REQUIREMENTS_BYTES:
            raise ValueError(
                "Requirements files exceed the combined "
                f"{MAX_REQUIREMENTS_BYTES}-byte limit."
            )
        for line_number, line in _logical_requirement_lines(path, display=display):
            expanded = _expand_requirement_variables(line)
            try:
                parts = shlex.split(expanded, comments=True)
            except ValueError as error:
                raise ValueError(
                    f"Invalid requirement syntax in {display} at line {line_number}"
                ) from error
            if not parts:
                continue
            first = parts[0]
            nested = None
            if first in ("-r", "--requirement"):
                if len(parts) != 2:
                    raise ValueError(
                        f"Invalid requirements include in {display} at line {line_number}"
                    )
                nested = parts[1]
            elif first.startswith("--requirement="):
                nested = first.partition("=")[2]
            elif first.startswith("-r") and len(first) > 2:
                nested = first[2:]
            if nested is not None:
                if _REQUIREMENT_VARIABLE.search(nested):
                    logger.warning(
                        "Cannot inspect an unresolved requirements include in %s at line %s",
                        display, line_number,
                    )
                    continue
                if "://" in nested:
                    logger.warning("Cannot inspect a remote requirements include in %s", display)
                else:
                    nested_path = Path(nested)
                    windows_path = PureWindowsPath(nested)
                    if (
                        not nested
                        or nested_path.is_absolute()
                        or windows_path.drive
                        or windows_path.root
                    ):
                        raise ValueError(
                            f"Invalid requirements include in {display} at line {line_number}"
                        )
                    resolved = (ctx / relative.parent / nested_path).resolve()
                    if not resolved.is_relative_to(root):
                        raise ValueError(
                            f"Requirements include in {display} escapes the build context"
                        )
                    read_requirements(
                        resolved.relative_to(root),
                        required=True,
                        environment_path=environment_path or expanded != line,
                    )
            elif not first.startswith("-") or first in ("-e", "--editable"):
                declaration = expanded
                if first in ("-e", "--editable"):
                    declaration = expanded.strip()[len(first):].strip()
                name = requirement_name(declaration)
                if name:
                    names.add(name)

    read_requirements(requirements_file)

    pyproject = selected_file("pyproject.toml")
    if pyproject:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        project = data.get("project", {})
        dependencies = project.get("dependencies", [])
        if not isinstance(dependencies, list) or not all(
            isinstance(item, str) for item in dependencies
        ):
            raise ValueError("[project].dependencies must be a list of strings")
        for dependency in dependencies:
            name = requirement_name(dependency)
            if name:
                names.add(name)
        poetry = data.get("tool", {}).get("poetry", {}).get("dependencies", {})
        if isinstance(poetry, dict):
            for name, value in poetry.items():
                if name != "python" and not (
                    isinstance(value, dict) and value.get("optional", False)
                ):
                    normalized = requirement_name(name)
                    if normalized:
                        names.add(normalized)

    setup_cfg = selected_file("setup.cfg")
    if setup_cfg:
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_string(setup_cfg.read_text(encoding="utf-8"))
        for dependency in parser.get("options", "install_requires", fallback="").splitlines():
            name = requirement_name(dependency)
            if name:
                names.add(name)
    return names


def detect_python_version(context_dir):
    """Detect Python version from pyproject.toml requires-python field."""
    ctx=Path(context_dir); py=ctx/'pyproject.toml'
    if py.exists():
        data=tomllib.loads(py.read_text())
        proj=data.get("project",{}); requires_py=proj.get("requires-python")
        if requires_py:
            match=re.search(r'(\d+\.\d+)', requires_py)
            if match: return match.group(1)
    return "3.11"

def detect_entrypoint(context_dir):
    ctx=Path(context_dir); py=ctx/'pyproject.toml'
    if py.exists():
        data=tomllib.loads(py.read_text())
        proj=data.get("project",{}); scripts=proj.get("scripts") or {}
        if scripts:
            name,target=next(iter(scripts.items()))
            if isinstance(target,str) and ":" not in target:
                return ["python","-m",target]
    return ["python","-m","app"]

def default_include_paths(
    context_dir,
    exclude_paths=None,
    *,
    exclude_defaults=True,
    requirements_file="requirements.txt",
):
    ctx=Path(context_dir)
    excludes=list(DEFAULT_CONTEXT_EXCLUDES) if exclude_defaults else []
    if exclude_paths is not None:
        excludes.extend(exclude_paths)
    c=[]
    for name in ("src","app","package"):
        if (ctx/name).exists() and not path_is_excluded(name, excludes):
            c.append(name)
    c.extend(
        path.name
        for path in sorted(ctx.glob("*.py"))
        if not path_is_excluded(path.name, excludes)
        and path.name not in c
    )
    for f in ("pyproject.toml","requirements.txt","setup.cfg"):
        if (ctx/f).exists() and not path_is_excluded(f, excludes):
            c.append(f)
    if not c:
        c.append(".")
    dependencies = declared_dependency_names(
        ctx, requirements_file, exclude_paths=excludes, exclude_defaults=False
    )
    include_notebooks = bool({"jupyterlab", "notebook"} & dependencies)
    if include_notebooks or logger.isEnabledFor(logging.DEBUG):
        for path, relative in iter_files(
            ctx, ["."], exclude_paths=excludes, exclude_defaults=False
        ):
            if path.suffix != ".ipynb":
                continue
            if include_notebooks and c != ["."]:
                c.append(relative.as_posix())
            elif not include_notebooks:
                logger.debug(
                    "Notebooks found but no jupyterlab or notebook dependency is declared."
                )
                break
    return c

def find_dependencies(
    context_dir: Path,
    requirements_file: str="requirements.txt",
    *,
    exclude_paths=None,
    exclude_defaults=True,
) -> List[Tuple[Path, Path]]:
    """Find dependency files to include in dependency layer."""
    ctx=Path(context_dir); deps=[]
    excludes = (
        [
            exclude
            for exclude in DEFAULT_CONTEXT_EXCLUDES
            if exclude not in VIRTUALENV_EXCLUDES
        ]
        if exclude_defaults
        else []
    )
    if exclude_paths is not None:
        excludes.extend(exclude_paths)
    
    venv_dirs=['venv','.venv','env']
    for venv in venv_dirs:
        venv_path=ctx/venv
        if venv_path.exists():
            for item, relative in iter_files(
                ctx,
                [venv],
                exclude_paths=excludes,
                exclude_defaults=False,
            ):
                if (
                    "site-packages" in relative.parts
                    and relative.parts.index("site-packages") > 0
                    and "__pycache__" not in relative.parts
                    and relative.suffix != ".pyc"
                ):
                    deps.append((item, relative))
            if deps:
                break
    
    if not deps:
        requirements_relative=relative_context_path(requirements_file)
        req_path=ctx/requirements_relative
        if req_path.exists():
            deps.append((req_path, requirements_relative))
    
    return deps
