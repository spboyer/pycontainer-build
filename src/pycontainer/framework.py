"""Framework detection and auto-configuration."""
import ast
import keyword
import logging
import tokenize
from dataclasses import dataclass
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
FRAMEWORK_PRIORITY = ("FastAPI", "Flask", "Django", "Streamlit", "Gradio", "Jupyter", "Celery")
FRAMEWORK_PORTS = {
    "FastAPI": [8000],
    "Flask": [5000],
    "Django": [8000],
    "Streamlit": [STREAMLIT_PORT],
    "Gradio": [GRADIO_PORT],
    "Jupyter": [JUPYTER_PORT],
    "Celery": [],
}


class FrameworkDetectionError(ValueError):
    """A framework was found but its entrypoint cannot be chosen safely."""


class FrameworkPortDetectionError(FrameworkDetectionError):
    """A framework listener port cannot be chosen safely."""


@dataclass
class _Detection:
    name: str
    entrypoint: List[str]
    ports: List[int]
    source_root: Optional[str] = None
    port_from_source: bool = False


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
    detection = _detect_framework(
        context_dir,
        include_paths,
        exclude_paths,
        exclude_defaults=exclude_defaults,
        requirements_file=requirements_file,
        files=files,
    )
    if detection is None:
        return None
    return detection.name, detection.entrypoint, detection.ports


def _detect_framework(
    context_dir,
    include_paths=None,
    exclude_paths=None,
    *,
    exclude_defaults=True,
    requirements_file="requirements.txt",
    files=None,
    configured_entrypoint=None,
    configured_cmd=None,
    configured_ports=False,
):
    context_path = Path(context_dir).absolute()
    ctx = context_path.resolve()
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
            path = absolute if absolute.is_relative_to(context_path) else context_path / path
        if path.suffix not in (".py", ".ipynb"):
            continue
        try:
            relative = path.relative_to(context_path)
        except ValueError:
            resolved = path.resolve()
            if not resolved.is_relative_to(ctx):
                raise ValueError(f"Selected source is outside the build context: {path}")
            relative = resolved.relative_to(ctx)
        if ".." in relative.parts:
            raise ValueError(f"Selected source is outside the build context: {path}")
        selected_path = ctx / relative
        if not selected_path.resolve().is_relative_to(ctx):
            raise ValueError(f"Selected source is outside the build context: {path}")
        if not selected_path.is_file():
            raise FileNotFoundError(f"Selected source file not found: {path}")
        selected[relative.as_posix()] = selected_path
    selected_files = [selected[name] for name in sorted(selected)]
    python_files = [path for path in selected_files if path.suffix == ".py"]
    notebooks = [path for path in selected_files if path.suffix == ".ipynb"]

    candidates = {
        "FastAPI": _detect_fastapi(python_files, ctx),
        "Flask": _detect_flask(python_files),
        "Django": [],
        "Streamlit": [],
        "Gradio": [],
        "Jupyter": [],
        "Celery": [],
    }
    django = _detect_django(python_files, ctx)
    if django:
        candidates["Django"].append(_Detection(*django))

    dependencies = declared_dependency_names(
        ctx,
        requirements_file,
        exclude_paths=exclude_paths,
        exclude_defaults=exclude_defaults,
    )
    problems = {}
    unlaunched_gradio = []
    unparsed = []
    gradio_port_problem = None
    for path in python_files:
        if not ({"streamlit", "gradio", "celery"} & dependencies):
            break
        tree = _parse_source(path, ctx)
        if tree is None:
            unparsed.append(path.relative_to(ctx).as_posix())
            continue
        (
            streamlit, celery_modules, celery_classes,
            gradio_modules, gradio_classes, gradio_mounts,
        ) = _import_aliases(tree)
        relative = path.relative_to(ctx).as_posix()
        if "streamlit" in dependencies and streamlit:
            candidates["Streamlit"].append(
                _Detection(
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
            launched, constructed, mounted, launch_ports = _gradio_app(
                tree, gradio_modules, gradio_classes, gradio_mounts
            )
            if launched:
                if len(launch_ports) > 1 and not configured_ports:
                    gradio_port_problem = (
                        f"Multiple Gradio listener ports were found in {relative}; "
                        "specify exposed ports explicitly."
                    )
                port = min(launch_ports) if launch_ports else GRADIO_PORT
                candidates["Gradio"].append(
                    _Detection(
                        "Gradio", ["python", relative], [port],
                        port_from_source=bool(launch_ports),
                    )
                )
            elif constructed and not (mounted and candidates["FastAPI"]):
                unlaunched_gradio.append(relative)
        if "celery" in dependencies:
            variables, constructed = _celery_apps(tree, celery_modules, celery_classes)
            module, source_root = _module_name(path, ctx)
            if constructed and (not variables or module is None):
                problems["Celery"] = (
                    f"Cannot determine a Celery application entrypoint in {relative}; "
                    "specify an explicit entrypoint or command."
                )
            elif module:
                for variable in variables:
                    candidates["Celery"].append(
                        _Detection(
                            "Celery",
                            ["celery", "-A", f"{module}:{variable}", "worker", "--loglevel=INFO"],
                            [],
                            source_root=source_root,
                        )
                    )

    if unlaunched_gradio:
        message = (
            f"Gradio application in {', '.join(unlaunched_gradio)} has no matching "
            "launch call; specify an explicit entrypoint or command."
        )
        if candidates["Gradio"]:
            logger.warning("%s", message)
        else:
            problems["Gradio"] = message

    for framework, matches in candidates.items():
        if len(matches) > 1:
            problems[framework] = (
                f"Multiple {framework} application candidates were found; "
                "specify an explicit entrypoint or command."
            )

    if notebooks and {"jupyterlab", "notebook"} & dependencies:
        subcommand = "lab" if "jupyterlab" in dependencies else "notebook"
        candidates["Jupyter"].append(_Detection(
            "Jupyter",
            [
                "jupyter", subcommand, f"--ip={ALL_INTERFACES}",
                f"--port={JUPYTER_PORT}", "--no-browser",
            ],
            [JUPYTER_PORT],
        ))
    elif notebooks:
        logger.debug("Notebooks found but no jupyterlab or notebook dependency is declared.")

    present = [name for name in FRAMEWORK_PRIORITY if candidates[name] or name in problems]
    if not present:
        if unparsed:
            raise FrameworkDetectionError(
                "Could not inspect selected Python sources; specify an explicit "
                "entrypoint or command."
            )
        return None
    chosen = present[0]
    configured = configured_entrypoint is not None or configured_cmd is not None
    selected_match = None
    if configured:
        arguments = [*(configured_entrypoint or []), *(configured_cmd or [])]
        matches = [
            candidate
            for name in present
            for candidate in candidates[name]
            if _matches_configured_command(candidate, arguments)
        ]
        if len(matches) == 1:
            selected_match = matches[0]
            chosen = selected_match.name
    if len(present) > 1:
        logger.warning(
            "Multiple frameworks detected (%s); %s %s. "
            "Specify an explicit entrypoint or command to choose another.",
            ", ".join(present), chosen,
            "matches the configured command" if selected_match else "takes precedence",
        )
    if "Celery" in present and chosen != "Celery":
        logger.warning(
            "Celery workers require a separate image or an explicit entrypoint."
        )
    for name in present:
        if name != chosen and name in problems:
            logger.warning("%s", problems[name])
    if gradio_port_problem and chosen != "Gradio":
        logger.warning("%s", gradio_port_problem)
    if chosen == "Gradio" and gradio_port_problem:
        raise FrameworkPortDetectionError(gradio_port_problem)
    if chosen in problems:
        if not configured:
            raise FrameworkDetectionError(problems[chosen])
        logger.warning("%s Using the configured entrypoint or command.", problems[chosen])
        if selected_match:
            return selected_match
        return _Detection(chosen, [], list(FRAMEWORK_PORTS[chosen]))
    return selected_match or candidates[chosen][0]


def _matches_configured_command(detection, arguments):
    if not detection.entrypoint:
        return False
    args = {str(arg).replace("\\", "/").removeprefix("./") for arg in arguments}
    executables = {arg.rsplit("/", 1)[-1] for arg in args}
    if detection.name == "Streamlit":
        return detection.entrypoint[2] in args and bool({"streamlit", "streamlit.exe"} & executables)
    if detection.name == "Gradio":
        return detection.entrypoint[1] in args and any(
            executable.startswith("python") for executable in executables
        )
    if detection.name in ("FastAPI", "Celery"):
        reference = detection.entrypoint[1 if detection.name == "FastAPI" else 2]
        executors = {"uvicorn", "gunicorn", "hypercorn"} if detection.name == "FastAPI" else {"celery"}
        has_executor = bool(executors & {name.removesuffix(".exe") for name in executables})
        return has_executor and (
            reference in args or f"--app={reference}" in args or f"-A{reference}" in args
        )
    return False


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
    gradio_modules, gradio_classes, gradio_mounts = set(), set(), set()
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
                if node.module == "gradio" and alias.name == "mount_gradio_app":
                    gradio_mounts.add(alias.asname or alias.name)
    return (
        streamlit, celery_modules, celery_classes,
        gradio_modules, gradio_classes, gradio_mounts,
    )


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
    source_root = None
    if len(parts) > 1 and parts[0] == "src":
        source_root = parts.pop(0)
    if parts and parts[-1] == "__init__":
        parts.pop()
    if not parts or any(not part.isidentifier() or keyword.iskeyword(part) for part in parts):
        return None, None
    return ".".join(parts), source_root


def _gradio_app(tree, modules, classes, mounts):
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

    launches = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "launch"
        and is_app_expression(node.func.value)
    ]
    ports = set()
    for launch in launches:
        for option in launch.keywords:
            if option.arg != "server_port":
                continue
            if (
                isinstance(option.value, ast.Constant)
                and isinstance(option.value.value, int)
                and not isinstance(option.value.value, bool)
                and 0 < option.value.value <= 65535
            ):
                ports.add(option.value.value)
            elif not (
                isinstance(option.value, ast.Constant) and option.value.value is None
            ):
                logger.warning(
                    "Cannot infer the Gradio listener port; configure exposed ports explicitly."
                )

    def is_mount(node):
        if not isinstance(node, ast.Call):
            return False
        function = node.func
        is_function = isinstance(function, ast.Name) and function.id in mounts
        is_member = (
            isinstance(function, ast.Attribute)
            and function.attr == "mount_gradio_app"
            and isinstance(function.value, ast.Name)
            and function.value.id in modules
        )
        return (is_function or is_member) and any(
            is_app_expression(argument)
            for argument in [*node.args[1:], *(keyword.value for keyword in node.keywords)]
        )

    mounted = any(is_mount(node) for node in ast.walk(tree))
    return bool(launches), constructed, mounted, ports


def _detect_fastapi(files: List[Path], ctx: Path) -> List[_Detection]:
    """Detect FastAPI applications."""
    detections = []
    for py in files:
        try:
            with tokenize.open(py) as source:
                content = source.read()
        except (OSError, UnicodeError, SyntaxError) as error:
            logger.warning("Cannot inspect %s: %s", py, error)
            continue
        module = ".".join(py.relative_to(ctx).with_suffix("").parts)
        for match in re.finditer(r'^\s*(\w+)\s*=\s*FastAPI\s*\(', content, re.MULTILINE):
            app_module = f"{module}:{match.group(1)}"
            detections.append(_Detection(
                "FastAPI", ["uvicorn", app_module, "--host", "0.0.0.0", "--port", "8000"], [8000]
            ))
    return detections

def _find_fastapi_app(file_path: Path, ctx: Path) -> Optional[str]:
    """Find FastAPI app variable in file."""
    try:
        with tokenize.open(file_path) as source:
            content = source.read()
        match=re.search(r'^\s*(\w+)\s*=\s*FastAPI\s*\(', content, re.MULTILINE)
        if match:
            app_var=match.group(1)
            rel=file_path.relative_to(ctx)
            module=".".join(rel.with_suffix("").parts)
            return f"{module}:{app_var}"
    except (OSError, UnicodeError, SyntaxError) as error:
        logger.warning("Cannot inspect %s: %s", file_path, error)
    return None


def _detect_flask(files: List[Path]) -> List[_Detection]:
    """Detect Flask applications."""
    detections = []
    for py in files:
        try:
            with tokenize.open(py) as source:
                content = source.read()
        except (OSError, UnicodeError, SyntaxError) as error:
            logger.warning("Cannot inspect %s: %s", py, error)
            continue
        for _ in re.finditer(r'^\s*\w+\s*=\s*Flask\s*\(', content, re.MULTILINE):
            detections.append(_Detection(
                "Flask", ["flask", "run", "--host=0.0.0.0", "--port=5000"], [5000]
            ))
    return detections

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


def _environment_port(value, name):
    try:
        port = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be a port number from 1 to 65535.") from error
    if not 0 < port <= 65535:
        raise ValueError(f"{name} must be a port number from 1 to 65535.")
    return port


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
    previous_entrypoint = getattr(config, "_framework_generated_entrypoint", None)
    previous_ports = getattr(config, "_framework_generated_ports", None)
    generated_entrypoint = (
        previous_entrypoint is not None
        and config.entrypoint is previous_entrypoint
        and tuple(previous_entrypoint) == getattr(config, "_framework_entrypoint_snapshot", ())
    )
    generated_ports = (
        previous_ports is not None
        and config.exposed_ports is previous_ports
        and tuple(previous_ports) == getattr(config, "_framework_ports_snapshot", ())
    )
    try:
        detection = _detect_framework(
            context_dir,
            include_paths,
            exclude_paths,
            exclude_defaults=exclude_defaults,
            requirements_file=config.requirements_file,
            files=files,
            configured_entrypoint=config.entrypoint,
            configured_cmd=config.cmd,
            configured_ports=config.has_exposed_ports(),
        )
    except FrameworkPortDetectionError:
        raise
    except FrameworkDetectionError as error:
        if config.entrypoint is None and config.cmd is None:
            raise
        logger.warning("%s Using the configured entrypoint or command.", error)
        return config
    if not detection:
        return config

    framework = detection.name
    entrypoint = list(detection.entrypoint) if detection.entrypoint else None
    ports = list(detection.ports)
    if framework == "Streamlit":
        env_port = config.env.get("STREAMLIT_SERVER_PORT")
        requested_port = (
            _environment_port(env_port, "STREAMLIT_SERVER_PORT")
            if env_port is not None else STREAMLIT_PORT
        )
        if (
            (generated_entrypoint or (config.entrypoint is None and config.cmd is None))
            and config.has_exposed_ports() and not generated_ports
            and len(config.exposed_ports) == 1
        ):
            if env_port is not None and requested_port != config.exposed_ports[0]:
                raise ValueError(
                    "STREAMLIT_SERVER_PORT conflicts with the explicitly exposed port."
                )
            requested_port = config.exposed_ports[0]
        ports = [requested_port]
        if entrypoint:
            entrypoint[-1] = f"--server.port={requested_port}"
            address = config.env.get("STREAMLIT_SERVER_ADDRESS")
            if address is not None:
                entrypoint[-2] = f"--server.address={address}"
    elif framework == "Gradio" and not detection.port_from_source:
        env_port = config.env.get("GRADIO_SERVER_PORT")
        if env_port is not None:
            ports = [_environment_port(env_port, "GRADIO_SERVER_PORT")]

    if generated_entrypoint or (config.entrypoint is None and config.cmd is None):
        config.entrypoint = entrypoint
        if config.cmd is None:
            config.cmd = []
        generated_entrypoint = True
        config._framework_generated_entrypoint = config.entrypoint
        config._framework_entrypoint_snapshot = tuple(config.entrypoint)

    if generated_ports or not config.has_exposed_ports():
        config.exposed_ports = ports
        generated_ports = True
        config._framework_generated_ports = config.exposed_ports
        config._framework_ports_snapshot = tuple(config.exposed_ports)
    if framework == "Gradio":
        default_env = getattr(config, "_framework_default_env", {})
        if "GRADIO_SERVER_NAME" not in config.env:
            config.env["GRADIO_SERVER_NAME"] = ALL_INTERFACES
            default_env["GRADIO_SERVER_NAME"] = ALL_INTERFACES
        elif default_env.get("GRADIO_SERVER_NAME") != config.env["GRADIO_SERVER_NAME"]:
            default_env.pop("GRADIO_SERVER_NAME", None)
        config._framework_default_env = default_env

    config._framework_detection = detection
    config._framework_entrypoint_generated = generated_entrypoint
    config._framework_ports_generated = generated_ports
    
    if not config.labels:
        config.labels={}
    config.labels["framework"]=framework.lower()
    logger.info("Detected framework: %s", framework.lower())
    
    return config
