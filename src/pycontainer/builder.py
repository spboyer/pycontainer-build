import hashlib, json, tarfile
from pathlib import Path
from typing import Optional
from .config import BuildConfig
from .oci import OCILayer, build_config_json, build_manifest_json, build_oci_layout, build_index_json
from .project import detect_entrypoint, default_include_paths
from .fs_utils import ensure_dir, iter_files
from .registry_client import RegistryClient, parse_image_reference
from .auth import get_auth_for_registry

class ImageBuilder:
    def __init__(self, config: BuildConfig): self.config=config

    def build(self):
        output=ensure_dir(self.config.output_dir)
        blobs=ensure_dir(output/'blobs')
        layers_dir=ensure_dir(blobs/'sha256')
        refs_dir=ensure_dir(output/'refs'/'tags')

        entry = self.config.entrypoint or detect_entrypoint(self.config.context_dir)
        include = self.config.include_paths or default_include_paths(self.config.context_dir)

        layer=self._create_app_layer(layers_dir, include)

        cfg = build_config_json("amd64","linux",self.config.env,self.config.workdir,entry,self.config.exposed_ports)
        cfg_bytes=json.dumps(cfg,separators=(',',':')).encode()
        cfg_digest="sha256:"+hashlib.sha256(cfg_bytes).hexdigest()
        cfg_path=layers_dir/cfg_digest.split(":",1)[1]
        cfg_path.write_bytes(cfg_bytes)

        manifest=build_manifest_json(cfg_digest,len(cfg_bytes),[layer])
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
        self.layers=[layer]
        
        return self.config.tag
    
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

    def _create_app_layer(self, layers_dir, include_paths):
        ctx=Path(self.config.context_dir)
        tmp=layers_dir/'app-layer.tar'
        with tarfile.open(tmp,'w') as tar:
            for abs_path, rel in iter_files(ctx, include_paths):
                arc=f"{self.config.workdir.lstrip('/')}/{rel.as_posix()}"
                tar.add(abs_path,arcname=arc)
        data=tmp.read_bytes()
        digest="sha256:"+hashlib.sha256(data).hexdigest()
        final=layers_dir/digest.split(":",1)[1]
        tmp.rename(final)
        return OCILayer("application/vnd.oci.image.layer.v1.tar",digest,len(data),str(final))
