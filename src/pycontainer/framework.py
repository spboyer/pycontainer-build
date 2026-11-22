"""Framework detection and auto-configuration."""
import ast
import keyword
import logging
import tokenize
from pathlib import Path
from typing import Optional, Tuple, List
import re
from .fs_utils import iter_files
from .project import declared_dependency_names, default_include_paths


logger = logging.getLogger(__name__)
ALL_INTERFACES = "0.0.0.0"
STREAMLIT_PORT = 8501
GRADIO_PORT = 7860
JUPYTER_PORT = 8888


class FrameworkDetectionError(ValueError):
    """A framework was found but its entrypoint cannot be chosen safely."""


def detect_framework(
    context_dir: Path,
    include_paths=None,
    exclude_paths=None,
    *,
    exclude_defaults=True,
    requirements_file="requirements.txt",
    files=None,
) -> Optional[Tuple[str, List[str], List[int]]]:
    """Detect a framework from selected files without importing project code."""
    ctx = Path(context_dir).resolve()
    if files is None:
        paths = (
            default_include_paths(
                ctx,
                exclude_paths=exclude_paths,
                exclude_defaults=exclude_defaults,
                requirements_file=requirements_file,
            )
            if include_paths is None
            else include_paths
        )
        files = (
            path
            for path, _ in iter_files(
                ctx,
                paths,
                exclude_paths=exclude_paths,
                exclude_defaults=exclude_defaults,
            )
        )
    selected = {}
    for item in files:
        path = Path(item)
        if not path.is_absolute():
            absolute = path.absolute()
            path = absolute if absolute.is_relative_to(ctx) else ctx / path
        if path.suffix not in (".py", ".ipynb"):
            continue
        if not path.is_relative_to(ctx) or not path.resolve().is_relative_to(ctx):
            raise ValueError(f"Selected source is outside the build context: {path}")
        if not path.is_file():
            raise FileNotFoundError(f"Selected source file not found: {path}")
        selected[path.relative_to(ctx).as_posix()] = path
    selected_files = [selected[name] for name in sorted(selected)]
    python_files = [path for path in selected_files if path.suffix == ".py"]
    notebooks = [path for path in selected_files if path.suffix == ".ipynb"]

    results = {}
    for detection in (
        _detect_fastapi(python_files, ctx),
        _detect_flask(python_files),
        _detect_django(python_files, ctx),
    ):
        if detection:
            results[detection[0]] = detection

    dependencies = declared_dependency_names(
        ctx,
        requirements_file,
        exclude_paths=exclude_paths,
        exclude_defaults=exclude_defaults,
    )
    scripts = {"Streamlit": [], "Gradio": [], "Celery": []}
    problems = {}
    unlaunched_gradio = []
    for path in python_files:
        if not ({"streamlit", "gradio", "celery"} & dependencies):
            break
        tree = _parse_source(path, ctx)
        if tree is None:
            continue
        streamlit, celery_modules, celery_classes, gradio_modules, gradio_classes = (
            _import_aliases(tree)
        )
        relative = path.relative_to(ctx).as_posix()
        if "streamlit" in dependencies and streamlit:
            scripts["Streamlit"].append(
                (
                    "Streamlit",
                    [
                        "streamlit", "run", relative,
                        f"--server.address={ALL_INTERFACES}",
                        f"--server.port={STREAMLIT_PORT}",
                    ],
                    [STREAMLIT_PORT],
                )
            )
        if "gradio" in dependencies:
            launched, constructed = _gradio_app(tree, gradio_modules, gradio_classes)
            if launched:
                scripts["Gradio"].append(
                    ("Gradio", ["python", relative], [GRADIO_PORT])
                )
            elif constructed and "FastAPI" not in results:
                unlaunched_gradio.append(relative)
        if "celery" in dependencies:
            variables, constructed = _celery_apps(tree, celery_modules, celery_classes)
            module = _module_name(path, ctx)
            if constructed and (not variables or module is None):
                problems["Celery"] = (
                    f"Cannot determine a Celery application entrypoint in {relative}; "
                    "specify an explicit entrypoint or command."
                )
            elif module:
                for variable in variables:
                    scripts["Celery"].append(
                        (
                            "Celery",
                            ["celery", "-A", f"{module}:{variable}", "worker", "--loglevel=INFO"],
                            [],
                        )
                    )

    if unlaunched_gradio and not scripts["Gradio"]:
        problems["Gradio"] = (
            f"Gradio application in {', '.join(unlaunched_gradio)} has no matching "
            "launch call; specify an explicit entrypoint or command."
        )

    for framework, candidates in scripts.items():
        if len(candidates) > 1:
            problems[framework] = (
                f"Multiple {framework} application candidates were found; "
                "specify an explicit entrypoint or command."
            )
        elif candidates and framework not in problems:
            results[framework] = candidates[0]

    if notebooks and {"jupyterlab", "notebook"} & dependencies:
        subcommand = "lab" if "jupyterlab" in dependencies else "notebook"
        results["Jupyter"] = (
            "Jupyter",
            [
                "jupyter", subcommand, f"--ip={ALL_INTERFACES}",
                f"--port={JUPYTER_PORT}", "--no-browser",
            ],
            [JUPYTER_PORT],
        )
    elif notebooks:
        logger.debug("Notebooks found but no jupyterlab or notebook dependency is declared.")

    priority = ("FastAPI", "Flask", "Django", "Streamlit", "Gradio", "Jupyter", "Celery")
    present = [name for name in priority if name in results or name in problems]
    if not present:
        return None
    chosen = present[0]
    if chosen in problems:
        raise FrameworkDetectionError(problems[chosen])
    if len(present) > 1:
        logger.warning(
            "Multiple frameworks detected (%s); %s takes precedence. "
            "Specify an explicit entrypoint or command to choose another.",
            ", ".join(present), chosen,
        )
    if "Celery" in present and chosen != "Celery":
        logger.warning(
            "Celery workers require a separate image or an explicit entrypoint."
        )
    for name in present[1:]:
        if name in problems:
            logger.warning("%s", problems[name])
    return results[chosen]


def _parse_source(path, context_dir):
    try:
        with tokenize.open(path) as source:
            return ast.parse(source.read(), filename=str(path))
    except (SyntaxError, UnicodeError) as error:
        logger.warning(
            "Cannot inspect %s: %s. Specify an explicit entrypoint if needed.",
            path.relative_to(context_dir).as_posix(), error,
        )
        return None


def _import_aliases(tree):
    streamlit = False
    celery_modules, celery_classes = set(), set()
    gradio_modules, gradio_classes = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "streamlit" or alias.name.startswith("streamlit."):
                    streamlit = True
                if alias.name == "celery" or alias.name.startswith("celery."):
                    celery_modules.add(alias.asname or "celery")
                if alias.name == "gradio" or alias.name.startswith("gradio."):
                    gradio_modules.add(alias.asname or "gradio")
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            if node.module == "streamlit" or node.module.startswith("streamlit."):
                streamlit = True
            for alias in node.names:
                if (
                    node.module == "celery" or node.module.startswith("celery.")
                ) and alias.name == "Celery":
                    celery_classes.add(alias.asname or alias.name)
                if (node.module == "gradio" or node.module.startswith("gradio.")) and (
                    alias.name in ("Blocks", "Interface")
                ):
                    gradio_classes.add(alias.asname or alias.name)
    return streamlit, celery_modules, celery_classes, gradio_modules, gradio_classes


def _is_constructor(node, modules, classes, attributes):
    if not isinstance(node, ast.Call):
        return False
    function = node.func
    root = function.value if isinstance(function, ast.Attribute) else None
    while isinstance(root, ast.Attribute):
        root = root.value
    return (
        isinstance(function, ast.Name) and function.id in classes
    ) or (
        isinstance(function, ast.Attribute)
        and isinstance(root, ast.Name)
        and root.id in modules
        and function.attr in attributes
    )


def _celery_apps(tree, modules, classes):
    def is_app(node):
        return _is_constructor(node, modules, classes, {"Celery"})

    variables = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and is_app(node.value):
            variables.extend(target.id for target in node.targets if isinstance(target, ast.Name))
        elif isinstance(node, ast.AnnAssign) and is_app(node.value):
            if isinstance(node.target, ast.Name):
                variables.append(node.target.id)
    return variables, any(is_app(node) for node in ast.walk(tree))


def _module_name(path, context_dir):
    parts = list(path.relative_to(context_dir).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    if not parts or any(not part.isidentifier() or keyword.iskeyword(part) for part in parts):
        return None
    return ".".join(parts)


def _gradio_app(tree, modules, classes):
    def is_app(node):
        return _is_constructor(node, modules, classes, {"Blocks", "Interface"})

    variables = set()
    constructed = False
    for node in ast.walk(tree):
        constructed = constructed or is_app(node)
        if isinstance(node, ast.Assign) and is_app(node.value):
            variables.update(target.id for target in node.targets if isinstance(target, ast.Name))
        elif isinstance(node, ast.AnnAssign) and is_app(node.value):
            if isinstance(node.target, ast.Name):
                variables.add(node.target.id)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if is_app(item.context_expr) and isinstance(item.optional_vars, ast.Name):
                    variables.add(item.optional_vars.id)

    def is_app_expression(node):
        if isinstance(node, ast.Name):
            return node.id in variables
        if is_app(node):
            return True
        return (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and is_app_expression(node.func.value)
        )

    launched = any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "launch"
        and is_app_expression(node.func.value)
        for node in ast.walk(tree)
    )
    return launched, constructed

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
    files=None,
):
    """Apply framework-specific defaults to build config."""
    try:
        detection=detect_framework(
            context_dir,
            include_paths,
            exclude_paths,
            exclude_defaults=exclude_defaults,
            requirements_file=config.requirements_file,
            files=files,
        )
    except FrameworkDetectionError as error:
        if config.entrypoint is None and config.cmd is None:
            raise
        logger.warning("%s Using the configured entrypoint or command.", error)
        return config
    if not detection:
        return config
    
    framework, entrypoint, ports=detection
    
    if config.entrypoint is None and config.cmd is None:
        config.entrypoint=entrypoint
        config.cmd=[]
    
    if config.exposed_ports is None:
        config.exposed_ports=ports
    if framework == "Gradio":
        config.env.setdefault("GRADIO_SERVER_NAME", ALL_INTERFACES)
    
    if not config.labels:
        config.labels={}
    config.labels["framework"]=framework.lower()
    logger.info("Detected framework: %s", framework.lower())
    
    return config
