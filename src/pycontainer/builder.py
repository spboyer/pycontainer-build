import hashlib, json, os, shutil, tarfile, tempfile, logging
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
    relative_context_path,
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
from .oci import OCILayer, build_config_json, build_manifest_json, build_oci_layout, build_index_json
from .project import detect_entrypoint, default_include_paths, find_dependencies
from .framework import apply_framework_defaults
from .registry_client import (
    RegistryClient,
    RegistryRequestError,
    parse_image_reference,
)
from .auth import get_registry_auth
from .cache import LayerCache
from .sbom import generate_sbom
from .dependencies import (
    create_console_entrypoints,
    install_requirements,
    staged_packages,
    stage_virtualenv,
    target_python_full_version,
    target_python_version,
    validate_target_platform,
)
from .lockfiles import detect_lockfile, load_lockfile, preflight_lockfile, target_environment

DEFAULT_REQUIREMENTS_FILE = "requirements.txt"

logger=logging.getLogger(__name__)

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

        os_name, arch = parse_platform(self.config.platform)
        base_layers, base_config = self._pull_base_image(layers_dir, os_name, arch)
        locked_names = ()
        if self.config.include_deps and self._uses_lockfile():
            locked_names = tuple(
                package.name
                for package in self._lock_selection(base_config, os_name, arch).packages
            )
        report_system_packages(self.config, base_layers, locked_names)
        variant = (
            base_config.get("variant") if base_config else None
        ) or parse_platform_variant(self.config.platform)
        
        entry = self.config.entrypoint or detect_entrypoint(self.config.context_dir)
        include = self.context_include_paths

        app_layers=[]
        if self.config.include_deps:
            deps_layer=self._create_deps_layer(
                layers_dir, base_config=base_config, os_name=os_name, arch=arch
            )
            if deps_layer: app_layers.append(deps_layer)
        
        app_layer=self._create_app_layer(layers_dir, include)
        app_layers.append(app_layer)
        
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
        cfg_bytes=json.dumps(cfg,separators=(',',':')).encode()
        cfg_digest="sha256:"+hashlib.sha256(cfg_bytes).hexdigest()
        cfg_path=layers_dir/cfg_digest.split(":",1)[1]
        _write_content_addressed_bytes(cfg_path, cfg_bytes, cfg_digest)

        manifest=build_manifest_json(cfg_digest,len(cfg_bytes),all_layers)
        manifest_bytes=json.dumps(manifest,separators=(',',':')).encode()
        manifest_digest="sha256:"+hashlib.sha256(manifest_bytes).hexdigest()
        manifest_path=layers_dir/manifest_digest.split(":",1)[1]
        _write_content_addressed_bytes(manifest_path, manifest_bytes, manifest_digest)

        oci_layout=build_oci_layout()
        _atomic_write_bytes(
            output/'oci-layout',
            json.dumps(oci_layout,separators=(',',':')).encode(),
        )

        index=build_index_json(
            manifest_digest,
            len(manifest_bytes),
            self.config.tag,
            arch,
            os_name,
            variant=variant,
        )
        _atomic_write_bytes(
            output/'index.json',
            json.dumps(index,separators=(',',':')).encode(),
        )

        for tag_ref in self.config.all_tags():
            _, _, tag_name=parse_image_reference(tag_ref)
            _atomic_write_bytes(refs_dir/tag_name, manifest_digest.encode())

        self.manifest_digest=manifest_digest
        self.config_digest=cfg_digest
        self.layers=all_layers
        
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
    
    def _pull_base_image(self, layers_dir: Path, os_name: str, arch: str) -> Tuple[List[OCILayer], Optional[Dict]]:
        """Pull base image from registry, return (base_layers, base_config)."""
        registry, repo, reference=parse_image_reference(self.config.base_image)
        auth=get_registry_auth(registry)
        client=RegistryClient(
            registry,
            repo,
            auth_token=auth.bearer_token,
            username=auth.username,
            password=auth.password,
        )
        
        platform = self.config.platform
        requested_variant = parse_platform_variant(platform)
        target = f"{os_name}/{arch}" + (
            f"/{requested_variant}" if requested_variant else ""
        )
        print(f"Pulling base image {self.config.base_image} for {target}...")
        manifest, _ = client.pull_manifest(
            reference,
            expected_digest=reference if reference.startswith("sha256:") else None,
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
                print(f"  Pulling layer {i}/{len(layer_descriptors)} ({layer_digest[:19]}...)")
            client.pull_blob(layer_digest, layer_path, layer_size)
            base_layers.append(
                OCILayer(media_type, layer_digest, layer_size, str(layer_path))
            )
        
        print(f"✓ Base image pulled ({len(base_layers)} layers)")
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
    
    def _show_build_plan(self):
        """Display build plan for dry-run mode."""
        print(f"Build Plan for {self.config.tag}:")
        if len(self.config.all_tags())>1:
            print(f"  Tags: {', '.join(self.config.all_tags())}")
        print(f"  Base Image: {self.config.base_image}")
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
        if self.config.include_deps:
            try:
                kind, value = self._dependency_source()
            except FileNotFoundError as error:
                print(f"  Dependency Source: unresolved ({error})")
            else:
                if kind == "lock":
                    preflight_lockfile(Path(self.config.context_dir), value)
                described = "virtualenv site-packages" if kind == "venv" else value.as_posix()
                print(f"  Dependency Source: {kind} ({described})")
                if kind == "lock":
                    print("  Lockfile target markers (including uv marker freshness) and wheels: checked during build.")
        if self.config.system_packages:
            print(f"  System Packages (verified, not installed): {', '.join(self.config.system_packages)}")
        print(f"  Use Cache: {self.config.use_cache}")

    def _uses_lockfile(self) -> bool:
        if self.config.lockfile:
            return True
        if self.config.requirements_file or not self.config.use_lockfile:
            return False
        return detect_lockfile(Path(self.config.context_dir)) is not None

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
                print(f"Using lockfile {detected} for dependencies (disable with --no-lockfile).")
                source = ("lock", Path(detected))
            else:
                deps_paths = find_dependencies(
                    ctx,
                    DEFAULT_REQUIREMENTS_FILE,
                    exclude_paths=self.dependency_excludes,
                    exclude_defaults=False,
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
        self._dependency_source_cache = source
        return source

    def _lock_selection(self, base_config, os_name, arch):
        cached = getattr(self, "_lock_selection_cache", None)
        if cached is not None:
            return cached
        _, relative = self._dependency_source()
        environment = target_environment(
            os_name,
            arch,
            target_python_version(self.config.base_image, base_config),
            target_python_full_version(base_config),
        )
        selection = load_lockfile(Path(self.config.context_dir), relative, environment)
        self._lock_selection_cache = selection
        return selection

    def _create_deps_layer(
        self,
        layers_dir: Path,
        *,
        base_config: Optional[Dict] = None,
        os_name: Optional[str] = None,
        arch: Optional[str] = None,
    ) -> Optional[OCILayer]:
        """Create a target-compatible dependency layer from a lock, requirements, or venv."""
        ctx=Path(self.config.context_dir)
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
        validate_target_platform(os_name, arch)
        if base_config and (
            base_config.get("os") != os_name
            or base_config.get("architecture") != arch
        ):
            raise ValueError(
                "Pulled base image platform does not match the requested "
                f"target {os_name}/{arch}."
            )
        version = target_python_version(self.config.base_image, base_config)
        layers_dir=Path(layers_dir)
        with tempfile.TemporaryDirectory(prefix="pycontainer-deps-") as stage:
            stage_root = Path(stage)
            site_packages = stage_root / "site-packages"
            if source_kind == "lock":
                selection = self._lock_selection(base_config, os_name, arch)
                print(
                    f"Staging {len(selection.packages)} locked packages from "
                    f"{selection.path.as_posix()}..."
                )
                locked_requirements = stage_root / "locked-requirements.txt"
                locked_requirements.write_text(selection.render_requirements(), encoding="utf-8")
                try:
                    install_requirements(
                        locked_requirements,
                        site_packages,
                        ctx,
                        os_name,
                        arch,
                        version,
                        self.config.base_image,
                        base_config,
                        locked=True,
                    )
                finally:
                    locked_requirements.unlink(missing_ok=True)
            elif source_kind == "requirements":
                install_requirements(
                    ctx / requirements_relative,
                    site_packages,
                    ctx,
                    os_name,
                    arch,
                    version,
                    self.config.base_image,
                    base_config,
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
