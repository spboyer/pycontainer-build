"""Cache identity for dependency layers staged from provably hash-pinned inputs.

A staged dependency layer is reused only when every input that can change pip's
result is part of the key. Anything that cannot be proven raises
``DependencyCacheMiss`` with a fixed reason that never contains URLs, paths or
credentials. Keys are SHA-256 hashes of a sanitized identity document.
"""
import ast
import hashlib
import json
import os
import platform
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit, urlunsplit

from .dependencies import (
    _is_hash_pinned,
    _target_marker_environment,
    _target_options,
    scan_requirements,
)

# Bump when the staged dependency layer layout or tar encoding changes.
KEY_VERSION = 1
# Bump when the installer changes layer-affecting behavior such as marker evaluation.
INSTALLER_BACKEND = "pip-target-v2"
INVENTORY_VERSION = 1
MAX_INVENTORY_BYTES = 16 * 1024 * 1024
PIP_CONFIG_TIMEOUT_SECONDS = 60

_AMBIENT_SECTIONS = {":env:", "global", "install"}
_AMBIENT_URLS = {"index-url", "extra-index-url", "find-links"}
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
# Transport, logging and authentication settings cannot change hash-pinned artifacts.
_AMBIENT_IGNORED = {
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
_SETTING_NAME = re.compile(r"[a-z0-9][a-z0-9-]*")


class DependencyCacheMiss(Exception):
    """The dependency layer cannot be proven reusable; the message is a fixed, secret-free reason."""


@dataclass(frozen=True)
class DependencyCacheKey:
    layer: str
    inventory: str
    source_files: Tuple[str, ...]


AmbientReader = Callable[[], List[List[str]]]


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
    base_image_tag_verified: bool = False,
    requirements: Optional[Path] = None,
    requirement_sets: Sequence[Tuple[str, str]] = (),
    lock_name: Optional[str] = None,
    ambient_reader: Optional[AmbientReader] = None,
) -> DependencyCacheKey:
    """Return the cache key for a provably reusable dependency layer or raise ``DependencyCacheMiss``."""
    if source_kind == "venv":
        raise DependencyCacheMiss("virtualenv dependencies are not provably hash-pinned")
    if not reproducible:
        raise DependencyCacheMiss("non-reproducible builds embed file timestamps")

    if source_kind == "lock":
        inputs = _lock_inputs(requirement_sets)
        source_files: Tuple[str, ...] = (lock_name,) if lock_name else ()
        # Locked installs run pip with --isolated and no PIP_* settings.
        ambient: object = "isolated"
    elif source_kind == "requirements" and requirements is not None:
        inputs, source_files = _requirements_inputs(requirements, context_dir)
        ambient = (ambient_reader or ambient_pip_settings)()
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
            "pip_options": _target_options(
                os_name, arch, python_version, base_image, base_config,
                base_image_tag_verified=base_image_tag_verified,
            ),
            "python": python_full_version or python_version,
            "marker_environment": _target_marker_environment(
                os_name, arch, python_version, base_config
            ),
        },
        "builder": {
            "installer": INSTALLER_BACKEND,
            "pip": pip_version,
            "python": f"{platform.python_implementation()} {platform.python_version()}",
            "platform": sys.platform,
        },
        "layer": {"workdir": workdir, "reproducible": True},
        "inputs": inputs,
        "ambient": ambient,
    }
    serialized = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(serialized).hexdigest()
    return DependencyCacheKey(
        f"deps-v{KEY_VERSION}:sha256:{digest}",
        f"deps-inventory-v{KEY_VERSION}:sha256:{digest}",
        source_files,
    )


def sanitize_url(value: str) -> str:
    """Drop userinfo and fragment; miss on queries or placeholders that may select another source."""
    parts = urlsplit(value)
    host = parts.netloc.rsplit("@", 1)[-1]
    if "$" in host + parts.path or "%24" in (host + parts.path).lower():
        raise DependencyCacheMiss(
            "an index or find-links URL uses an environment placeholder in its host or path"
        )
    if parts.query:
        # A query can route to another tenant or feed, so it is not a safe credential to drop.
        raise DependencyCacheMiss("an index or find-links URL has a query string")
    return urlunsplit((parts.scheme.lower(), host.lower(), parts.path.rstrip("/"), "", ""))


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


def _requirements_inputs(requirements: Path, context_dir: Path) -> Tuple[List[dict], Tuple[str, ...]]:
    hash_pinned, files = scan_requirements(requirements, context_dir)
    if not hash_pinned:
        raise DependencyCacheMiss("requirements are not fully pinned with ==versions and --hash values")

    context_root = context_dir.resolve()
    inputs = []
    source_files = []
    for path, is_constraint, lines in files:
        relative = path.relative_to(context_root).as_posix()
        source_files.append(relative)
        keyed_lines = []
        find_links = []
        for tokens in lines:
            if "--no-deps" in tokens:
                raise DependencyCacheMiss("--no-deps leaves the transitive closure unproven")
            if not tokens[0].startswith("-"):
                text = " ".join(tokens)
                if is_constraint and ("@" in text or "://" in text):
                    raise DependencyCacheMiss("constraint files use a direct URL reference")
            for value in _option_values(tokens, _FIND_LINKS):
                find_links.append(_find_links_identity(value, path.parent, context_root))
            keyed_lines.append([_key_token(token) for token in tokens])
        inputs.append(
            {
                "path": relative,
                "constraint": is_constraint,
                "lines": keyed_lines,
                "find_links": find_links,
            }
        )
    return inputs, tuple(source_files)


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


def _find_links_identity(value: str, requirements_dir: Path, context_root: Path) -> object:
    if "://" in value:
        if urlsplit(value).scheme.lower() == "file":
            raise DependencyCacheMiss("file: URL find-links are not provably keyed")
        return sanitize_url(value)
    # pip resolves a relative find-links path against the requirements file, then the working directory.
    candidate = requirements_dir / value
    location = candidate if candidate.exists() else context_root / value
    if location.is_dir():
        return {
            "files": [
                [entry.name, _file_digest(entry)]
                for entry in sorted(location.iterdir(), key=lambda item: item.name)
                if entry.is_file()
            ]
        }
    if location.is_file():
        return {"files": [[location.name, _file_digest(location)]]}
    return {"files": None}


def _file_digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def ambient_pip_settings() -> List[List[str]]:
    """Read the effective pip configuration for plain requirements and key only provable settings."""
    environment = dict(os.environ, PYTHONIOENCODING="utf-8")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "--disable-pip-version-check", "config", "list"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            stdin=subprocess.DEVNULL,
            timeout=PIP_CONFIG_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise DependencyCacheMiss("the ambient pip configuration could not be read") from error
    if result.returncode:
        raise DependencyCacheMiss("the ambient pip configuration could not be read")
    return parse_pip_config(result.stdout)


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
        if setting in _AMBIENT_IGNORED:
            continue
        try:
            value = ast.literal_eval(raw_value.strip())
        except (SyntaxError, ValueError) as error:
            raise DependencyCacheMiss("the ambient pip configuration could not be parsed") from error
        if not isinstance(value, str):
            raise DependencyCacheMiss("the ambient pip configuration could not be parsed")
        if setting in _AMBIENT_URLS:
            values = []
            for item in value.split():
                if setting == "find-links" and (
                    "://" not in item or urlsplit(item).scheme.lower() == "file"
                ):
                    raise DependencyCacheMiss("ambient pip find-links point to local files")
                values.append(sanitize_url(item))
            value = " ".join(values)
        elif setting in _AMBIENT_KEYED:
            if "$" in value or "://" in value:
                raise DependencyCacheMiss(
                    f"ambient pip setting {setting} cannot be keyed without its value"
                )
        else:
            label = setting if _SETTING_NAME.fullmatch(setting) else "<unrecognized>"
            raise DependencyCacheMiss(f"ambient pip setting {label} is not provably keyed")
        settings.append([section, setting, value])
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
