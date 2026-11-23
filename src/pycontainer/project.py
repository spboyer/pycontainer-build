import tomllib, re
from pathlib import Path
from typing import List, Tuple
from .fs_utils import (
    DEFAULT_CONTEXT_EXCLUDES,
    VIRTUALENV_EXCLUDES,
    is_conda_prefix,
    iter_files,
    path_is_excluded,
    relative_context_path,
)

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

def detect_entrypoint(context_dir, *, files=None):
    ctx=Path(context_dir); py=ctx/'pyproject.toml'
    selected=None if files is None else {Path(path).as_posix() for path in files}
    if (selected is None or "pyproject.toml" in selected) and py.exists():
        data=tomllib.loads(py.read_text())
        proj=data.get("project",{}); scripts=proj.get("scripts") or {}
        if scripts:
            name,target=next(iter(scripts.items()))
            if isinstance(target,str) and ":" not in target:
                parts=target.split(".")
                if selected is not None and all(part.isidentifier() for part in parts):
                    module=Path(*parts)
                    candidates=(module.with_suffix(".py"), module/"__main__.py")
                    existing=[path for path in candidates if (ctx/path).is_file()]
                    if existing and not any(path.as_posix() in selected for path in existing):
                        raise ValueError(
                            f"Entrypoint module {target!r} exists in the build context "
                            "but is not selected; include it or specify an explicit entrypoint."
                        )
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
        if venv_path.exists() and not is_conda_prefix(venv_path):
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
