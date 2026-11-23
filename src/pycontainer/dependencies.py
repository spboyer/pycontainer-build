"""Target-aware staging for Python runtime dependencies."""
import codecs
import configparser
import csv
import json
import logging
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
from typing import Dict, Iterable, List, Optional, Tuple

from .venv_markers import declared_roots, venv_layout_os, verify_virtualenv_markers


MAX_REQUIREMENTS_BYTES = 1024 * 1024
logger = logging.getLogger(__name__)
MAX_ENTRY_POINTS_BYTES = 1024 * 1024
MAX_STAGED_BYTES = 512 * 1024 * 1024
MAX_STAGED_FILES = 100_000
INSTALL_TIMEOUT_SECONDS = 300

_PIP_PLATFORMS = {
    ("linux", "amd64"): "manylinux2014_x86_64",
    ("linux", "arm64"): "manylinux2014_aarch64",
}
_MARKER_MACHINES = {"amd64": "x86_64", "arm64": "aarch64"}
_PIP_TARGET_SCRIPT = Path(__file__).with_name("_pip_target.py")
_MARKER_ERROR_EXIT = 86
_PATCH_ERROR_EXIT = 87
_MARKER_ERROR_FILE = "pycontainer-marker-error.txt"
_SUPPORTED_LIBC = {
    "glibc-2.17": "manylinux_2_17",
    "musl-1.2": "musllinux_1_2",
}
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


def target_python_version(
    base_image: str,
    base_config: Optional[dict],
    *,
    base_image_tag_verified: bool = False,
) -> str:
    """Resolve the CPython major/minor version from the pulled base image."""
    environment = _base_environment(base_config)
    repository, image_tag = _base_image_parts(base_image)
    official_python = (
        _is_official_python_image(repository)
        and base_image_tag_verified
        and "@" not in base_image
    )
    implementation = environment.get("PYTHON_IMPLEMENTATION")
    if not official_python and implementation not in {
        "CPython",
        "cpython",
    }:
        raise ValueError(
            "Cannot validate the target Python ABI for an unverified or custom base image; "
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
        "use a verified tag-only official python:<major>.<minor> pull or a base image with "
        "PYTHON_VERSION metadata."
    )


def target_python_full_version(base_config: Optional[dict]) -> Optional[str]:
    """Return the base image's exact ``PYTHON_VERSION`` when it is a final release."""
    value = _base_environment(base_config).get("PYTHON_VERSION")
    if value and re.fullmatch(r"\d+\.\d+\.\d+", value):
        return value
    return None


def _base_environment(base_config: Optional[dict]) -> dict:
    environment = {}
    if base_config:
        for item in base_config.get("config", {}).get("Env", []):
            if "=" in item:
                key, value = item.split("=", 1)
                environment[key] = value
    return environment


def target_wheel_platform(
    os_name: str,
    arch: str,
    base_image: str,
    base_config: Optional[dict],
    *,
    base_image_tag_verified: bool = False,
) -> str:
    """Validate libc and select a wheel platform; names alone are not provenance."""
    try:
        glibc_platform = _PIP_PLATFORMS[(os_name, arch)]
    except KeyError as exc:
        raise ValueError(
            f"Dependency staging does not support target platform {os_name}/{arch}; "
            "supported targets are linux/amd64 and linux/arm64."
        ) from exc
    repository, image_tag = _base_image_parts(base_image)
    libc = _base_environment(base_config).get("PYTHON_LIBC")
    if libc is not None and libc not in _SUPPORTED_LIBC:
        raise ValueError(
            f"Unsupported PYTHON_LIBC value {libc!r} in the base image config; "
            "supported values are glibc-2.17 and musl-1.2."
        )
    if (
        _is_official_python_image(repository)
        and base_image_tag_verified
        and "@" not in base_image
    ):
        tag_family = "musl" if "alpine" in image_tag.lower() else "glibc"
        if libc and libc.split("-", 1)[0] != tag_family:
            raise ValueError(
                f"Base image tag {image_tag or 'latest'!r} implies {tag_family}, "
                f"which conflicts with PYTHON_LIBC={libc} in its image config."
            )
        if tag_family == "musl":
            return glibc_platform.replace("manylinux2014", "musllinux_1_2")
        return glibc_platform
    if libc:
        return glibc_platform.replace("manylinux2014", _SUPPORTED_LIBC[libc])
    raise ValueError(
        "Cannot validate target wheel compatibility for an unverified or custom "
        "base image; digest references (including tag@digest) and local image "
        "names cannot establish libc. Set PYTHON_LIBC to glibc-2.17 or musl-1.2 "
        "in the image config, or use a verified tag-only registry pull."
    )


def _target_options(
    os_name: str,
    arch: str,
    python_version: str,
    base_image: str = "python:3.11-slim",
    base_config: Optional[dict] = None,
    *,
    base_image_tag_verified: bool = False,
) -> List[str]:
    platform = target_wheel_platform(
        os_name, arch, base_image, base_config,
        base_image_tag_verified=base_image_tag_verified,
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
    *,
    locked: bool = False,
    base_image_tag_verified: bool = False,
) -> None:
    """Install requirements for the target; ``locked`` files are generated, hashed and complete."""
    hash_pinned = locked or validate_requirements(requirements, context_dir)
    if not hash_pinned:
        logger.warning(
            "%s is not fully pinned with ==versions and --hash values, so the "
            "dependency layer is not reproducible. Use a lockfile or "
            "`pip-compile --generate-hashes`.",
            requirements.name,
        )

    destination.mkdir(parents=True, exist_ok=True)
    # Locked installs use only the generated --index-url: no pip config files or PIP_* options.
    locked_options = ["--isolated", "--no-deps"] if locked else []
    # Explicit hash-checking mode makes pip reject any unpinned transitive dependency.
    hash_options = ["--require-hashes"] if hash_pinned else []
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
        *locked_options,
        *hash_options,
        # pip's conflict check evaluates markers of the host's installed packages.
        "--no-warn-conflicts",
        "--target",
        str(destination),
        *_target_options(
            os_name, arch, python_version, base_image, base_config,
            base_image_tag_verified=base_image_tag_verified,
        ),
        "-r",
        str(requirements),
    ]
    environment = dict(os.environ)
    if locked:
        environment = {key: value for key, value in environment.items() if not key.upper().startswith("PIP_")}
        environment["PIP_CONFIG_FILE"] = os.devnull
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
        hint = (
            " Locked artifacts must match their recorded hashes."
            if locked
            else ""
        )
        raise RuntimeError(
            "Target dependency installation failed. Only wheels compatible with "
            f"{os_name}/{arch}, CPython {python_version}, and binary-only resolution "
            f"are supported.{hint}"
        )

    for scripts_dir in (destination / "bin", destination / "Scripts"):
        if scripts_dir.exists():
            shutil.rmtree(scripts_dir)
    _drop_external_record_entries(destination)

    _check_stage_limits(destination.parent)


def install_locked_requirements(
    requirement_sets: Iterable[Tuple[str, str]],
    destination: Path,
    context_dir: Path,
    os_name: str,
    arch: str,
    python_version: str,
    base_image: str,
    base_config: Optional[dict],
    *,
    base_image_tag_verified: bool = False,
) -> None:
    """Install each recorded index's packages from that index alone, then merge them."""
    requirement_sets = list(requirement_sets)
    stage_root = destination.parent
    for number, (_, text) in enumerate(requirement_sets):
        requirements = stage_root / f"locked-requirements-{number}.txt"
        target = destination if len(requirement_sets) == 1 else stage_root / f"locked-site-{number}"
        requirements.write_text(text, encoding="utf-8")
        try:
            install_requirements(
                requirements, target, context_dir, os_name, arch, python_version,
                base_image, base_config, locked=True,
                base_image_tag_verified=base_image_tag_verified,
            )
        finally:
            requirements.unlink(missing_ok=True)
        if target != destination:
            _merge_site_packages(target, destination)
            shutil.rmtree(target)
    destination.mkdir(parents=True, exist_ok=True)


def _merge_site_packages(source: Path, destination: Path) -> None:
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        target = destination / relative
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif target.exists():
            if target.read_bytes() != path.read_bytes():
                raise RuntimeError(
                    f"Locked packages from different indexes both install {relative.as_posix()}."
                )
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(path, target)


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


RequirementsFile = Tuple[Path, bool, List[List[str]]]


def _inspect_requirements(
    requirements: Path, context_dir: Path, check_overrides: bool = True
) -> Tuple[bool, List[str], List[RequirementsFile]]:
    """Validate requirements and collect their pinning status, roots and tokenized files."""
    context_root = context_dir.resolve()
    pending = [(requirements.resolve(strict=True), False)]
    visited = set()
    counted_files = set()
    total_bytes = 0
    hash_pinned = True
    scanned: List[RequirementsFile] = []
    roots: List[str] = []
    while pending:
        path, is_constraint = pending.pop()
        visit = (path, is_constraint)
        if visit in visited:
            continue
        try:
            path.relative_to(context_root)
        except ValueError as exc:
            raise ValueError(
                f"Requirements include must stay inside the build context: {path}"
            ) from exc
        visited.add(visit)
        if path not in counted_files:
            counted_files.add(path)
            total_bytes += path.stat().st_size
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

        file_lines: List[List[str]] = []
        scanned.append((path, is_constraint, file_lines))
        for line in logical_lines:
            try:
                options = shlex.split(line, comments=True)
            except ValueError as exc:
                raise ValueError(f"Invalid requirements syntax in {path}.") from exc
            if not options:
                continue
            file_lines.append(options)
            if not options[0].startswith("-"):
                if not is_constraint:
                    hash_pinned = hash_pinned and _is_hash_pinned(options)
            elif options[0] == "--editable" or options[0].startswith(("-e", "--editable=")):
                hash_pinned = False
            for option in options:
                name = option.split("=", 1)[0]
                if check_overrides and name in _PIP_OVERRIDE_OPTIONS:
                    raise ValueError(
                        f"Requirements files cannot override target install option {name}."
                    )

            include = None
            include_constraint = is_constraint
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
            elif not option.startswith("-") and not is_constraint:
                requirement = re.sub(r"(^|\s)#.*$", "", line)
                roots.append(re.split(r"\s+--?[A-Za-z]", requirement, maxsplit=1)[0].strip())

            if include is not None:
                if include.startswith(("http://", "https://", "file://")):
                    raise ValueError(
                        "Remote and file-URL requirements includes are unsupported; "
                        "use a local file inside the build context."
                    )
                included_path = (path.parent / include).resolve(strict=True)
                pending.append((included_path, include_constraint))
    return hash_pinned, roots, scanned


def validate_requirements(requirements: Path, context_dir: Path) -> bool:
    """Validate includes and options; return whether every requirement is hash-pinned."""
    return _inspect_requirements(requirements, context_dir)[0]


def scan_requirements(
    requirements: Path, context_dir: Path
) -> Tuple[bool, List[RequirementsFile]]:
    """Validate a requirements tree and return pinning and tokenized files."""
    hash_pinned, _, scanned = _inspect_requirements(requirements, context_dir)
    return hash_pinned, scanned


def _is_hash_pinned(options: List[str]) -> bool:
    words = []
    has_hash = False
    skip_value = False
    for option in options:
        if skip_value:
            skip_value = False
        elif option == "--hash":
            has_hash = skip_value = True
        elif option.startswith("--hash="):
            has_hash = True
        elif not option.startswith("-"):
            words.append(option)
    specifier = " ".join(words).split(";", 1)[0].strip()
    exact = re.fullmatch(r"[^=<>!~@,]+===?\s*[^\s,*]+", specifier) is not None
    return exact and has_hash


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
    _verify_virtualenv_targets(
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


def _verify_virtualenv_targets(
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
    root_lines = (
        _inspect_requirements(requirements, context_dir, check_overrides=False)[1]
        if context_dir is not None and requirements is not None and requirements.is_file()
        else []
    )
    roots = declared_roots(context_dir, root_lines) if context_dir is not None else []
    errors = verify_virtualenv_markers(
        site_packages,
        _target_marker_environment(os_name, arch, python_version, base_config),
        roots,
        shared_python_version=version_established,
        host_os_name=host_os_name,
    )
    if errors:
        shown = "; ".join(errors[:10])
        more = f"; and {len(errors) - 10} more" if len(errors) > 10 else ""
        raise ValueError(
            "The virtualenv was resolved with another environment's markers and is "
            f"incomplete or unverifiable for target {os_name}/{arch}, CPython "
            f"{python_version}; refusing to stage it. Use a requirements file so pip "
            f"resolves for the target. {shown}{more}"
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


def create_console_entrypoints(
    site_packages: Path,
    scripts_dir: Path,
    *,
    owners: Optional[Dict[str, List[Path]]] = None,
) -> None:
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
            if owners is not None:
                owners.setdefault(name, []).append(entry_points.parent)

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
