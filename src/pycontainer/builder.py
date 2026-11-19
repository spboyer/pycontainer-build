import hashlib, json, os, shutil, tarfile, tempfile, logging
from pathlib import Path
from typing import Optional, List, Dict, Tuple
from .config import BuildConfig
from .fs_utils import (
    VIRTUALENV_EXCLUDES,
    context_exclude_paths,
    ensure_dir,
    iter_files,
    output_directory_lock,
)


_HASH_CHUNK_SIZE = 1024 * 1024


class LayerIntegrityError(RuntimeError):
    """Raised when a staged layer does not match its expected content."""


def _create_temporary_file(directory: Path, prefix: str) -> Path:
    descriptor, name = tempfile.mkstemp(
        prefix=f".{prefix}-", suffix=".tmp", dir=directory
    )
    os.close(descriptor)
    return Path(name)


def _hash_file(path: Path) -> Tuple[str, int]:
    hasher = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while True:
            chunk = source.read(_HASH_CHUNK_SIZE)
            if not chunk:
                break
            hasher.update(chunk)
            size += len(chunk)
    return f"sha256:{hasher.hexdigest()}", size


def _blob_matches(path: Path, digest: str, size: int) -> bool:
    if path.is_symlink() or not path.is_file() or path.stat().st_size != size:
        return False
    actual_digest, actual_size = _hash_file(path)
    return actual_digest == digest and actual_size == size


def _publish_content_addressed(
    staged: Path, destination: Path, digest: str, size: int
) -> Tuple[str, int]:
    try:
        with staged.open("rb+") as source:
            source.flush()
            os.fsync(source.fileno())
        if _blob_matches(destination, digest, size):
            return digest, size
        if destination.exists() or destination.is_symlink():
            raise FileExistsError(
                f"Existing content-addressed blob {destination} does not match "
                f"{digest}; refusing to overwrite it."
            )

        try:
            os.link(staged, destination)
        except FileExistsError:
            if not _blob_matches(destination, digest, size):
                raise FileExistsError(
                    f"Existing content-addressed blob {destination} does not match "
                    f"{digest}; refusing to overwrite it."
                )
        return digest, size
    finally:
        staged.unlink(missing_ok=True)


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
    """Parse platform string (e.g., 'linux/amd64') into (os, arch)."""
    parts=platform.split('/')
    if len(parts)!=2:
        raise ValueError(f"Invalid platform format: {platform}. Expected 'os/arch' (e.g., 'linux/amd64')")
    return parts[0], parts[1]
from .oci import OCILayer, build_config_json, build_manifest_json, build_oci_layout, build_index_json
from .project import detect_entrypoint, default_include_paths, find_dependencies
from .framework import apply_framework_defaults
from .registry_client import RegistryClient, parse_image_reference
from .auth import get_registry_auth
from .cache import LayerCache
from .sbom import generate_sbom
from .dependencies import (
    create_console_entrypoints,
    install_requirements,
    staged_packages,
    stage_virtualenv,
    target_python_version,
    validate_target_platform,
)

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

        index=build_index_json(manifest_digest,len(manifest_bytes),self.config.tag,arch,os_name)
        _atomic_write_bytes(
            output/'index.json',
            json.dumps(index,separators=(',',':')).encode(),
        )

        _, _, tag_name=parse_image_reference(self.config.tag)
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
                package_inventory=getattr(self, "_staged_packages", []),
            )
            logger.info(f"✓ SBOM saved to {sbom_path}")
        
        return self.config.tag
    
    def _pull_base_image(self, layers_dir: Path, os_name: str, arch: str) -> Tuple[List[OCILayer], Optional[Dict]]:
        """Pull base image from registry, return (base_layers, base_config)."""
        registry, repo, tag=parse_image_reference(self.config.base_image)
        auth=get_registry_auth(registry)
        client=RegistryClient(
            registry,
            repo,
            auth_token=auth.bearer_token,
            username=auth.username,
            password=auth.password,
        )
        
        print(f"Pulling base image {self.config.base_image} for {os_name}/{arch}...")
        manifest, _=client.pull_manifest(tag)
        
        if manifest.get('mediaType')=='application/vnd.oci.image.index.v1+json':
            for m in manifest.get('manifests',[]):
                plat=m.get('platform',{})
                if plat.get('architecture')==arch and plat.get('os')==os_name:
                    manifest, _=client.pull_manifest(m['digest'])
                    break
        
        config_desc=manifest.get('config',{})
        config_digest=config_desc.get('digest')
        config_path=layers_dir/config_digest.split(':',1)[1]
        if not config_path.exists():
            client.pull_blob(config_digest, config_path)
        base_config=json.loads(config_path.read_bytes())
        
        base_layers=[]
        for i, layer_desc in enumerate(manifest.get('layers',[]), 1):
            layer_digest=layer_desc['digest']
            layer_size=layer_desc['size']
            layer_path=layers_dir/layer_digest.split(':',1)[1]
            if not layer_path.exists():
                print(f"  Pulling layer {i}/{len(manifest['layers'])} ({layer_digest[:19]}...)")
                client.pull_blob(layer_digest, layer_path)
            base_layers.append(OCILayer(layer_desc['mediaType'], layer_digest, layer_size, str(layer_path)))
        
        print(f"✓ Base image pulled ({len(base_layers)} layers)")
        return base_layers, base_config
    
    def push(self, registry_url: Optional[str]=None, auth_token: Optional[str]=None, username: Optional[str]=None, password: Optional[str]=None, show_progress: bool=True):
        """Push built image to registry."""
        if not hasattr(self, 'manifest_digest'):
            raise RuntimeError("Must call build() before push()")
        
        target=registry_url or self.config.tag
        registry, repo, tag=parse_image_reference(target)
        
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
        client.push_manifest(tag, manifest_data)
        
        if show_progress: print(f"✓ Pushed {registry}/{repo}:{tag}")
        return f"{registry}/{repo}:{tag}"
    
    def _show_build_plan(self):
        """Display build plan for dry-run mode."""
        print(f"Build Plan for {self.config.tag}:")
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
            if is_requirements:
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
            self._staged_packages = staged_packages(site_packages)
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
                                if path.stat().st_mode & 0o111
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
