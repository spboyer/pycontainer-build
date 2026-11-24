"""Framework detection and auto-configuration."""
import ast
import keyword
import logging
import os
import posixpath
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple, List
from .config import (
    _DefaultPorts, _GeneratedLabels, _GeneratedList, _is_generated_field,
    _is_generated_label,
)
from .fs_utils import DEFAULT_CONTEXT_EXCLUDES, iter_files, path_is_excluded
from .project import declared_dependency_names, default_include_paths


logger = logging.getLogger(__name__)
ALL_INTERFACES = "0.0.0.0"
STREAMLIT_PORT = 8501
GRADIO_PORT = 7860
JUPYTER_PORT = 8888
MAX_FRAMEWORK_SOURCE_BYTES = 1024 * 1024
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
    port_problem: Optional[str] = None


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
    configured_workdir=None,
    log_redact=str,
):
    if include_paths is not None and not include_paths:
        return None
    context_path = Path(context_dir).absolute()
    ctx = context_path.resolve()
    excludes = list(DEFAULT_CONTEXT_EXCLUDES) if exclude_defaults else []
    if exclude_paths is not None:
        excludes.extend(exclude_paths)
    if files is None:
        paths = (
            default_include_paths(
                ctx,
                exclude_paths=exclude_paths,
                exclude_defaults=exclude_defaults,
                requirements_file=requirements_file,
                log_redact=log_redact,
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
    available = set()
    for item in files:
        path = Path(item)
        if not path.is_absolute():
            absolute = path.absolute()
            path = absolute if absolute.is_relative_to(context_path) else context_path / path
        if path.suffix not in (".py", ".ipynb"):
            try:
                relative = path.relative_to(context_path)
            except ValueError:
                continue
            if ".." not in relative.parts and not path_is_excluded(relative, excludes):
                available.add(relative)
            continue
        try:
            relative = path.relative_to(context_path)
        except ValueError:
            resolved = path.resolve()
            if not resolved.is_relative_to(ctx):
                raise ValueError(log_redact(f"Selected source is outside the build context: {path}"))
            relative = resolved.relative_to(ctx)
        if ".." in relative.parts:
            raise ValueError(log_redact(f"Selected source is outside the build context: {path}"))
        if path_is_excluded(relative, excludes):
            continue
        selected_path = ctx / relative
        if not selected_path.resolve().is_relative_to(ctx):
            if any(
                (ctx / parent).is_symlink()
                for parent in relative.parents
                if parent != Path(".")
            ):
                logger.warning(
                    "%s", log_redact(
                        f"Skipping source {relative}: its directory symlink resolves outside "
                        "the build context."
                    ),
                )
                continue
            raise ValueError(log_redact(f"Selected source is outside the build context: {path}"))
        if not selected_path.is_file():
            raise FileNotFoundError(log_redact(f"Selected source file not found: {path}"))
        available.add(relative)
        selected[relative.as_posix()] = selected_path
    selected_files = []
    for name in sorted(selected):
        path = selected[name]
        target = path.resolve().relative_to(ctx)
        if path_is_excluded(target, excludes) or not _selected_symlink_chain(path, ctx, available):
            logger.warning(
                "%s", log_redact(
                    f"Skipping source symlink {name}: its target chain is excluded or not selected."
                ),
            )
            continue
        selected_files.append(path)
    python_files = [path for path in selected_files if path.suffix == ".py"]
    notebooks = [path for path in selected_files if path.suffix == ".ipynb"]

    candidates = {
        "FastAPI": [],
        "Flask": [],
        "Django": [],
        "Streamlit": [],
        "Gradio": [],
        "Jupyter": [],
        "Celery": [],
    }
    dependencies = declared_dependency_names(
        ctx,
        requirements_file,
        exclude_paths=exclude_paths,
        exclude_defaults=exclude_defaults,
        log_redact=log_redact,
    )
    problems = {}
    unlaunched_gradio = []
    unparsed = []
    for path in python_files:
        tree, hints = _parse_source(path, ctx, dependencies, log_redact)
        if tree is None:
            if hints:
                unparsed.append((path.relative_to(ctx).as_posix(), hints))
            continue
        relative = path.relative_to(ctx).as_posix()
        module = ".".join(path.relative_to(ctx).with_suffix("").parts)
        fastapi_variables, fastapi_constructed = _web_app_details(tree, "FastAPI")
        mounted = _mounted_names(tree)
        fastapi_variables = [name for name in fastapi_variables if name not in mounted]
        if fastapi_constructed and not fastapi_variables:
            problems["FastAPI"] = (
                f"Cannot determine an importable FastAPI entrypoint in {relative}; "
                "specify an explicit entrypoint or command."
            )
        for variable in fastapi_variables:
            candidates["FastAPI"].append(_Detection(
                "FastAPI",
                ["uvicorn", f"{module}:{variable}", "--host", ALL_INTERFACES, "--port", "8000"],
                [8000],
            ))
        # `flask run` discovers module-level apps and factories itself, so every
        # Flask construction maps to the same single entrypoint.
        if _web_app_details(tree, "Flask")[1] and not candidates["Flask"]:
            candidates["Flask"].append(_Detection(
                "Flask", ["flask", "run", "--host=0.0.0.0", "--port=5000"], [5000]
            ))
        if "Django" in hints:
            candidates["Django"].append(_Detection(
                "Django", ["python", "manage.py", "runserver", "0.0.0.0:8000"], [8000]
            ))
        (
            streamlit, celery_modules, celery_classes,
            gradio_modules, gradio_classes, gradio_mounts,
        ) = _import_aliases(tree)
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
            launched, constructed, mounted, launch_ports, unknown_port = _gradio_app(
                tree, gradio_modules, gradio_classes, gradio_mounts
            )
            if launched:
                port_problem = None
                if len(launch_ports) > 1:
                    port_problem = (
                        f"Multiple Gradio listener ports were found in {relative}; "
                        "specify exposed ports explicitly."
                    )
                elif unknown_port:
                    port_problem = (
                        f"Cannot infer the Gradio listener port in {relative}; "
                        "specify exposed ports explicitly."
                    )
                port = min(launch_ports) if launch_ports else GRADIO_PORT
                candidates["Gradio"].append(
                    _Detection(
                        "Gradio", ["python", relative], [port],
                        port_from_source=bool(launch_ports),
                        port_problem=port_problem,
                    )
                )
            elif constructed:
                unlaunched_gradio.append((relative, mounted))
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

    unlaunched_gradio = [
        path for path, mounted in unlaunched_gradio
        if not (mounted and candidates["FastAPI"])
    ]
    if unlaunched_gradio:
        message = (
            f"Gradio application in {', '.join(unlaunched_gradio)} has no matching "
            "launch call; specify an explicit entrypoint or command."
        )
        if candidates["Gradio"]:
            logger.warning("%s", log_redact(message))
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
        logger.debug("%s", log_redact("Notebooks found but no jupyterlab or notebook dependency is declared."))

    present = [name for name in FRAMEWORK_PRIORITY if candidates[name] or name in problems]
    configured = configured_entrypoint is not None or configured_cmd is not None
    if unparsed and not configured:
        possible = {name for _, hints in unparsed for name in hints}
        if not present or any(
            FRAMEWORK_PRIORITY.index(name) <= FRAMEWORK_PRIORITY.index(present[0])
            for name in possible
        ):
            raise FrameworkDetectionError(
                "Could not inspect selected Python sources; specify an explicit "
                "entrypoint or command."
            )
    if not present:
        return None
    chosen = present[0]
    selected_match = None
    if configured:
        arguments = [*(configured_entrypoint or []), *(configured_cmd or [])]
        normalized = [str(arg).replace("\\", "/").removeprefix("./") for arg in arguments]
        executable, command_arguments, _, _ = _configured_command(normalized)
        if executable == "celery" and _celery_subcommand(command_arguments) != "worker":
            logger.warning(
                "%s", log_redact(
                    "The configured Celery command is not a worker; no framework defaults applied."
                )
            )
            return None
        matches = [
            candidate
            for name in present
            for candidate in candidates[name]
            if _matches_configured_command(candidate, arguments, configured_workdir)
        ]
        if matches and len({candidate.name for candidate in matches}) == 1:
            selected_match = matches[0]
            chosen = selected_match.name
        if (
            (configured_entrypoint == [] or configured_cmd == [])
            and selected_match is None
        ):
            logger.warning(
                "%s", log_redact(
                    "The configured command clears an image command field and does not "
                    "identify a detected application; no framework defaults applied."
                )
            )
            return None
    if len(present) > 1:
        logger.warning(
            "%s", log_redact(
                f"Multiple frameworks detected ({', '.join(present)}); {chosen} "
                f"{'matches the configured command' if selected_match else 'takes precedence'}. "
                "Specify an explicit entrypoint or command to choose another."
            ),
        )
    if "Celery" in present and chosen != "Celery":
        logger.warning(
            "%s", log_redact(
                "Celery workers require a separate image or an explicit entrypoint."
            )
        )
    for name in present:
        if name != chosen and name in problems:
            logger.warning("%s", log_redact(problems[name]))
    if (
        chosen == "Gradio" and configured and not selected_match
        and any(candidate.port_problem for candidate in candidates["Gradio"])
    ):
        logger.warning(
            "%s", log_redact(
                "The configured command does not identify a Gradio app; "
                "its listener defaults will not be applied."
            )
        )
        return None
    if chosen == "Gradio" and not configured_ports:
        relevant = [selected_match] if selected_match else candidates["Gradio"]
        port_problem = next(
            (candidate.port_problem for candidate in relevant if candidate.port_problem), None
        )
        if port_problem:
            raise FrameworkPortDetectionError(log_redact(port_problem))
    if chosen in problems:
        if not configured:
            raise FrameworkDetectionError(log_redact(problems[chosen]))
        logger.warning(
            "%s", log_redact(f"{problems[chosen]} Using the configured entrypoint or command.")
        )
        if selected_match:
            return selected_match
        return _Detection(chosen, [], list(FRAMEWORK_PORTS[chosen]))
    return selected_match or candidates[chosen][0]


def _selected_symlink_chain(path, context_dir, available):
    seen = set()
    while path.is_symlink():
        if path in seen:
            return False
        seen.add(path)
        link = os.readlink(path)
        if "\\" in link:
            return False
        target = Path(link)
        if target.is_absolute() or target.drive or target.root:
            return False
        path = Path(os.path.abspath(path.parent / target))
        if not path.is_relative_to(context_dir) or path.relative_to(context_dir) not in available:
            return False
    return True


def _configured_executable(arguments):
    if not arguments:
        return None, 0, None
    index = 0
    directory = None
    executable = arguments[index].rsplit("/", 1)[-1].removesuffix(".exe")
    if executable == "env":
        index += 1
        while index < len(arguments):
            option = arguments[index]
            if option in ("-u", "--unset", "-C", "--chdir"):
                if index + 1 >= len(arguments):
                    return None, index, directory
                if option in ("-C", "--chdir"):
                    directory = arguments[index + 1]
                index += 2
            elif option.startswith("--chdir="):
                directory = option.partition("=")[2]
                index += 1
            elif option.startswith("-C") and len(option) > 2:
                directory = option[2:]
                index += 1
            elif option.startswith("-") or "=" in option:
                index += 1
            else:
                break
        if index >= len(arguments):
            return None, index, directory
        executable = arguments[index].rsplit("/", 1)[-1].removesuffix(".exe")
    return executable, index + 1, directory


def _configured_python_target(arguments):
    executable, index, _ = _configured_executable(arguments)
    if not executable or not (executable.startswith("python") or executable in ("py", "pyw")):
        return None
    while index < len(arguments):
        argument = arguments[index]
        if argument in ("-V", "--version", "-h", "--help", "-"):
            return None
        if argument.startswith("-m") and len(argument) > 2:
            return "module", argument[2:], index + 1
        if argument.startswith("-c") and len(argument) > 2:
            return None
        if argument in ("-m", "-c"):
            if argument == "-c" or index + 1 >= len(arguments):
                return None
            return "module", arguments[index + 1], index + 2
        if argument == "--":
            index += 1
            break
        if argument in ("-W", "-X", "--check-hash-based-pycs"):
            index += 2
        elif argument.startswith("-"):
            index += 1
        else:
            break
    if index < len(arguments):
        return "script", arguments[index], index + 1
    return None


def _configured_command(arguments):
    executable, index, directory = _configured_executable(arguments)
    target = _configured_python_target(arguments)
    if target and target[0] == "module":
        executable, index = target[1], target[2]
    return executable, arguments[index:], target, directory


def _celery_subcommand(arguments):
    index = 0
    while index < len(arguments):
        option = arguments[index]
        if option in ("-A", "--app", "-b", "--broker", "--result-backend", "--loader", "--config", "--workdir"):
            index += 2
        elif option.startswith("-"):
            index += 1
        else:
            return option
    return None


def _configured_script_path(script, workdir, directory):
    if not workdir or not posixpath.isabs(workdir):
        return None
    root = posixpath.normpath(workdir)
    working = posixpath.normpath(posixpath.join(root, directory or ""))
    absolute = posixpath.normpath(posixpath.join(working, script))
    if root == "/":
        return absolute.lstrip("/")
    prefix = root + "/"
    return absolute[len(prefix):] if absolute.startswith(prefix) else None


def _matches_configured_command(detection, arguments, workdir):
    if not detection.entrypoint:
        return False
    arguments = [str(arg).replace("\\", "/").removeprefix("./") for arg in arguments]
    executable, command_arguments, target, directory = _configured_command(arguments)
    if detection.name == "Streamlit":
        if executable != "streamlit" or not command_arguments or command_arguments[0] != "run":
            return False
        index = 1
        while index < len(command_arguments):
            option = command_arguments[index]
            if option == "--":
                index += 1
                break
            if option in ("--server.port", "--server.address"):
                index += 2
            elif option in ("--help", "-h"):
                return False
            elif option.startswith("-") and "=" in option:
                index += 1
            elif option.startswith("-"):
                index += (
                    2 if index + 1 < len(command_arguments)
                    and not command_arguments[index + 1].startswith("-") else 1
                )
            else:
                break
        return (
            index < len(command_arguments)
            and _configured_script_path(command_arguments[index], workdir, directory)
            == detection.entrypoint[2]
        )
    if detection.name == "Gradio":
        if not target:
            return False
        script = detection.entrypoint[1]
        target_path = target[1] if target[0] == "script" else target[1].replace(".", "/") + ".py"
        return _configured_script_path(target_path, workdir, directory) == script
    if detection.name in ("FastAPI", "Celery"):
        reference = detection.entrypoint[1 if detection.name == "FastAPI" else 2]
        executors = {"uvicorn", "gunicorn", "hypercorn"} if detection.name == "FastAPI" else {"celery"}
        if executable not in executors:
            return False
        args = set(command_arguments[:command_arguments.index("--")]) if "--" in command_arguments else set(command_arguments)
        references = {reference}
        if detection.name == "Celery":
            references.add(reference.partition(":")[0])
        return any(
            ref in args or f"--app={ref}" in args or f"-A{ref}" in args or f"-A={ref}" in args
            for ref in references
        )
    if detection.name == "Flask":
        if executable != "flask":
            return False
        index = 0
        while index < len(command_arguments):
            option = command_arguments[index]
            if option in ("--app", "-A", "--env-file", "-e"):
                index += 2
            elif option.startswith("-"):
                index += 1
            else:
                return option == "run"
        return False
    if detection.name == "Django":
        if target and target[0] == "script" and _configured_script_path(
            target[1], workdir, directory
        ) == "manage.py":
            command_arguments = arguments[target[2]:]
        elif executable not in {"manage.py", "django-admin", "django"}:
            return False
        return bool(command_arguments) and command_arguments[0] == "runserver"
    if detection.name == "Jupyter":
        return executable in {"jupyter-lab", "jupyter-notebook", "jupyterlab", "notebook"} or (
            executable == "jupyter" and bool(command_arguments)
            and command_arguments[0] in {"lab", "notebook"}
        )
    return False


def _parse_source(path, context_dir, dependencies, log_redact=str):
    markers = {"FastAPI": b"fastapi", "Flask": b"flask"}
    for package, framework in (
        ("streamlit", "Streamlit"), ("gradio", "Gradio"), ("celery", "Celery")
    ):
        if package in dependencies:
            markers[framework] = package.encode("ascii")
    if path.name == "manage.py" and path.parent == context_dir:
        markers["Django"] = b"django"

    hints = set()
    content = bytearray()
    tail = b""
    overlap = max(map(len, markers.values())) - 1
    with path.open("rb") as source:
        while chunk := source.read(64 * 1024):
            part = tail + chunk.lower()
            hints.update(name for name, marker in markers.items() if marker in part)
            tail = part[-overlap:]
            if content is not None:
                content.extend(chunk)
                if len(content) > MAX_FRAMEWORK_SOURCE_BYTES:
                    content = None
            if content is None and len(hints) == len(markers):
                break
    if not hints:
        return None, hints
    relative = path.relative_to(context_dir).as_posix()
    if content is None:
        logger.warning(
            "%s", log_redact(
                f"Cannot inspect {relative}: source exceeds the {MAX_FRAMEWORK_SOURCE_BYTES}-byte limit."
            ),
        )
        return None, hints
    try:
        return ast.parse(bytes(content), filename=str(path)), hints
    except (SyntaxError, UnicodeError) as error:
        logger.warning(
            "%s", log_redact(
                f"Cannot inspect {relative}: {error}. Specify an explicit entrypoint if needed."
            ),
        )
        return None, hints


def _module_scope_nodes(tree):
    pending = list(reversed(tree.body))
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        yield node
        pending.extend(reversed(list(ast.iter_child_nodes(node))))


def _web_app_details(tree, constructor):
    package = constructor.lower()
    modules = set()
    classes = {constructor}
    nodes = list(_module_scope_nodes(tree))
    variables = []
    for node in nodes:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == package:
                    modules.add(alias.asname or package)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module == package:
            for alias in node.names:
                if alias.name == constructor:
                    classes.add(alias.asname or alias.name)
    for node in nodes:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if not _is_constructor(node.value, modules, classes, {constructor}):
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                variables.append(target.id)
    constructed = any(
        _is_constructor(node, modules, classes, {constructor}) for node in ast.walk(tree)
    )
    return variables, constructed


def _mounted_names(tree):
    """Return names passed as sub-applications to another object's ``mount``."""
    mounted = set()
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "mount"
        ):
            continue
        owner = node.func.value.id if isinstance(node.func.value, ast.Name) else None
        for arg in [*node.args, *(keyword.value for keyword in node.keywords)]:
            if isinstance(arg, ast.Name) and arg.id != owner:
                mounted.add(arg.id)
    return mounted


def _web_app_variables(tree, constructor):
    yield from _web_app_details(tree, constructor)[0]


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
    unknown_port = False
    uses_default_port = False
    for launch in launches:
        has_port = False
        for option in launch.keywords:
            if option.arg is None:
                unknown_port = True
            if option.arg != "server_port":
                continue
            has_port = True
            if (
                isinstance(option.value, ast.Constant)
                and isinstance(option.value.value, int)
                and not isinstance(option.value.value, bool)
                and 0 < option.value.value <= 65535
            ):
                ports.add(option.value.value)
            elif (
                isinstance(option.value, ast.Constant) and option.value.value is None
            ):
                uses_default_port = True
            else:
                unknown_port = True
        if not has_port:
            uses_default_port = True
    if ports and uses_default_port:
        unknown_port = True

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
    return bool(launches), constructed, mounted, ports, unknown_port

def _find_fastapi_app(file_path: Path, ctx: Path) -> Optional[str]:
    """Find FastAPI app variable in file."""
    tree, _ = _parse_source(file_path, ctx, set())
    if tree is None:
        return None
    variable = next(_web_app_variables(tree, "FastAPI"), None)
    if variable is None:
        return None
    module = ".".join(file_path.relative_to(ctx).with_suffix("").parts)
    return f"{module}:{variable}"


def _environment_port(value, name):
    try:
        port = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be a port number from 1 to 65535.") from error
    if not 0 < port <= 65535:
        raise ValueError(f"{name} must be a port number from 1 to 65535.")
    return port


def _configured_jupyter_port(arguments):
    normalized = [str(arg).replace("\\", "/").removeprefix("./") for arg in arguments]
    _, command_arguments, _, _ = _configured_command(normalized)
    options = {"--port", "--ServerApp.port", "--NotebookApp.port", "--LabApp.port"}
    ports = set()
    index = 0
    while index < len(command_arguments):
        argument = command_arguments[index]
        if argument == "--":
            break
        option, separator, value = argument.partition("=")
        if option in options:
            if not separator:
                index += 1
                value = command_arguments[index] if index < len(command_arguments) else None
            ports.add(_environment_port(value, option))
        index += 1
    if len(ports) > 1:
        raise FrameworkPortDetectionError(
            "Conflicting Jupyter listener ports were specified; configure exposed ports explicitly."
        )
    return next(iter(ports), None)


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
    generated_entrypoint = _is_generated_field(config, "entrypoint")
    generated_ports = _is_generated_field(config, "exposed_ports")
    generated_cmd = _is_generated_field(config, "cmd")
    generated_label = _is_generated_label(config)
    configured_entrypoint = None if generated_entrypoint else config.entrypoint
    configured_cmd = None if generated_cmd else config.cmd
    if generated_entrypoint and configured_cmd is not None:
        config.entrypoint = None
        generated_entrypoint = False
    elif configured_entrypoint is not None and generated_cmd:
        config.cmd = None
        generated_cmd = False
    try:
        detection = _detect_framework(
            context_dir,
            include_paths,
            exclude_paths,
            exclude_defaults=exclude_defaults,
            requirements_file=config.requirements_file,
            files=files,
            configured_entrypoint=configured_entrypoint,
            configured_cmd=configured_cmd,
            configured_ports=config.has_exposed_ports() and not generated_ports,
            configured_workdir=config.workdir,
            log_redact=config.redact,
        )
    except FrameworkPortDetectionError:
        raise
    except FrameworkDetectionError as error:
        if configured_entrypoint is None and configured_cmd is None:
            raise
        logger.warning(
            "%s", config.redact(f"{error} Using the configured entrypoint or command.")
        )
        return config
    if not detection:
        if generated_entrypoint:
            config.entrypoint = None
        if generated_cmd:
            config.cmd = None
        if generated_ports:
            object.__setattr__(config, "exposed_ports", _DefaultPorts())
        if generated_label:
            config.labels = dict(config.labels)
            del config.labels["framework"]
            config._framework_generated_labels = None
        config._framework_default_env = {}
        config._framework_detection = None
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
            (generated_entrypoint or (config.entrypoint is None and configured_cmd is None))
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
    elif (
        framework == "Jupyter" and (generated_ports or not config.has_exposed_ports())
        and (configured_entrypoint is not None or configured_cmd is not None)
    ):
        arguments = [*(configured_entrypoint or []), *(configured_cmd or [])]
        if _matches_configured_command(detection, arguments, config.workdir):
            command_port = _configured_jupyter_port(arguments)
            if command_port is not None:
                ports = [command_port]

    if generated_entrypoint or (config.entrypoint is None and configured_cmd is None):
        config.entrypoint = _GeneratedList(entrypoint, generated=True)
        if config.cmd is None or generated_cmd:
            config.cmd = _GeneratedList(generated=True)
            config._framework_generated_cmd = config.cmd
            config._framework_cmd_snapshot = tuple(config.cmd)
        generated_entrypoint = True
        config._framework_generated_entrypoint = config.entrypoint
        config._framework_entrypoint_snapshot = tuple(config.entrypoint)

    if generated_ports or not config.has_exposed_ports():
        config.exposed_ports = _GeneratedList(ports, generated=True)
        generated_ports = True
        config._framework_generated_ports = config.exposed_ports
        config._framework_ports_snapshot = tuple(config.exposed_ports)
    config._framework_default_env = (
        {"GRADIO_SERVER_NAME": ALL_INTERFACES} if framework == "Gradio" else {}
    )

    config._framework_detection = detection
    config._framework_entrypoint_generated = generated_entrypoint
    config._framework_ports_generated = generated_ports
    
    config.labels = dict(config.labels or {})
    if generated_label or "framework" not in config.labels:
        config.labels["framework"] = framework.lower()
        config.labels = _GeneratedLabels(config.labels, generated=True)
        config._framework_generated_labels = config.labels
    else:
        config._framework_generated_labels = None
    logger.info("%s", config.redact(f"Detected framework: {framework.lower()}"))
    
    return config
