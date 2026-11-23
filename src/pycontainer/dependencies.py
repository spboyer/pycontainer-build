"""Target-aware staging for Python runtime dependencies."""
import configparser
import csv
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


MAX_REQUIREMENTS_BYTES = 1024 * 1024
MAX_ENTRY_POINTS_BYTES = 1024 * 1024
MAX_STAGED_BYTES = 512 * 1024 * 1024
MAX_STAGED_FILES = 100_000
INSTALL_TIMEOUT_SECONDS = 300

# Shared target table. Values are the conservative codename-less glibc floor;
# the suffix after "manylinux2014_" is the machine tag for every generated tag.
_PIP_PLATFORMS = {
    ("linux", "amd64"): "manylinux2014_x86_64",
    ("linux", "arm64"): "manylinux2014_aarch64",
}
_FLOOR_GLIBC_PREFIX = "manylinux2014_"
_MIN_GLIBC_MINOR = 17
_MIN_MUSL_MINOR = 1
_FLOOR_MUSL_MINOR = 2
# Two-digit minors keep the generated --platform list bounded.
_LIBC_PATTERN = re.compile(r"(glibc)-2\.([1-9]\d?)|(musl)-1\.([1-9]\d?)")
_ALPINE_TOKEN = re.compile(r"alpine3\.\d+")
_SUPPORTED_LIBC_TEXT = "glibc-2.N (N 17-99) and musl-1.N (N 1-99)"
# Conservative libc bounds; see the sources in docs/libc-codename-mappings.md.
_DEBIAN_GLIBC_MINOR = {
    "jessie": 17,
    "stretch": 19,
    "buster": 24,
    "bullseye": 28,
    "bookworm": 31,
    "trixie": 36,
}
_ALPINE_MUSL_MINOR = {f"alpine3.{n}": 1 if n <= 12 else 2 for n in range(25)}
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


def _base_environment(base_config: Optional[dict]) -> dict:
    environment = {}
    if base_config:
        for item in base_config.get("config", {}).get("Env", []):
            if "=" in item:
                key, value = item.split("=", 1)
                environment[key] = value
    return environment


def _parse_libc(libc: str) -> Tuple[str, int]:
    match = _LIBC_PATTERN.fullmatch(libc)
    family = match and (match.group(1) or match.group(3))
    minor = match and int(match.group(2) or match.group(4))
    if not match or (family == "glibc" and minor < _MIN_GLIBC_MINOR):
        raise ValueError(
            f"Unsupported PYTHON_LIBC value {libc!r} in the base image config; "
            f"supported values are {_SUPPORTED_LIBC_TEXT}."
        )
    return family, minor


def _tag_tokens(image_tag: str) -> List[str]:
    return image_tag.lower().split("-")


def _is_alpine_tag(image_tag: str) -> bool:
    return any(
        token == "alpine" or _ALPINE_TOKEN.fullmatch(token)
        for token in _tag_tokens(image_tag)
    )


def _codename_libc(image_tag: str) -> Optional[Tuple[str, int]]:
    found = set()
    for token in _tag_tokens(image_tag):
        if token in _DEBIAN_GLIBC_MINOR:
            found.add(("glibc", _DEBIAN_GLIBC_MINOR[token]))
        elif token in _ALPINE_MUSL_MINOR:
            found.add(("musl", _ALPINE_MUSL_MINOR[token]))
    if len(found) > 1:
        raise ValueError(
            f"Base image tag {image_tag!r} names more than one distribution codename."
        )
    return found.pop() if found else None


def _target_libc(
    base_image: str, base_config: Optional[dict], base_image_tag_verified: bool
) -> Tuple[str, Optional[int]]:
    """Return the target libc family and minor; None keeps the codename-less floor."""
    repository, image_tag = _base_image_parts(base_image)
    libc = _base_environment(base_config).get("PYTHON_LIBC")
    explicit = _parse_libc(libc) if libc is not None else None
    if not (
        _is_official_python_image(repository)
        and base_image_tag_verified
        and "@" not in base_image
    ):
        if explicit:
            return explicit
        raise ValueError(
            "Cannot validate target wheel compatibility for an unverified or custom "
            "base image; digest references (including tag@digest) and local image "
            f"names cannot establish libc. Set PYTHON_LIBC ({_SUPPORTED_LIBC_TEXT}) "
            "in the image config, or use a verified tag-only registry pull."
        )
    tag_family = "musl" if _is_alpine_tag(image_tag) else "glibc"
    codename = _codename_libc(image_tag)
    if codename and codename[0] != tag_family:
        raise ValueError(
            f"Base image tag {image_tag!r} names a {codename[0]} codename but "
            f"implies {tag_family}."
        )
    if explicit and explicit[0] != tag_family:
        raise ValueError(
            f"Base image tag {image_tag or 'latest'!r} implies {tag_family}, "
            f"which conflicts with PYTHON_LIBC={libc} in its image config."
        )
    claims = [claim[1] for claim in (explicit, codename) if claim]
    if claims:
        return tag_family, min(claims)
    return tag_family, None


def target_wheel_platforms(
    os_name: str,
    arch: str,
    base_image: str,
    base_config: Optional[dict],
    *,
    base_image_tag_verified: bool = False,
) -> List[str]:
    """Return wheel platform tags through the selected libc bound, newest first.

    Image names alone are not provenance: libc comes from explicit PYTHON_LIBC
    metadata or, for verified tag-only official pulls, a mapped codename.
    """
    validate_target_platform(os_name, arch)
    floor = _PIP_PLATFORMS[(os_name, arch)]
    machine = floor[len(_FLOOR_GLIBC_PREFIX):]
    family, minor = _target_libc(base_image, base_config, base_image_tag_verified)
    if minor is None:
        if family == "musl":
            return [f"musllinux_1_{_FLOOR_MUSL_MINOR}_{machine}"]
        return [floor]
    if family == "musl":
        return [
            f"musllinux_1_{value}_{machine}"
            for value in range(minor, _MIN_MUSL_MINOR - 1, -1)
        ]
    return [
        f"manylinux_2_{value}_{machine}"
        for value in range(minor, _MIN_GLIBC_MINOR - 1, -1)
    ] + [floor]


def target_wheel_platform(
    os_name: str,
    arch: str,
    base_image: str,
    base_config: Optional[dict],
    *,
    base_image_tag_verified: bool = False,
) -> str:
    """Validate libc and return the most specific compatible wheel platform."""
    return target_wheel_platforms(
        os_name, arch, base_image, base_config,
        base_image_tag_verified=base_image_tag_verified,
    )[0]


def _uses_conservative_libc_floor(
    base_image: str, base_config: Optional[dict], base_image_tag_verified: bool
) -> bool:
    return _target_libc(base_image, base_config, base_image_tag_verified)[1] is None


def _target_options(
    os_name: str,
    arch: str,
    python_version: str,
    base_image: str = "python:3.11-slim",
    base_config: Optional[dict] = None,
    *,
    base_image_tag_verified: bool = False,
) -> List[str]:
    platforms = target_wheel_platforms(
        os_name, arch, base_image, base_config,
        base_image_tag_verified=base_image_tag_verified,
    )
    major, minor = python_version.split(".")
    return [
        *(option for platform in platforms for option in ("--platform", platform)),
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
    base_image_tag_verified: bool = False,
) -> None:
    validate_requirements(requirements, context_dir)

    destination.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-input",
        "--no-cache-dir",
        "--no-compile",
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

    if returncode:
        raise RuntimeError(
            "Target dependency installation failed. Only wheels compatible with "
            f"{os_name}/{arch}, CPython {python_version}, and binary-only resolution "
            "are supported."
            + (
                " The base image tag names no mapped codename, so only the "
                "conservative manylinux2014/musllinux_1_2 floor was used; pin a "
                "codename tag (for example python:3.11-slim-bookworm or "
                "python:3.11-alpine3.20) or set PYTHON_LIBC in the image config."
                if _uses_conservative_libc_floor(
                    base_image, base_config, base_image_tag_verified
                )
                else ""
            )
        )

    for scripts_dir in (destination / "bin", destination / "Scripts"):
        if scripts_dir.exists():
            shutil.rmtree(scripts_dir)
    _drop_external_record_entries(destination)

    _check_stage_limits(destination.parent)


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


def validate_requirements(requirements: Path, context_dir: Path) -> None:
    context_root = context_dir.resolve()
    pending = [requirements.resolve(strict=True)]
    visited = set()
    total_bytes = 0
    while pending:
        path = pending.pop()
        if path in visited:
            continue
        try:
            path.relative_to(context_root)
        except ValueError as exc:
            raise ValueError(
                f"Requirements include must stay inside the build context: {path}"
            ) from exc
        visited.add(path)
        size = path.stat().st_size
        total_bytes += size
        if total_bytes > MAX_REQUIREMENTS_BYTES:
            raise ValueError(
                "Requirements files exceed the combined "
                f"{MAX_REQUIREMENTS_BYTES}-byte limit."
            )

        logical_lines = []
        continuation = ""
        for raw_line in path.read_text(encoding="utf-8").splitlines():
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
                if name in _PIP_OVERRIDE_OPTIONS:
                    raise ValueError(
                        f"Requirements files cannot override target install option {name}."
                    )

            include = None
            option = options[0]
            if option in {"-r", "--requirement", "-c", "--constraint"}:
                if len(options) != 2:
                    raise ValueError(f"Invalid requirements include in {path}.")
                include = options[1]
            elif option.startswith("--requirement="):
                include = option.split("=", 1)[1]
            elif option.startswith("--constraint="):
                include = option.split("=", 1)[1]
            elif option.startswith("-r") and not option.startswith("--") and len(option) > 2:
                include = option[2:]
            elif option.startswith("-c") and not option.startswith("--") and len(option) > 2:
                include = option[2:]

            if include is not None:
                if include.startswith(("http://", "https://", "file://")):
                    raise ValueError(
                        "Remote and file-URL requirements includes are unsupported; "
                        "use a local file inside the build context."
                    )
                included_path = (path.parent / include).resolve(strict=True)
                pending.append(included_path)


def stage_virtualenv(
    dependencies: Iterable[Tuple[Path, Path]],
    destination: Path,
    python_version: str,
    os_name: str,
    arch: str,
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
    _validate_virtualenv_version(files, root, python_version)
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


def _validate_virtualenv_version(
    files: List[Tuple[Path, Path]], root: Tuple[str, ...], python_version: str
) -> None:
    for _, relative in files:
        match = re.search(r"(?:^|/)python(\d+\.\d+)/site-packages/", relative.as_posix())
        if match and match.group(1) != python_version:
            raise ValueError(
                f"Virtualenv targets Python {match.group(1)}, but the image targets "
                f"Python {python_version}."
            )
    if not files:
        return
    context_root = files[0][0]
    for _ in root:
        context_root = context_root.parent
    venv_config = context_root / "pyvenv.cfg"
    if venv_config.is_file():
        text = venv_config.read_text(errors="replace")
        match = re.search(r"(?im)^\s*version\s*=\s*(\d+\.\d+)", text)
        if match and match.group(1) != python_version:
            raise ValueError(
                f"Virtualenv targets Python {match.group(1)}, but the image targets "
                f"Python {python_version}."
            )


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
