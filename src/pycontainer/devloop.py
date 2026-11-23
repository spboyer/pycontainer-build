"""Skaffold and Tilt custom-builder contracts.

Both tools hand a build request to an external command through environment
variables. These helpers translate that request into a single-platform,
registry-pushed build and reject everything the builder cannot honor, rather
than falling back to a local Docker daemon.
"""

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

from .builder import parse_platform_variant
from .registry_client import parse_image_reference

MULTI_PLATFORM_ISSUE = "https://github.com/spboyer/pycontainer-build/issues/29"
TILT_CONTENT_TAG_PREFIX = "tilt-"
TILT_PLACEHOLDER_TAG = "tilt-pending"
DOCKER_HUB_HOSTS = ("docker.io", "index.docker.io", "registry-1.docker.io")


def _has_registry_host(reference: str) -> bool:
    first, slash, _ = reference.partition("/")
    return bool(slash) and (first == "localhost" or "." in first or ":" in first)


class DevLoopError(ValueError):
    """Raised when a Skaffold or Tilt request cannot be honored."""


@dataclass(frozen=True)
class SkaffoldRequest:
    image: str
    context_dir: str
    platform: Optional[str]


def require_push_reference(reference: str, source: str) -> str:
    """Validate a tagged image reference that names its registry explicitly."""
    try:
        parse_image_reference(reference)
    except ValueError as error:
        raise DevLoopError(f"{source} is not a valid image reference: {error}") from error
    if "@" in reference or ":" not in reference.rsplit("/", 1)[-1]:
        raise DevLoopError(f"{source} must be a tagged image reference without a digest.")
    require_repository(reference.rsplit(":", 1)[0], source)
    return reference


def require_repository(repository: str, source: str) -> str:
    """Validate an untagged repository that names its registry explicitly."""
    try:
        registry, _, _ = parse_image_reference(repository)
    except ValueError as error:
        raise DevLoopError(f"{source} is not a valid image repository: {error}") from error
    if "@" in repository or ":" in repository.rsplit("/", 1)[-1]:
        raise DevLoopError(f"{source} must be a repository without a tag or digest.")
    if not repository.startswith(f"{registry}/"):
        raise DevLoopError(
            f"{source} must name its registry explicitly (for example "
            "localhost:5000/app); refusing to push to Docker Hub implicitly."
        )
    return repository


def single_platform(value: Optional[str], source: str) -> Optional[str]:
    """Return the one requested platform, or None when none was requested."""
    platforms = list(dict.fromkeys(p.strip() for p in (value or "").split(",") if p.strip()))
    if len(platforms) > 1:
        raise DevLoopError(
            f"{source} requests multiple platforms ({', '.join(platforms)}); "
            "multi-platform images are not supported yet "
            f"(see {MULTI_PLATFORM_ISSUE}). Request a single platform."
        )
    if not platforms:
        return None
    try:
        parse_platform_variant(platforms[0])
    except ValueError as error:
        raise DevLoopError(f"{source} is not a valid platform: {error}") from error
    return platforms[0]


def skaffold_request(env: Mapping[str, str]) -> SkaffoldRequest:
    """Translate Skaffold's custom-builder environment into a build request."""
    image = env.get("IMAGE", "")
    if not image:
        raise DevLoopError("IMAGE is not set; run this command as a Skaffold custom buildCommand.")
    image = require_push_reference(image, "IMAGE")

    context_dir = env.get("BUILD_CONTEXT", "")
    if not context_dir:
        raise DevLoopError("BUILD_CONTEXT is not set; run this command as a Skaffold custom buildCommand.")
    if not os.path.isabs(context_dir) or not Path(context_dir).is_dir():
        raise DevLoopError("BUILD_CONTEXT must be an absolute path to an existing directory.")

    push = env.get("PUSH_IMAGE")
    if push is None or push == "":
        raise DevLoopError("PUSH_IMAGE is not set; run this command as a Skaffold custom buildCommand.")
    if push == "false":
        raise DevLoopError(
            "PUSH_IMAGE=false asks for an image in the local Docker daemon, but "
            "pycontainer builds without a daemon and has no supported import "
            "bridge. Set build.local.push: true in skaffold.yaml and use a "
            "registry the cluster can pull from."
        )
    if push != "true":
        raise DevLoopError(f"PUSH_IMAGE must be 'true' or 'false', got {push!r}.")

    return SkaffoldRequest(image, context_dir, single_platform(env.get("PLATFORMS"), "PLATFORMS"))


def tilt_expected_reference(env: Mapping[str, str], image: Optional[str]) -> Optional[str]:
    """Return Tilt's EXPECTED_REF, or None when the content-tag mode is used."""
    expected = env.get("EXPECTED_REF", "")
    if expected and image:
        raise DevLoopError("--image cannot be combined with Tilt's EXPECTED_REF.")
    if expected:
        registry_host = env.get("EXPECTED_REGISTRY", "").split("/", 1)[0].lower()
        if registry_host in DOCKER_HUB_HOSTS and not _has_registry_host(expected):
            # Tilt prints Docker Hub refs in familiar form, dropping docker.io/.
            expected = f"docker.io/{expected}"
        try:
            return require_push_reference(expected, "EXPECTED_REF")
        except DevLoopError as error:
            raise DevLoopError(
                f"{error} Configure default_registry() or a registry-qualified "
                "custom_build ref, and do not use custom_build(tag=...)."
            ) from error
    if not image:
        raise DevLoopError(
            "EXPECTED_REF is not set; run this command as a Tilt custom_build, "
            "or pass --image REPOSITORY with --output-ref for outputs_image_ref_to."
        )
    require_repository(image, "--image")
    return None


def tilt_content_tag(manifest_digest: str) -> str:
    """Return the content-derived tag published in outputs_image_ref_to mode."""
    algorithm, _, hex_digest = manifest_digest.partition(":")
    if algorithm != "sha256" or len(hex_digest) != 64:
        raise DevLoopError(f"Unexpected manifest digest {manifest_digest!r}.")
    return TILT_CONTENT_TAG_PREFIX + hex_digest


def write_reference(path: Path, reference: str) -> None:
    """Atomically publish the pushed reference for Tilt to read."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(reference + "\n")
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise
