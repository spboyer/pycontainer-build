import tomllib
from pathlib import Path
from typing import List, Tuple

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

def default_include_paths(context_dir):
    ctx=Path(context_dir)
    c=[]
    for name in ("src","app","package"):
        if (ctx/name).exists(): c.append(name)
    for f in ("pyproject.toml","requirements.txt","setup.cfg"):
        if (ctx/f).exists(): c.append(f)
    if not c: c.append(".")
    return c

def find_dependencies(context_dir: Path, requirements_file: str="requirements.txt") -> List[Tuple[Path, Path]]:
    """Find dependency files to include in dependency layer."""
    ctx=Path(context_dir); deps=[]
    
    venv_dirs=['venv','.venv','env']
    for venv in venv_dirs:
        venv_path=ctx/venv
        if venv_path.exists() and (venv_path/'lib').exists():
            for item in (venv_path/'lib').rglob('*'):
                if item.is_file() and 'site-packages' in str(item):
                    deps.append((item, item.relative_to(ctx)))
            break
    
    if not deps:
        req_path=ctx/requirements_file
        if req_path.exists():
            deps.append((req_path, Path(requirements_file)))
    
    return deps
