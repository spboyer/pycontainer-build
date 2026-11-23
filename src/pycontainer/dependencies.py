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
import tempfile
import time
from email.parser import Parser
from pathlib import Path, PureWindowsPath
from typing import Iterable, List, Mapping, Optional, Tuple

from .credentials import (
    PLACEHOLDER,
    find_credentials,
    known_secrets,
    redact,
    url_has_credentials,
)
from .venv_markers import declared_roots, host_environments, venv_layout_os


MAX_REQUIREMENTS_BYTES = 1024 * 1024
logger = logging.getLogger(__name__)
MAX_ENTRY_POINTS_BYTES = 1024 * 1024
MAX_METADATA_BYTES = 1024 * 1024
CLOSURE_TIMEOUT_SECONDS = 120
MAX_STAGED_BYTES = 512 * 1024 * 1024
MAX_STAGED_FILES = 100_000
INSTALL_TIMEOUT_SECONDS = 300
_LOCKFILE_STAGING_PREFIX = ".pycontainer-lockfile-"

# Shared target table. Values are legacy aliases for validated glibc targets;
# the suffix after "manylinux2014_" is the machine tag for every generated tag.
_PIP_PLATFORMS = {
    ("linux", "amd64"): "manylinux2014_x86_64",
    ("linux", "arm64"): "manylinux2014_aarch64",
    ("linux", "arm"): "manylinux2014_armv7l",
}
# 32-bit ARM wheels are tagged armv7l, so only the v7 variant may use them.
_REQUIRED_VARIANTS = {("linux", "arm"): "v7"}
SUPPORTED_TARGETS_TEXT = "linux/amd64, linux/arm64 and linux/arm/v7"
_NO_SOURCE_FALLBACK = (
    "Source builds and host binaries are never used as a fallback."
)
_ARMV7_WHEEL_NOTE = " Many packages do not publish armv7l wheels."
_MARKER_MACHINES = {"amd64": "x86_64", "arm64": "aarch64", "arm": "armv7l"}
_PIP_TARGET_SCRIPT = Path(__file__).with_name("_pip_target.py")
_VENV_CLOSURE_SCRIPT = Path(__file__).with_name("_venv_closure.py")
_CLOSURE_HEADERS = ("Metadata-Version", "Name", "Version", "Requires-Dist", "Provides-Extra", "Requires-Python")
_MARKER_ERROR_EXIT = 86
_PATCH_ERROR_EXIT = 87
_SNAPSHOT_ERROR_EXIT = 88
_MARKER_ERROR_FILE = "pycontainer-marker-error.txt"
_FLOOR_GLIBC_PREFIX = "manylinux2014_"
_MIN_GLIBC_MINOR = 17
_MIN_MUSL_MINOR = 1
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
# Host pip settings that only choose and authenticate to an index; every other
# PIP_* variable or pip config key could change target selection and is ignored.
_PIP_ENVIRONMENT_ALLOWED = {
    "PIP_CERT",
    "PIP_CLIENT_CERT",
    "PIP_CONFIG_FILE",
    "PIP_DEFAULT_TIMEOUT",
    "PIP_EXTRA_INDEX_URL",
    "PIP_FIND_LINKS",
    "PIP_INDEX_URL",
    "PIP_KEYRING_PROVIDER",
    "PIP_NO_INDEX",
    "PIP_PROXY",
    "PIP_REQUIRE_HASHES",
    "PIP_RETRIES",
    "PIP_TIMEOUT",
    "PIP_TRUSTED_HOST",
}
_PIP_CONFIG_ALLOWED = sorted(
    name[4:].lower().replace("_", "-")
    for name in _PIP_ENVIRONMENT_ALLOWED - {"PIP_CONFIG_FILE"}
)
# Locked installs never take source-selection settings from the host: only the
# lock's recorded index may supply artifacts.
_PIP_SOURCE_CONFIG_KEYS = {
    "extra-index-url",
    "find-links",
    "index-url",
    "no-index",
    "trusted-host",
}
_PIP_LOCKED_CONFIG_ALLOWED = sorted(set(_PIP_CONFIG_ALLOWED) - _PIP_SOURCE_CONFIG_KEYS)
_PIP_STDERR_LIMIT = 1024 * 1024
_PIP_EXCERPT_LINES = 6
_PIP_EXCERPT_LINE_CHARS = 300
_AUTH_HINT = (
    "Provide index credentials to the builder through PIP_INDEX_URL or "
    "PIP_EXTRA_INDEX_URL, a pip config file, .netrc (or NETRC), keyring with "
    "PIP_KEYRING_PROVIDER=import or subprocess, or ${VAR} placeholders in the "
    "requirements file."
)
_LOCKED_AUTH_HINT = (
    "Locked installs ignore host pip settings and config files; provide index "
    "credentials through ${VAR} placeholders in the lock's index URL or "
    ".netrc (or NETRC)."
)


class PipConfigurationSnapshotError(Exception):
    """The cached install's pip configuration could not be safely frozen."""


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
    match = re.match(r"(\d+\.\d+)(?:\.(\d+))?(?:-|$)", image_tag)
    if value and re.fullmatch(r"\d+\.\d+(?:\.\d+)?", value):
        if official_python and match:
            version_parts = tuple(int(part) for part in value.split("."))
            tag_parts = tuple(int(part) for part in match.group(1).split("."))
            if match.group(2) is not None:
                tag_parts += (int(match.group(2)),)
            if version_parts[:len(tag_parts)] != tag_parts[:len(version_parts)]:
                raise ValueError(
                    f"Verified base image tag {image_tag!r} conflicts with "
                    f"PYTHON_VERSION={value} in its image config."
                )
        return ".".join(value.split(".")[:2])

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
) -> Tuple[str, int]:
    """Return target libc only from explicit metadata or a mapped codename."""
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
    raise ValueError(
        f"Cannot validate target {tag_family} version from base image tag "
        f"{image_tag or 'latest'!r}; pin a mapped codename tag (for example "
        "python:3.11-slim-bookworm or python:3.11-alpine3.20) or set "
        f"PYTHON_LIBC ({_SUPPORTED_LIBC_TEXT}) in the image config. "
        "Codename-less and unmapped tags do not establish a compatible libc floor."
    )


def target_wheel_platforms(
    os_name: str,
    arch: str,
    base_image: str,
    base_config: Optional[dict],
    *,
    base_image_tag_verified: bool = False,
    variant: Optional[str] = None,
) -> List[str]:
    """Return wheel platform tags through the selected libc bound, newest first.

    Image names alone are not provenance: libc comes from explicit PYTHON_LIBC
    metadata or, for verified tag-only official pulls, a mapped codename.
    """
    validate_target_platform(os_name, arch, variant)
    floor = _PIP_PLATFORMS[(os_name, arch)]
    machine = floor[len(_FLOOR_GLIBC_PREFIX):]
    family, minor = _target_libc(base_image, base_config, base_image_tag_verified)
    if family == "musl":
        return [
            f"musllinux_1_{value}_{machine}"
            for value in range(minor, _MIN_MUSL_MINOR - 1, -1)
        ]
    return [
        f"manylinux_2_{value}_{machine}"
        for value in range(minor, _MIN_GLIBC_MINOR - 1, -1)
    ] + [floor]


def target_libc_config(
    os_name: str, arch: str, base_image: str, base_config: Optional[dict],
    base_layers: Iterable, *, base_image_tag_verified: bool = False,
    variant: Optional[str] = None,
) -> Optional[dict]:
    """Add layer-derived libc only to staging config for verified official tag pulls."""
    validate_target_platform(os_name, arch, variant)
    repository, image_tag = _base_image_parts(base_image)
    if (
        not base_image_tag_verified
        or "@" in base_image
        or not _is_official_python_image(repository)
        or "PYTHON_LIBC" in _base_environment(base_config)
        or _codename_libc(image_tag)
    ):
        return base_config
    from .base_libc import infer_libc_from_layers

    family = "musl" if _is_alpine_tag(image_tag) else "glibc"
    machine = _PIP_PLATFORMS[(os_name, arch)][len(_FLOOR_GLIBC_PREFIX):]
    libc = infer_libc_from_layers(base_layers, family, arch, machine)
    _parse_libc(libc)
    config = dict(base_config or {})
    image_config = dict(config.get("config", {}))
    image_config["Env"] = [*image_config.get("Env", []), f"PYTHON_LIBC={libc}"]
    config["config"] = image_config
    return config


def target_wheel_platform(
    os_name: str,
    arch: str,
    base_image: str,
    base_config: Optional[dict],
    *,
    base_image_tag_verified: bool = False,
    variant: Optional[str] = None,
) -> str:
    """Validate libc and return the most specific compatible wheel platform."""
    return target_wheel_platforms(
        os_name, arch, base_image, base_config,
        base_image_tag_verified=base_image_tag_verified,
        variant=variant,
    )[0]


def _target_options(
    os_name: str,
    arch: str,
    python_version: str,
    base_image: str = "python:3.11-slim",
    base_config: Optional[dict] = None,
    *,
    base_image_tag_verified: bool = False,
    variant: Optional[str] = None,
) -> List[str]:
    platforms = target_wheel_platforms(
        os_name, arch, base_image, base_config,
        base_image_tag_verified=base_image_tag_verified,
        variant=variant,
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


def _target_text(os_name: str, arch: str, variant: Optional[str]) -> str:
    return f"{os_name}/{arch}" + (f"/{variant}" if variant else "")


def validate_target_platform(
    os_name: str, arch: str, variant: Optional[str] = None
) -> None:
    required = _REQUIRED_VARIANTS.get((os_name, arch))
    if (os_name, arch) not in _PIP_PLATFORMS or (
        required is not None and variant != required
    ):
        raise ValueError(
            "Dependency staging does not support target platform "
            f"{_target_text(os_name, arch, variant)}; supported targets are "
            f"{SUPPORTED_TARGETS_TEXT}."
        )


def _target_marker_environment(
    os_name: str, arch: str, python_version: str, base_config: Optional[dict],
    variant: Optional[str] = None,
) -> dict:
    """PEP 508 marker values for the target; omitted keys are unknown and fail closed."""
    validate_target_platform(os_name, arch, variant)
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
    variant: Optional[str] = None,
    *,
    locked: bool = False,
    base_image_tag_verified: bool = False,
    requirements_context: Optional[Path] = None,
    environment_snapshot: Optional[Mapping[str, str]] = None,
    pip_configuration: Optional[dict] = None,
) -> List[Path]:
    """Install target wheels and return the validated requirements files."""
    target_options = _target_options(
        os_name, arch, python_version, base_image, base_config,
        base_image_tag_verified=base_image_tag_verified,
        variant=variant,
    )
    _, requirement_files, hash_pinned = _scan_requirements(
        requirements, requirements_context or context_dir
    )
    if not locked and not hash_pinned:
        logger.warning(
            "%s is not fully pinned with ==versions and --hash values, so the "
            "dependency layer is not reproducible. Use a lockfile or "
            "`pip-compile --generate-hashes`.",
            requirements.name,
        )
    target = _target_text(os_name, arch, variant)

    destination.mkdir(parents=True, exist_ok=True)
    # Locked installs use only the generated --index-url: no pip config files or PIP_* options.
    locked_options = ["--isolated", "--no-deps"] if locked else []
    # Explicit hash-checking mode makes pip reject any unpinned transitive dependency.
    hash_options = ["--require-hashes"] if locked or hash_pinned else []
    error_file = destination.parent / _MARKER_ERROR_FILE
    marker_config = {
        "environment": _target_marker_environment(
            os_name, arch, python_version, base_config, variant
        ),
        "error_file": str(error_file),
        "allowed_config_keys": _PIP_LOCKED_CONFIG_ALLOWED if locked else _PIP_CONFIG_ALLOWED,
    }
    command = [
        sys.executable,
        # Ignore PYTHONPATH/PYTHONHOME so host modules cannot shadow the pinned pip.
        "-E",
        "-P",
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
        *target_options,
        "-r",
        str(requirements),
    ]
    environment = (
        _locked_pip_environment(destination.parent, environment_snapshot)
        if locked
        else _pip_environment(destination.parent, environment_snapshot)
    )
    if pip_configuration is not None:
        environment["PIP_CONFIG_FILE"] = os.devnull
    # pip logs index HTTP failures only at debug level, so keep its debug log in
    # host temp space (never the tarred stage) to classify authentication errors.
    with tempfile.TemporaryDirectory(prefix="pycontainer-pip-") as diagnostics:
        if pip_configuration is not None:
            snapshot_file = Path(diagnostics) / "configuration.json"
            snapshot_file.write_text(json.dumps(pip_configuration), encoding="utf-8")
            marker_config["configuration_snapshot"] = str(snapshot_file)
            command[5] = json.dumps(marker_config)
        pip_log = Path(diagnostics) / "pip.log"
        with open(Path(diagnostics) / "stderr.txt", "w+b") as pip_errors:
            try:
                process = subprocess.Popen(
                    [*command, "--log", str(pip_log)],
                    cwd=context_dir,
                    env=environment,
                    stdout=subprocess.DEVNULL,
                    stderr=pip_errors,
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
                                f"Target dependency installation for {target} exceeded "
                                f"{INSTALL_TIMEOUT_SECONDS} seconds."
                            )
            except BaseException:
                _stop_process(process)
                raise
            pip_errors.seek(0)
            pip_output = pip_errors.read(_PIP_STDERR_LIMIT).decode("utf-8", "replace")
        pip_debug = _read_tail(pip_log, _PIP_STDERR_LIMIT) if returncode else ""

    secrets = known_secrets(environment, _placeholder_names(requirement_files))
    if pip_configuration is not None:
        for values in pip_configuration["configuration"]:
            for name, value in values.items():
                setting = name.partition(".")[2].upper().replace("-", "_")
                if setting in {"INDEX_URL", "EXTRA_INDEX_URL", "FIND_LINKS", "PROXY"}:
                    secrets.update(known_secrets({f"PIP_{setting}": value}))
    if returncode == _SNAPSHOT_ERROR_EXIT and pip_configuration is not None:
        _read_marker_error(error_file)
        raise PipConfigurationSnapshotError("the pip configuration snapshot could not be verified")
    if returncode in {_MARKER_ERROR_EXIT, _PATCH_ERROR_EXIT}:
        details = redact(_read_marker_error(error_file), secrets)
        if returncode == _MARKER_ERROR_EXIT:
            raise ValueError(
                "Cannot evaluate dependency environment markers for target "
                f"{target}, CPython {python_version}; refusing to stage an "
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
            _pip_failure_message(
                redact(pip_output, secrets),
                os_name,
                arch,
                python_version,
                redact(pip_debug, secrets),
                _LOCKED_AUTH_HINT if locked else _AUTH_HINT,
                variant,
            ) + hint
        )

    for scripts_dir in (destination / "bin", destination / "Scripts"):
        if scripts_dir.exists():
            shutil.rmtree(scripts_dir)
    _drop_external_record_entries(destination)
    reject_credentialed_direct_urls(destination)

    _check_stage_limits(destination.parent)
    return requirement_files


def _pip_environment(
    temporary_dir: Path, source_environment: Optional[Mapping[str, str]] = None
) -> dict:
    environment = {}
    ignored = []
    for name, value in (os.environ if source_environment is None else source_environment).items():
        upper = name.upper()
        if upper.startswith(("PIP_", "_PIP_")) and upper not in _PIP_ENVIRONMENT_ALLOWED:
            ignored.append(upper)
            continue
        environment[name] = value
    if ignored:
        print(
            "Ignoring host pip settings that could change target dependency "
            f"selection: {', '.join(sorted(ignored))}"
        )
    environment.update(
        {
            "TMP": str(temporary_dir),
            "TEMP": str(temporary_dir),
            "TMPDIR": str(temporary_dir),
        }
    )
    return environment


def _locked_pip_environment(
    temporary_dir: Path, source_environment: Optional[Mapping[str, str]] = None
) -> dict:
    """Environment for a locked install: no host PIP_* settings or pip config files."""
    environment = {
        name: value
        for name, value in (os.environ if source_environment is None else source_environment).items()
        if not name.upper().startswith(("PIP_", "_PIP_"))
    }
    environment.update(
        {
            "PIP_CONFIG_FILE": os.devnull,
            "TMP": str(temporary_dir),
            "TEMP": str(temporary_dir),
            "TMPDIR": str(temporary_dir),
        }
    )
    return environment


def _placeholder_names(requirement_files: Iterable[Path]) -> List[str]:
    names = set()
    for path in requirement_files:
        names.update(PLACEHOLDER.findall(_decode_requirements(path.read_bytes(), errors="replace")))
    return sorted(names)


_PROXY_AUTH_FAILURE = re.compile(r"\b407\b[^\n]*proxy authentication", re.IGNORECASE)
_INDEX_AUTH_FAILURE = re.compile(
    r"\b(401|403)\b[^\n]*(unauthori[sz]ed|forbidden|client error|credentials not correct)",
    re.IGNORECASE,
)
_CONNECTION_FAILURE = re.compile(
    r"NewConnectionError|Failed to establish a new connection|ConnectTimeout"
    r"|ReadTimeout|SSLError|CERTIFICATE_VERIFY_FAILED|ProxyError"
    r"|Name or service not known|getaddrinfo failed|nodename nor servname"
)


def _pip_failure_message(
    pip_output: str,
    os_name: str,
    arch: str,
    python_version: str,
    pip_debug: str = "",
    auth_hint: str = _AUTH_HINT,
    variant: Optional[str] = None,
) -> str:
    """Classify a failed install from redacted pip stderr and debug log text."""
    excerpt = _pip_excerpt(pip_output, pip_debug)
    details = f" pip reported (redacted): {excerpt}" if excerpt else ""
    diagnostics = f"{pip_output}\n{pip_debug}"
    if _PROXY_AUTH_FAILURE.search(diagnostics):
        return (
            "Proxy authentication failed while installing target dependencies "
            "(HTTP 407). Check the proxy credentials available to the builder."
            + details
        )
    if _INDEX_AUTH_FAILURE.search(diagnostics):
        return (
            "Authentication to a package index failed (HTTP 401/403) while "
            f"installing target dependencies. {auth_hint}" + details
        )
    if _CONNECTION_FAILURE.search(diagnostics):
        return (
            "Could not reach the package index while installing target "
            "dependencies. Check the index URL, network, proxy and TLS settings."
            + details
        )
    return (
        "Target dependency installation failed. Only wheels compatible with "
        f"{_target_text(os_name, arch, variant)}, CPython {python_version}, and binary-only resolution "
        f"are supported. {_NO_SOURCE_FALLBACK}"
        + (_ARMV7_WHEEL_NOTE if arch == "arm" else "")
        + details
    )


def _pip_excerpt(pip_output: str, pip_debug: str = "") -> str:
    errors = [
        line.strip() for line in pip_output.splitlines() if line.strip()
    ]
    relevant = [line for line in errors if line.startswith("ERROR:")] or errors[-3:]
    seen = set()
    for line in pip_debug.splitlines():
        if (
            _PROXY_AUTH_FAILURE.search(line)
            or _INDEX_AUTH_FAILURE.search(line)
            or _CONNECTION_FAILURE.search(line)
        ):
            message = line.strip().split(" ", 1)[-1]
            if message not in seen:
                seen.add(message)
                relevant.insert(0, message)
    return " | ".join(
        line[:_PIP_EXCERPT_LINE_CHARS] for line in relevant[:_PIP_EXCERPT_LINES]
    )


def _read_tail(path: Path, limit: int) -> str:
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - limit))
            return handle.read().decode("utf-8", "replace")
    except FileNotFoundError:
        return ""


def reject_credentialed_direct_urls(site_packages: Path) -> None:
    for direct_url in sorted(site_packages.glob("*.dist-info/direct_url.json")):
        try:
            url = json.loads(direct_url.read_text(encoding="utf-8")).get("url", "")
        except (OSError, ValueError, AttributeError) as exc:
            raise ValueError(
                f"Cannot validate installed package source metadata: {direct_url.parent.name}"
            ) from exc
        if isinstance(url, str) and url_has_credentials(url):
            raise ValueError(
                f"Installed package metadata {direct_url.parent.name}/direct_url.json "
                "records a URL with credentials; refusing to place it in the image."
            )


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
    variant: Optional[str] = None,
    base_image_tag_verified: bool = False,
    environment_snapshot: Optional[Mapping[str, str]] = None,
) -> None:
    """Install each recorded index's packages from that index alone, then merge them."""
    requirement_sets = list(requirement_sets)
    stage_root = destination.parent
    with tempfile.TemporaryDirectory(prefix=_LOCKFILE_STAGING_PREFIX, dir=context_dir.resolve()) as temp:
        for number, (_, text) in enumerate(requirement_sets):
            requirements = Path(temp) / f"locked-requirements-{number}.txt"
            target = destination if len(requirement_sets) == 1 else stage_root / f"locked-site-{number}"
            requirements.write_text(text, encoding="utf-8")
            install_requirements(
                requirements, target, context_dir, os_name, arch, python_version,
                base_image, base_config, variant=variant, locked=True,
                base_image_tag_verified=base_image_tag_verified,
                environment_snapshot=environment_snapshot,
            )
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


def _decode_requirements(data: bytes, errors: str = "strict") -> str:
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return data.decode("utf-16", errors)
    return data.decode("utf-8-sig", errors)


RequirementsFile = Tuple[Path, bool, List[List[str]], bytes]


def _inspect_requirements(
    requirements: Path,
    context_dir: Path,
    check_overrides: bool = True,
    constraints: Optional[List[str]] = None,
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
        path, constraint = pending.pop()
        visit = (path, constraint)
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

        content = path.read_bytes()
        text = _decode_requirements(content)
        reject_literal_credentials(path, text, context_root)
        logical_lines = []
        continuation = ""
        for raw_line in text.splitlines():
            if raw_line.rstrip().endswith("\\"):
                continuation += raw_line.rstrip()[:-1] + " "
                continue
            logical_lines.append(continuation + raw_line)
            continuation = ""
        if continuation:
            raise ValueError(f"Requirements file has an incomplete line continuation: {path}")

        file_lines: List[List[str]] = []
        scanned.append((path, constraint, file_lines, content))
        for line in logical_lines:
            try:
                options = shlex.split(line, comments=True)
            except ValueError as exc:
                raise ValueError(f"Invalid requirements syntax in {path}.") from exc
            if not options:
                continue
            file_lines.append(options)
            if not options[0].startswith("-"):
                if not constraint:
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
    return hash_pinned, roots, scanned


def _scan_requirements(
    requirements: Path,
    context_dir: Path,
    check_overrides: bool = True,
    constraints: Optional[List[str]] = None,
) -> Tuple[List[str], List[Path], bool]:
    """Validate a local requirements graph and report roots, files and hash pinning."""
    hash_pinned, roots, scanned = _inspect_requirements(
        requirements, context_dir, check_overrides, constraints
    )
    return roots, sorted({path for path, _, _, _ in scanned}), hash_pinned


def validate_requirements(
    requirements: Path,
    context_dir: Path,
    check_overrides: bool = True,
    constraints: Optional[List[str]] = None,
) -> List[str]:
    """Validate requirements files and return their root requirement lines.

    Constraint-file requirement lines are appended to ``constraints`` when given.
    """
    return _scan_requirements(requirements, context_dir, check_overrides, constraints)[0]


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
def reject_literal_credentials(path: Path, text: str, context_root: Path) -> None:
    findings = find_credentials(text)
    if not findings:
        return
    try:
        shown = path.resolve().relative_to(context_root.resolve()).as_posix()
    except ValueError:
        shown = path.name
    locations = ", ".join(f"line {line} ({url})" for line, url in findings[:5])
    raise ValueError(
        f"{shown} contains a URL with literal credentials at {locations}; "
        "refusing to build because build-context files can be copied into "
        "image layers. Reference secrets as ${VAR} placeholders, which pip "
        "expands at install time, or supply them through PIP_INDEX_URL, a pip "
        "config file, .netrc or keyring."
    )


def reject_shipped_credentials(
    files: Iterable[Tuple[Path, Path]],
    context_dir: Path,
    requirement_paths: Iterable[Path] = (),
) -> None:
    """Fail before app-layer creation when pip inputs that would ship hold credentials."""
    context_root = Path(context_dir).resolve()
    requirement_set = {Path(path).resolve() for path in requirement_paths}
    # pip runs from the context, so relative credential-file variables resolve there.
    configured = {
        name: (context_root / os.environ[name]).resolve()
        for name in ("PIP_CONFIG_FILE", "NETRC")
        if os.environ.get(name) and os.environ[name] != os.devnull
    }
    for source, relative in files:
        name = relative.name.lower()
        resolved = Path(source).resolve()
        if name in {".netrc", "_netrc"} or resolved == configured.get("NETRC"):
            raise ValueError(
                f"{relative.as_posix()} is a credentials file and would be copied into "
                "the image; exclude it from the build context."
            )
        if not (
            name in {"pip.conf", "pip.ini"}
            or re.fullmatch(r"(requirements|constraints).*\.(txt|in)", name)
            or resolved in requirement_set
            or resolved == configured.get("PIP_CONFIG_FILE")
        ):
            continue
        with open(source, "rb") as handle:
            data = handle.read(MAX_REQUIREMENTS_BYTES + 1)
        if len(data) > MAX_REQUIREMENTS_BYTES:
            raise ValueError(
                f"{relative.as_posix()} is too large to scan for credentials "
                f"(over {MAX_REQUIREMENTS_BYTES} bytes) and would be copied into the "
                "image; shrink it or exclude it from the build context."
            )
        text = _decode_requirements(data, "replace")
        reject_literal_credentials(Path(source), text, context_root)


def stage_virtualenv(
    dependencies: Iterable[Tuple[Path, Path]],
    destination: Path,
    python_version: str,
    os_name: str,
    arch: str,
    base_config: Optional[dict] = None,
    context_dir: Optional[Path] = None,
    requirements: Optional[Path] = None,
    variant: Optional[str] = None,
) -> None:
    validate_target_platform(os_name, arch, variant)
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
        variant,
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
    variant: Optional[str] = None,
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
    distributions = _read_staged_distributions(site_packages)
    if not distributions and not any(lines for _, _, lines in roots):
        return

    environment = _target_marker_environment(os_name, arch, python_version, base_config, variant)
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
            f"{_target_text(os_name, arch, variant)}, CPython {python_version}; refusing to stage: "
            + "; ".join(problems)
            + ". Use a requirements file so pip resolves dependencies for the target."
        )


def _read_staged_distributions(site_packages: Path) -> List[dict]:
    """Collect bounded wheel metadata without importing the staged packages."""
    distributions = []
    for dist_info in sorted(site_packages.glob("*.dist-info")):
        metadata = dist_info / "METADATA"
        if not metadata.is_file() or metadata.is_symlink():
            raise ValueError(f"Package is missing METADATA: {dist_info.name}")
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
    return distributions


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
        script.write_text(content, encoding="utf-8", newline="\n")
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
