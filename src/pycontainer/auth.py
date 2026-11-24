"""Authentication providers for container registries."""
import json, base64, logging, os, re, subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Dict, Tuple
from abc import ABC, abstractmethod

GHCR_HOST='ghcr.io'
HTTPS_PORT=443
_DNS_LABEL=re.compile(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?')
_ACR_LOGIN_SERVER=re.compile(r'([a-z0-9]{5,50})(?:-[a-z0-9]+)?\.azurecr\.io')
logger=logging.getLogger(__name__)


def _https_registry_host(registry: object) -> Optional[str]:
    """Return the lowercase hostname of a bare HTTPS authority, or None."""
    if not isinstance(registry, str) or not registry.isascii():
        return None
    host, separator, port=registry.partition(':')
    if separator:
        if not port.isdecimal():
            return None
        try:
            if int(port)!=HTTPS_PORT:
                return None
        except ValueError:
            return None
    host=host.lower()
    if len(host)>253 or not all(_DNS_LABEL.fullmatch(label) for label in host.split('.')):
        return None
    return host


def _is_ghcr_registry(registry: str) -> bool:
    return _https_registry_host(registry)==GHCR_HOST


def _acr_registry_name(registry: str) -> Optional[str]:
    host=_https_registry_host(registry)
    match=_ACR_LOGIN_SERVER.fullmatch(host) if host else None
    return match.group(1) if match else None


@dataclass(frozen=True)
class RegistryAuth:
    bearer_token: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None


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
    """Read credentials from environment variables.

    REGISTRY_TOKEN, REGISTRY_USERNAME and REGISTRY_PASSWORD are not scoped to a
    registry; set include_generic_environment=False for requests that must not
    receive destination credentials.
    """

    def __init__(self, *, include_generic_environment: bool = True):
        self.include_generic_environment=include_generic_environment
    
    def get_credentials(self, registry: str) -> Optional[Tuple[str, str]]:
        if self.include_generic_environment:
            user=os.getenv('REGISTRY_USERNAME')
            pwd=os.getenv('REGISTRY_PASSWORD')
            if user and pwd: return (user, pwd)
        
        if _is_ghcr_registry(registry):
            token=os.getenv('GITHUB_TOKEN')
            if token: return ('USERNAME', token)
        
        return None
    
    def get_token(self, registry: str) -> Optional[str]:
        if _is_ghcr_registry(registry):
            return os.getenv('GITHUB_TOKEN')
        if self.include_generic_environment:
            return os.getenv('REGISTRY_TOKEN')
        return None

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
        
        # Docker Hub special cases
        docker_hub_keys=['https://index.docker.io/v1/', 'index.docker.io', 'docker.io', 'registry-1.docker.io']
        if registry in docker_hub_keys or registry=='docker.io':
            for key in docker_hub_keys:
                if key in auths:
                    auth_data=auths[key]
                    if 'auth' in auth_data:
                        return self._decode_auth(auth_data['auth'])
                    if 'username' in auth_data and 'password' in auth_data:
                        return (auth_data['username'], auth_data['password'])
        
        # Try various registry URL formats
        for reg_key in [registry, f'https://{registry}', f'https://{registry}/v2/', f'{registry}/v2/']:
            if reg_key in auths:
                auth_data=auths[reg_key]
                if 'auth' in auth_data:
                    return self._decode_auth(auth_data['auth'])
                if 'username' in auth_data and 'password' in auth_data:
                    return (auth_data['username'], auth_data['password'])
        
        return None
    
    def get_token(self, registry: str) -> Optional[str]:
        return None

class AzureCLIAuthProvider(AuthProvider):
    """Get credentials from Azure CLI for ACR."""

    def __init__(self, *, log_redact: Optional[Callable[[str], str]] = None):
        self._log_redact = log_redact

    def _warning(self, message: str, *args) -> None:
        rendered = message % args
        if self._log_redact is not None:
            rendered = self._log_redact(rendered)
        logger.warning("%s", rendered)

    def get_credentials(self, registry: str) -> Optional[Tuple[str, str]]:
        registry_name=_acr_registry_name(registry)
        if not registry_name: return None
        
        try:
            result=subprocess.run(
                ['az', 'acr', 'login', '--name', registry_name, '--expose-token', '--output', 'json'],
                capture_output=True, text=True, timeout=10
            )
        except FileNotFoundError:
            return None
        except (OSError, subprocess.SubprocessError) as error:
            self._warning("Azure CLI authentication failed for %s: %s", registry, error)
            return None
        if result.returncode!=0:
            self._warning(
                "Azure CLI authentication failed for %s (exit code %s).",
                registry, result.returncode
            )
            return None
        try:
            data=json.loads(result.stdout)
        except (TypeError, json.JSONDecodeError):
            self._warning("Azure CLI returned invalid credentials for %s.", registry)
            return None
        if not isinstance(data, dict):
            self._warning("Azure CLI returned invalid credentials for %s.", registry)
            return None
        if _https_registry_host(data.get('loginServer'))!=_https_registry_host(registry):
            self._warning("Azure CLI returned a different or invalid login server for %s.", registry)
            return None
        token=data.get('accessToken')
        if not isinstance(token, str) or not token:
            self._warning("Azure CLI returned an invalid token for %s.", registry)
            return None
        return ('00000000-0000-0000-0000-000000000000', token)
    
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

def get_default_auth_provider(
    *, include_generic_environment: bool = True,
    log_redact: Optional[Callable[[str], str]] = None,
) -> AuthProvider:
    """Return default auth provider chain."""
    return ChainAuthProvider([
        EnvironmentAuthProvider(
            include_generic_environment=include_generic_environment
        ),
        DockerConfigAuthProvider(),
        AzureCLIAuthProvider(log_redact=log_redact)
    ])


def get_registry_auth(
    registry: str,
    *,
    bearer_token: Optional[str] = None,
    username: Optional[str] = None,
    password: Optional[str] = None,
    log_redact: Optional[Callable[[str], str]] = None,
    include_generic_environment: bool = True,
) -> RegistryAuth:
    """Resolve registry credentials without treating a password as a bearer token."""
    if bearer_token:
        return RegistryAuth(bearer_token=bearer_token)
    if username:
        if not password:
            raise ValueError("A registry username must be paired with a password.")
        return RegistryAuth(username=username, password=password)
    if password:
        return RegistryAuth(bearer_token=password)

    options = {"log_redact": log_redact} if log_redact is not None else {}
    provider=get_default_auth_provider(
        include_generic_environment=include_generic_environment, **options
    )

    token=provider.get_token(registry)
    if token:
        return RegistryAuth(bearer_token=token)

    creds=provider.get_credentials(registry)
    if creds:
        credential_username, credential_password = creds
        if credential_username and credential_password:
            return RegistryAuth(
                username=credential_username, password=credential_password
            )
        if credential_password:
            return RegistryAuth(bearer_token=credential_password)

    return RegistryAuth()


def get_mirror_auth(
    host: str, *, log_redact: Optional[Callable[[str], str]] = None
) -> RegistryAuth:
    """Resolve credentials scoped to exactly one mirror host.

    Host-agnostic REGISTRY_* environment variables are deliberately ignored:
    they usually belong to the upstream or push registry and must never be
    sent to a mirror.
    """
    host = host.lower()
    creds = DockerConfigAuthProvider().get_credentials(host)
    if not creds and _acr_registry_name(host):
        creds = AzureCLIAuthProvider(log_redact=log_redact).get_credentials(host)
    if not creds and _is_ghcr_registry(host) and os.getenv("GITHUB_TOKEN"):
        return RegistryAuth(bearer_token=os.getenv("GITHUB_TOKEN"))
    if creds:
        username, password = creds
        if username and password:
            return RegistryAuth(username=username, password=password)
        if password:
            return RegistryAuth(bearer_token=password)
    return RegistryAuth()


def get_auth_for_registry(
    registry: str, username: Optional[str] = None, password: Optional[str] = None
) -> Optional[str]:
    """Return a bearer token only; basic credentials are resolved separately."""
    return get_registry_auth(
        registry, username=username, password=password
    ).bearer_token
