import tomllib, re, os
from pathlib import Path
from typing import List, Tuple
from .fs_utils import (
    DEFAULT_CONTEXT_EXCLUDES,
    VIRTUALENV_EXCLUDES,
    iter_files,
    path_is_excluded,
    relative_context_path,
    conda_prefix,
    _raise_walk_error,
)

CONDA_FILES = ("environment.yml", "environment.yaml", "conda-lock.yml")


def detect_conda_inputs(context_dir, *, exclude_paths=(), exclude_defaults=True):
    """Detect supported Conda signal names, without parsing environment files."""
    ctx = Path(context_dir)
    excludes = [
        path for path in DEFAULT_CONTEXT_EXCLUDES if path not in VIRTUALENV_EXCLUDES
    ] if exclude_defaults else []
    excludes.extend(exclude_paths)
    signals = []
    for root, directories, filenames in os.walk(ctx, onerror=_raise_walk_error):
        root_path = Path(root)
        relative = root_path.relative_to(ctx)
        metadata = relative / "conda-meta"
        if "conda-meta" in directories:
            if not path_is_excluded(metadata, excludes):
                signals.append(metadata)
            directories[:] = []
        else:
            directories[:] = sorted(
                directory for directory in directories
                if not path_is_excluded(relative / directory, excludes)
            )
        for name in CONDA_FILES:
            path = relative / name
            if name in filenames and not path_is_excluded(path, excludes):
                signals.append(path)
    return sorted(signals, key=lambda path: path.as_posix())

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
    if not c: c.append(".")
    return c

def find_dependencies(
    context_dir: Path,
    requirements_file: str="requirements.txt",
    *,
    exclude_paths=None,
    exclude_defaults=True,
    prefer_requirements=False,
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
    requirements_relative=relative_context_path(requirements_file)
    req_path=ctx/requirements_relative
    if prefer_requirements and req_path.is_file():
        return [(req_path, requirements_relative)]
    
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
        if req_path.is_file() and conda_prefix(ctx, requirements_relative) is None:
            deps.append((req_path, requirements_relative))
    
    return deps
