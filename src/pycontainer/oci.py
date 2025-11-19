from dataclasses import dataclass
from typing import List, Dict

@dataclass
class OCILayer:
    media_type: str
    digest: str
    size: int
    tar_path: str

    def to_descriptor(self) -> Dict:
        return {"mediaType": self.media_type,"digest": self.digest,"size": self.size}

def build_config_json(architecture, os_name, env, working_dir, entrypoint, exposed_ports):
    cfg={"architecture":architecture,"os":os_name,"config":{"Env":[f"{k}={v}" for k,v in env.items()],"WorkingDir":working_dir,"Entrypoint":entrypoint}}
    if exposed_ports:
        cfg["config"]["ExposedPorts"]={f"{p}/tcp":{} for p in exposed_ports}
    return cfg

def build_manifest_json(config_digest, config_size, layers: List[OCILayer]):
    return {"schemaVersion":2,"mediaType":"application/vnd.oci.image.manifest.v1+json",
            "config":{"mediaType":"application/vnd.oci.image.config.v1+json","digest":config_digest,"size":config_size},
            "layers":[l.to_descriptor() for l in layers]}
