import hashlib, json, os, shutil, tarfile, tempfile, logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, List, Dict, Tuple
from .config import BuildConfig
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
    iter_files,
    output_directory_lock,
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
from .project import detect_entrypoint, default_include_paths, find_dependencies
from .framework import apply_framework_defaults
from .registry_client import (
    RegistryClient,
    RegistryRequestError,
    parse_image_reference,
)
from .auth import get_registry_auth
from .local_image import (
    LocalImageError,
    check_local_image_bounds,
    open_local_image_source,
    parse_local_image_reference,
    verify_layer_diff_ids,
)
from .cache import LayerCache
from .sbom import generate_sbom
from .squash import SquashError, squash_layers
from .layer_analysis import analyze_platform, shared_blobs
from .dependencies import (
    create_console_entrypoints,
    install_requirements,
    staged_packages,
    stage_virtualenv,
    target_python_version,
    target_wheel_platform,
    validate_target_platform,
)

logger=logging.getLogger(__name__)

UNCOMPRESSED_LAYER_MEDIA_TYPE = "application/vnd.oci.image.layer.v1.tar"
VIRTUALENV_TARGET_FOLLOW_UP = "https://github.com/spboyer/pycontainer-build/issues/180"
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
        context_dir=Path(config.context_dir)
        default_cache_dir=Path.home()/".pycontainer"/"cache"
        cache_dir=(
            Path(config.cache_dir).expanduser()
            if config.cache_dir
            else default_cache_dir
        )
        self.context_excludes=context_exclude_paths(
            context_dir,
            output_dir=config.output_dir,
            cache_dir=cache_dir,
            exclude_paths=config.exclude_paths,
            exclude_defaults=config.exclude_defaults,
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
        self.config=apply_framework_defaults(
            config,
            context_dir,
            self.context_include_paths,
            self.context_excludes,
            exclude_defaults=False,
        )
        self.verbose=getattr(config, 'verbose', False)
        self.dry_run=getattr(config, 'dry_run', False)
        if config.use_cache and not self.dry_run:
            self.cache=LayerCache(cache_dir=cache_dir, max_size_mb=config.max_cache_size_mb)
        else:
            self.cache=None

    def build(self):
        if self.dry_run:
            logger.info("DRY RUN: Would build image %s", self.config.tag)
            self._show_build_plan()
            return self.config.tag

        with output_directory_lock(self.config.output_dir):
            return self._build_image()

    def _build_image(self):
        output=ensure_dir(self.config.output_dir)
        blobs=ensure_dir(output/'blobs')
        layers_dir=ensure_dir(blobs/'sha256')
        refs_dir=ensure_dir(output/'refs'/'tags')

        platforms=self.config.target_platforms()
        self._resolved_base_reference=None
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

        index=build_index_json(
            image.manifest_digest,
            image.manifest_size,
            self.config.tag,
            image.platform["architecture"],
            image.platform["os"],
            variant=image.platform.get("variant"),
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
            logger.info(f"✓ SBOM saved to {sbom_path}")
        
        return self.config.tag

    def _build_multi_platform(self, output: Path, layers_dir: Path, refs_dir: Path, platforms: List[str]):
        """Build every platform in isolation, then publish one index only if all succeed."""
        images=[]
        for platform in platforms:
            print(f"Building platform {platform}...")
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
        layout_index=build_layout_index_json(index_digest, index_size, self.config.all_tags())
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
        print(f"✓ Image index created for {', '.join(platforms)} ({index_digest[:19]}...)")
        return self.config.tag

    def _write_image_index(self, layers_dir: Path, images: List["PlatformImage"]) -> Tuple[str, int]:
        descriptors=[
            OCIManifestDescriptor(OCI_MANIFEST_MEDIA_TYPE, image.manifest_digest, image.manifest_size, dict(image.platform))
            for image in images
        ]
        index_bytes=json.dumps(build_image_index_json(descriptors),separators=(',',':')).encode()
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
        base_layers, base_config = self._pull_base_image(layers_dir, os_name, arch)
        report_system_packages(self.config, base_layers)
        variant = (
            base_config.get("variant") if base_config else None
        ) or parse_platform_variant(platform)
        
        entry = self.config.entrypoint or detect_entrypoint(self.config.context_dir)
        include = self.context_include_paths

        app_layers=[]
        if self.config.include_deps:
            deps_layer=self._create_deps_layer(
                layers_dir, base_config=base_config, os_name=os_name, arch=arch
            )
            if deps_layer: app_layers.append(deps_layer)
        
        # The application tar is byte-identical for every target, so build it once.
        if self._app_layer is None:
            self._app_layer=self._create_app_layer(layers_dir, include)
        app_layers.append(self._app_layer)

        source_layers=list(app_layers)
        squash_stats=None
        if self.config.squash and len(app_layers)>1:
            squashed, squash_stats=self._squash_builder_layers(layers_dir, app_layers)
            app_layers=[squashed]
            print(
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
                                layer_diff_ids=[layer.digest for layer in app_layers])
        if squash_stats is not None:
            _append_squash_history(cfg, len(base_layers), len(source_layers))
        cfg_bytes=json.dumps(cfg,separators=(',',':')).encode()
        cfg_digest="sha256:"+hashlib.sha256(cfg_bytes).hexdigest()
        cfg_path=layers_dir/cfg_digest.split(":",1)[1]
        _write_content_addressed_bytes(cfg_path, cfg_bytes, cfg_digest)

        manifest=build_manifest_json(cfg_digest,len(cfg_bytes),all_layers)
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
            print(line)
            for warning in platform["warnings"]:
                print(f"  warning: {warning}")
        print(f"✓ Layer analysis saved to {output/'layer-analysis.json'}")
    
    def _requested_platform(self) -> str:
        return getattr(self, "_target_platform", None) or self.config.platform

    def _pull_base_image(self, layers_dir: Path, os_name: str, arch: str) -> Tuple[List[OCILayer], Optional[Dict]]:
        """Pull or load the base image, return (base_layers, base_config)."""
        self._base_image_name=self.config.base_image
        requested_variant = parse_platform_variant(self._requested_platform())
        local=parse_local_image_reference(self.config.base_image)
        if local is None:
            registry, repo, reference=parse_image_reference(self.config.base_image)
            auth=get_registry_auth(registry)
            client=RegistryClient(
                registry,
                repo,
                auth_token=auth.bearer_token,
                username=auth.username,
                password=auth.password,
            )
            return self._read_base_image(client, reference, layers_dir, os_name, arch)

        target = f"{os_name}/{arch}" + (
            f"/{requested_variant}" if requested_variant else ""
        )
        print(f"Loading local base image {self.config.base_image} for {target}...")
        with open_local_image_source(
            local, layers_dir, Path(self.config.output_dir), os_name, arch, requested_variant
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
        # Later registry platforms re-select from the same verified base index as the first.
        pinned = None if local else getattr(self, "_resolved_base_reference", None)
        pull_reference = pinned or reference
        if not local:
            print(f"Pulling base image {self.config.base_image} for {target}...")
        manifest, resolved_digest = client.pull_manifest(
            pull_reference,
            expected_digest=(
                pull_reference
                if pull_reference and pull_reference.startswith("sha256:")
                else None
            ),
        )
        if (
            not local
            and pinned is None
            and isinstance(resolved_digest, str)
            and resolved_digest.startswith("sha256:")
        ):
            self._resolved_base_reference = resolved_digest
            # Only a registry pull by tag can vouch for the tag; a user-supplied
            # tag@digest or digest is pulled by digest and never proves its tag.
            self._resolved_base_from_tag = not reference.startswith("sha256:")
        if local:
            self._base_image_tag_verified = False
        elif pinned is None:
            self._base_image_tag_verified = (
                not reference.startswith("sha256:")
                and isinstance(resolved_digest, str)
                and resolved_digest.startswith("sha256:")
            )
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
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise RegistryRequestError("Base image config is not valid JSON.") from error
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
                print(f"  {'Copying' if local else 'Pulling'} layer {i}/{len(layer_descriptors)} ({layer_digest[:19]}...)")
            client.pull_blob(layer_digest, layer_path, layer_size)
            base_layers.append(
                OCILayer(media_type, layer_digest, layer_size, str(layer_path))
            )
        
        print(f"✓ Base image {'loaded' if local else 'pulled'} ({len(base_layers)} layers)")
        return base_layers, base_config
    
    def push(self, registry_url: Optional[str]=None, auth_token: Optional[str]=None, username: Optional[str]=None, password: Optional[str]=None, show_progress: bool=True):
        """Push built image to registry."""
        if not hasattr(self, 'manifest_digest'):
            raise RuntimeError("Must call build() before push()")
        
        target=registry_url or self.config.tag
        registry, repo, tag=parse_image_reference(target)
        push_tags=[tag]
        for tag_ref in self.config.all_tags()[1:]:
            extra_tag=parse_image_reference(tag_ref)[2]
            if extra_tag not in push_tags: push_tags.append(extra_tag)

        if getattr(self, "index_digest", None):
            self.pushed_refs=[]
        
        auth=get_registry_auth(
            registry,
            bearer_token=auth_token,
            username=username,
            password=password,
        )
        client=RegistryClient(
            registry,
            repo,
            auth_token=auth.bearer_token,
            username=auth.username,
            password=auth.password,
        )
        output=Path(self.config.output_dir)
        layers_dir=output/'blobs'/'sha256'
        
        if show_progress: print(f"Pushing to {registry}/{repo}:{tag}")

        if getattr(self, "index_digest", None):
            self._push_image_index(client, layers_dir, push_tags, f"{registry}/{repo}", show_progress)
            return f"{registry}/{repo}:{tag}"
        
        for i, layer in enumerate(self.layers, 1):
            if show_progress: print(f"  Pushing layer {i}/{len(self.layers)} ({layer.digest[:19]}...)")
            blob_path=layers_dir/layer.digest.split(":",1)[1]
            skipped=not client.push_blob(layer.digest, blob_path, check_exists=True)
            if show_progress and skipped: print(f"    Layer exists, skipped")
        
        if show_progress: print(f"  Pushing config ({self.config_digest[:19]}...)")
        cfg_path=layers_dir/self.config_digest.split(":",1)[1]
        client.push_blob(self.config_digest, cfg_path, check_exists=True)
        
        if show_progress: print(f"  Pushing manifest ({self.manifest_digest[:19]}...)")
        manifest_path=layers_dir/self.manifest_digest.split(":",1)[1]
        manifest_data=manifest_path.read_bytes()
        for push_tag in push_tags:
            client.push_manifest(push_tag, manifest_data)
            if show_progress: print(f"✓ Pushed {registry}/{repo}:{push_tag}")
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
            if show_progress: print(f"  Pushing blob {i}/{len(blobs)} ({digest[:19]}...)")
            skipped=not client.push_blob(digest, layers_dir/_digest_filename(digest), check_exists=True)
            if show_progress and skipped: print(f"    Blob exists, skipped")
        for image in self.platform_images:
            target="/".join(image.platform[k] for k in ("os","architecture","variant") if k in image.platform)
            if show_progress: print(f"  Pushing {target} manifest ({image.manifest_digest[:19]}...)")
            manifest_data=(layers_dir/_digest_filename(image.manifest_digest)).read_bytes()
            client.push_manifest(image.manifest_digest, manifest_data)
        index_data=(layers_dir/_digest_filename(self.index_digest)).read_bytes()
        if show_progress: print(f"  Pushing image index ({self.index_digest[:19]}...)")
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
            if show_progress: print(f"✓ Pushed index tag {push_tag}")
    
    def _show_build_plan(self):
        """Display build plan for dry-run mode."""
        print(f"Build Plan for {self.config.tag}:")
        if len(self.config.all_tags())>1:
            print(f"  Tags: {', '.join(self.config.all_tags())}")
        print(f"  Base Image: {self.config.base_image}")
        platforms=self.config.target_platforms()
        if len(platforms)>1:
            print(f"  Platforms (image index): {', '.join(platforms)}")
        else:
            print(f"  Platform: {platforms[0]}")
        print(f"  Context: {self.config.context_dir}")
        print(f"  Working Dir: {self.config.workdir}")
        print(f"  Entrypoint: {' '.join(self.config.entrypoint or ['<auto-detect>'])}")
        if self.config.exposed_ports:
            print(f"  Exposed Ports: {', '.join(map(str, self.config.exposed_ports))}")
        if self.config.env:
            print(f"  Environment: {', '.join(f'{k}={v}' for k,v in self.config.env.items())}")
        if self.config.labels:
            print(f"  Labels: {', '.join(f'{k}={v}' for k,v in self.config.labels.items())}")
        print(f"  Include Dependencies: {self.config.include_deps}")
        if self.config.squash:
            print("  Squash: builder-created layers flattened into one (base layers unchanged)")
        if self.config.analyze_layers:
            print("  Layer Analysis: layer-analysis.json in the output directory")
        if self.config.system_packages:
            print(f"  System Packages (verified, not installed): {', '.join(self.config.system_packages)}")
        print(f"  Use Cache: {self.config.use_cache}")

    def _create_deps_layer(
        self,
        layers_dir: Path,
        *,
        base_config: Optional[Dict] = None,
        os_name: Optional[str] = None,
        arch: Optional[str] = None,
    ) -> Optional[OCILayer]:
        """Create a target-compatible dependency layer from a venv or requirements."""
        ctx=Path(self.config.context_dir)
        deps_paths=find_dependencies(
            ctx,
            self.config.requirements_file,
            exclude_paths=self.dependency_excludes,
            exclude_defaults=False,
        )
        requirements_relative = Path(self.config.requirements_file)
        if requirements_relative.is_absolute() or ".." in requirements_relative.parts:
            raise ValueError("Requirements path must be relative and stay inside the build context.")
        is_requirements = any(
            relative == requirements_relative for _, relative in deps_paths
        )
        if not deps_paths:
            raise FileNotFoundError(
                "Dependency staging was requested, but no supported virtualenv "
                f"site-packages or {self.config.requirements_file!r} was found."
            )
        if not is_requirements and len(self.config.target_platforms()) > 1:
            raise ValueError(
                "Multi-platform builds cannot stage dependencies from a copied "
                "virtualenv: its packages were resolved for the host, so "
                "target-specific dependencies may be missing. Exclude any "
                "virtualenvs (for example, --exclude .venv) and provide "
                f"{self.config.requirements_file!r} for wheel resolution. "
                f"See {VIRTUALENV_TARGET_FOLLOW_UP}."
            )
        if os_name is None or arch is None:
            os_name, arch = parse_platform(self.config.platform)
        validate_target_platform(os_name, arch)
        if base_config and (
            base_config.get("os") != os_name
            or base_config.get("architecture") != arch
        ):
            raise ValueError(
                "Pulled base image platform does not match the requested "
                f"target {os_name}/{arch}."
            )
        base_image_name = getattr(self, "_base_image_name", self.config.base_image)
        # Local annotations/tags and any digest reference are identity claims, not
        # a resolved registry tag, so they cannot establish the Python ABI or libc.
        # Later platforms are pulled by the pinned index digest; they inherit tag
        # provenance only when that digest was resolved from the initial tag pull.
        base_image_tag_verified = (
            parse_local_image_reference(self.config.base_image) is None
            and "@" not in self.config.base_image
            and getattr(self, "_base_image_tag_verified", False)
        )
        version = target_python_version(
            base_image_name, base_config,
            base_image_tag_verified=base_image_tag_verified,
        )
        target_wheel_platform(
            os_name, arch, base_image_name, base_config,
            base_image_tag_verified=base_image_tag_verified,
        )
        layers_dir=Path(layers_dir)
        with tempfile.TemporaryDirectory(prefix="pycontainer-deps-") as stage:
            stage_root = Path(stage)
            site_packages = stage_root / "site-packages"
            if is_requirements:
                install_requirements(
                    ctx / requirements_relative,
                    site_packages,
                    ctx,
                    os_name,
                    arch,
                    version,
                    base_image_name,
                    base_config,
                    base_image_tag_verified=base_image_tag_verified,
                )
            else:
                stage_virtualenv(
                    deps_paths, site_packages, version, os_name, arch
                )

            create_console_entrypoints(site_packages, stage_root / "bin")
            self._staged_packages = staged_packages(
                site_packages,
                strict=bool(self.config.generate_sbom),
            )
            files = sorted(
                (path for path in stage_root.rglob("*") if path.is_file()),
                key=lambda path: path.relative_to(stage_root).as_posix(),
            )
            self._dependency_site_packages = bool(files)
            if not files:
                return None

            print(f"Creating dependency layer ({len(files)} staged files)...")
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
                print(f"✓ Dependency layer created ({digest[:19]}...)")
                return OCILayer(
                    "application/vnd.oci.image.layer.v1.tar", digest, size, str(final)
                )
            finally:
                tmp.unlink(missing_ok=True)

    def _create_app_layer(self, layers_dir, include_paths):
        ctx=Path(self.config.context_dir)
        files=list(
            iter_files(
                ctx,
                include_paths,
                exclude_paths=self.context_excludes,
                exclude_defaults=self.config.exclude_defaults,
            )
        )

        if self.cache:
            cached=self.cache.get_layer(
                files,
                workdir=self.config.workdir,
                reproducible=self.config.reproducible,
            )
            if cached:
                digest, cache_path=cached
                final=layers_dir/digest.split(":",1)[1]
                tmp=_create_temporary_file(layers_dir,"app-cache")
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

        tmp=_create_temporary_file(layers_dir,"app-layer")
        files_sorted=sorted(files, key=lambda x: x[1].as_posix()) if self.config.reproducible else files
        try:
            with tarfile.open(tmp,'w') as tar:
                for abs_path, rel in files_sorted:
                    arc=f"{self.config.workdir.lstrip('/')}/{rel.as_posix()}"
                    if self.config.reproducible:
                        tarinfo=tar.gettarinfo(abs_path, arcname=arc)
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
                    else:
                        tar.add(abs_path,arcname=arc)
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
                )

            return OCILayer(
                "application/vnd.oci.image.layer.v1.tar", digest, size, str(final)
            )
        finally:
            tmp.unlink(missing_ok=True)
