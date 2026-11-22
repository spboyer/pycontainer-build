"""Independent Python-version builds with digest-first, non-atomic publication."""
import re
from contextlib import ExitStack
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Dict, List, Optional

from .auth import get_registry_auth
from .blob_store import digest_filename
from .builder import ImageBuilder, _verify_image_index, _verify_platform_image
from .config import BuildConfig
from .dependencies import _base_environment, _base_image_parts, _is_official_python_image
from .fs_utils import context_exclude_paths, output_directory_lock
from .local_image import _paths_overlap, parse_local_image_reference
from .registry_client import RegistryClient, RegistryHTTPError, parse_image_reference
from .tags import resolve_tags


def matrix_targets(config: BuildConfig) -> Dict[str, str]:
    versions = config.python_versions
    if not isinstance(versions, list) or not versions:
        raise ValueError("python_matrix/default_python_version requires python_versions.")
    if any(not isinstance(v, str) or not re.fullmatch(r"3\.(?:[1-9]\d*)", v) for v in versions):
        raise ValueError("python_versions must contain CPython minors such as '3.11', without whitespace.")
    if len(set(versions)) != len(versions):
        raise ValueError("python_versions must not contain duplicates.")
    if config.default_python_version is not None and config.default_python_version not in versions:
        raise ValueError("default_python_version must be one of python_versions.")
    if not isinstance(config.python_matrix, dict) or any(
        not isinstance(k, str) or not isinstance(v, str) or not v.strip()
        for k, v in config.python_matrix.items()
    ):
        raise ValueError("python_matrix must map Python minors to non-empty base references.")
    if set(config.python_matrix) - set(versions):
        raise ValueError("python_matrix contains versions not requested in python_versions.")
    repository, tag = _base_image_parts(config.base_image)
    match = re.fullmatch(r"3\.\d+(?:\.\d+)?(?P<suffix>-[A-Za-z0-9_.-]+)?", tag)
    rewritable = _is_official_python_image(repository) and "@" not in config.base_image and match
    targets = {}
    for version in versions:
        if version in config.python_matrix:
            targets[version] = config.python_matrix[version]
        elif rewritable:
            targets[version] = f"{repository}:{version}{match['suffix'] or ''}"
        else:
            raise ValueError(
                f"Python {version} requires an explicit [build.python_matrix] base map; "
                "only unpinned official python:<minor> tags can be rewritten."
            )
        try:
            local = parse_local_image_reference(targets[version])
            if local is None:
                parse_image_reference(targets[version])
            elif _paths_overlap(local.path, Path(config.output_dir)):
                raise ValueError("local base overlaps the matrix output directory; copy it elsewhere.")
        except ValueError as error:
            raise ValueError(f"Python {version}: invalid matrix base reference: {error}") from error
    explicit = resolve_tags(config.tag, config.tags)
    suffixed = [
        f"{ref}-py{version}" for version in versions for ref in explicit
    ]
    # Validate generated tag lengths, and reject alias/version collisions.
    for ref in suffixed:
        resolve_tags(ref)
    aliases = config.all_tags()
    if set(suffixed) & set(aliases):
        raise ValueError("Python matrix aliases collide with generated version tags.")
    return targets


def verify_python_version(base: str, base_config: Optional[dict], version: str, platform: str) -> None:
    metadata = base_config.get("config") if isinstance(base_config, dict) else None
    if (
        not isinstance(metadata, dict)
        or not isinstance(metadata.get("Env"), list)
        or any(not isinstance(item, str) for item in metadata["Env"])
    ):
        raise ValueError(f"Python {version} ({platform}): base has malformed config.Env metadata.")
    environment = _base_environment(base_config)
    actual = environment.get("PYTHON_VERSION", "")
    repository, _ = _base_image_parts(base)
    implementation = environment.get("PYTHON_IMPLEMENTATION")
    if (
        not re.fullmatch(r"\d+\.\d+(?:\.\d+)?", actual)
        or ".".join(actual.split(".")[:2]) != version
        or (implementation and implementation.lower() != "cpython")
        or (not _is_official_python_image(repository) and (implementation or "").lower() != "cpython")
    ):
        raise ValueError(
            f"Python {version} ({platform}): base {base!r} must provide matching "
            f"PYTHON_VERSION and CPython provenance; found {actual!r}, {implementation!r}."
        )


@dataclass
class PublicationOperation:
    kind: str
    reference: str
    path: Path
    outcome: str = "unattempted"


class MatrixPublicationError(RuntimeError):
    def __init__(self, operations: List[PublicationOperation], cause: Exception):
        self.operations = operations
        groups = [
            f"{status}: " + (", ".join(
                f"{op.kind} {op.reference}" for op in operations if op.outcome == status
            ) or "(none)")
            for status in ("confirmed", "failed", "unknown", "unattempted")
        ]
        super().__init__(
            "Partial Python matrix publication; " + "; ".join(groups)
            + f". No rollback or retry-overwrite was attempted. Cause: {cause}"
        )


class PythonMatrixBuilder:
    def __init__(self, config: BuildConfig):
        self.config = config
        self.targets = matrix_targets(config)
        self.default = config.default_python_version or config.python_versions[0]
        self.children = {}
        self.operations: List[PublicationOperation] = []
        self.built = False
        explicit = resolve_tags(config.tag, config.tags)
        self.version_tag_count = len(explicit)
        excludes = context_exclude_paths(
            config.context_dir, output_dir=config.output_dir,
            exclude_paths=config.exclude_paths, exclude_defaults=False,
        )
        for version, base in self.targets.items():
            tags = [f"{ref}-py{version}" for ref in explicit]
            if version == self.default:
                tags.extend(config.all_tags())
            child_config = replace(
                config, base_image=base, output_dir=str(Path(config.output_dir) / f"py{version}"),
                tag=tags[0], tags=tags[1:], latest_tag=False, semver_tags=False,
                python_versions=[], python_matrix={}, default_python_version=None,
                exclude_paths=[*excludes, "**/.pycontainer-build-*.lock"],
            )
            child = ImageBuilder(child_config)
            child.expected_python_version = version
            self.children[version] = child

    def build(self):
        self.built = False
        if self.config.dry_run:
            for version, child in self.children.items():
                print(f"Python {version}: base={child.config.base_image}, output={child.config.output_dir}")
                print(f"Tags: {', '.join(child.config.all_tags())}")
                child.build()
            return self.config.tag
        with self._locks():
            for child in self.children.values():
                child._build_image()
            self._verify()
            for version, child in self.children.items():
                if version != self.default:
                    refs = Path(child.config.output_dir) / "refs" / "tags"
                    for alias in self.config.all_tags():
                        (refs / parse_image_reference(alias)[2]).unlink(missing_ok=True)
            self.built = True
        return self.config.tag

    def _locks(self):
        # Child locks also exclude standalone builders aimed at a version subdirectory.
        stack = ExitStack()
        try:
            stack.enter_context(output_directory_lock(self.config.output_dir))
            for child in self.children.values():
                stack.enter_context(output_directory_lock(child.config.output_dir))
        except BaseException:
            stack.close()
            raise
        return stack

    def _verify(self):
        for child in self.children.values():
            layers = Path(child.config.output_dir) / "blobs" / "sha256"
            for image in child.platform_images:
                _verify_platform_image(layers, image, check_diff_ids=True)
            if child.index_digest:
                _verify_image_index(
                    layers, child.index_digest, child.index_size, child.platform_images
                )
            child._verify_local_base_unchanged(layers)

    def push(self, registry_url=None, auth_token=None, username=None, password=None, show_progress=True):
        self.operations = []
        if not self.built:
            raise RuntimeError("Must successfully build and validate every Python version before push().")
        with self._locks():
            self._verify()
            registry, repo, _ = parse_image_reference(registry_url or self.config.tag)
            repository = f"{registry}/{repo}"
            blobs, manifests, indexes, version_tags, aliases = [], [], [], [], []
            seen = set()
            for version, child in self.children.items():
                layers = Path(child.config.output_dir) / "blobs" / "sha256"
                for image in child.platform_images:
                    for digest in [*(layer.digest for layer in image.layers), image.config_digest]:
                        if digest not in seen:
                            blobs.append(PublicationOperation("blob", f"{repository}@{digest}",
                                                             layers / digest_filename(digest)))
                            seen.add(digest)
                    manifests.append(PublicationOperation("manifest", f"{repository}@{image.manifest_digest}",
                                                         layers / digest_filename(image.manifest_digest)))
                kind = "index" if child.index_digest else "manifest"
                path = layers / digest_filename(child.manifest_digest)
                if child.index_digest:
                    indexes.append(PublicationOperation(kind, f"{repository}@{child.index_digest}", path))
                for i, ref in enumerate(child.config.all_tags()):
                    name = parse_image_reference(ref)[2]
                    operation = PublicationOperation(kind, f"{repository}:{name}", path)
                    (version_tags if i < self.version_tag_count else aliases).append(operation)
            self.operations = blobs + manifests + indexes + version_tags + aliases
            try:
                auth = get_registry_auth(registry, bearer_token=auth_token, username=username, password=password)
                client = RegistryClient(registry, repo, auth_token=auth.bearer_token,
                                        username=auth.username, password=auth.password)
            except (RuntimeError, OSError, ValueError) as error:
                self.operations.insert(0, PublicationOperation(
                    "authentication", repository, Path(self.config.output_dir), "failed"
                ))
                raise MatrixPublicationError(self.operations, error) from error
            for operation in self.operations:
                reference = (
                    operation.reference.split("@", 1)[1] if "@" in operation.reference
                    else parse_image_reference(operation.reference)[2]
                )
                try:
                    if operation.kind == "blob":
                        client.push_blob(reference, operation.path, check_exists=True)
                    elif operation.kind == "index":
                        client.push_index(reference, operation.path.read_bytes())
                    else:
                        client.push_manifest(reference, operation.path.read_bytes())
                except (RuntimeError, OSError, ValueError) as error:
                    operation.outcome = (
                        "failed" if isinstance(error, RegistryHTTPError)
                        and 400 <= error.status < 500 and error.status != 408
                        else "unknown"
                    )
                    raise MatrixPublicationError(self.operations, error) from error
                operation.outcome = "confirmed"
                if show_progress:
                    print(f"Confirmed {operation.kind}: {operation.reference}")
        return self.config.tag
