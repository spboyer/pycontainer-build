import tomllib
from pathlib import Path

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
