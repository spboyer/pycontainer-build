import copy, hashlib, json, os, shutil, tarfile, tempfile, logging
from dataclasses import dataclass, field
from contextlib import nullcontext
from contextvars import ContextVar
from pathlib import Path
from typing import Optional, List, Dict, Tuple
from .config import BuildConfig, MULTI_PLATFORM_SCAN_ERROR, canonical_registry, validate_exposed_ports
from .system_packages import report_system_packages
from .blob_store import (
    BlobIntegrityError,
    HASH_CHUNK_SIZE as _HASH_CHUNK_SIZE,
    blob_matches as _blob_matches,
    create_temporary_file as _create_temporary_file,
    digest_filename as _digest_filename,
    hash_file as _hash_file,
    publish_content_addressed as _publish_content_addressed,
)
from .fs_utils import (
    VIRTUALENV_EXCLUDES,
    context_exclude_paths,
    ensure_dir,
    output_directory_lock,
    relative_context_path,
    select_context_files,
)


class LayerIntegrityError(BlobIntegrityError):
    """Raised when a staged layer does not match its expected content."""


class UnsupportedPlatformError(ValueError):
    """Raised when a base image cannot satisfy the requested platform."""


OCI_INDEX_MEDIA_TYPES = {
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
}
IMAGE_MANIFEST_MEDIA_TYPES = {
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
}
IMAGE_CONFIG_MEDIA_TYPES = {
    "application/vnd.oci.image.config.v1+json",
    "application/vnd.docker.container.image.v1+json",
}


def _write_content_addressed_bytes(
    destination: Path, content: bytes, digest: str
) -> Tuple[str, int]:
    staged = _create_temporary_file(destination.parent, destination.name)
    try:
        with staged.open("wb") as target:
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
        actual_digest, size = _hash_file(staged)
        if actual_digest != digest:
            raise LayerIntegrityError(
                f"Staged blob hashes to {actual_digest}, not {digest}."
            )
        return _publish_content_addressed(staged, destination, digest, size)
    finally:
        staged.unlink(missing_ok=True)


def _atomic_write_bytes(destination: Path, content: bytes) -> None:
    descriptor, name = tempfile.mkstemp(
        prefix=f".{destination.name}-", suffix=".tmp", dir=destination.parent
    )
    staged = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as target:
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
        os.replace(staged, destination)
    finally:
        staged.unlink(missing_ok=True)


def parse_platform(platform: str) -> Tuple[str, str]:
    """Parse a platform string into its OS and architecture components."""
    parts = platform.split("/")
    if (
        len(parts) not in (2, 3)
        or any(not part for part in parts)
        or any(part.strip() != part for part in parts)
    ):
        raise ValueError(
            f"Invalid platform format: {platform}. Expected 'os/arch[/variant]' "
            "(e.g., 'linux/amd64' or 'linux/arm/v7')."
        )
    return parts[0], parts[1]


def parse_platform_variant(platform: str) -> Optional[str]:
    """Return an optional platform variant after validating the platform string."""
    parse_platform(platform)
    parts = platform.split("/")
    return parts[2] if len(parts) == 3 else None


def _validate_registry_descriptor(
    descriptor: Dict, description: str
) -> Tuple[str, int, str]:
    if not isinstance(descriptor, dict):
        raise RegistryRequestError(f"{description} is not a descriptor object.")
    digest = descriptor.get("digest")
    try:
        _digest_filename(digest)
    except ValueError as error:
        raise RegistryRequestError(f"{description} has an invalid digest.") from error
    size = descriptor.get("size")
    if type(size) is not int or size < 0:
        raise RegistryRequestError(f"{description} has an invalid size.")
    media_type = descriptor.get("mediaType")
    if not isinstance(media_type, str) or not media_type:
        raise RegistryRequestError(f"{description} has an invalid media type.")
    return digest, size, media_type


def _select_index_descriptor(
    index: Dict, os_name: str, arch: str, variant: Optional[str]
) -> Tuple[Dict, Optional[str]]:
    descriptors = index.get("manifests")
    if not isinstance(descriptors, list):
        raise RegistryRequestError("Base image index does not contain a manifest list.")

    candidates = []
    available = []
    for descriptor in descriptors:
        if not isinstance(descriptor, dict):
            raise RegistryRequestError("Base image index contains an invalid descriptor.")
        platform = descriptor.get("platform")
        if not isinstance(platform, dict):
            continue
        descriptor_os = platform.get("os")
        descriptor_arch = platform.get("architecture")
        descriptor_variant = platform.get("variant")
        if (
            not isinstance(descriptor_os, str)
            or not descriptor_os
            or not isinstance(descriptor_arch, str)
            or not descriptor_arch
        ):
            continue
        if descriptor_variant is not None and (
            not isinstance(descriptor_variant, str) or not descriptor_variant
        ):
            raise RegistryRequestError(
                "Base image index contains an invalid platform variant."
            )
        available.append(
            f"{descriptor_os}/{descriptor_arch}"
            + (f"/{descriptor_variant}" if descriptor_variant else "")
        )
        if descriptor_os == os_name and descriptor_arch == arch:
            candidates.append((descriptor, descriptor_variant))

    if variant is not None:
        candidates = [item for item in candidates if item[1] == variant]
    else:
        unqualified = [item for item in candidates if item[1] is None]
        if len(unqualified) == 1:
            candidates = unqualified

    if len(candidates) == 1:
        return candidates[0]
    target = f"{os_name}/{arch}" + (f"/{variant}" if variant else "")
    available_text = ", ".join(sorted(set(available))) or "none"
    if not candidates:
        raise UnsupportedPlatformError(
            f"Base image has no exact manifest for {target}. "
            f"Available platforms: {available_text}."
        )
    raise UnsupportedPlatformError(
        f"Base image has multiple manifests matching {target}; specify an exact "
        f"variant. Available platforms: {available_text}."
    )
from .oci import (
    OCI_MANIFEST_MEDIA_TYPE,
    OCILayer,
    OCIManifestDescriptor,
    build_config_json,
    build_image_index_json,
    build_index_json,
    build_layout_index_json,
    build_manifest_json,
    build_oci_layout,
)
from .project import (
    CONDA_FILES, detect_conda_inputs, detect_entrypoint,
    default_include_paths, find_dependencies,
)
from .fs_utils import conda_prefix, relative_context_path
from .framework import apply_framework_defaults
from .release_notes import release_notes_labels
from .registry_client import (
    RegistryClient,
    RegistryRequestError,
    parse_image_reference,
)
from .auth import get_mirror_auth, get_registry_auth
from .registry_mirrors import MirroredPullClient
from .local_image import (
    LocalImageError,
    check_local_image_bounds,
    open_local_image_source,
    parse_local_image_reference,
    verify_layer_diff_ids,
)
from .cache import LayerCache, app_layer_arcname, emitted_hardlinks, ordered_app_files
from .dependency_cache import (
    MAX_INVENTORY_BYTES,
    DependencyCacheMiss,
    decode_inventory,
    dependency_cache_key,
    encode_inventory,
    materialize_requirements_snapshot,
)
from .dependency_groups import (
    GROUPS,
    decode_group_bundle,
    encode_group_bundle,
    group_bundle_key,
    group_layer_key,
    partition_staged_files,
    plan_groups,
)
from .history import BuildHistory, BuildTiming
from .sbom import generate_sbom
from .squash import SquashError, squash_layers
from .layer_analysis import analyze_platform, shared_blobs
from .tags import local_reference_names
from .security import SECURITY_DIR, render_security_fragments, write_security_fragments
from .dependencies import (
    _LOCKFILE_STAGING_PREFIX,
    PipConfigurationSnapshotError,
    create_console_entrypoints,
    install_locked_requirements,
    install_requirements,
    reject_credentialed_direct_urls,
    reject_shipped_credentials,
    staged_packages,
    stage_virtualenv,
    strip_type_stubs,
    target_python_full_version,
    target_python_version,
    target_libc_config,
    target_wheel_platform,
    validate_target_platform,
)
from .lockfiles import (
    LockSelection,
    _locked_verification_backend,
    _locked_verification_roots,
    detect_lockfile, load_lockfile, preflight_lockfile, target_environment,
    verify_locked_installation,
)

DEFAULT_REQUIREMENTS_FILE = "requirements.txt"

logger=logging.getLogger(__name__)

UNCOMPRESSED_LAYER_MEDIA_TYPE = "application/vnd.oci.image.layer.v1.tar"
SQUASH_HISTORY_CREATOR = "pycontainer-build --squash"


def _append_squash_history(cfg: Dict, base_layer_count: int, source_layer_count: int) -> None:
    """Describe the squashed layer only when base history already aligns with its layers."""
    history = cfg.get("history")
    if not isinstance(history, list):
        return
    layered = [entry for entry in history if not (isinstance(entry, dict) and entry.get("empty_layer"))]
    if len(layered) != base_layer_count:
        return
    history.append({
        "created_by": SQUASH_HISTORY_CREATOR,
        "comment": f"Squashed {source_layer_count} builder-created layers; base layers unchanged.",
    })


@dataclass
class PlatformImage:
    """One platform-specific image manifest built for an image index."""

    platform: Dict[str, str]
    manifest_digest: str
    manifest_size: int
    config_digest: str
    config_size: int
    layers: List[OCILayer]
    app_layer_count: int
    base_exposed_ports: List[str] = field(default_factory=list)


def _read_verified_blob(layers_dir: Path, digest: str, size: int, description: str) -> bytes:
    path = layers_dir / _digest_filename(digest)
    if path.is_symlink() or not path.is_file():
        raise LayerIntegrityError(f"{description} blob {digest} is missing.")
    content = path.read_bytes()
    actual = "sha256:" + hashlib.sha256(content).hexdigest()
    if actual != digest or len(content) != size:
        raise LayerIntegrityError(
            f"{description} blob {digest} does not match its descriptor."
        )
    return content


def _verify_platform_image(
    layers_dir: Path, image: PlatformImage, check_diff_ids: bool = False
) -> None:
    """Check manifest, config, platform, layer and DiffID agreement for one image."""
    target = "/".join(
        image.platform[key]
        for key in ("os", "architecture", "variant")
        if key in image.platform
    )
    manifest = json.loads(
        _read_verified_blob(
            layers_dir, image.manifest_digest, image.manifest_size, f"{target} manifest"
        )
    )
    if manifest.get("mediaType") != OCI_MANIFEST_MEDIA_TYPE:
        raise LayerIntegrityError(f"{target} manifest has an unexpected media type.")
    expected_config = {
        "mediaType": "application/vnd.oci.image.config.v1+json",
        "digest": image.config_digest,
        "size": image.config_size,
    }
    if manifest.get("config") != expected_config:
        raise LayerIntegrityError(f"{target} manifest config descriptor does not match.")
    if manifest.get("layers") != [layer.to_descriptor() for layer in image.layers]:
        raise LayerIntegrityError(f"{target} manifest layers do not match the build.")
    config = json.loads(
        _read_verified_blob(
            layers_dir, image.config_digest, image.config_size, f"{target} config"
        )
    )
    if (
        config.get("os") != image.platform.get("os")
        or config.get("architecture") != image.platform.get("architecture")
        or config.get("variant") != image.platform.get("variant")
    ):
        raise LayerIntegrityError(
            f"{target} config platform does not match its index descriptor."
        )
    diff_ids = config.get("rootfs", {}).get("diff_ids")
    if not isinstance(diff_ids, list) or len(diff_ids) != len(image.layers):
        raise LayerIntegrityError(
            f"{target} config has {len(diff_ids or [])} DiffIDs for "
            f"{len(image.layers)} layers."
        )
    for position, (layer, diff_id) in enumerate(zip(image.layers, diff_ids), 1):
        if layer.media_type == UNCOMPRESSED_LAYER_MEDIA_TYPE and diff_id != layer.digest:
            raise LayerIntegrityError(
                f"{target} layer {position} DiffID does not match its uncompressed digest."
            )
        if not _blob_matches(
            layers_dir / _digest_filename(layer.digest), layer.digest, layer.size
        ):
            raise LayerIntegrityError(
                f"{target} layer {position} blob {layer.digest} is missing or corrupt."
            )
    if check_diff_ids:
        # Compressed base layers need decompression; done once, before publication.
        try:
            verify_layer_diff_ids(image.layers, config)
        except (BlobIntegrityError, LocalImageError) as error:
            raise LayerIntegrityError(
                f"{target} layer DiffIDs their content: {error}"
            ) from error


def _verify_image_index(
    layers_dir: Path,
    index_digest: str,
    index_size: int,
    images: List[PlatformImage],
    check_diff_ids: bool = False,
) -> None:
    """Check that an index blob references exactly the verified platform images."""
    index = json.loads(
        _read_verified_blob(layers_dir, index_digest, index_size, "Image index")
    )
    expected = [
        {
            "mediaType": OCI_MANIFEST_MEDIA_TYPE,
            "digest": image.manifest_digest,
            "size": image.manifest_size,
            "platform": image.platform,
        }
        for image in images
    ]
    if index.get("manifests") != expected:
        raise LayerIntegrityError("Image index descriptors do not match the built images.")
    platforms = [tuple(sorted(image.platform.items())) for image in images]
    if len(set(platforms)) != len(platforms):
        raise LayerIntegrityError("Image index contains duplicate platforms.")
    for image in images:
        _verify_platform_image(layers_dir, image, check_diff_ids)

class ImageBuilder:
    def __init__(self, config: BuildConfig):
        config.validate_metadata()
        self._history = (
            BuildHistory(config.history_dir)
            if config.record_history and not config.dry_run
            else None
        )
        self._history_timer = (
            ContextVar("pycontainer_build_history", default=None)
            if self._history else None
        )
        if self._history:
            self._history.validate_location(config.context_dir, config.output_dir)
            timing = BuildTiming()
            try:
                with timing.phase("initialization"):
                    self._initialize(config)
            except BaseException:
                self._record_failed_history(timing, config)
                raise
        else:
            self._initialize(config)

    def _initialize(self, config: BuildConfig):
        context_dir=Path(config.context_dir)
        default_cache_dir=Path.home()/".pycontainer"/"cache"
        cache_dir=(
            Path(config.cache_dir).expanduser()
            if config.cache_dir
            else default_cache_dir
        )
        self.context_excludes=(
            *context_exclude_paths(
                context_dir,
                output_dir=config.output_dir,
                cache_dir=cache_dir,
                exclude_paths=config.exclude_paths,
                exclude_defaults=config.exclude_defaults,
            ),
            f"{_LOCKFILE_STAGING_PREFIX}*",
        )
        self.context_include_paths=(
            config.include_paths
            if config.include_paths is not None
            else default_include_paths(
                context_dir,
                exclude_paths=self.context_excludes,
                exclude_defaults=config.exclude_defaults,
            )
        )
        self.dependency_excludes=tuple(
            exclude
            for exclude in self.context_excludes
            if not (
                config.exclude_defaults
                and exclude in VIRTUALENV_EXCLUDES
                and exclude not in config.exclude_paths
            )
        )
        self.release_notes_labels=release_notes_labels(config)
        framework_label_explicit="framework" in (config.labels or {})
        self.config=apply_framework_defaults(
            config,
            context_dir,
            self.context_include_paths,
            self.context_excludes,
            exclude_defaults=False,
        )
        if "framework" in self.release_notes_labels and not framework_label_explicit and self.config.labels:
            # The configured release-notes key takes precedence over the framework default.
            self.config.labels.pop("framework", None)
        self.verbose=getattr(config, 'verbose', False)
        self.dry_run=getattr(config, 'dry_run', False)
        if config.use_cache and not self.dry_run:
            self.cache=LayerCache(cache_dir=cache_dir, max_size_mb=config.max_cache_size_mb)
        else:
            self.cache=None

    def _print(self, message: str):
        print(self.config.redact(message))

    def _record_failed_history(self, timing, config):
        try:
            self._history.validate_location(config.context_dir, config.output_dir)
            self._history.append(timing.record("failed"))
        except BaseException as error:
            # Recording must never replace the original build failure.
            logger.error("Build history could not be saved or confirmed: %s", config.redact(str(error)))

    def _time_history_phase(self, name):
        timing = self._history_timer.get() if self._history_timer else None
        return timing.phase(name) if timing else nullcontext()

    def build(self):
        validate_exposed_ports(self.config.exposed_ports)
        self.config.validate_metadata()
        self._conda_warning_emitted = False
        self._dependency_source_cache = None
        if self.config.scan and len(self.config.target_platforms()) > 1:
            raise ValueError(MULTI_PLATFORM_SCAN_ERROR)
        if conda_prefix(self.config.context_dir, ".") is not None:
            raise ValueError(
                "The build context is inside a host Conda environment; use a "
                "source-only build context to avoid copying host environment files. "
                "Conda staging is unsupported. Export pip-compatible requirements "
                "with `python -m pip freeze > requirements.txt` and select that "
                "file with --requirements and --include-deps."
            )
        if self.config.include_deps:
            self._check_conda_dependencies()
            if self.config.dependency_layer_strategy != "single":
                self._grouped_dependency_source()
        if self.dry_run:
            logger.info("DRY RUN: Would build image %s", self.config.redact(self.config.tag))
            self._show_build_plan()
            return self.config.tag

        timing = BuildTiming() if self._history else None
        token = self._history_timer.set(timing) if timing else None
        try:
            try:
                if self._history:
                    self._history.validate_location(
                        self.config.context_dir, self.config.output_dir
                    )
                with output_directory_lock(self.config.output_dir):
                    security_fragments=render_security_fragments(
                        self.config.security, self.config.context_dir
                    )
                    result = self._build_image()
                    if self.config.scan:
                        from .scanning import scanned_image

                        with scanned_image(
                            Path(self.config.output_dir), self.manifest_digest, self.config
                        ):
                            logger.info(self.config.redact(
                                f"Vulnerability gate passed for {self.manifest_digest}"
                            ))
                    for fragment_path in write_security_fragments(
                        Path(self.config.output_dir), security_fragments
                    ):
                        logger.info(self.config.redact(f"✓ Security fragment saved to {fragment_path} (not enforced by the image)"))
            except BaseException:
                if timing:
                    self._record_failed_history(timing, self.config)
                raise

            if timing:
                self._history.validate_location(
                    self.config.context_dir, self.config.output_dir
                )
                self._history.append(timing.record("success"))
            return result
        finally:
            if timing:
                self._history_timer.reset(token)

    def _build_image(self):
        self._dependency_source_cache = None
        self._lock_selection_cache = {}
        self._requirement_files = []
        output=ensure_dir(self.config.output_dir)
        blobs=ensure_dir(output/'blobs')
        layers_dir=ensure_dir(blobs/'sha256')
        refs_dir=ensure_dir(output/'refs'/'tags')

        platforms=self.config.target_platforms()
        self._registry_base_snapshot=None
        self._resolved_base_from_tag=False
        self._base_image_tag_verified=False
        self._local_base_snapshot=None
        self._app_layer=None
        self._layer_reports=[]
        if len(platforms)>1:
            return self._build_multi_platform(output, layers_dir, refs_dir, platforms)

        image=self._build_platform_image(layers_dir, platforms[0])
        if self.config.analyze_layers:
            self._write_layer_analysis(output, [image])

        oci_layout=build_oci_layout()
        _atomic_write_bytes(
            output/'oci-layout',
            json.dumps(oci_layout,separators=(',',':')).encode(),
        )
        ref_names=local_reference_names(self.config.tag, self.config.all_tags())
        index=build_index_json(
            image.manifest_digest,
            image.manifest_size,
            ref_names[0],
            image.platform["architecture"],
            image.platform["os"],
            variant=image.platform.get("variant"),
            annotations=self.config.index_annotations,
            ref_names=ref_names,
        )
        _atomic_write_bytes(
            output/'index.json',
            json.dumps(index,separators=(',',':')).encode(),
        )

        for tag_ref in self.config.all_tags():
            _, _, tag_name=parse_image_reference(tag_ref)
            _atomic_write_bytes(refs_dir/tag_name, image.manifest_digest.encode())

        self.manifest_digest=image.manifest_digest
        self.config_digest=image.config_digest
        self.layers=image.layers
        self.index_digest=None
        self.platform_images=[image]

        if getattr(self.config, 'generate_sbom', False):
            sbom_path=output/'sbom.json'
            logger.info("Generating SBOM...")
            with self._time_history_phase("sbom"):
                generate_sbom(
                    Path(self.config.context_dir),
                    sbom_path,
                    format=(
                        self.config.generate_sbom
                        if isinstance(self.config.generate_sbom, str)
                        else "spdx"
                    ),
                    package_inventory=getattr(self, "_staged_packages", []),
                )
            logger.info(self.config.redact(f"✓ SBOM saved to {sbom_path}"))
        return self.config.tag

    def _build_multi_platform(self, output: Path, layers_dir: Path, refs_dir: Path, platforms: List[str]):
        """Build every platform in isolation, then publish one index only if all succeed."""
        images=[]
        for platform in platforms:
            self._print(f"Building platform {platform}...")
            images.append(self._build_platform_image(layers_dir, platform))

        index_digest, index_size=self._write_image_index(layers_dir, images)
        _verify_image_index(layers_dir, index_digest, index_size, images, check_diff_ids=True)
        self._verify_local_base_unchanged(layers_dir)
        if self.config.analyze_layers:
            self._write_layer_analysis(output, images)

        _atomic_write_bytes(
            output/'oci-layout',
            json.dumps(build_oci_layout(),separators=(',',':')).encode(),
        )
        ref_names=local_reference_names(self.config.tag, self.config.all_tags())
        layout_index=build_layout_index_json(
            index_digest, index_size, ref_names, annotations=self.config.index_annotations
        )
        _atomic_write_bytes(
            output/'index.json',
            json.dumps(layout_index,separators=(',',':')).encode(),
        )
        for tag_ref in self.config.all_tags():
            _, _, tag_name=parse_image_reference(tag_ref)
            _atomic_write_bytes(refs_dir/tag_name, index_digest.encode())

        self.index_digest=index_digest
        self.index_size=index_size
        self.manifest_digest=index_digest
        self.config_digest=None
        self.platform_images=images
        self.layers=list({layer.digest: layer for image in images for layer in image.layers}.values())
        self._print(f"✓ Image index created for {', '.join(platforms)} ({index_digest[:19]}...)")
        return self.config.tag

    def _write_image_index(self, layers_dir: Path, images: List["PlatformImage"]) -> Tuple[str, int]:
        descriptors=[
            OCIManifestDescriptor(OCI_MANIFEST_MEDIA_TYPE, image.manifest_digest, image.manifest_size, dict(image.platform))
            for image in images
        ]
        index_bytes=json.dumps(
            build_image_index_json(descriptors, annotations=self.config.index_annotations),
            separators=(',',':'),
        ).encode()
        index_digest="sha256:"+hashlib.sha256(index_bytes).hexdigest()
        _write_content_addressed_bytes(layers_dir/_digest_filename(index_digest), index_bytes, index_digest)
        return index_digest, len(index_bytes)

    def _build_platform_image(self, layers_dir: Path, platform: str) -> "PlatformImage":
        """Build config and manifest blobs for one platform without touching tags."""
        self._target_platform=platform
        self._staged_packages=[]
        self._dependency_site_packages=False
        self._base_image_tag_verified=False
        os_name, arch = parse_platform(platform)
        if self.config.include_deps:
            validate_target_platform(os_name, arch, parse_platform_variant(platform))
        with self._time_history_phase("base_image"):
            base_layers, base_config = self._pull_base_image(layers_dir, os_name, arch)
        base_exposed = ((base_config or {}).get("config") or {}).get("ExposedPorts")
        base_exposed_ports = list(base_exposed) if isinstance(base_exposed, dict) else []
        variant = (
            base_config.get("variant") if base_config else None
        ) or parse_platform_variant(platform)
        locked_names = ()
        if self.config.include_deps and self._uses_lockfile():
            locked_names = tuple(
                package.name
                for package in self._lock_selection(
                    base_config, os_name, arch, variant=variant,
                    base_image_tag_verified=self._has_verified_base_image_tag(),
                ).packages
            )
        report_system_packages(self.config, base_layers, locked_names, redact=self.config.redact)
        
        # Like Docker, a configured cmd without entrypoint runs under the base Entrypoint.
        if self.config.entrypoint is not None:
            entry = self.config.entrypoint
        elif self.config.cmd is not None:
            entry = None
        else:
            entry = detect_entrypoint(self.config.context_dir)
        include = self.context_include_paths

        app_layers=[]
        if self.config.include_deps:
            with self._time_history_phase("dependencies"):
                staging_config = target_libc_config(
                    os_name, arch, self.config.base_image, base_config, base_layers,
                    base_image_tag_verified=self._has_verified_base_image_tag(),
                    variant=variant,
                )
                if self.config.dependency_layer_strategy == "single":
                    deps_layer=self._create_deps_layer(
                        layers_dir, base_config=staging_config, os_name=os_name, arch=arch,
                        variant=variant,
                    )
                    if deps_layer: app_layers.append(deps_layer)
                else:
                    app_layers.extend(self._create_grouped_deps_layers(
                        layers_dir, base_config=staging_config, os_name=os_name, arch=arch,
                        variant=variant,
                    ))
        
        # The application tar is byte-identical for every target, so build it once.
        if self._app_layer is None:
            with self._time_history_phase("application"):
                self._app_layer=self._create_app_layer(layers_dir, include)
        app_layers.append(self._app_layer)

        source_layers=list(app_layers)
        squash_stats=None
        if self.config.squash and len(app_layers)>1:
            squashed, squash_stats=self._squash_builder_layers(layers_dir, app_layers)
            app_layers=[squashed]
            self._print(
                f"✓ Squashed {len(source_layers)} builder layers into one "
                f"({squash_stats.source_bytes} -> {squashed.size} bytes)"
            )
        elif self.config.squash:
            logger.info("Only one builder-created layer; --squash leaves the layout unchanged.")
        
        all_layers=base_layers+app_layers

        image_env = dict(self.config.env)
        if getattr(self, "_dependency_site_packages", False):
            base_env = {
                item.split("=", 1)[0]: item.split("=", 1)[1]
                for item in base_config.get("config", {}).get("Env", [])
                if "=" in item
            }
            prefix = self.config.workdir.rstrip("/") + "/.python"
            site_path = f"{prefix}/site-packages"
            bin_path = f"{prefix}/bin"
            current_pythonpath = image_env.get(
                "PYTHONPATH", base_env.get("PYTHONPATH", "")
            )
            current_path = image_env.get(
                "PATH",
                base_env.get(
                    "PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
                ),
            )
            image_env["PYTHONPATH"] = ":".join(
                part for part in (site_path, current_pythonpath) if part
            )
            image_env["PATH"] = f"{bin_path}:{current_path}"

        cfg = build_config_json(arch,os_name,image_env,self.config.workdir,entry,self.config.exposed_ports,
                                labels=self.config.labels,user=self.config.user,cmd=self.config.cmd,base_config=base_config,
                                layer_diff_ids=[layer.digest for layer in app_layers],
                                stop_signal=self.config.stop_signal,volumes=self.config.volumes,
                                generated_labels=self.release_notes_labels)
        if squash_stats is not None:
            _append_squash_history(cfg, len(base_layers), len(source_layers))
        if variant:
            cfg["variant"]=variant
        cfg_bytes=json.dumps(cfg,separators=(',',':')).encode()
        cfg_digest="sha256:"+hashlib.sha256(cfg_bytes).hexdigest()
        cfg_path=layers_dir/cfg_digest.split(":",1)[1]
        _write_content_addressed_bytes(cfg_path, cfg_bytes, cfg_digest)

        manifest=build_manifest_json(
            cfg_digest,len(cfg_bytes),all_layers,
            annotations=self.config.manifest_annotations,
        )
        manifest_bytes=json.dumps(manifest,separators=(',',':')).encode()
        manifest_digest="sha256:"+hashlib.sha256(manifest_bytes).hexdigest()
        manifest_path=layers_dir/manifest_digest.split(":",1)[1]
        _write_content_addressed_bytes(manifest_path, manifest_bytes, manifest_digest)

        platform_descriptor={"architecture": arch, "os": os_name}
        if variant:
            platform_descriptor["variant"]=variant
        image=PlatformImage(
            platform=platform_descriptor,
            manifest_digest=manifest_digest,
            manifest_size=len(manifest_bytes),
            config_digest=cfg_digest,
            config_size=len(cfg_bytes),
            layers=all_layers,
            app_layer_count=len(app_layers),
            base_exposed_ports=base_exposed_ports,
        )
        if self.config.analyze_layers:
            self._layer_reports.append(analyze_platform(image, source_layers, squash_stats))
        return image

    def _squash_builder_layers(self, layers_dir: Path, layers: List[OCILayer]):
        """Publish one uncompressed layer equivalent to the builder-created layers."""
        for layer in layers:
            if layer.media_type!=UNCOMPRESSED_LAYER_MEDIA_TYPE:
                raise SquashError(f"Cannot squash builder layer with media type {layer.media_type}.")
        tmp=_create_temporary_file(layers_dir,"squash-layer")
        try:
            with tmp.open("wb") as target:
                stats=squash_layers([Path(layer.tar_path) for layer in layers], target)
                target.flush()
                os.fsync(target.fileno())
            digest, size=_hash_file(tmp)
            final=layers_dir/_digest_filename(digest)
            _publish_content_addressed(tmp, final, digest, size)
            return OCILayer(UNCOMPRESSED_LAYER_MEDIA_TYPE, digest, size, str(final)), stats
        finally:
            tmp.unlink(missing_ok=True)

    def _write_layer_analysis(self, output: Path, images: List["PlatformImage"]) -> None:
        report={
            "squash_requested": bool(self.config.squash),
            "platforms": self._layer_reports,
            "shared_blobs": shared_blobs(images),
        }
        _atomic_write_bytes(
            output/'layer-analysis.json',
            (json.dumps(report,indent=2,sort_keys=True)+"\n").encode(),
        )
        for platform in self._layer_reports:
            squash=platform["squash"]
            line=(
                f"Layer analysis {platform['platform']}: {len(platform['layers'])} layers, "
                f"{platform['compressed_bytes']} blob bytes"
            )
            if "measured_bytes" in squash:
                line+=(
                    f"; builder layers {squash['source_bytes']} bytes, squashed "
                    f"{squash['measured_bytes']} (predicted {squash['predicted_bytes']})"
                )
            elif "unsquashable" in squash:
                line+=f"; builder layers cannot be squashed: {squash['unsquashable']}"
            self._print(line)
            for warning in platform["warnings"]:
                self._print(f"  warning: {warning}")
        self._print(f"✓ Layer analysis saved to {output/'layer-analysis.json'}")
    
    def _requested_platform(self) -> str:
        return getattr(self, "_target_platform", None) or self.config.platform

    def _registry_client(self, registry: str, repo: str, auth) -> RegistryClient:
        """Create a registry client honoring insecure-host and CA settings."""
        insecure=registry.lower() in self.config.insecure_registries
        log_options = {"log_redact": self.config.redact} if self.config.build_args else {}
        client=RegistryClient(
            registry,
            repo,
            auth_token=auth.bearer_token,
            username=auth.username,
            password=auth.password,
            insecure=insecure,
            ca_file=self.config.registry_ca_file,
            **log_options,
        )
        warned=self.__dict__.setdefault('_warned_insecure_registries', set())
        if insecure and registry.lower() not in warned:
            warned.add(registry.lower())
            logger.warning(self.config.redact(
                f"WARNING: using plain HTTP for insecure registry {registry}; "
                "traffic is unencrypted and unauthenticated by TLS."
            ))
        return client

    def _base_image_client(self, registry: str, repo: str):
        """Return a pull client that tries configured mirrors before upstream."""
        log_options = {"log_redact": self.config.redact} if self.config.build_args else {}

        def upstream_client():
            auth=get_registry_auth(registry, include_generic_environment=False, **log_options)
            return self._registry_client(registry, repo, auth)

        mirrors=self.config.registry_mirrors.get(canonical_registry(registry), [])
        if not mirrors:
            return upstream_client()

        def mirror_client(host):
            return lambda: self._registry_client(host, repo, get_mirror_auth(host, **log_options))

        endpoints=[(host, mirror_client(host)) for host in mirrors]
        endpoints.append((registry, upstream_client))
        return MirroredPullClient(registry, endpoints, **log_options)

    def _pull_base_image(self, layers_dir: Path, os_name: str, arch: str) -> Tuple[List[OCILayer], Optional[Dict]]:
        """Pull or load the base image, return (base_layers, base_config)."""
        self._base_image_name=self.config.base_image
        requested_variant = parse_platform_variant(self._requested_platform())
        local=parse_local_image_reference(self.config.base_image)
        if local is None:
            registry, repo, reference=parse_image_reference(self.config.base_image)
            client=self._base_image_client(registry, repo)
            return self._read_base_image(client, reference, layers_dir, os_name, arch)

        target = f"{os_name}/{arch}" + (
            f"/{requested_variant}" if requested_variant else ""
        )
        self._print(f"Loading local base image {self.config.base_image} for {target}...")
        with open_local_image_source(
            local, layers_dir, Path(self.config.output_dir), os_name, arch, requested_variant,
            progress=self._print,
        ) as source:
            self._pin_local_base_snapshot(getattr(source, "snapshot_digest", None))
            base_layers, base_config = self._read_base_image(
                source, None, layers_dir, os_name, arch
            )
            verify_layer_diff_ids(base_layers, base_config)
            self._base_image_name=source.source_name
        return base_layers, base_config

    def _pin_local_base_snapshot(self, snapshot: Optional[str]) -> None:
        """Require every platform to select from the same local layout index."""
        if snapshot is None:
            return
        pinned=getattr(self, "_local_base_snapshot", None)
        if pinned is None:
            self._local_base_snapshot=snapshot
        elif snapshot != pinned:
            raise LocalImageError(
                f"Local base image {self.config.base_image} changed during the "
                f"multi-platform build (index {pinned[:19]}... became {snapshot[:19]}...); "
                "rebuild once the layout is stable."
            )

    def _verify_local_base_unchanged(self, layers_dir: Path) -> None:
        """Re-read a pinned local layout index before publishing the image index."""
        if getattr(self, "_local_base_snapshot", None) is None:
            return
        local=parse_local_image_reference(self.config.base_image)
        with open_local_image_source(
            local, layers_dir, Path(self.config.output_dir), None, None
        ) as source:
            self._pin_local_base_snapshot(getattr(source, "snapshot_digest", None))

    def _read_base_image(self, client, reference: Optional[str], layers_dir: Path, os_name: str, arch: str) -> Tuple[List[OCILayer], Optional[Dict]]:
        """Resolve and validate a base image through a registry or local source."""
        local = reference is None
        platform = self._requested_platform()
        requested_variant = parse_platform_variant(platform)
        target = f"{os_name}/{arch}" + (
            f"/{requested_variant}" if requested_variant else ""
        )
        # Every registry platform selects from the first verified base snapshot; the
        # mutable tag is never requested again, so a moved tag cannot mix bases.
        snapshot = None if local else getattr(self, "_registry_base_snapshot", None)
        if not local:
            self._print(f"Pulling base image {self.config.base_image} for {target}...")
        if snapshot is None:
            manifest, resolved_digest = client.pull_manifest(
                reference,
                expected_digest=(
                    reference
                    if reference and reference.startswith("sha256:")
                    else None
                ),
            )
        else:
            manifest, resolved_digest = copy.deepcopy(snapshot[0]), snapshot[1]
        if local:
            self._base_image_tag_verified = False
        elif snapshot is None:
            self._registry_base_snapshot = (copy.deepcopy(manifest), resolved_digest)
            # Only a registry pull by tag can vouch for the tag; a user-supplied
            # tag@digest or digest is pulled by digest and never proves its tag.
            # RegistryClient verifies sha256 and sha512 digests against the bytes.
            self._resolved_base_from_tag = (
                not reference.startswith("sha256:")
                and isinstance(resolved_digest, str)
                and resolved_digest.startswith(("sha256:", "sha512:"))
            )
            self._base_image_tag_verified = self._resolved_base_from_tag
        else:
            self._base_image_tag_verified = bool(
                getattr(self, "_resolved_base_from_tag", False)
            )

        selected_variant = None
        selected_from_index = False
        if manifest.get("mediaType") in OCI_INDEX_MEDIA_TYPES:
            selected_from_index = True
            descriptor, selected_variant = _select_index_descriptor(
                manifest, os_name, arch, requested_variant
            )
            manifest_digest, manifest_size, descriptor_media_type = (
                _validate_registry_descriptor(
                    descriptor, "Selected base image manifest descriptor"
                )
            )
            if descriptor_media_type not in IMAGE_MANIFEST_MEDIA_TYPES:
                raise RegistryRequestError(
                    "Selected base image descriptor does not reference an image manifest."
                )
            manifest, _ = client.pull_manifest(
                manifest_digest,
                expected_digest=manifest_digest,
                expected_size=manifest_size,
            )
            if manifest.get("mediaType") != descriptor_media_type:
                raise RegistryRequestError(
                    "Selected base image manifest media type does not match its index descriptor."
                )
        elif manifest.get("mediaType") not in IMAGE_MANIFEST_MEDIA_TYPES:
            raise RegistryRequestError(
                f"Base image has unsupported manifest media type "
                f"{manifest.get('mediaType')!r}."
            )

        if manifest.get("schemaVersion") != 2:
            raise RegistryRequestError("Base image manifest has an unsupported schema version.")

        config_digest, config_size, config_media_type = _validate_registry_descriptor(
            manifest.get("config"), "Base image config descriptor"
        )
        if config_media_type not in IMAGE_CONFIG_MEDIA_TYPES:
            raise RegistryRequestError(
                f"Base image config has unsupported media type {config_media_type!r}."
            )

        layer_descriptors = manifest.get("layers")
        if not isinstance(layer_descriptors, list):
            raise RegistryRequestError("Base image manifest does not contain a layer list.")
        validated_layers = [
            _validate_registry_descriptor(
                descriptor, f"Base image layer {index}"
            )
            for index, descriptor in enumerate(layer_descriptors, 1)
        ]
        if local:
            check_local_image_bounds(config_size, [size for _, size, _ in validated_layers])

        config_path=layers_dir/_digest_filename(config_digest)
        client.pull_blob(config_digest, config_path, config_size)
        try:
            base_config=json.loads(config_path.read_bytes())
        except (json.JSONDecodeError, UnicodeDecodeError):
            invalid_base_config = True
        else:
            invalid_base_config = False
        if invalid_base_config:
            raise RegistryRequestError("Base image config is not valid JSON.")
        if not isinstance(base_config, dict):
            raise RegistryRequestError("Base image config is not a JSON object.")

        config_os = base_config.get("os")
        config_arch = base_config.get("architecture")
        if config_os != os_name or config_arch != arch:
            raise UnsupportedPlatformError(
                "Pulled base image config platform "
                f"{config_os!r}/{config_arch!r} does not match requested {target}."
            )
        config_variant = base_config.get("variant")
        if config_variant is not None and (
            not isinstance(config_variant, str) or not config_variant
        ):
            raise RegistryRequestError("Base image config has an invalid platform variant.")
        if (
            selected_from_index
            and config_variant is not None
            and config_variant != selected_variant
        ):
            raise UnsupportedPlatformError(
                "Pulled base image config variant "
                f"{config_variant!r} does not match index variant "
                f"{selected_variant!r}."
            )
        resolved_variant = (
            selected_variant if selected_from_index else config_variant
        )
        if requested_variant is not None and resolved_variant != requested_variant:
            raise UnsupportedPlatformError(
                "Resolved base image variant "
                f"{resolved_variant!r} does not match requested variant "
                f"{requested_variant!r}."
            )
        if resolved_variant is not None:
            base_config["variant"] = resolved_variant

        base_layers=[]
        for i, (layer_digest, layer_size, media_type) in enumerate(
            validated_layers, 1
        ):
            layer_path=layers_dir/_digest_filename(layer_digest)
            if not layer_path.exists() or layer_path.is_symlink():
                self._print(f"  {'Copying' if local else 'Pulling'} layer {i}/{len(layer_descriptors)} ({layer_digest[:19]}...)")
            client.pull_blob(layer_digest, layer_path, layer_size)
            base_layers.append(
                OCILayer(media_type, layer_digest, layer_size, str(layer_path))
            )
        
        self._print(f"✓ Base image {'loaded' if local else 'pulled'} ({len(base_layers)} layers)")
        if self.verbose and isinstance(client, MirroredPullClient):
            served=", ".join(client.serving_hosts) or "local blob store"
            self._print(f"  Served by: {served}")
        return base_layers, base_config
    
    def push(self, registry_url: Optional[str]=None, auth_token: Optional[str]=None, username: Optional[str]=None, password: Optional[str]=None, show_progress: bool=True, *, _output_locked: bool=False):
        """Push built image to registry."""
        if not hasattr(self, 'manifest_digest'):
            raise RuntimeError("Must call build() before push()")
        if self.config.scan:
            if getattr(self, "index_digest", None):
                raise ValueError(MULTI_PLATFORM_SCAN_ERROR)
            from .scanning import scanned_image

            with nullcontext() if _output_locked else output_directory_lock(self.config.output_dir):
                with scanned_image(
                    Path(self.config.output_dir), self.manifest_digest, self.config
                ) as (snapshot, descriptor):
                    return self._push_image(registry_url, auth_token, username, password, show_progress, snapshot, descriptor)
        return self._push_image(registry_url, auth_token, username, password, show_progress)

    def _push_image(self, registry_url, auth_token, username, password, show_progress, output=None, descriptor=None):
        output=output or Path(self.config.output_dir)
        layers_dir=output/'blobs'/'sha256'
        manifest_digest=descriptor["digest"] if descriptor else self.manifest_digest
        manifest_path=layers_dir/manifest_digest.split(":",1)[1]
        manifest_data=manifest_path.read_bytes()
        if descriptor:
            manifest=json.loads(manifest_data)
            layers=manifest["layers"]
            config_digest=manifest["config"]["digest"]
        else:
            layers=[layer.to_descriptor() for layer in self.layers]
            config_digest=self.config_digest
        target=registry_url or self.config.tag
        registry, repo, tag=parse_image_reference(target)
        push_tags=[tag]
        for tag_ref in self.config.all_tags()[1:]:
            extra_tag=parse_image_reference(tag_ref)[2]
            if extra_tag not in push_tags: push_tags.append(extra_tag)

        if getattr(self, "index_digest", None):
            self.pushed_refs=[]
        
        log_options = {"log_redact": self.config.redact} if self.config.build_args else {}
        auth=get_registry_auth(
            registry,
            bearer_token=auth_token,
            username=username,
            password=password,
            **log_options,
        )
        client=self._registry_client(registry, repo, auth)
        
        if show_progress: self._print(f"Pushing to {registry}/{repo}:{tag}")

        if getattr(self, "index_digest", None):
            self._push_image_index(client, layers_dir, push_tags, f"{registry}/{repo}", show_progress)
            return f"{registry}/{repo}:{tag}"
        
        for i, layer in enumerate(layers, 1):
            digest=layer["digest"]
            if show_progress: self._print(f"  Pushing layer {i}/{len(layers)} ({digest[:19]}...)")
            blob_path=layers_dir/digest.split(":",1)[1]
            skipped=not client.push_blob(digest, blob_path, check_exists=True)
            if show_progress and skipped: self._print("    Layer exists, skipped")
        
        if show_progress: self._print(f"  Pushing config ({config_digest[:19]}...)")
        cfg_path=layers_dir/config_digest.split(":",1)[1]
        client.push_blob(config_digest, cfg_path, check_exists=True)
        
        if show_progress: self._print(f"  Pushing manifest ({manifest_digest[:19]}...)")
        for push_tag in push_tags:
            client.push_manifest(push_tag, manifest_data)
            if show_progress: self._print(f"✓ Pushed {registry}/{repo}:{push_tag}")
        self.pushed_refs=[f"{registry}/{repo}:{t}" for t in push_tags]

        return f"{registry}/{repo}:{tag}"

    def _push_image_index(self, client, layers_dir: Path, push_tags: List[str], repository: str, show_progress: bool) -> None:
        """Push blobs, then platform manifests by digest, then the index; tags go last."""
        _verify_image_index(layers_dir, self.index_digest, self.index_size, self.platform_images)
        blobs={}
        for image in self.platform_images:
            for layer in image.layers: blobs.setdefault(layer.digest, None)
            blobs.setdefault(image.config_digest, None)
        for i, digest in enumerate(blobs, 1):
            if show_progress: self._print(f"  Pushing blob {i}/{len(blobs)} ({digest[:19]}...)")
            skipped=not client.push_blob(digest, layers_dir/_digest_filename(digest), check_exists=True)
            if show_progress and skipped: self._print(f"    Blob exists, skipped")
        for image in self.platform_images:
            target="/".join(image.platform[k] for k in ("os","architecture","variant") if k in image.platform)
            if show_progress: self._print(f"  Pushing {target} manifest ({image.manifest_digest[:19]}...)")
            manifest_data=(layers_dir/_digest_filename(image.manifest_digest)).read_bytes()
            client.push_manifest(image.manifest_digest, manifest_data)
        index_data=(layers_dir/_digest_filename(self.index_digest)).read_bytes()
        if show_progress: self._print(f"  Pushing image index ({self.index_digest[:19]}...)")
        client.push_index(self.index_digest, index_data)
        for i, push_tag in enumerate(push_tags):
            ref=f"{repository}:{push_tag}"
            try:
                client.push_index(push_tag, index_data)
            except (RegistryRequestError, OSError) as error:
                confirmed=", ".join(self.pushed_refs) or "(none)"
                remaining=", ".join(f"{repository}:{tag}" for tag in push_tags[i+1:]) or "(none)"
                raise RegistryRequestError(
                    f"Image index tag publication failed. Confirmed pushed: {confirmed}. "
                    f"Failed tag write (outcome unknown): {ref}. Not attempted: {remaining}. "
                    f"Previously pushed tags were not rolled back. Cause: {error}"
                ) from error
            self.pushed_refs.append(ref)
            if show_progress: self._print(f"✓ Pushed index tag {push_tag}")
    
    def _show_build_plan(self):
        """Display build plan for dry-run mode."""
        self._print(f"Build Plan for {self.config.tag}:")
        if len(self.config.all_tags())>1:
            self._print(f"  Tags: {', '.join(self.config.all_tags())}")
        self._print(f"  Base Image: {self.config.base_image}")
        if self.config.registry_mirrors:
            mirrors="; ".join(f"{k} -> {', '.join(v)}" for k,v in self.config.registry_mirrors.items())
            self._print(f"  Base Image Mirrors: {mirrors}")
        platforms=self.config.target_platforms()
        if len(platforms)>1:
            self._print(f"  Platforms (image index): {', '.join(platforms)}")
        else:
            self._print(f"  Platform: {platforms[0]}")
        self._print(f"  Context: {self.config.context_dir}")
        self._print(f"  Working Dir: {self.config.workdir}")
        if self.config.entrypoint is not None:
            entrypoint = ' '.join(self.config.entrypoint) or '<none>'
        elif self.config.cmd is not None:
            entrypoint = '<inherited from base image>'
        else:
            entrypoint = '<auto-detect>'
        self._print(f"  Entrypoint: {entrypoint}")
        if self.config.cmd is not None:
            self._print(f"  Cmd: {' '.join(self.config.cmd) or '<none>'}")
        if self.config.exposed_ports:
            self._print(f"  Exposed Ports: {', '.join(map(str, self.config.exposed_ports))}")
        if self.config.env:
            self._print(f"  Environment: {', '.join(f'{k}={v}' for k,v in self.config.env.items())}")
        if self.config.labels:
            self._print(f"  Labels: {len(self.config.labels)} configured (values hidden)")
        for key, notes in self.release_notes_labels.items():
            self._print(f"  Release Notes: label {key}, {len(notes.encode('utf-8'))} bytes (contents hidden)")
        if self.config.manifest_annotations:
            self._print(f"  Manifest Annotations: {len(self.config.manifest_annotations)} configured (values hidden)")
        if self.config.index_annotations:
            self._print(f"  Index Annotations: {len(self.config.index_annotations)} configured (values hidden)")
        if self.config.stop_signal:
            self._print(f"  Stop Signal: {self.config.stop_signal}")
        if self.config.volumes:
            self._print(f"  Volumes (metadata, merged with base): {', '.join(self.config.volumes)}")
        self._print(f"  Include Dependencies: {self.config.include_deps}")
        if self.config.squash:
            self._print("  Squash: builder-created layers flattened into one (base layers unchanged)")
        if self.config.analyze_layers:
            self._print("  Layer Analysis: layer-analysis.json in the output directory")
        if self.config.include_deps:
            self._print(f"  Dependency Layer Strategy: {self.config.dependency_layer_strategy}")
            if self.config.dependency_layer_strategy != "single":
                self._print(f"  Stable Dependencies: {', '.join(self.config.stable_dependencies) or '<none>'}")
                self._print(f"  Frequent Dependencies: {', '.join(self.config.frequent_dependencies) or '<none>'}")
                if self.config.dependency_layer_strategy == "history":
                    self._print(f"  Dependency History Ref: {self.config.dependency_history_ref or 'HEAD'}")
                self._grouped_dependency_source()
            try:
                kind, value = self._dependency_source()
            except FileNotFoundError as error:
                self._print(f"  Dependency Source: unresolved ({error})")
            else:
                if kind == "lock":
                    preflight_lockfile(Path(self.config.context_dir), value)
                    if self.config.dependency_layer_strategy == "history":
                        from .dependency_history import read_dependency_history

                        snapshot, commits, _ = read_dependency_history(
                            Path(self.config.context_dir), value,
                            self.config.dependency_history_ref,
                        )
                        self._print(
                            f"  Dependency history snapshot: {snapshot}; "
                            f"first-parent lock commits: {', '.join(commits)}"
                        )
                        self._print("  Target-specific history assignments: checked during build.")
                described = "virtualenv site-packages" if kind == "venv" else value.as_posix()
                self._print(f"  Dependency Source: {kind} ({described})")
                if kind == "lock":
                    self._print("  Lockfile target markers (including uv marker freshness) and wheels: checked during build.")
            if self.config.strip_type_stubs:
                self._print("  Strip Type Stubs: True")
        if self.config.system_packages:
            self._print(f"  System Packages (verified, not installed): {', '.join(self.config.system_packages)}")
        for name in render_security_fragments(self.config.security, self.config.context_dir):
            self._print(f"  Security Fragment (applied by your runtime, not the image): {SECURITY_DIR}/{name}")
        self._print(f"  Use Cache: {self.config.use_cache}")

    def _uses_lockfile(self) -> bool:
        if self.config.lockfile:
            return True
        if self.config.requirements_file or not self.config.use_lockfile:
            return False
        return detect_lockfile(Path(self.config.context_dir)) is not None

    def _has_verified_base_image_tag(self) -> bool:
        # A local name or digest reference cannot establish registry tag provenance.
        return (
            parse_local_image_reference(self.config.base_image) is None
            and "@" not in self.config.base_image
            and getattr(self, "_base_image_tag_verified", False)
        )

    def _dependency_source(self):
        """Choose the dependency source: lock, requirements file, or virtualenv."""
        cached = getattr(self, "_dependency_source_cache", None)
        if cached is not None:
            return cached
        ctx = Path(self.config.context_dir)
        if self.config.lockfile:
            source = ("lock", relative_context_path(self.config.lockfile))
        elif self.config.requirements_file:
            source = ("requirements", relative_context_path(self.config.requirements_file))
        else:
            detected = detect_lockfile(ctx) if self.config.use_lockfile else None
            if detected:
                self._print(f"Using lockfile {detected} for dependencies (disable with --no-lockfile).")
                source = ("lock", Path(detected))
            else:
                deps_paths = find_dependencies(
                    ctx,
                    DEFAULT_REQUIREMENTS_FILE,
                    exclude_paths=self.dependency_excludes,
                    exclude_defaults=False,
                    prefer_requirements=self._check_conda_dependencies(),
                )
                if not deps_paths:
                    raise FileNotFoundError(
                        "Dependency staging was requested, but no supported lockfile, "
                        "virtualenv site-packages or requirements.txt was found."
                    )
                if any(relative == Path(DEFAULT_REQUIREMENTS_FILE) for _, relative in deps_paths):
                    source = ("requirements", Path(DEFAULT_REQUIREMENTS_FILE))
                else:
                    source = ("venv", deps_paths)
        if source[0] != "venv":
            self._dependency_source_cache = source
        return source

    def _lock_selection(self, base_config, os_name, arch, *, variant=None, base_image_tag_verified=False):
        _, relative = self._dependency_source()
        environment = target_environment(
            os_name,
            arch,
            target_python_version(
                getattr(self, "_base_image_name", self.config.base_image), base_config,
                base_image_tag_verified=base_image_tag_verified,
            ),
            target_python_full_version(base_config),
            variant=variant,
        )
        key = tuple(sorted(environment.items()))
        cache = getattr(self, "_lock_selection_cache", None)
        if cache is None:
            cache = self._lock_selection_cache = {}
        if key not in cache:
            cache[key] = load_lockfile(Path(self.config.context_dir), relative, environment)
        return cache[key]

    def _check_conda_dependencies(self):
        ctx = Path(self.config.context_dir)
        selected_lock = self.config.lockfile
        if not selected_lock and not self.config.requirements_file and self.config.use_lockfile:
            selected_lock = detect_lockfile(ctx)
        relative = relative_context_path(
            selected_lock or self.config.requirements_file or DEFAULT_REQUIREMENTS_FILE
        )
        signals = detect_conda_inputs(
            ctx, exclude_paths=self.dependency_excludes, exclude_defaults=False,
            selected_paths=self.context_include_paths,
        )
        selected = ctx / relative
        selected_exists = selected.is_file()
        conda_requirements = selected_exists and (
            relative.name in CONDA_FILES
            or selected.resolve().name in CONDA_FILES
        )
        selected_prefix = (
            conda_prefix(ctx, relative) if selected_exists else None
        )
        if conda_requirements or selected_prefix is not None:
            if relative not in signals:
                signals.append(relative)
        if not signals:
            return False
        if (
            conda_requirements
            or not selected_exists
            or selected_prefix is not None
        ):
            raise ValueError(
                f"Conda inputs detected ({', '.join(path.as_posix() for path in signals)}). "
                "Conda staging is unsupported. Export pip-compatible requirements "
                "with `python -m pip freeze > requirements.txt` in your Conda "
                "environment, then select that file with --requirements and "
                "--include-deps. Conda-only packages must be provided by the base "
                "image; host Conda environments are never copied."
            )
        if not selected.resolve().is_relative_to(ctx.resolve()):
            raise ValueError("Dependency file must stay inside the build context.")
        if not getattr(self, "_conda_warning_emitted", False):
            logger.warning(
                "Conda inputs ignored (%s); installing only %s from %s. "
                "Conda-only packages are not staged.",
                self.config.redact(", ".join(path.as_posix() for path in signals)),
                "locked dependencies" if selected_lock else "pip requirements",
                self.config.redact(relative.as_posix()),
            )
            self._conda_warning_emitted = True
        return True

    def _grouped_dependency_source(self):
        source_kind, source_value = self._dependency_source()
        if source_kind != "lock" or source_value.name != "uv.lock":
            raise ValueError(
                "Grouped dependency layers currently require uv.lock; requirements, "
                "other lock formats, and virtualenv grouping are not supported."
            )
        return source_kind, source_value

    def _create_deps_layer(
        self,
        layers_dir: Path,
        *,
        base_config: Optional[Dict] = None,
        os_name: Optional[str] = None,
        arch: Optional[str] = None,
        variant: Optional[str] = None,
    ) -> Optional[OCILayer]:
        """Create a target-compatible dependency layer from a lock, requirements, or venv."""
        ctx=Path(self.config.context_dir)
        self._check_conda_dependencies()
        source_kind, source_value = self._dependency_source()
        if source_kind == "venv":
            deps_paths = source_value
        elif source_kind == "requirements":
            requirements_relative = source_value
            if requirements_relative.is_absolute() or ".." in requirements_relative.parts:
                raise ValueError("Requirements path must be relative and stay inside the build context.")
            if not (ctx / requirements_relative).is_file():
                raise FileNotFoundError(
                    f"Requirements file not found: {requirements_relative.as_posix()}"
                )
        if os_name is None or arch is None:
            os_name, arch = parse_platform(self.config.platform)
        if variant is None:
            variant = parse_platform_variant(self._requested_platform())
        validate_target_platform(os_name, arch, variant)
        if base_config and (
            base_config.get("os") != os_name
            or base_config.get("architecture") != arch
            or (variant is not None and base_config.get("variant") not in (None, variant))
        ):
            raise ValueError(
                "Pulled base image platform does not match the requested "
                f"target {os_name}/{arch}" + (f"/{variant}" if variant else "") + "."
            )
        base_image_name = getattr(self, "_base_image_name", self.config.base_image)
        # Local annotations/tags and any digest reference are identity claims, not
        # a resolved registry tag, so they cannot establish the Python ABI or libc.
        # Later platforms reuse the first pinned base snapshot; they inherit tag
        # provenance only when that snapshot was resolved from the initial tag pull.
        base_image_tag_verified = self._has_verified_base_image_tag()
        version = target_python_version(
            base_image_name, base_config,
            base_image_tag_verified=base_image_tag_verified,
        )
        target_wheel_platform(
            os_name, arch, base_image_name, base_config,
            base_image_tag_verified=base_image_tag_verified,
            variant=variant,
        )
        layers_dir=Path(layers_dir)
        cache_key = None
        verification_roots = None
        if self.cache:
            try:
                cache_key, verification_roots = self._dependency_cache_key(
                    source_kind, source_value, base_config, os_name, arch, version,
                    base_image_name, base_image_tag_verified, variant=variant,
                )
            except DependencyCacheMiss as miss:
                self._print(f"Dependency layer cache miss: {miss}.")
            else:
                cached_layer = self._cached_dependency_layer(layers_dir, cache_key)
                if cached_layer:
                    if source_kind == "lock":
                        selection = self._lock_selection(
                            base_config, os_name, arch, variant=variant,
                            base_image_tag_verified=base_image_tag_verified,
                        )
                        _locked_verification_roots(
                            selection,
                            ctx,
                            target_environment(
                                os_name, arch, version, target_python_full_version(base_config),
                                variant=variant,
                            ),
                        )
                    elif source_kind == "requirements":
                        self._requirement_files = [
                            ctx / path for path in cache_key.source_files
                        ]
                    return cached_layer
                self._print("Dependency layer cache miss: no verified entry for these inputs.")
        with tempfile.TemporaryDirectory(prefix="pycontainer-deps-") as stage:
            stage_root = Path(stage)
            site_packages = stage_root / "site-packages"
            if source_kind == "lock":
                selection = self._lock_selection(
                    base_config, os_name, arch, variant=variant,
                    base_image_tag_verified=base_image_tag_verified,
                )
                self._print(
                    f"Staging {len(selection.packages)} locked packages from "
                    f"{selection.path.as_posix()}..."
                )
                install_locked_requirements(
                    selection.requirement_sets(),
                    site_packages,
                    ctx,
                    os_name,
                    arch,
                    version,
                    base_image_name,
                    base_config,
                    variant=variant,
                    base_image_tag_verified=base_image_tag_verified,
                    environment_snapshot=cache_key.environment if cache_key else None,
                )
                verify_locked_installation(
                    selection,
                    ctx,
                    site_packages,
                    target_environment(
                        os_name, arch, version, target_python_full_version(base_config),
                        variant=variant,
                    ),
                    verification_roots=verification_roots,
                )
            elif source_kind == "requirements":
                snapshot_dir = (
                    tempfile.TemporaryDirectory(prefix="pycontainer-inputs-")
                    if cache_key else nullcontext(None)
                )
                with snapshot_dir as temporary:
                    requirement_path = ctx / requirements_relative
                    snapshot_options = {}
                    if cache_key:
                        snapshot = cache_key.requirements_snapshot
                        if snapshot is None:
                            raise RuntimeError("Dependency cache key has no requirements snapshot.")
                        snapshot_root = Path(temporary)
                        try:
                            materialize_requirements_snapshot(snapshot, snapshot_root)
                        except DependencyCacheMiss as miss:
                            self._print(f"Dependency layer cache miss: {miss}.")
                            cache_key = None
                        else:
                            requirement_path = snapshot_root / requirements_relative
                            snapshot_options = {
                                "requirements_context": snapshot_root,
                                "environment_snapshot": snapshot.ambient.environment,
                                "pip_configuration": {
                                    "configuration": snapshot.ambient.configuration,
                                    "ordered": snapshot.ambient.ordered,
                                },
                            }
                    try:
                        self._requirement_files = install_requirements(
                            requirement_path,
                            site_packages,
                            ctx,
                            os_name,
                            arch,
                            version,
                            base_image_name,
                            base_config,
                            variant=variant,
                            base_image_tag_verified=base_image_tag_verified,
                            **snapshot_options,
                        )
                    except PipConfigurationSnapshotError as miss:
                        if not cache_key:
                            raise
                        self._print(f"Dependency layer cache miss: {miss}.")
                        cache_key = None
                        self._requirement_files = install_requirements(
                            ctx / requirements_relative,
                            site_packages,
                            ctx,
                            os_name,
                            arch,
                            version,
                            base_image_name,
                            base_config,
                            variant=variant,
                            base_image_tag_verified=base_image_tag_verified,
                        )
                    if cache_key:
                        self._requirement_files = [ctx / path for path in cache_key.source_files]
            else:
                stage_virtualenv(
                    deps_paths,
                    site_packages,
                    version,
                    os_name,
                    arch,
                    base_config,
                    ctx,
                    ctx / DEFAULT_REQUIREMENTS_FILE,
                    variant=variant,
                )
                reject_credentialed_direct_urls(site_packages)

            if self.config.strip_type_stubs:
                removed = strip_type_stubs(site_packages)
                if removed:
                    print(f"Removed type stub distributions: {', '.join(removed)}")
            create_console_entrypoints(site_packages, stage_root / "bin")
            self._staged_packages = staged_packages(
                site_packages,
                strict=bool(self.config.generate_sbom),
            )
            files = sorted(
                (
                    path
                    for directory in (site_packages, stage_root / "bin")
                    if directory.is_dir()
                    for path in directory.rglob("*")
                    if path.is_file()
                ),
                key=lambda path: path.relative_to(stage_root).as_posix(),
            )
            self._dependency_site_packages = bool(files)
            if not files:
                return None

            self._print(f"Creating dependency layer ({len(files)} staged files)...")
            layer = self._write_dependency_layer(layers_dir, stage_root, files)
            if cache_key:
                self._store_dependency_layer(cache_key, layer.digest, Path(layer.tar_path), site_packages)
            return layer

    def _write_dependency_layer(self, layers_dir, stage_root, files):
        tmp=_create_temporary_file(layers_dir,"deps-layer")
        try:
            with tarfile.open(tmp, "w") as tar:
                for path in files:
                    relative = path.relative_to(stage_root)
                    arc = (
                        f"{self.config.workdir.rstrip('/')}/.python/"
                        f"{relative.as_posix()}"
                    )
                    if self.config.reproducible:
                        info = tar.gettarinfo(path, arcname=arc)
                        info.mtime = 0
                        info.uid = 0
                        info.gid = 0
                        info.uname = "root"
                        info.gname = "root"
                        info.mode = (
                            0o755
                            if relative.parts[0] == "bin"
                            or path.stat().st_mode & 0o111
                            else 0o644
                        )
                        with path.open("rb") as source:
                            tar.addfile(info, source)
                    else:
                        tar.add(path, arcname=arc)
            digest, size = _hash_file(tmp)
            final=layers_dir/digest.split(":",1)[1]
            _publish_content_addressed(tmp, final, digest, size)
            self._print(f"✓ Dependency layer created ({digest[:19]}...)")
            return OCILayer(
                "application/vnd.oci.image.layer.v1.tar", digest, size, str(final)
            )
        finally:
            tmp.unlink(missing_ok=True)

    def _create_grouped_deps_layers(self, layers_dir, *, base_config, os_name, arch, variant=None):
        source_kind, source_value = self._grouped_dependency_source()
        validate_target_platform(os_name, arch, variant)
        if base_config and (
            base_config.get("os") != os_name
            or base_config.get("architecture") != arch
            or (variant is not None and base_config.get("variant") not in (None, variant))
        ):
            raise ValueError(
                "Pulled base image platform does not match the requested "
                f"target {os_name}/{arch}" + (f"/{variant}" if variant else "") + "."
            )
        ctx = Path(self.config.context_dir)
        for _, relative in select_context_files(
            ctx, self.context_include_paths, self.context_excludes,
        ):
            if relative.parts and relative.parts[0].casefold() == ".python":
                raise ValueError(
                    "Grouped dependencies reserve the .python directory; "
                    "application files cannot overwrite it."
                )
        base_image_name = getattr(self, "_base_image_name", self.config.base_image)
        base_image_tag_verified = self._has_verified_base_image_tag()
        version = target_python_version(
            base_image_name, base_config,
            base_image_tag_verified=base_image_tag_verified,
        )
        target_wheel_platform(
            os_name, arch, base_image_name, base_config,
            base_image_tag_verified=base_image_tag_verified,
            variant=variant,
        )
        selection = self._lock_selection(
            base_config, os_name, arch, variant=variant,
            base_image_tag_verified=base_image_tag_verified,
        )
        environment = target_environment(
            os_name, arch, version, target_python_full_version(base_config),
            variant=variant,
        )
        plan = plan_groups(
            selection, self.config.dependency_layer_strategy,
            self.config.stable_dependencies, self.config.frequent_dependencies,
            ctx, self.config.dependency_history_ref, self._requested_platform(),
            environment=environment, report=self._print,
        )
        assignments = dict(plan.assignments)
        cache_key = None
        verification_roots = None
        scoped_keys = {}
        if self.cache:
            try:
                cache_key, verification_roots = self._dependency_cache_key(
                    source_kind, source_value, base_config, os_name, arch, version,
                    base_image_name, base_image_tag_verified,
                    variant=variant, selection_override=selection,
                )
                for group in GROUPS:
                    packages = tuple(
                        package for package in selection.packages
                        if assignments[package.name] == group
                    )
                    if not packages:
                        continue
                    subset = LockSelection(
                        selection.kind, selection.path, packages, selection.verification_roots
                    )
                    scoped, _ = self._dependency_cache_key(
                        source_kind, source_value, base_config, os_name, arch, version,
                        base_image_name, base_image_tag_verified,
                        variant=variant, selection_override=subset,
                        environment_snapshot=cache_key.environment,
                    )
                    scoped_keys[group] = scoped.layer
            except DependencyCacheMiss as miss:
                cache_key = None
                verification_roots = None
                self._print(f"Dependency group cache miss: {miss}.")
            else:
                cached = self._cached_group_bundle(
                    layers_dir, cache_key, plan, selection, scoped_keys
                )
                if cached is not None:
                    _locked_verification_roots(selection, ctx, environment)
                    return cached
                self._print("Dependency group cache miss: no complete verified bundle for these inputs.")

        with tempfile.TemporaryDirectory(prefix="pycontainer-deps-") as stage:
            stage_root = Path(stage)
            site_packages = stage_root / "site-packages"
            self._print(f"Staging {len(selection.packages)} locked packages from {selection.path.as_posix()}...")
            install_locked_requirements(
                selection.requirement_sets(), site_packages, ctx,
                os_name, arch, version, base_image_name, base_config,
                variant=variant, base_image_tag_verified=base_image_tag_verified,
                environment_snapshot=cache_key.environment if cache_key else None,
            )
            verify_locked_installation(
                selection, ctx, site_packages, environment,
                verification_roots=verification_roots,
            )
            partition_selection = selection
            if self.config.strip_type_stubs:
                partition_staged_files(stage_root, selection, plan, {})
                removed = strip_type_stubs(site_packages)
                if removed:
                    self._print(f"Removed type stub distributions: {', '.join(removed)}")
                    partition_selection = LockSelection(
                        selection.kind, selection.path,
                        tuple(package for package in selection.packages if package.name not in removed),
                        selection.verification_roots,
                    )
            script_owners = {}
            create_console_entrypoints(site_packages, stage_root / "bin", owners=script_owners)
            self._staged_packages = staged_packages(site_packages, strict=True)
            groups = partition_staged_files(stage_root, partition_selection, plan, script_owners)
            self._dependency_site_packages = any(groups.values())
            layers, bundle_layers = [], []
            for group in GROUPS:
                files = groups[group]
                if not files:
                    continue
                key = None
                if cache_key:
                    files_key = self.cache._compute_files_digest(
                        [(path, path.relative_to(stage_root)) for path in files],
                        workdir=f"{self.config.workdir.rstrip('/')}/.python",
                        reproducible=True,
                    )
                    key = group_layer_key(scoped_keys[group], plan, group, files_key)
                    cached = self.cache.get_entry(key)
                else:
                    cached = None
                if cached:
                    try:
                        layer = self._publish_cached_layer(layers_dir, *cached, "deps-group-cache")
                    except (FileNotFoundError, LayerIntegrityError):
                        self._print(
                            f"Dependency group cache miss: {group} blob was evicted "
                            "or changed during reuse."
                        )
                        cached = None
                if cached:
                    self._print(f"✓ Dependency group cache hit ({group}, {layer.digest[:19]}...)")
                else:
                    self._print(f"Creating {group} dependency layer ({len(files)} staged files)...")
                    layer = self._write_dependency_layer(layers_dir, stage_root, files)
                    if key:
                        self.cache.store_entry(
                            key, layer.digest, Path(layer.tar_path), list(cache_key.source_files)
                        )
                layers.append(layer)
                if key:
                    bundle_layers.append(
                        {"group": group, "key": key, "digest": layer.digest, "files": files_key}
                    )
            if cache_key and layers:
                self.cache.store_bytes(
                    group_bundle_key(cache_key.layer, plan),
                    encode_group_bundle(cache_key.layer, plan, bundle_layers, self._staged_packages),
                    list(cache_key.source_files),
                )
            return layers

    def _cached_group_bundle(self, layers_dir, cache_key, plan, selection, scoped_keys):
        content = self.cache.get_bytes(group_bundle_key(cache_key.layer, plan), MAX_INVENTORY_BYTES)
        if content is None:
            return None
        decoded = decode_group_bundle(
            content, cache_key.layer, plan, selection,
            scoped_keys=scoped_keys, strip_type_stubs=self.config.strip_type_stubs,
        )
        if decoded is None:
            self._print("Dependency group cache miss: bundle metadata is invalid.")
            return None
        records, packages = decoded
        cached_layers = []
        for record in records:
            cached = self.cache.get_entry(record["key"])
            if not cached or cached[0] != record["digest"]:
                self._print(f"Dependency group cache miss: {record['group']} blob is missing or invalid.")
                return None
            cached_layers.append((record["group"], cached))
        try:
            layers = [
                self._publish_cached_layer(layers_dir, *cached, "deps-group-cache")
                for _, cached in cached_layers
            ]
        except (FileNotFoundError, LayerIntegrityError):
            self._print("Dependency group cache miss: a blob was evicted or changed during reuse.")
            return None
        self._staged_packages = packages
        self._dependency_site_packages = bool(layers)
        for (group, _), layer in zip(cached_layers, layers):
            self._print(f"✓ Dependency group cache hit ({group}, {layer.digest[:19]}...)")
        return layers

    def _dependency_cache_key(
        self, source_kind, source_value, base_config, os_name, arch, version,
        base_image_name, base_image_tag_verified, *, variant=None, selection_override=None,
        environment_snapshot=None,
    ):
        """Return the cache key and the manifest roots it must verify."""
        ctx = Path(self.config.context_dir)
        common = dict(
            source_kind=source_kind,
            context_dir=ctx,
            os_name=os_name,
            arch=arch,
            python_version=version,
            python_full_version=target_python_full_version(base_config),
            base_image=base_image_name,
            base_config=base_config,
            base_image_tag_verified=base_image_tag_verified,
            variant=variant,
            workdir=self.config.workdir,
            reproducible=self.config.reproducible,
            strip_type_stubs=self.config.strip_type_stubs,
            environment_snapshot=environment_snapshot,
        )
        if source_kind == "lock":
            selection = selection_override or self._lock_selection(
                base_config, os_name, arch, variant=variant,
                base_image_tag_verified=base_image_tag_verified,
            )
            verification_roots = _locked_verification_roots(
                selection,
                ctx,
                target_environment(
                    os_name, arch, version, target_python_full_version(base_config),
                    variant=variant,
                ),
            )
            return dependency_cache_key(
                requirement_sets=selection.requirement_sets(),
                lock_name=selection.path.as_posix(),
                lock_kind=selection.kind,
                lock_roots=tuple(str(root) for root in verification_roots[0]),
                lock_verifier=_locked_verification_backend(),
                **common,
            ), verification_roots
        if source_kind == "requirements":
            return dependency_cache_key(requirements=ctx / source_value, **common), None
        return dependency_cache_key(**common), None

    def _cached_dependency_layer(self, layers_dir, cache_key):
        cached = self.cache.get_entry(cache_key.layer)
        if not cached:
            return None
        content = self.cache.get_bytes(cache_key.inventory, MAX_INVENTORY_BYTES)
        packages = decode_inventory(content, cached[0]) if content is not None else None
        if packages is None:
            return None
        layer = self._publish_cached_layer(layers_dir, *cached, "deps-cache")
        self._staged_packages = packages
        self._dependency_site_packages = True
        self._print(f"✓ Dependency layer cache hit ({layer.digest[:19]}...)")
        return layer

    def _store_dependency_layer(self, cache_key, digest, layer_path, site_packages):
        try:
            packages = staged_packages(site_packages, strict=True)
        except (UnicodeDecodeError, ValueError):
            self._print("Dependency layer not cached: staged package metadata is not strictly readable.")
            return
        # The layer is indexed first; a hit requires the inventory, so interruption here is a miss.
        self.cache.store_entry(cache_key.layer, digest, layer_path, list(cache_key.source_files))
        self.cache.store_bytes(
            cache_key.inventory,
            encode_inventory(digest, packages),
            list(cache_key.source_files),
        )

    def _publish_cached_layer(self, layers_dir, digest, cache_path, label):
        final=layers_dir/digest.split(":",1)[1]
        tmp=_create_temporary_file(layers_dir,label)
        try:
            with cache_path.open("rb") as source, tmp.open("wb") as target:
                shutil.copyfileobj(source, target, _HASH_CHUNK_SIZE)
                target.flush()
                os.fsync(target.fileno())
            actual_digest, size = _hash_file(tmp)
            if actual_digest != digest:
                raise LayerIntegrityError(
                    f"Cached layer hashes to {actual_digest}, not {digest}."
                )
            _publish_content_addressed(tmp, final, digest, size)
            return OCILayer(
                "application/vnd.oci.image.layer.v1.tar",
                digest,
                size,
                str(final),
            )
        finally:
            tmp.unlink(missing_ok=True)

    def _create_app_layer(self, layers_dir, include_paths):
        ctx=Path(self.config.context_dir)
        files=select_context_files(ctx, include_paths, self.context_excludes)
        configured_requirements = ctx / (self.config.requirements_file or DEFAULT_REQUIREMENTS_FILE)
        reject_shipped_credentials(
            files,
            ctx,
            [
                *getattr(self, "_requirement_files", ()),
                configured_requirements,
            ],
        )

        if self.cache:
            cached=self.cache.get_layer(
                files,
                workdir=self.config.workdir,
                reproducible=self.config.reproducible,
                namespace=self.config.cache_identity(),
            )
            if cached:
                return self._publish_cached_layer(layers_dir, *cached, "app-cache")

        tmp=_create_temporary_file(layers_dir,"app-layer")
        files_sorted=ordered_app_files(files, self.config.reproducible)
        try:
            with tarfile.open(tmp,'w') as tar:
                for abs_path, rel in files_sorted:
                    arc=app_layer_arcname(self.config.workdir, rel)
                    tarinfo=tar.gettarinfo(abs_path, arcname=arc)
                    if tarinfo.issym():
                        tarinfo.linkname=tarinfo.linkname.replace(os.sep, "/")
                    if self.config.reproducible:
                        tarinfo.mtime=0
                        tarinfo.uid=0
                        tarinfo.gid=0
                        tarinfo.uname="root"
                        tarinfo.gname="root"
                    if tarinfo.isfile():
                        with open(abs_path, 'rb') as f:
                            tar.addfile(tarinfo, f)
                    else:
                        tar.addfile(tarinfo)
                hardlinks=emitted_hardlinks(files_sorted, tar.getmembers())
            digest, size = _hash_file(tmp)
            final=layers_dir/digest.split(":",1)[1]
            _publish_content_addressed(tmp, final, digest, size)

            if self.cache:
                self.cache.store_layer(
                    files,
                    digest,
                    final,
                    workdir=self.config.workdir,
                    reproducible=self.config.reproducible,
                    namespace=self.config.cache_identity(),
                    record_source_files=not bool(self.config.build_args),
                    hardlinks=hardlinks,
                )

            return OCILayer(
                "application/vnd.oci.image.layer.v1.tar", digest, size, str(final)
            )
        finally:
            tmp.unlink(missing_ok=True)
