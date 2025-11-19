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

    def to_dict(self) -> Dict:
        d={"mediaType":self.media_type,"digest":self.digest,"size":self.size}
        if self.platform: d["platform"]=self.platform
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

def build_config_json(architecture, os_name, env, working_dir, entrypoint, exposed_ports, labels=None, user=None, cmd=None, base_config=None):
    """Build OCI config, optionally merging with base image config."""
    if base_config:
        cfg=base_config.copy()
        base_env={kv.split('=',1)[0]:kv.split('=',1)[1] for kv in base_config.get('config',{}).get('Env',[]) if '=' in kv}
        merged_env={**base_env, **env}
        cfg['config']['Env']=[f"{k}={v}" for k,v in merged_env.items()]
        cfg['config']['WorkingDir']=working_dir or base_config.get('config',{}).get('WorkingDir','/app')
        
        base_entrypoint=base_config.get('config',{}).get('Entrypoint')
        if entrypoint:
            if is_distroless(base_config) and entrypoint and entrypoint[0] in ['/bin/sh','/bin/bash','sh','bash']:
                cfg['config']['Cmd']=entrypoint
                if base_entrypoint: cfg['config']['Entrypoint']=base_entrypoint
            else:
                cfg['config']['Entrypoint']=entrypoint
        elif base_entrypoint:
            cfg['config']['Entrypoint']=base_entrypoint
        
        if cmd: cfg['config']['Cmd']=cmd
        elif 'Cmd' in base_config.get('config',{}): cfg['config']['Cmd']=base_config['config']['Cmd']
        if user: cfg['config']['User']=user
        elif 'User' in base_config.get('config',{}): cfg['config']['User']=base_config['config']['User']
        base_labels=base_config.get('config',{}).get('Labels',{})
        if labels: cfg['config']['Labels']={**base_labels, **labels}
        elif base_labels: cfg['config']['Labels']=base_labels
    else:
        cfg={"architecture":architecture,"os":os_name,"config":{"Env":[f"{k}={v}" for k,v in env.items()],"WorkingDir":working_dir,"Entrypoint":entrypoint}}
        if labels: cfg['config']['Labels']=labels
        if user: cfg['config']['User']=user
        if cmd: cfg['config']['Cmd']=cmd
    if exposed_ports:
        cfg["config"]["ExposedPorts"]={f"{p}/tcp":{} for p in exposed_ports}
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

def build_index_json(manifest_digest, manifest_size, tag=None):
    desc=OCIManifestDescriptor("application/vnd.oci.image.manifest.v1+json",manifest_digest,manifest_size,{"architecture":"amd64","os":"linux"})
    annot={"org.opencontainers.image.ref.name":tag} if tag else None
    return OCIIndex(2,[desc],annot).to_json()
