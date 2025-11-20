import hashlib, json, tarfile, shutil, tempfile, logging
from pathlib import Path
from typing import Optional, List, Dict, Tuple
from .config import BuildConfig
from .oci import OCILayer, build_config_json, build_manifest_json, build_oci_layout, build_index_json
from .project import detect_entrypoint, default_include_paths, find_dependencies
from .framework import apply_framework_defaults
from .fs_utils import ensure_dir, iter_files
from .registry_client import RegistryClient, parse_image_reference
from .auth import get_auth_for_registry
from .cache import LayerCache
from .sbom import generate_sbom

logger=logging.getLogger(__name__)

class ImageBuilder:
    def __init__(self, config: BuildConfig): 
        self.config=apply_framework_defaults(config, Path(config.context_dir))
        self.cache=LayerCache(
            cache_dir=Path(config.cache_dir) if config.cache_dir else None,
            max_size_mb=config.max_cache_size_mb
        ) if config.use_cache else None
        self.verbose=getattr(config, 'verbose', False)
        self.dry_run=getattr(config, 'dry_run', False)

    def build(self):
        if self.dry_run:
            logger.info("DRY RUN: Would build image %s", self.config.tag)
            self._show_build_plan()
            return self.config.tag
        
        output=ensure_dir(self.config.output_dir)
        blobs=ensure_dir(output/'blobs')
        layers_dir=ensure_dir(blobs/'sha256')
        refs_dir=ensure_dir(output/'refs'/'tags')

        base_layers, base_config = self._pull_base_image(layers_dir)
        
        entry = self.config.entrypoint or detect_entrypoint(self.config.context_dir)
        include = self.config.include_paths or default_include_paths(self.config.context_dir)

        app_layers=[]
        if self.config.include_deps:
            deps_layer=self._create_deps_layer(layers_dir)
            if deps_layer: app_layers.append(deps_layer)
        
        app_layer=self._create_app_layer(layers_dir, include)
        app_layers.append(app_layer)
        
        all_layers=base_layers+app_layers

        cfg = build_config_json("amd64","linux",self.config.env,self.config.workdir,entry,self.config.exposed_ports,
                                labels=self.config.labels,user=self.config.user,cmd=self.config.cmd,base_config=base_config)
        cfg_bytes=json.dumps(cfg,separators=(',',':')).encode()
        cfg_digest="sha256:"+hashlib.sha256(cfg_bytes).hexdigest()
        cfg_path=layers_dir/cfg_digest.split(":",1)[1]
        cfg_path.write_bytes(cfg_bytes)

        manifest=build_manifest_json(cfg_digest,len(cfg_bytes),all_layers)
        manifest_bytes=json.dumps(manifest,separators=(',',':')).encode()
        manifest_digest="sha256:"+hashlib.sha256(manifest_bytes).hexdigest()
        manifest_path=layers_dir/manifest_digest.split(":",1)[1]
        manifest_path.write_bytes(manifest_bytes)

        oci_layout=build_oci_layout()
        (output/'oci-layout').write_bytes(json.dumps(oci_layout,separators=(',',':')).encode())

        index=build_index_json(manifest_digest,len(manifest_bytes),self.config.tag)
        (output/'index.json').write_bytes(json.dumps(index,separators=(',',':')).encode())

        _, _, tag_name=parse_image_reference(self.config.tag)
        (refs_dir/tag_name).write_text(manifest_digest)

        self.manifest_digest=manifest_digest
        self.config_digest=cfg_digest
        self.layers=all_layers
        
        if getattr(self.config, 'generate_sbom', False):
            sbom_path=output/'sbom.json'
            logger.info("Generating SBOM...")
            generate_sbom(Path(self.config.context_dir), sbom_path)
            logger.info(f"✓ SBOM saved to {sbom_path}")
        
        return self.config.tag
    
    def _pull_base_image(self, layers_dir: Path) -> Tuple[List[OCILayer], Optional[Dict]]:
        """Pull base image from registry, return (base_layers, base_config)."""
        registry, repo, tag=parse_image_reference(self.config.base_image)
        auth_token=get_auth_for_registry(registry)
        client=RegistryClient(registry, repo, auth_token=auth_token)
        
        print(f"Pulling base image {self.config.base_image}...")
        manifest, _=client.pull_manifest(tag)
        
        if manifest.get('mediaType')=='application/vnd.oci.image.index.v1+json':
            for m in manifest.get('manifests',[]):
                plat=m.get('platform',{})
                if plat.get('architecture')=='amd64' and plat.get('os')=='linux':
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
        
        if not auth_token and not password:
            auth_token=get_auth_for_registry(registry, username, password)
        
        client=RegistryClient(registry, repo, auth_token=auth_token, username=username, password=password)
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

    def _create_deps_layer(self, layers_dir: Path) -> Optional[OCILayer]:
        """Create dependency layer from venv or requirements.txt."""
        ctx=Path(self.config.context_dir)
        deps_paths=find_dependencies(ctx, self.config.requirements_file)
        if not deps_paths:
            return None
        
        print(f"Creating dependency layer ({len(deps_paths)} files)...")
        tmp=layers_dir/'deps-layer.tar'
        with tarfile.open(tmp,'w') as tar:
            for abs_path, rel in deps_paths:
                arc=f"{self.config.workdir.lstrip('/')}/{rel.as_posix()}"
                tar.add(abs_path,arcname=arc)
        data=tmp.read_bytes()
        digest="sha256:"+hashlib.sha256(data).hexdigest()
        final=layers_dir/digest.split(":",1)[1]
        tmp.rename(final)
        print(f"✓ Dependency layer created ({digest[:19]}...)")
        return OCILayer("application/vnd.oci.image.layer.v1.tar",digest,len(data),str(final))

    def _create_app_layer(self, layers_dir, include_paths):
        ctx=Path(self.config.context_dir)
        files=list(iter_files(ctx, include_paths))
        
        if self.cache:
            cached=self.cache.get_layer(files)
            if cached:
                digest, cache_path=cached
                final=layers_dir/digest.split(":",1)[1]
                if not final.exists():
                    shutil.copy2(cache_path, final)
                return OCILayer("application/vnd.oci.image.layer.v1.tar",digest,cache_path.stat().st_size,str(final))
        
        tmp=layers_dir/'app-layer.tar'
        files_sorted=sorted(files, key=lambda x: x[1].as_posix()) if self.config.reproducible else files
        
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
        data=tmp.read_bytes()
        digest="sha256:"+hashlib.sha256(data).hexdigest()
        final=layers_dir/digest.split(":",1)[1]
        tmp.rename(final)
        
        if self.cache:
            self.cache.store_layer(files, digest, final)
        
        return OCILayer("application/vnd.oci.image.layer.v1.tar",digest,len(data),str(final))
