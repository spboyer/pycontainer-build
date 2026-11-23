"""Target-aware staging for Python runtime dependencies."""
import codecs
import configparser
import csv
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import time
from email.parser import Parser
from pathlib import Path, PureWindowsPath
from typing import Iterable, List, Optional, Tuple

from .venv_markers import declared_roots, host_environments, venv_layout_os


MAX_REQUIREMENTS_BYTES = 1024 * 1024
MAX_ENTRY_POINTS_BYTES = 1024 * 1024
MAX_METADATA_BYTES = 1024 * 1024
CLOSURE_TIMEOUT_SECONDS = 120
MAX_STAGED_BYTES = 512 * 1024 * 1024
MAX_STAGED_FILES = 100_000
INSTALL_TIMEOUT_SECONDS = 300

_PIP_PLATFORMS = {
    ("linux", "amd64"): "manylinux2014_x86_64",
    ("linux", "arm64"): "manylinux2014_aarch64",
}
_MARKER_MACHINES = {"amd64": "x86_64", "arm64": "aarch64"}
_PIP_TARGET_SCRIPT = Path(__file__).with_name("_pip_target.py")
_VENV_CLOSURE_SCRIPT = Path(__file__).with_name("_venv_closure.py")
_CLOSURE_HEADERS = ("Metadata-Version", "Name", "Version", "Requires-Dist", "Provides-Extra", "Requires-Python")
_MARKER_ERROR_EXIT = 86
_PATCH_ERROR_EXIT = 87
_MARKER_ERROR_FILE = "pycontainer-marker-error.txt"
_NATIVE_SUFFIXES = {".so", ".pyd", ".dll", ".dylib"}
_PIP_OVERRIDE_OPTIONS = {
    "--abi",
    "--build",
    "--cache-dir",
    "--implementation",
    "--log",
    "--no-binary",
    "--only-binary",
    "--platform",
    "--prefix",
    "--python-version",
    "--report",
    "--root",
    "--src",
    "--target",
    "--user",
}


def _base_image_parts(base_image: str) -> Tuple[str, str]:
    reference = base_image.split("@", 1)[0]
    repository, separator, tag = reference.rpartition(":")
    if not separator or "/" in tag:
        repository, tag = reference, ""
    return repository.lower(), tag


def _is_official_python_image(repository: str) -> bool:
    return repository in {
        "python",
        "library/python",
        "docker.io/python",
        "docker.io/library/python",
        "index.docker.io/python",
        "index.docker.io/library/python",
    }


def target_python_version(base_image: str, base_config: Optional[dict]) -> str:
    """Resolve the CPython major/minor version from the pulled base image."""
    environment = _base_environment(base_config)
    repository, image_tag = _base_image_parts(base_image)
    official_python = _is_official_python_image(repository)
    implementation = environment.get("PYTHON_IMPLEMENTATION")
    if not official_python and implementation not in {
        "CPython",
        "cpython",
    }:
        raise ValueError(
            "Cannot validate the target Python ABI for a custom base image; "
            "set PYTHON_IMPLEMENTATION=CPython and PYTHON_VERSION in its image config."
        )
    if implementation and implementation.lower() != "cpython":
        raise ValueError("Dependency staging supports CPython targets only.")

    value = environment.get("PYTHON_VERSION")
    if value and re.fullmatch(r"\d+\.\d+(?:\.\d+)?", value):
        return ".".join(value.split(".")[:2])

    match = re.match(r"(\d+\.\d+)(?:\.\d+)?(?:-|$)", image_tag)
    if official_python and match:
        return match.group(1)
    raise ValueError(
        "Cannot determine the target CPython version from the base image; "
        "use an official python:<major>.<minor> image or a base image with "
        "PYTHON_VERSION metadata."
    )


def _base_environment(base_config: Optional[dict]) -> dict:
    environment = {}
    if base_config:
        for item in base_config.get("config", {}).get("Env", []):
            if "=" in item:
                key, value = item.split("=", 1)
                environment[key] = value
    return environment


def _target_options(
    os_name: str,
    arch: str,
    python_version: str,
    base_image: str = "python:3.11-slim",
    base_config: Optional[dict] = None,
) -> List[str]:
    try:
        glibc_platform = _PIP_PLATFORMS[(os_name, arch)]
    except KeyError as exc:
        raise ValueError(
            f"Dependency staging does not support target platform {os_name}/{arch}; "
            "supported targets are linux/amd64 and linux/arm64."
        ) from exc
    repository, image_tag = _base_image_parts(base_image)
    libc = _base_environment(base_config).get("PYTHON_LIBC")
    if _is_official_python_image(repository):
        if "alpine" in image_tag.lower():
            platform = glibc_platform.replace("manylinux2014", "musllinux_1_2")
        else:
            platform = glibc_platform
    elif libc == "glibc-2.17":
        platform = glibc_platform.replace("manylinux2014", "manylinux_2_17")
    elif libc == "musl-1.2":
        platform = glibc_platform.replace("manylinux2014", "musllinux_1_2")
    else:
        raise ValueError(
            "Cannot validate target wheel compatibility for this base image; "
            "set PYTHON_LIBC to glibc-2.17 or musl-1.2 in its image config."
        )

    major, minor = python_version.split(".")
    return [
        "--platform",
        platform,
        "--python-version",
        f"{major}.{minor}",
        "--implementation",
        "cp",
        "--abi",
        f"cp{major}{minor}",
        "--only-binary=:all:",
    ]


def validate_target_platform(os_name: str, arch: str) -> None:
    if (os_name, arch) not in _PIP_PLATFORMS:
        raise ValueError(
            f"Dependency staging does not support target platform {os_name}/{arch}; "
            "supported targets are linux/amd64 and linux/arm64."
        )


def _target_marker_environment(
    os_name: str, arch: str, python_version: str, base_config: Optional[dict]
) -> dict:
    """PEP 508 marker values for the target; omitted keys are unknown and fail closed."""
    validate_target_platform(os_name, arch)
    environment = {
        "implementation_name": "cpython",
        "os_name": "posix",
        "platform_machine": _MARKER_MACHINES[arch],
        "platform_python_implementation": "CPython",
        "platform_system": "Linux",
        "python_version": python_version,
        "sys_platform": "linux",
    }
    full_version = _base_environment(base_config).get("PYTHON_VERSION", "")
    if re.fullmatch(r"\d+\.\d+\.\d+", full_version) and (
        ".".join(full_version.split(".")[:2]) == python_version
    ):
        environment["python_full_version"] = full_version
        environment["implementation_version"] = full_version
    return environment


def install_requirements(
    requirements: Path,
    destination: Path,
    context_dir: Path,
    os_name: str,
    arch: str,
    python_version: str,
    base_image: str,
    base_config: Optional[dict],
) -> None:
    validate_requirements(requirements, context_dir)

    destination.mkdir(parents=True, exist_ok=True)
    error_file = destination.parent / _MARKER_ERROR_FILE
    marker_config = {
        "environment": _target_marker_environment(
            os_name, arch, python_version, base_config
        ),
        "error_file": str(error_file),
    }
    command = [
        sys.executable,
        "-c",
        _PIP_TARGET_SCRIPT.read_text(encoding="utf-8"),
        json.dumps(marker_config),
        "install",
        "--disable-pip-version-check",
        "--no-input",
        "--no-cache-dir",
        "--no-compile",
        # pip's conflict check evaluates markers of the host's installed packages.
        "--no-warn-conflicts",
        "--target",
        str(destination),
        *_target_options(
            os_name, arch, python_version, base_image, base_config
        ),
        "-r",
        str(requirements),
    ]
    environment = dict(os.environ)
    environment.update(
        {
            "TMP": str(destination.parent),
            "TEMP": str(destination.parent),
            "TMPDIR": str(destination.parent),
        }
    )
    with open(os.devnull, "wb") as null_output:
        try:
            process = subprocess.Popen(
                command,
                cwd=context_dir,
                env=environment,
                stdout=null_output,
                stderr=null_output,
            )
        except OSError as exc:
            raise RuntimeError(
                "Unable to start pip for target dependency installation."
            ) from exc
        try:
            started = time.monotonic()
            while True:
                try:
                    returncode = process.wait(timeout=0.5)
                    break
                except subprocess.TimeoutExpired:
                    _check_stage_limits(destination.parent)
                    if time.monotonic() - started > INSTALL_TIMEOUT_SECONDS:
                        raise TimeoutError(
                            f"Target dependency installation exceeded "
                            f"{INSTALL_TIMEOUT_SECONDS} seconds."
                        )
        except BaseException:
            _stop_process(process)
            raise

    if returncode in {_MARKER_ERROR_EXIT, _PATCH_ERROR_EXIT}:
        details = _read_marker_error(error_file)
        if returncode == _MARKER_ERROR_EXIT:
            raise ValueError(
                "Cannot evaluate dependency environment markers for target "
                f"{os_name}/{arch}, CPython {python_version}; refusing to stage an "
                f"incomplete dependency set. {details}"
            )
        raise RuntimeError(
            "The builder's pip cannot evaluate environment markers for the target "
            f"platform; refusing to use host markers. {details}"
        )
    if returncode:
        raise RuntimeError(
            "Target dependency installation failed. Only wheels compatible with "
            f"{os_name}/{arch}, CPython {python_version}, and binary-only resolution "
            "are supported."
        )

    for scripts_dir in (destination / "bin", destination / "Scripts"):
        if scripts_dir.exists():
            shutil.rmtree(scripts_dir)
    _drop_external_record_entries(destination)

    _check_stage_limits(destination.parent)


def _read_marker_error(error_file: Path) -> str:
    try:
        return error_file.read_text(encoding="utf-8", errors="replace")[:4096].strip()
    except OSError:
        return "No marker diagnostics were produced."
    finally:
        error_file.unlink(missing_ok=True)


def _drop_external_record_entries(site_packages: Path) -> None:
    # Launcher entries point outside site-packages, and their hashes differ per build on Windows.
    for record in sorted(site_packages.glob("*.dist-info/RECORD")):
        lines = record.read_bytes().splitlines(keepends=True)
        kept = [line for line in lines if not _is_external_record_line(line)]
        if len(kept) != len(lines):
            record.write_bytes(b"".join(kept))


def _is_external_record_line(line: bytes) -> bool:
    text = line.decode("utf-8", errors="replace").strip()
    if not text:
        return False
    path = next(csv.reader([text]))[0].replace("\\", "/")
    return path.startswith(("../", "/")) or PureWindowsPath(path).drive != ""


def _read_requirements_text(path: Path) -> str:
    data = path.read_bytes()
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return data.decode("utf-16")
    return data.decode("utf-8-sig")


def validate_requirements(
    requirements: Path,
    context_dir: Path,
    check_overrides: bool = True,
    constraints: Optional[List[str]] = None,
) -> List[str]:
    """Validate requirements files and return their root requirement lines.

    Constraint-file requirement lines are appended to ``constraints`` when given.
    """
    context_root = context_dir.resolve()
    pending = [(requirements.resolve(strict=True), False)]
    visited = set()
    total_bytes = 0
    roots = []
    while pending:
        path, constraint = pending.pop()
        if (path, constraint) in visited:
            continue
        try:
            path.relative_to(context_root)
        except ValueError as exc:
            raise ValueError(
                f"Requirements include must stay inside the build context: {path}"
            ) from exc
        visited.add((path, constraint))
        size = path.stat().st_size
        total_bytes += size
        if total_bytes > MAX_REQUIREMENTS_BYTES:
            raise ValueError(
                "Requirements files exceed the combined "
                f"{MAX_REQUIREMENTS_BYTES}-byte limit."
            )

        logical_lines = []
        continuation = ""
        for raw_line in _read_requirements_text(path).splitlines():
            if raw_line.rstrip().endswith("\\"):
                continuation += raw_line.rstrip()[:-1] + " "
                continue
            logical_lines.append(continuation + raw_line)
            continuation = ""
        if continuation:
            raise ValueError(f"Requirements file has an incomplete line continuation: {path}")

        for line in logical_lines:
            try:
                options = shlex.split(line, comments=True)
            except ValueError as exc:
                raise ValueError(f"Invalid requirements syntax in {path}.") from exc
            if not options:
                continue
            for option in options:
                name = option.split("=", 1)[0]
                if check_overrides and name in _PIP_OVERRIDE_OPTIONS:
                    raise ValueError(
                        f"Requirements files cannot override target install option {name}."
                    )

            include = None
            include_constraint = constraint
            option = options[0]
            if option in {"-r", "--requirement", "-c", "--constraint"}:
                if len(options) != 2:
                    raise ValueError(f"Invalid requirements include in {path}.")
                include = options[1]
                include_constraint |= option in {"-c", "--constraint"}
            elif option.startswith("--requirement="):
                include = option.split("=", 1)[1]
            elif option.startswith("--constraint="):
                include = option.split("=", 1)[1]
                include_constraint = True
            elif option.startswith("-r") and not option.startswith("--") and len(option) > 2:
                include = option[2:]
            elif option.startswith("-c") and not option.startswith("--") and len(option) > 2:
                include = option[2:]
                include_constraint = True
            elif not option.startswith("-"):
                requirement = re.sub(r"(^|\s)#.*$", "", line)
                requirement = re.split(r"\s+--?[A-Za-z]", requirement, maxsplit=1)[0].strip()
                if not constraint:
                    roots.append(requirement)
                elif constraints is not None:
                    constraints.append(requirement)

            if include is not None:
                if include.startswith(("http://", "https://", "file://")):
                    raise ValueError(
                        "Remote and file-URL requirements includes are unsupported; "
                        "use a local file inside the build context."
                    )
                included_path = (path.parent / include).resolve(strict=True)
                pending.append((included_path, include_constraint))
    return roots


def stage_virtualenv(
    dependencies: Iterable[Tuple[Path, Path]],
    destination: Path,
    python_version: str,
    os_name: str,
    arch: str,
    base_config: Optional[dict] = None,
    context_dir: Optional[Path] = None,
    requirements: Optional[Path] = None,
) -> None:
    validate_target_platform(os_name, arch)
    files = list(dependencies)
    site_roots = set()
    for source, relative in files:
        parts = relative.parts
        if "site-packages" not in parts:
            raise ValueError(f"Virtualenv dependency is outside site-packages: {relative}")
        site_roots.add(parts[: parts.index("site-packages") + 1])
        _validate_virtualenv_file(source, relative)
    if len(site_roots) > 1:
        raise ValueError("Dependencies from multiple virtualenv site-packages roots are unsupported.")

    root = next(iter(site_roots), ())
    version_established = _validate_virtualenv_version(files, root, python_version)
    destination.mkdir(parents=True, exist_ok=True)
    total_bytes = 0
    total_files = 0
    for source, relative in files:
        parts = relative.parts
        inside = Path(*parts[parts.index("site-packages") + 1 :])
        if not inside.parts or "__pycache__" in inside.parts or inside.suffix == ".pyc":
            continue
        total_files += 1
        total_bytes += source.stat().st_size
        if total_files > MAX_STAGED_FILES or total_bytes > MAX_STAGED_BYTES:
            raise ValueError(
                "Staged dependencies exceed the limit of "
                f"{MAX_STAGED_FILES} files or {MAX_STAGED_BYTES} bytes."
            )
        target = destination / inside
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    _check_stage_limits(destination)
    _validate_wheel_metadata(destination, python_version)
    _verify_target_closure(
        destination,
        os_name,
        arch,
        python_version,
        base_config,
        context_dir,
        requirements,
        version_established,
        venv_layout_os(root, _read_venv_config(files, root)),
    )


def _verify_target_closure(
    site_packages: Path,
    os_name: str,
    arch: str,
    python_version: str,
    base_config: Optional[dict],
    context_dir: Optional[Path],
    requirements: Optional[Path],
    version_established: bool,
    host_os_name: Optional[str] = None,
) -> None:
    """Reject staged packages whose target-active requirements are not installed."""
    constraint_lines: List[str] = []
    root_lines = (
        validate_requirements(
            requirements, context_dir, check_overrides=False, constraints=constraint_lines
        )
        if context_dir is not None and requirements is not None and requirements.is_file()
        else []
    )
    roots = declared_roots(context_dir, root_lines) if context_dir is not None else []
    legacy = sorted(
        path.name
        for pattern in ("*.egg-info", "*.egg", "*.egg-link")
        for path in site_packages.glob(pattern)
    )
    if legacy:
        raise ValueError(
            "Cannot verify the target dependency closure of egg distributions: "
            f"{', '.join(legacy)}. Reinstall them as wheels or use a requirements file."
        )
    distributions = []
    for dist_info in sorted(site_packages.glob("*.dist-info")):
        metadata = dist_info / "METADATA"
        if not metadata.is_file() or metadata.is_symlink():
            raise ValueError(f"Virtualenv package is missing METADATA: {dist_info.name}")
        if metadata.stat().st_size > MAX_METADATA_BYTES:
            raise ValueError(
                f"Package metadata exceeds the {MAX_METADATA_BYTES}-byte limit: "
                f"{dist_info.name}"
            )
        try:
            text = metadata.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"Package metadata is not valid UTF-8: {dist_info.name}") from exc
        message = Parser().parsestr(text, headersonly=True)
        if message.defects:
            raise ValueError(f"Package metadata has malformed headers: {dist_info.name}")
        headers = {
            name: [str(value) for value in message.get_all(name, [])]
            for name in _CLOSURE_HEADERS
        }
        distributions.append(
            {
                "path": f"{dist_info.name}/METADATA",
                "directory": dist_info.name[: -len(".dist-info")],
                "headers": headers,
            }
        )
    if not distributions and not any(lines for _, _, lines in roots):
        return

    environment = _target_marker_environment(os_name, arch, python_version, base_config)
    request = {
        "environment": environment,
        "hosts": host_environments(environment, version_established, host_os_name),
        "distributions": distributions,
        "roots": roots,
        "constraints": constraint_lines,
    }
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-P",
                "-c",
                _VENV_CLOSURE_SCRIPT.read_text(encoding="utf-8"),
            ],
            input=json.dumps(request),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=site_packages.parent,
            timeout=CLOSURE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(
            f"Virtualenv dependency verification exceeded {CLOSURE_TIMEOUT_SECONDS} seconds."
        ) from exc
    except OSError as exc:
        raise RuntimeError("Unable to start virtualenv dependency verification.") from exc
    try:
        outcome = json.loads(result.stdout)
    except ValueError:
        outcome = None
    problems = outcome.get("problems") if isinstance(outcome, dict) else None
    if (
        not isinstance(outcome, dict)
        or "fatal" in outcome
        or result.returncode
        or not isinstance(problems, list)
        or not all(isinstance(problem, str) for problem in problems)
    ):
        details = outcome.get("fatal", "") if isinstance(outcome, dict) else ""
        raise RuntimeError(
            "Virtualenv dependency verification needs pip's packaging library in the "
            "builder interpreter and could not evaluate target markers; refusing to "
            f"stage an unverified dependency set. {details}".rstrip()
        )
    if problems:
        raise ValueError(
            f"Virtualenv dependencies are incomplete or unverifiable for target "
            f"{os_name}/{arch}, CPython {python_version}; refusing to stage: "
            + "; ".join(problems)
            + ". Use a requirements file so pip resolves dependencies for the target."
        )


def _validate_virtualenv_version(
    files: List[Tuple[Path, Path]], root: Tuple[str, ...], python_version: str
) -> bool:
    """Reject a mismatched venv Python; return whether its version was established."""
    established = False
    for _, relative in files:
        match = re.search(r"(?:^|/)python(\d+\.\d+)/site-packages/", relative.as_posix())
        if match and match.group(1) != python_version:
            raise ValueError(
                f"Virtualenv targets Python {match.group(1)}, but the image targets "
                f"Python {python_version}."
            )
        established |= bool(match)
    text = _read_venv_config(files, root)
    if text is not None:
        match = re.search(r"(?im)^\s*version(?:_info)?\s*=\s*(\d+\.\d+)", text)
        if match and match.group(1) != python_version:
            raise ValueError(
                f"Virtualenv targets Python {match.group(1)}, but the image targets "
                f"Python {python_version}."
            )
        established |= bool(match)
    return established


def _read_venv_config(files: List[Tuple[Path, Path]], root: Tuple[str, ...]) -> Optional[str]:
    """Return the text of the pyvenv.cfg enclosing the site-packages root, if any."""
    if not files:
        return None
    source, relative = files[0]
    context_root = source
    for _ in relative.parts:
        context_root = context_root.parent
    venv_dirs = [context_root.joinpath(*root[:index]) for index in range(len(root) - 1, 0, -1)]
    venv_config = next(
        (path / "pyvenv.cfg" for path in venv_dirs if (path / "pyvenv.cfg").is_file()), None
    )
    return venv_config.read_text(errors="replace") if venv_config is not None else None


def _validate_virtualenv_file(source: Path, relative: Path) -> None:
    if source.is_symlink():
        raise ValueError(f"Symlinked virtualenv dependencies are unsupported: {relative}")
    if source.name == "direct_url.json" and source.parent.name.endswith(".dist-info"):
        raise ValueError(
            "Virtualenv direct URL metadata is unsupported because it can contain "
            f"credentials: {relative}. Use a requirements file instead."
        )
    suffix = source.suffix.lower()
    if suffix in _NATIVE_SUFFIXES or ".so." in source.name.lower():
        raise ValueError(
            f"Native virtualenv binary is not validated for the target image: {relative}. "
            "Use a requirements file so pip can select a target-compatible wheel."
        )
    if suffix == ".pth":
        with source.open(encoding="utf-8", errors="replace") as content:
            for line in content:
                if line.strip() and not line.lstrip().startswith("#"):
                    raise ValueError(
                        "Virtualenv .pth files with paths or executable lines are "
                        f"unsupported: {relative}"
                    )


def _validate_wheel_metadata(site_packages: Path, python_version: str) -> None:
    major, minor = python_version.split(".")
    allowed_python_tags = {"py3", f"py{major}{minor}", f"cp{major}{minor}"}
    for wheel_file in sorted(site_packages.glob("*.dist-info/WHEEL")):
        values = {}
        with wheel_file.open(encoding="utf-8", errors="replace") as metadata:
            for line in metadata:
                if ":" in line:
                    key, value = line.split(":", 1)
                    values.setdefault(key.strip(), []).append(value.strip())
        tags = values.get("Tag", [])
        if values.get("Root-Is-Purelib", ["false"])[0].lower() != "true" or not tags:
            raise ValueError(f"Virtualenv package is not marked as a pure-Python wheel: {wheel_file}")
        compatible = False
        for tag in tags:
            parts = tag.split("-")
            if len(parts) != 3:
                continue
            python_tags, abi, platform = parts
            if (
                set(python_tags.split(".")) & allowed_python_tags
                and abi == "none"
                and platform == "any"
            ):
                compatible = True
        if not compatible:
            raise ValueError(
                f"Virtualenv package wheel tags are incompatible with CPython "
                f"{python_version}: {wheel_file}"
            )


def staged_packages(
    site_packages: Path, *, strict: bool = False
) -> List[Tuple[str, str]]:
    packages = []
    metadata_files = list(site_packages.glob("*.dist-info/METADATA"))
    metadata_files.extend(site_packages.glob("*.egg-info/PKG-INFO"))
    metadata_files.extend(
        path for path in site_packages.glob("*.egg-info") if path.is_file()
    )
    for metadata in sorted(metadata_files):
        with metadata.open(
            encoding="utf-8",
            errors="strict" if strict else "replace",
        ) as stream:
            message = Parser().parse(stream, headersonly=True)
        name, version = message.get("Name"), message.get("Version")
        if name and version:
            packages.append((name.strip(), version.strip()))
        elif strict:
            raise ValueError(
                f"Staged package metadata is missing Name or Version: {metadata}"
            )
    return sorted(set(packages), key=lambda item: (item[0].lower(), item[1]))


def create_console_entrypoints(site_packages: Path, scripts_dir: Path) -> None:
    scripts = {}
    entry_point_files = list(
        site_packages.glob("*.dist-info/entry_points.txt")
    )
    entry_point_files.extend(site_packages.glob("*.egg-info/entry_points.txt"))
    for entry_points in sorted(entry_point_files):
        if entry_points.stat().st_size > MAX_ENTRY_POINTS_BYTES:
            raise ValueError(
                f"Package entry-point metadata exceeds the "
                f"{MAX_ENTRY_POINTS_BYTES}-byte limit: {entry_points.name}"
            )
        parser = configparser.ConfigParser(interpolation=None)
        parser.optionxform = str
        parser.read(entry_points, encoding="utf-8")
        if not parser.has_section("console_scripts"):
            continue
        for name, value in parser.items("console_scripts"):
            target = value.split("[", 1)[0].strip()
            if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
                raise ValueError(f"Unsupported console entrypoint name: {name}")
            if ":" not in target:
                raise ValueError(f"Unsupported console entrypoint target: {target}")
            module, attribute = (part.strip() for part in target.split(":", 1))
            if not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", module):
                raise ValueError(f"Unsupported console entrypoint target: {target}")
            if not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", attribute):
                raise ValueError(f"Unsupported console entrypoint target: {target}")
            if name in scripts and scripts[name] != (module, attribute):
                raise ValueError(f"Conflicting console entrypoint name: {name}")
            scripts[name] = (module, attribute)

    if not scripts:
        return
    scripts_dir.mkdir(parents=True, exist_ok=True)
    for name, (module, attribute) in sorted(scripts.items()):
        content = (
            "#!/usr/bin/env python\n"
            "import importlib\n"
            "import sys\n"
            f"_value = importlib.import_module({module!r})\n"
            f"for _part in {attribute.split('.')!r}:\n"
            "    _value = getattr(_value, _part)\n"
            "sys.exit(_value())\n"
        )
        script = scripts_dir / name
        script.write_text(content)
        script.chmod(0o755)
    _check_stage_limits(scripts_dir.parent)


def _stop_process(process) -> None:
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _check_stage_limits(directory: Path) -> None:
    # pip creates and moves files while this runs, so entries that vanish
    # between listing and stat are skipped rather than treated as errors.
    total_bytes = 0
    total_files = 0
    for root, dirs, files in os.walk(directory):
        for name in (*dirs, *files):
            path = Path(root) / name
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode):
                raise ValueError(f"Staged dependency symlinks are unsupported: {name}")
            if not stat.S_ISREG(info.st_mode):
                continue
            total_files += 1
            total_bytes += info.st_size
            if total_files > MAX_STAGED_FILES or total_bytes > MAX_STAGED_BYTES:
                raise ValueError(
                    "Staged dependencies exceed the limit of "
                    f"{MAX_STAGED_FILES} files or {MAX_STAGED_BYTES} bytes."
                )
