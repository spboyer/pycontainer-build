"""Framework detection and auto-configuration."""
from pathlib import Path
from typing import Optional, Tuple, List
import re
from .fs_utils import iter_files
from .project import default_include_paths


def detect_framework(
    context_dir: Path,
    include_paths=None,
    exclude_paths=None,
    *,
    exclude_defaults=True,
) -> Optional[Tuple[str, List[str], List[int]]]:
    """Detect web framework and return (name, entrypoint, exposed_ports).
    
    Returns:
        Tuple of (framework_name, entrypoint_cmd, ports) or None if not detected
    """
    ctx=Path(context_dir)
    paths = (
        default_include_paths(
            ctx,
            exclude_paths=exclude_paths,
            exclude_defaults=exclude_defaults,
        )
        if include_paths is None
        else include_paths
    )
    files = [
        path
        for path, _ in iter_files(
            ctx,
            paths,
            exclude_paths=exclude_paths,
            exclude_defaults=exclude_defaults,
        )
        if path.suffix == ".py"
    ]
    
    fastapi=_detect_fastapi(files, ctx)
    if fastapi: return fastapi
    
    flask=_detect_flask(files)
    if flask: return flask
    
    django=_detect_django(files, ctx)
    if django: return django
    
    return None

def _detect_fastapi(files: List[Path], ctx: Path) -> Optional[Tuple[str, List[str], List[int]]]:
    """Detect FastAPI applications."""
    for py in files:
        try:
            content=py.read_text()
            if "FastAPI" in content:
                app_module=_find_fastapi_app(py, ctx)
                if app_module:
                    return ("FastAPI", ["uvicorn", app_module, "--host", "0.0.0.0", "--port", "8000"], [8000])
        except: pass
    return None

def _find_fastapi_app(file_path: Path, ctx: Path) -> Optional[str]:
    """Find FastAPI app variable in file."""
    try:
        content=file_path.read_text()
        match=re.search(r'^\s*(\w+)\s*=\s*FastAPI\s*\(', content, re.MULTILINE)
        if match:
            app_var=match.group(1)
            rel=file_path.relative_to(ctx)
            module=".".join(rel.with_suffix("").parts)
            return f"{module}:{app_var}"
    except: pass
    return None

def _detect_flask(files: List[Path]) -> Optional[Tuple[str, List[str], List[int]]]:
    """Detect Flask applications."""
    for py in files:
        try:
            content=py.read_text()
            if "Flask" in content and re.search(
                r'^\s*\w+\s*=\s*Flask\s*\(', content, re.MULTILINE
            ):
                return ("Flask", ["flask", "run", "--host=0.0.0.0", "--port=5000"], [5000])
        except: pass
    return None

def _detect_django(
    files: List[Path], context_dir: Path
) -> Optional[Tuple[str, List[str], List[int]]]:
    """Detect Django applications."""
    for manage_py in files:
        if manage_py.name != "manage.py" or manage_py.parent != context_dir:
            continue
        try:
            content=manage_py.read_text()
            if "django" in content.lower():
                return ("Django", ["python", "manage.py", "runserver", "0.0.0.0:8000"], [8000])
        except: pass
    return None

def apply_framework_defaults(
    config,
    context_dir: Path,
    include_paths=None,
    exclude_paths=None,
    *,
    exclude_defaults=True,
):
    """Apply framework-specific defaults to build config."""
    detection=detect_framework(
        context_dir,
        include_paths,
        exclude_paths,
        exclude_defaults=exclude_defaults,
    )
    if not detection:
        return config
    
    framework, entrypoint, ports=detection
    
    if not config.entrypoint:
        config.entrypoint=entrypoint
    
    if not config.exposed_ports and ports:
        config.exposed_ports=ports
    
    if not config.labels:
        config.labels={}
    config.labels.setdefault("framework", framework.lower())
    
    return config
