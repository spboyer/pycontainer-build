"""Configuration file loading and validation."""
import tomllib
from pathlib import Path
from typing import Dict, Any, Optional
from .config import BuildConfig

def load_config_file(path: Path) -> Dict[str, Any]:
    """Load configuration from pycontainer.toml file."""
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    
    with open(path, 'rb') as f:
        data=tomllib.load(f)
    
    return data.get('build', {})

def merge_configs(file_config: Dict[str, Any], cli_args: Dict[str, Any]) -> Dict[str, Any]:
    """Merge file config with CLI args (CLI takes precedence)."""
    merged=file_config.copy()
    
    for key, value in cli_args.items():
        if value is not None:
            if key=='env' and isinstance(value, dict) and key in merged:
                merged[key]={**merged.get(key, {}), **value}
            elif key=='labels' and isinstance(value, dict) and key in merged:
                merged[key]={**merged.get(key, {}), **value}
            else:
                merged[key]=value
    
    return merged

def config_from_file(path: Path, cli_overrides: Optional[Dict]=None) -> BuildConfig:
    """Create BuildConfig from file with optional CLI overrides."""
    file_cfg=load_config_file(path)
    
    if cli_overrides:
        file_cfg=merge_configs(file_cfg, cli_overrides)
    
    file_cfg={k.replace('-', '_'): v for k, v in file_cfg.items()}
    
    return BuildConfig(**file_cfg)
