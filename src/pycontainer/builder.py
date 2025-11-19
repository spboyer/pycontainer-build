import hashlib, json, tarfile
from pathlib import Path
from .config import BuildConfig
from .oci import OCILayer, build_config_json, build_manifest_json
from .project import detect_entrypoint, default_include_paths
from .fs_utils import ensure_dir, iter_files

class ImageBuilder:
    def __init__(self, config: BuildConfig): self.config=config

    def build(self):
        output=ensure_dir(self.config.output_dir)
        blobs=ensure_dir(output/'blobs')
        layers_dir=ensure_dir(blobs/'sha256')

        entry = self.config.entrypoint or detect_entrypoint(self.config.context_dir)
        include = self.config.include_paths or default_include_paths(self.config.context_dir)

        layer=self._create_app_layer(layers_dir, include)

        cfg = build_config_json("amd64","linux",self.config.env,self.config.workdir,entry,self.config.exposed_ports)
        cfg_bytes=json.dumps(cfg,separators=(',',':')).encode()
        cfg_digest="sha256:"+hashlib.sha256(cfg_bytes).hexdigest()
        cfg_path=layers_dir/cfg_digest.split(":",1)[1]
        cfg_path.write_bytes(cfg_bytes)

        manifest=build_manifest_json(cfg_digest,len(cfg_bytes),[layer])
        (output/'manifest.json').write_bytes(json.dumps(manifest,separators=(',',':')).encode())

        return self.config.tag

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
