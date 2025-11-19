"""Framework detection and auto-configuration."""
from pathlib import Path
from typing import Optional, Tuple, List
import re

def detect_framework(context_dir: Path) -> Optional[Tuple[str, List[str], List[int]]]:
    """Detect web framework and return (name, entrypoint, exposed_ports).
    
    Returns:
        Tuple of (framework_name, entrypoint_cmd, ports) or None if not detected
    """
    ctx=Path(context_dir)
    
    fastapi=_detect_fastapi(ctx)
    if fastapi: return fastapi
    
    flask=_detect_flask(ctx)
    if flask: return flask
    
    django=_detect_django(ctx)
    if django: return django
    
    return None

def _detect_fastapi(ctx: Path) -> Optional[Tuple[str, List[str], List[int]]]:
    """Detect FastAPI applications."""
    for py in ctx.rglob("*.py"):
        try:
            content=py.read_text()
            if "from fastapi import FastAPI" in content or "from fastapi import" in content and "FastAPI" in content:
                app_module=_find_fastapi_app(py, ctx)
                if app_module:
                    return ("FastAPI", ["uvicorn", app_module, "--host", "0.0.0.0", "--port", "8000"], [8000])
        except: pass
    return None

def _find_fastapi_app(file_path: Path, ctx: Path) -> Optional[str]:
    """Find FastAPI app variable in file."""
    try:
        content=file_path.read_text()
        match=re.search(r'(\w+)\s*=\s*FastAPI\(', content)
        if match:
            app_var=match.group(1)
            rel=file_path.relative_to(ctx)
            module=str(rel.with_suffix('')).replace('/', '.')
            return f"{module}:{app_var}"
    except: pass
    
    rel=file_path.relative_to(ctx)
    module=str(rel.with_suffix('')).replace('/', '.')
    return f"{module}:app"

def _detect_flask(ctx: Path) -> Optional[Tuple[str, List[str], List[int]]]:
    """Detect Flask applications."""
    for py in ctx.rglob("*.py"):
        try:
            content=py.read_text()
            if "from flask import Flask" in content or ("from flask import" in content and "Flask" in content):
                return ("Flask", ["flask", "run", "--host=0.0.0.0", "--port=5000"], [5000])
        except: pass
    return None

def _detect_django(ctx: Path) -> Optional[Tuple[str, List[str], List[int]]]:
    """Detect Django applications."""
    manage_py=ctx/"manage.py"
    if manage_py.exists():
        try:
            content=manage_py.read_text()
            if "django" in content.lower():
                return ("Django", ["python", "manage.py", "runserver", "0.0.0.0:8000"], [8000])
        except: pass
    return None

def apply_framework_defaults(config, context_dir: Path):
    """Apply framework-specific defaults to build config."""
    detection=detect_framework(context_dir)
    if not detection:
        return config
    
    framework, entrypoint, ports=detection
    
    if not config.entrypoint:
        config.entrypoint=entrypoint
    
    if not config.exposed_ports and ports:
        config.exposed_ports=ports
    
    if not config.labels:
        config.labels={}
    config.labels["framework"]=framework.lower()
    
    return config
