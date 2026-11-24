"""Cache identity for dependency layers staged from provably hash-pinned inputs.

A staged dependency layer is reused only when every input that can change pip's
result is part of the key. Anything that cannot be proven raises
``DependencyCacheMiss`` with a fixed reason that never contains URLs, paths or
credentials. Keys are SHA-256 hashes of a sanitized identity document.
"""
import ast
import hashlib
import json
import mimetypes
import os
import platform
import re
import shlex
import subprocess
import sys
import urllib.request
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from typing import Callable, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit, urlunsplit

from .credentials import url_has_credentials
from .dependencies import (
    MAX_STAGED_BYTES,
    MAX_STAGED_FILES,
    _is_hash_pinned,
    _PIP_CONFIG_ALLOWED,
    _target_marker_environment,
    _target_options,
    scan_requirements,
)

# Bump when the staged dependency layer layout or tar encoding changes.
KEY_VERSION = 1
# Bump when installer or closure-verification behavior changes.
INSTALLER_BACKEND = "pip-target-v10"
INVENTORY_VERSION = 1
MAX_INVENTORY_BYTES = 16 * 1024 * 1024
PIP_CONFIG_TIMEOUT_SECONDS = 60

_AMBIENT_SECTIONS = {":env:", "global", "install"}
_AMBIENT_URLS = {"index-url", "extra-index-url", "find-links"}
_PROXY_ENVIRONMENT = {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"}
_AMBIENT_KEYED = {
    "force-reinstall",
    "ignore-requires-python",
    "isolated",
    "no-binary",
    "no-build-isolation",
    "no-index",
    "no-warn-conflicts",
    "no-warn-script-location",
    "only-binary",
    "pre",
    "prefer-binary",
    "require-hashes",
    "trusted-host",
    "upgrade",
    "upgrade-strategy",
    "use-deprecated",
    "use-feature",
}
# Special-case source-routing settings before ignoring these options.
_AMBIENT_IGNORED = {
    # The target bootstrap removes this host-system installation override.
    "break-system-packages",
    "cache-dir",
    "cert",
    "client-cert",
    "config-file",
    "default-timeout",
    "disable-pip-version-check",
    "exists-action",
    "keyring-provider",
    "log",
    "log-file",
    "no-cache-dir",
    "no-color",
    "no-input",
    "progress-bar",
    "proxy",
    "quiet",
    "require-virtualenv",
    "retries",
    "root-user-action",
    "timeout",
    "verbose",
}
_FIND_LINKS = ("-f", "--find-links")
_INDEX_OPTIONS = ("-i", "--index-url", "--extra-index-url")
_INCLUDE_OPTIONS = ("-r", "--requirement", "-c", "--constraint")
_SETTING_NAME = re.compile(r"[a-z0-9][a-z0-9-]*")
_ENCODING_DECLARATION = re.compile(r"^[ \t]*#.*?coding[:=][ \t]*([-\w.]+)")
_PIP_CONFIG_SNAPSHOT = """
import json
from pip._internal.commands import create_command

parser = create_command("install").parser
parser.config.load()
print(json.dumps({
    "configuration": [values for _, values in parser.config.items()],
    "ordered": list(parser._get_ordered_configuration_items()),
}))
"""
_PYTHON_IMAGE_METADATA = {
    "PYTHON_VERSION",
    "PYTHON_PIP_VERSION",
    "PYTHON_SETUPTOOLS_VERSION",
    "PYTHON_GET_PIP_URL",
    "PYTHON_GET_PIP_SHA256",
}
_LOCK_MANIFESTS = {
    "pipenv": "Pipfile",
    "pdm": "pyproject.toml",
    "poetry": "pyproject.toml",
    "uv": "pyproject.toml",
}


class DependencyCacheMiss(Exception):
    """The dependency layer cannot be proven reusable; the message is a fixed, secret-free reason."""


@dataclass(frozen=True)
class AmbientPipSnapshot:
    identity: Tuple[Tuple[str, str], ...]
    configuration: Tuple[dict, ...] = field(repr=False)
    ordered: Tuple[Tuple[str, str], ...] = field(repr=False)
    environment: Mapping[str, str] = field(repr=False)


@dataclass(frozen=True)
class LocalFindLinksSnapshot:
    destination: Path
    source: Path = field(repr=False)
    kind: str
    files: Tuple[Tuple[str, str], ...]


@dataclass(frozen=True)
class RequirementsSnapshot:
    files: Tuple[Tuple[str, bytes], ...] = field(repr=False)
    find_links: Tuple[LocalFindLinksSnapshot, ...] = field(repr=False)
    ambient: AmbientPipSnapshot = field(repr=False)


@dataclass(frozen=True)
class DependencyCacheKey:
    layer: str
    inventory: str
    source_files: Tuple[str, ...]
    environment: Mapping[str, str] = field(repr=False, compare=False)
    requirements_snapshot: Optional[RequirementsSnapshot] = field(default=None, repr=False)


AmbientReader = Callable[[Mapping[str, str]], AmbientPipSnapshot]


def dependency_cache_key(
    *,
    source_kind: str,
    context_dir: Path,
    os_name: str,
    arch: str,
    python_version: str,
    python_full_version: Optional[str],
    base_image: str,
    base_config: Optional[dict],
    workdir: str,
    reproducible: bool,
    strip_type_stubs: bool = False,
    base_image_tag_verified: bool = False,
    variant: Optional[str] = None,
    requirements: Optional[Path] = None,
    requirement_sets: Sequence[Tuple[str, str]] = (),
    lock_name: Optional[str] = None,
    lock_kind: Optional[str] = None,
    lock_roots: Optional[Sequence[str]] = None,
    lock_verifier: Optional[str] = None,
    ambient_reader: Optional[AmbientReader] = None,
) -> DependencyCacheKey:
    """Return the cache key for a provably reusable dependency layer or raise ``DependencyCacheMiss``."""
    if source_kind == "venv":
        raise DependencyCacheMiss("virtualenv dependencies are not provably hash-pinned")
    if not reproducible:
        raise DependencyCacheMiss("non-reproducible builds embed file timestamps")
    environment = dict(os.environ)
    if any(
        name.upper().startswith("PYTHON")
        and name.upper() not in _PYTHON_IMAGE_METADATA
        and value
        for name, value in environment.items()
    ):
        raise DependencyCacheMiss("the builder Python startup environment is not provably isolated")
    _disable_netrc_for_cached_install(environment)
    no_proxy = [value for name, value in environment.items() if name.upper() == "NO_PROXY"]
    bypass_all = bool(no_proxy) and all(value.strip() == "*" for value in no_proxy)
    if not bypass_all:
        if any(name.upper() in _PROXY_ENVIRONMENT and value for name, value in environment.items()):
            raise DependencyCacheMiss("a proxy can change the effective package source")
        try:
            proxies = _system_proxies()
            if any(proxies.get(name) for name in ("http", "https", "all")):
                raise DependencyCacheMiss("a proxy can change the effective package source")
        except (OSError, ValueError) as error:
            raise DependencyCacheMiss("the system proxy configuration could not be read") from error
    environment["NO_PROXY"] = "*"
    environment["no_proxy"] = "*"

    requirements_snapshot = None
    if source_kind == "lock":
        manifest_name = _LOCK_MANIFESTS.get(lock_kind or "")
        if manifest_name is None or lock_roots is None or not lock_verifier:
            raise DependencyCacheMiss("the locked verification inputs are unavailable")
        if any("://" in root or "$" in root for root in lock_roots):
            raise DependencyCacheMiss("a locked manifest requirement cannot be keyed safely")
        inputs = {
            "kind": lock_kind,
            "requirements": _lock_inputs(requirement_sets),
            "roots": sorted(lock_roots),
            "verifier": lock_verifier,
        }
        source_files: Tuple[str, ...] = (
            (lock_name, (Path(lock_name).parent / manifest_name).as_posix())
            if lock_name else ()
        )
        # Locked installs run pip with --isolated and no PIP_* settings.
        ambient: object = "isolated"
    elif source_kind == "requirements" and requirements is not None:
        config_file = environment.get("PIP_CONFIG_FILE", "")
        if (
            config_file
            and os.path.normcase(config_file) != os.path.normcase(os.devnull)
            and not os.path.isabs(config_file)
        ):
            raise DependencyCacheMiss("a relative pip configuration path is not provably stable")
        inputs, source_files, files, find_links = _requirements_inputs(requirements, context_dir)
        pip_snapshot = (ambient_reader or ambient_pip_settings)(environment)
        ambient = pip_snapshot.identity
        requirements_snapshot = RequirementsSnapshot(files, find_links, pip_snapshot)
    else:
        raise DependencyCacheMiss("the dependency source is not cacheable")

    try:
        pip_version = metadata.version("pip")
    except metadata.PackageNotFoundError as error:
        raise DependencyCacheMiss("the builder pip version could not be determined") from error

    identity = {
        "version": KEY_VERSION,
        "scope": "all",
        "source": source_kind,
        "target": {
            "os": os_name,
            "arch": arch,
            "variant": variant,
            "pip_options": _target_options(
                os_name, arch, python_version, base_image, base_config,
                base_image_tag_verified=base_image_tag_verified,
                variant=variant,
            ),
            "python": python_full_version or python_version,
            "marker_environment": _target_marker_environment(
                os_name, arch, python_version, base_config, variant=variant
            ),
        },
        "builder": {
            "installer": INSTALLER_BACKEND,
            "pip": pip_version,
            "python": f"{platform.python_implementation()} {platform.python_version()}",
            "platform": sys.platform,
        },
        "layer": {
            "workdir": workdir,
            "reproducible": True,
            "strip_type_stubs": strip_type_stubs,
        },
        "inputs": inputs,
        "ambient": ambient,
    }
    serialized = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(serialized).hexdigest()
    return DependencyCacheKey(
        f"deps-v{KEY_VERSION}:sha256:{digest}",
        f"deps-inventory-v{KEY_VERSION}:sha256:{digest}",
        source_files,
        environment,
        requirements_snapshot,
    )


def _disable_netrc_for_cached_install(environment: dict) -> None:
    configured = [value for name, value in environment.items() if name.upper() == "NETRC"]
    if configured:
        if any(value and os.path.normcase(value) != os.path.normcase(os.devnull) for value in configured):
            raise DependencyCacheMiss("netrc credentials can change the effective package source")
    else:
        for name in (".netrc", "_netrc"):
            try:
                Path(os.path.expanduser(f"~/{name}")).stat()
            except FileNotFoundError:
                continue
            except OSError as error:
                raise DependencyCacheMiss("netrc configuration could not be checked") from error
            raise DependencyCacheMiss("netrc credentials can change the effective package source")
    for name in tuple(environment):
        if name.upper() == "NETRC":
            del environment[name]
    environment["NETRC"] = os.devnull


def _system_proxies() -> Mapping[str, str]:
    if sys.platform == "win32":
        return urllib.request.getproxies_registry()
    if sys.platform == "darwin":
        return urllib.request.getproxies_macosx_sysconf()
    return {}


def sanitize_url(value: str) -> str:
    """Miss on source selectors that cannot be keyed safely."""
    try:
        parts = urlsplit(value)
    except ValueError as error:
        raise DependencyCacheMiss("an index or find-links URL cannot be parsed safely") from error
    if "@" in parts.netloc:
        raise DependencyCacheMiss("index or find-links credentials can change the effective package source")
    host = parts.netloc
    if "$" in host + parts.path or "%24" in (host + parts.path).lower():
        raise DependencyCacheMiss(
            "an index or find-links URL uses an environment placeholder in its host or path"
        )
    if "?" in value.split("#", 1)[0]:
        # A query can route to another tenant or feed, so it is not a safe credential to drop.
        raise DependencyCacheMiss("an index or find-links URL has a query string")
    if "#" in value:
        raise DependencyCacheMiss("an index or find-links URL has a fragment")
    identity = urlunsplit((parts.scheme.lower(), host.lower(), parts.path, "", ""))
    if url_has_credentials(identity):
        raise DependencyCacheMiss("an index or find-links URL has a credential-bearing path")
    return identity


def _key_token(token: str) -> str:
    name, separator, value = token.partition("=")
    if token.startswith("--") and separator and "://" in value:
        return f"{name}={sanitize_url(value)}"
    if token.startswith("-") and not token.startswith("--") and "://" in token:
        return token[:2] + sanitize_url(token[2:])
    if "://" in token:
        return sanitize_url(token)
    if "$" in token:
        raise DependencyCacheMiss(
            "requirements use an environment placeholder outside index credentials"
        )
    return token


def _lock_inputs(requirement_sets: Iterable[Tuple[str, str]]) -> List[dict]:
    inputs = []
    for url, text in requirement_sets:
        lines = []
        for line in text.splitlines():
            tokens = shlex.split(line, comments=True)
            if not tokens:
                continue
            if not tokens[0].startswith("-") and not _is_hash_pinned(tokens):
                raise DependencyCacheMiss("a locked requirement is not hash-pinned")
            lines.append([_key_token(token) for token in tokens])
        inputs.append({"index": sanitize_url(url), "lines": lines})
    return inputs


def _requirements_inputs(
    requirements: Path, context_dir: Path
) -> Tuple[
    List[dict], Tuple[str, ...], Tuple[Tuple[str, bytes], ...],
    Tuple[LocalFindLinksSnapshot, ...],
]:
    hash_pinned, files = scan_requirements(requirements, context_dir)
    if not hash_pinned:
        raise DependencyCacheMiss("requirements are not fully pinned with ==versions and --hash values")

    context_root = context_dir.resolve()
    try:
        relative_requirements = requirements.relative_to(context_dir)
        expected = Path(os.path.abspath(context_root / relative_requirements))
    except ValueError as error:
        raise DependencyCacheMiss("requirements paths cannot be snapshotted safely") from error
    if expected != requirements.resolve():
        raise DependencyCacheMiss("symlinked requirements paths cannot be snapshotted safely")

    inputs = []
    source_files = []
    contents = {}
    local_links = []
    for path, is_constraint, lines, content in files:
        relative = path.relative_to(context_root).as_posix()
        source_files.append(relative)
        if relative in contents and contents[relative] != content:
            raise DependencyCacheMiss("requirements changed while their inputs were scanned")
        contents[relative] = content
        if content.startswith((b"\xff\xfe", b"\xfe\xff")):
            text = content.decode("utf-16")
        else:
            text = content.decode("utf-8-sig")
            for line in text.splitlines()[:2]:
                declaration = _ENCODING_DECLARATION.match(line)
                if declaration and declaration.group(1).lower().replace("-", "") not in {
                    "utf8", "utf8sig"
                }:
                    raise DependencyCacheMiss("a requirements encoding cannot be snapshotted safely")
        for line in text.splitlines():
            for position, character in enumerate(line):
                if character == "#":
                    if position and not line[position - 1].isspace():
                        raise DependencyCacheMiss(
                            "requirements contain a fragment or ambiguous comment"
                        )
                    break
        keyed_lines = []
        find_links = []
        for tokens in lines:
            if "--no-deps" in tokens:
                raise DependencyCacheMiss("--no-deps leaves the transitive closure unproven")
            if not tokens[0].startswith("-"):
                text = " ".join(tokens)
                if is_constraint and ("@" in text or "://" in text):
                    raise DependencyCacheMiss("constraint files use a direct URL reference")
            for value in _option_values(tokens, _INCLUDE_OPTIONS):
                include = Path(value)
                lexical = Path(os.path.abspath(path.parent / include))
                if include.is_absolute() or include.drive or lexical != lexical.resolve():
                    raise DependencyCacheMiss(
                        "absolute or symlinked requirements includes cannot be snapshotted safely"
                    )
            for value in _option_values(tokens, _INDEX_OPTIONS):
                _network_source(value, "index")
            for value in _option_values(tokens, _FIND_LINKS):
                identity, snapshot = _find_links_identity(value, path.parent, context_root)
                find_links.append(identity)
                if snapshot is not None:
                    local_links.append(snapshot)
            keyed_lines.append([_key_token(token) for token in tokens])
        inputs.append(
            {
                "path": relative,
                "constraint": is_constraint,
                "lines": keyed_lines,
                "find_links": find_links,
            }
        )
    return inputs, tuple(source_files), tuple(contents.items()), tuple(local_links)


def _option_values(tokens: List[str], names: Tuple[str, ...]) -> List[str]:
    values = []
    for index, token in enumerate(tokens):
        for name in names:
            if token == name and index + 1 < len(tokens):
                values.append(tokens[index + 1])
            elif name.startswith("--") and token.startswith(name + "="):
                values.append(token[len(name) + 1 :])
            elif not name.startswith("--") and token.startswith(name) and len(token) > len(name):
                values.append(token[len(name) :])
    return values


def _network_source(value: str, label: str) -> str:
    try:
        parts = urlsplit(value)
    except ValueError as error:
        raise DependencyCacheMiss(f"an {label} URL cannot be parsed safely") from error
    if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
        raise DependencyCacheMiss(f"a non-HTTP {label} source is not provably keyed")
    return sanitize_url(value)


def _find_links_identity(
    value: str, requirements_dir: Path, context_root: Path
) -> Tuple[object, Optional[LocalFindLinksSnapshot]]:
    if value.lower().startswith("file:"):
        raise DependencyCacheMiss("file: URL find-links are not provably keyed")
    if "://" in value:
        return _network_source(value, "find-links"), None
    if Path(value).is_absolute() or Path(value).drive:
        raise DependencyCacheMiss("absolute local find-links cannot be snapshotted safely")
    # pip resolves a relative find-links path against the requirements file, then the working directory.
    candidate = Path(os.path.abspath(requirements_dir / value))
    location = candidate if candidate.exists() else Path(os.path.abspath(context_root / value))
    if not candidate.is_relative_to(context_root) or not location.is_relative_to(context_root):
        raise DependencyCacheMiss("local find-links outside the context cannot be snapshotted safely")
    if location.resolve() != location:
        raise DependencyCacheMiss("symlinked local find-links cannot be snapshotted safely")
    destination = candidate.relative_to(context_root)
    try:
        if location.is_dir():
            entries = sorted(location.iterdir(), key=lambda item: item.name)
            if any(
                entry.is_symlink()
                or not entry.is_file()
                or mimetypes.guess_type(entry.name, strict=False)[0] == "text/html"
                for entry in entries
            ):
                raise DependencyCacheMiss("local find-links contain unsupported entries")
            files = tuple((entry.name, _file_digest(entry)) for entry in entries)
            snapshot = LocalFindLinksSnapshot(destination, location, "directory", files)
            return {"files": [list(item) for item in files]}, snapshot
        if location.is_file():
            if mimetypes.guess_type(location.name, strict=False)[0] == "text/html":
                raise DependencyCacheMiss("local find-links contain unsupported entries")
            files = ((location.name, _file_digest(location)),)
            snapshot = LocalFindLinksSnapshot(destination, location, "file", files)
            return {"files": [list(item) for item in files]}, snapshot
        if location.exists():
            raise DependencyCacheMiss("local find-links contain unsupported entries")
    except OSError as error:
        raise DependencyCacheMiss("local find-links could not be snapshotted safely") from error
    return {"files": None}, LocalFindLinksSnapshot(destination, location, "missing", ())


def _file_digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def materialize_requirements_snapshot(snapshot: RequirementsSnapshot, root: Path) -> None:
    """Create the exact local inputs that were used to key a plain-requirements layer."""
    try:
        total_bytes = 0
        total_files = 0
        for relative, content in snapshot.files:
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            total_bytes += len(content)
            total_files += 1
            if total_bytes > MAX_STAGED_BYTES:
                raise DependencyCacheMiss("the local input snapshot exceeds its size limit")
            if total_files > MAX_STAGED_FILES:
                raise DependencyCacheMiss("the local input snapshot exceeds its file limit")
        for link in snapshot.find_links:
            target = root / link.destination
            if link.kind in {"directory", "missing"}:
                target.mkdir(parents=True, exist_ok=True)
            for name, expected in link.files:
                source = link.source / name if link.kind == "directory" else link.source
                destination = target / name if link.kind == "directory" else target
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    if not destination.is_file() or _file_digest(destination) != expected:
                        raise DependencyCacheMiss("local input snapshots overlap inconsistently")
                    continue
                digest = hashlib.sha256()
                with source.open("rb") as reader, destination.open("xb") as writer:
                    for chunk in iter(lambda: reader.read(1024 * 1024), b""):
                        total_bytes += len(chunk)
                        if total_bytes > MAX_STAGED_BYTES:
                            raise DependencyCacheMiss("the local input snapshot exceeds its size limit")
                        writer.write(chunk)
                        digest.update(chunk)
                total_files += 1
                if total_files > MAX_STAGED_FILES:
                    raise DependencyCacheMiss("the local input snapshot exceeds its file limit")
                if digest.hexdigest() != expected:
                    raise DependencyCacheMiss("local find-links changed while being snapshotted")
        for link in snapshot.find_links:
            if link.kind in {"directory", "missing"}:
                actual = {entry.name for entry in (root / link.destination).iterdir()}
                if actual != {name for name, _ in link.files}:
                    raise DependencyCacheMiss("local input snapshots overlap inconsistently")
    except OSError as error:
        raise DependencyCacheMiss("local inputs could not be snapshotted safely") from error


def ambient_pip_settings(
    environment: Optional[Mapping[str, str]] = None,
) -> AmbientPipSnapshot:
    """Capture pip's effective configuration and its secret-free cache identity."""
    environment = dict(os.environ if environment is None else environment)
    try:
        result = subprocess.run(
            [
                sys.executable, "-E", "-P", "-X", "utf8", "-c", _PIP_CONFIG_SNAPSHOT,
            ],
            capture_output=True,
            encoding="utf-8",
            errors="strict",
            env=environment,
            stdin=subprocess.DEVNULL,
            timeout=PIP_CONFIG_TIMEOUT_SECONDS,
        )
    except (OSError, UnicodeError, subprocess.TimeoutExpired) as error:
        raise DependencyCacheMiss("the ambient pip configuration could not be read") from error
    if result.returncode:
        raise DependencyCacheMiss("the ambient pip configuration could not be read")
    try:
        captured = json.loads(result.stdout)
        configuration = captured["configuration"]
        ordered = captured["ordered"]
        if not isinstance(configuration, list) or not isinstance(ordered, list):
            raise ValueError
        if not all(
            isinstance(item, dict)
            and all(isinstance(key, str) and isinstance(value, str) for key, value in item.items())
            for item in configuration
        ):
            raise ValueError
        if not all(
            isinstance(item, list)
            and len(item) == 2
            and all(isinstance(value, str) for value in item)
            for item in ordered
        ):
            raise ValueError
    except (KeyError, TypeError, ValueError) as error:
        raise DependencyCacheMiss("the ambient pip configuration could not be parsed") from error

    identity = []
    for level, values in enumerate(configuration):
        for name, value in values.items():
            section, dot, setting = name.partition(".")
            if not dot or section not in _AMBIENT_SECTIONS:
                continue
            setting = setting.strip().lower().replace("_", "-")
            sanitized = _sanitize_pip_setting(setting, value)
            if sanitized is not None:
                identity.append((f"{level}:{section}.{setting}", sanitized))
    allowed = set(_PIP_CONFIG_ALLOWED)
    frozen = []
    for values in configuration:
        frozen.append(
            {
                name: value
                for name, value in values.items()
                if name.partition(".")[0] in _AMBIENT_SECTIONS
                and name.partition(".")[2].lower().replace("_", "-") in allowed
            }
        )
    effective = tuple(
        (name, value)
        for name, value in ordered
        if name.lower().replace("_", "-") in allowed
    )
    return AmbientPipSnapshot(tuple(identity), tuple(frozen), effective, environment)


def _sanitize_pip_setting(setting: str, value: str) -> Optional[str]:
    if setting == "proxy" and value:
        raise DependencyCacheMiss("a proxy can change the effective package source")
    if setting == "client-cert" and value:
        raise DependencyCacheMiss("a client certificate can change the effective package source")
    if setting == "keyring-provider" and value.lower() not in {"", "auto", "disabled"}:
        raise DependencyCacheMiss("a keyring can change the effective package source")
    if setting in _AMBIENT_IGNORED:
        return None
    if setting in _AMBIENT_URLS:
        values = []
        for item in value.split():
            if setting == "find-links" and (
                "://" not in item or item.lower().startswith("file:")
            ):
                raise DependencyCacheMiss("ambient pip find-links point to local files")
            values.append(_network_source(item, setting))
        return " ".join(values)
    if setting in _AMBIENT_KEYED:
        if "$" in value or "://" in value:
            raise DependencyCacheMiss(
                f"ambient pip setting {setting} cannot be keyed without its value"
            )
        return value
    label = setting if _SETTING_NAME.fullmatch(setting) else "<unrecognized>"
    raise DependencyCacheMiss(f"ambient pip setting {label} is not provably keyed")


def parse_pip_config(output: str) -> List[List[str]]:
    """Return sorted ``[section, setting, sanitized value]`` rows from ``pip config list`` output."""
    settings = []
    for line in output.splitlines():
        if not line.strip():
            continue
        name, separator, raw_value = line.partition("=")
        section, dot, setting = name.partition(".")
        if not separator or not dot:
            raise DependencyCacheMiss("the ambient pip configuration could not be parsed")
        if section not in _AMBIENT_SECTIONS:
            continue
        setting = setting.strip().lower().replace("_", "-")
        try:
            value = ast.literal_eval(raw_value.strip())
        except (SyntaxError, ValueError) as error:
            raise DependencyCacheMiss("the ambient pip configuration could not be parsed") from error
        if not isinstance(value, str):
            raise DependencyCacheMiss("the ambient pip configuration could not be parsed")
        sanitized = _sanitize_pip_setting(setting, value)
        if sanitized is not None:
            settings.append([section, setting, sanitized])
    return sorted(settings)


def encode_inventory(layer_digest: str, packages: Iterable[Tuple[str, str]]) -> bytes:
    document = {
        "version": INVENTORY_VERSION,
        "layer": layer_digest,
        "packages": [[name, version] for name, version in packages],
    }
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")


def decode_inventory(content: bytes, layer_digest: str) -> Optional[List[Tuple[str, str]]]:
    """Return the package inventory bound to ``layer_digest``, or ``None`` if it does not match."""
    try:
        document = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if (
        not isinstance(document, dict)
        or document.get("version") != INVENTORY_VERSION
        or document.get("layer") != layer_digest
        or not isinstance(document.get("packages"), list)
    ):
        return None
    packages = []
    for item in document["packages"]:
        if (
            not isinstance(item, list)
            or len(item) != 2
            or not all(isinstance(value, str) for value in item)
        ):
            return None
        packages.append((item[0], item[1]))
    return packages
