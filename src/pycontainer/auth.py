"""Authentication providers for container registries."""
import json, base64, os, subprocess
from pathlib import Path
from typing import Optional, Dict, Tuple
from abc import ABC, abstractmethod

class AuthProvider(ABC):
    """Base authentication provider interface."""
    
    @abstractmethod
    def get_credentials(self, registry: str) -> Optional[Tuple[str, str]]:
        """Return (username, password/token) for registry, or None."""
        pass
    
    @abstractmethod
    def get_token(self, registry: str) -> Optional[str]:
        """Return bearer token for registry, or None."""
        pass

class EnvironmentAuthProvider(AuthProvider):
    """Read credentials from environment variables."""
    
    def get_credentials(self, registry: str) -> Optional[Tuple[str, str]]:
        user=os.getenv('REGISTRY_USERNAME')
        pwd=os.getenv('REGISTRY_PASSWORD')
        if user and pwd: return (user, pwd)
        
        if 'ghcr.io' in registry:
            token=os.getenv('GITHUB_TOKEN')
            if token: return ('USERNAME', token)
        
        return None
    
    def get_token(self, registry: str) -> Optional[str]:
        if 'ghcr.io' in registry:
            return os.getenv('GITHUB_TOKEN')
        return os.getenv('REGISTRY_TOKEN')

class DockerConfigAuthProvider(AuthProvider):
    """Read credentials from ~/.docker/config.json."""
    
    def __init__(self, config_path: Optional[Path]=None):
        self.config_path=config_path or Path.home()/'.docker'/'config.json'
    
    def _load_config(self) -> Optional[Dict]:
        if not self.config_path.exists(): return None
        try:
            return json.loads(self.config_path.read_text())
        except: return None
    
    def _decode_auth(self, auth_str: str) -> Tuple[str, str]:
        """Decode base64 auth string to (username, password)."""
        decoded=base64.b64decode(auth_str).decode('utf-8')
        if ':' in decoded:
            user, pwd=decoded.split(':', 1)
            return (user, pwd)
        return ('', decoded)
    
    def get_credentials(self, registry: str) -> Optional[Tuple[str, str]]:
        cfg=self._load_config()
        if not cfg: return None
        
        auths=cfg.get('auths', {})
        
        for reg_key in [f'https://{registry}', registry, f'https://{registry}/v2/', f'{registry}/v2/']:
            if reg_key in auths:
                auth_data=auths[reg_key]
                if 'auth' in auth_data:
                    return self._decode_auth(auth_data['auth'])
                if 'username' in auth_data and 'password' in auth_data:
                    return (auth_data['username'], auth_data['password'])
        
        return None
    
    def get_token(self, registry: str) -> Optional[str]:
        creds=self.get_credentials(registry)
        if creds: return creds[1]
        return None

class AzureCLIAuthProvider(AuthProvider):
    """Get credentials from Azure CLI for ACR."""
    
    def get_credentials(self, registry: str) -> Optional[Tuple[str, str]]:
        if 'azurecr.io' not in registry: return None
        
        try:
            result=subprocess.run(
                ['az', 'acr', 'login', '--name', registry.split('.')[0], '--expose-token', '--output', 'json'],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode==0:
                data=json.loads(result.stdout)
                return ('00000000-0000-0000-0000-000000000000', data['accessToken'])
        except: pass
        
        return None
    
    def get_token(self, registry: str) -> Optional[str]:
        creds=self.get_credentials(registry)
        if creds: return creds[1]
        return None

class ChainAuthProvider(AuthProvider):
    """Try multiple auth providers in sequence."""
    
    def __init__(self, providers: list):
        self.providers=providers
    
    def get_credentials(self, registry: str) -> Optional[Tuple[str, str]]:
        for provider in self.providers:
            creds=provider.get_credentials(registry)
            if creds: return creds
        return None
    
    def get_token(self, registry: str) -> Optional[str]:
        for provider in self.providers:
            token=provider.get_token(registry)
            if token: return token
        return None

def get_default_auth_provider() -> AuthProvider:
    """Return default auth provider chain."""
    return ChainAuthProvider([
        EnvironmentAuthProvider(),
        DockerConfigAuthProvider(),
        AzureCLIAuthProvider()
    ])

def get_auth_for_registry(registry: str, username: Optional[str]=None, password: Optional[str]=None) -> Optional[str]:
    """Get auth token for registry, trying multiple sources."""
    if password:
        return password
    
    if username and password:
        return password
    
    provider=get_default_auth_provider()
    
    token=provider.get_token(registry)
    if token: return token
    
    creds=provider.get_credentials(registry)
    if creds: return creds[1]
    
    return None
