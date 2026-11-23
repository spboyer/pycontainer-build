from copy import deepcopy
from dataclasses import dataclass
from typing import List, Dict, Optional

@dataclass
class OCILayer:
    media_type: str
    digest: str
    size: int
    tar_path: str

    def to_descriptor(self) -> Dict:
        return {"mediaType": self.media_type,"digest": self.digest,"size": self.size}

@dataclass
class OCIManifestDescriptor:
    media_type: str
    digest: str
    size: int
    platform: Optional[Dict] = None
    annotations: Optional[Dict[str,str]] = None

    def to_dict(self) -> Dict:
        d={"mediaType":self.media_type,"digest":self.digest,"size":self.size}
        if self.platform: d["platform"]=self.platform
        if self.annotations: d["annotations"]=self.annotations
        return d

@dataclass
class OCIIndex:
    schema_version: int
    manifests: List[OCIManifestDescriptor]
    annotations: Optional[Dict[str,str]] = None

    def to_json(self) -> Dict:
        idx={"schemaVersion":self.schema_version,"mediaType":"application/vnd.oci.image.index.v1+json","manifests":[m.to_dict() for m in self.manifests]}
        if self.annotations: idx["annotations"]=self.annotations
        return idx

def build_config_json(architecture, os_name, env, working_dir, entrypoint, exposed_ports, labels=None, user=None, cmd=None, base_config=None, layer_diff_ids=None, stop_signal=None, volumes=None):
    """Build OCI config, optionally merging with base image config."""
    if base_config:
        cfg=deepcopy(base_config)
        base_env={kv.split('=',1)[0]:kv.split('=',1)[1] for kv in base_config.get('config',{}).get('Env',[]) if '=' in kv}
        merged_env={**base_env, **env}
        cfg['config']['Env']=[f"{k}={v}" for k,v in merged_env.items()]
        cfg['config']['WorkingDir']=working_dir or base_config.get('config',{}).get('WorkingDir','/app')
        
        base_entrypoint=base_config.get('config',{}).get('Entrypoint')
        if entrypoint == []:
            cfg['config'].pop('Entrypoint', None)
        elif entrypoint:
            if cmd is None and is_distroless(base_config) and entrypoint[0] in ['/bin/sh','/bin/bash','sh','bash']:
                cfg['config']['Cmd']=entrypoint
                if base_entrypoint: cfg['config']['Entrypoint']=base_entrypoint
            else:
                cfg['config']['Entrypoint']=entrypoint
        elif base_entrypoint:
            cfg['config']['Entrypoint']=base_entrypoint
        
        if cmd: cfg['config']['Cmd']=cmd
        elif cmd == []: cfg['config'].pop('Cmd', None)
        elif 'Cmd' in base_config.get('config',{}): cfg['config']['Cmd']=base_config['config']['Cmd']
        if user: cfg['config']['User']=user
        elif 'User' in base_config.get('config',{}): cfg['config']['User']=base_config['config']['User']
        base_labels=base_config.get('config',{}).get('Labels',{})
        if labels: cfg['config']['Labels']={**base_labels, **labels}
        elif base_labels: cfg['config']['Labels']=base_labels
    else:
        cfg={"architecture":architecture,"os":os_name,"config":{"Env":[f"{k}={v}" for k,v in env.items()],"WorkingDir":working_dir}}
        if entrypoint: cfg['config']['Entrypoint']=entrypoint
        if labels: cfg['config']['Labels']=labels
        if user: cfg['config']['User']=user
        if cmd: cfg['config']['Cmd']=cmd
    rootfs=cfg.setdefault("rootfs", {})
    rootfs["type"]=rootfs.get("type", "layers")
    rootfs["diff_ids"]=[*rootfs.get("diff_ids", []), *(layer_diff_ids or [])]
    if exposed_ports is not None:
        cfg["config"]["ExposedPorts"]={f"{p}/tcp":{} for p in exposed_ports}
    if stop_signal:
        cfg["config"]["StopSignal"]=stop_signal
    if volumes:
        merged_volumes=set(cfg["config"].get("Volumes") or {})|set(volumes)
        cfg["config"]["Volumes"]={path:{} for path in sorted(merged_volumes)}
    return cfg

def is_distroless(config: Dict) -> bool:
    """Detect if base image is distroless (no shell)."""
    labels=config.get('config',{}).get('Labels',{})
    return 'distroless' in labels.get('org.opencontainers.image.base.name','').lower() or 'distroless' in labels.get('name','').lower()

def build_manifest_json(config_digest, config_size, layers: List[OCILayer]):
    return {"schemaVersion":2,"mediaType":"application/vnd.oci.image.manifest.v1+json",
            "config":{"mediaType":"application/vnd.oci.image.config.v1+json","digest":config_digest,"size":config_size},
            "layers":[l.to_descriptor() for l in layers]}

def build_oci_layout():
    return {"imageLayoutVersion":"1.0.0"}

def build_index_json(
    manifest_digest,
    manifest_size,
    tag=None,
    architecture="amd64",
    os_name="linux",
    variant=None,
    *,
    ref_names=None,
):
    platform = {"architecture": architecture, "os": os_name}
    if variant:
        platform["variant"] = variant
    annot={"org.opencontainers.image.ref.name":tag} if tag else None
    # The image-layout spec resolves ref.name on the manifest descriptor, one
    # descriptor per reference; the index-level copy is kept for existing consumers.
    names=list(dict.fromkeys(ref_names or ([tag] if tag else [])))
    descriptors=[
        OCIManifestDescriptor("application/vnd.oci.image.manifest.v1+json",manifest_digest,manifest_size,
                              dict(platform),{"org.opencontainers.image.ref.name":name})
        for name in names
    ] or [OCIManifestDescriptor("application/vnd.oci.image.manifest.v1+json",manifest_digest,manifest_size,platform)]
    return OCIIndex(2,descriptors,annot).to_json()
