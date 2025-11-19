"""Tests for authentication providers."""
import json, tempfile, os
from pathlib import Path
from pycontainer.auth import (
    EnvironmentAuthProvider,
    DockerConfigAuthProvider,
    AzureCLIAuthProvider,
    ChainAuthProvider,
    get_auth_for_registry
)

def test_environment_auth_provider():
    """Test reading credentials from environment variables."""
    os.environ['REGISTRY_USERNAME']='testuser'
    os.environ['REGISTRY_PASSWORD']='testpass'
    
    provider=EnvironmentAuthProvider()
    creds=provider.get_credentials('docker.io')
    assert creds==('testuser','testpass')
    
    del os.environ['REGISTRY_USERNAME']
    del os.environ['REGISTRY_PASSWORD']

def test_github_token_env():
    """Test GitHub token from environment."""
    os.environ['GITHUB_TOKEN']='ghp_test123'
    
    provider=EnvironmentAuthProvider()
    creds=provider.get_credentials('ghcr.io')
    assert creds==('USERNAME','ghp_test123')
    
    token=provider.get_token('ghcr.io')
    assert token=='ghp_test123'
    
    del os.environ['GITHUB_TOKEN']

def test_docker_config_auth_provider():
    """Test reading Docker config.json."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config_path=Path(tmpdir)/'config.json'
        
        config={
            "auths":{
                "ghcr.io":{
                    "auth":"dGVzdHVzZXI6dGVzdHBhc3M="
                },
                "https://index.docker.io/v1/":{
                    "username":"dockeruser",
                    "password":"dockerpass"
                }
            }
        }
        config_path.write_text(json.dumps(config))
        
        provider=DockerConfigAuthProvider(config_path)
        
        creds=provider.get_credentials('ghcr.io')
        assert creds==('testuser','testpass')
        
        creds=provider.get_credentials('index.docker.io')
        assert creds==('dockeruser','dockerpass')
        
        creds=provider.get_credentials('unknown.registry')
        assert creds is None

def test_docker_config_base64_decode():
    """Test base64 auth string decoding."""
    import base64
    
    with tempfile.TemporaryDirectory() as tmpdir:
        config_path=Path(tmpdir)/'config.json'
        
        auth_str=base64.b64encode(b'user:pass').decode()
        config={"auths":{"test.io":{"auth":auth_str}}}
        config_path.write_text(json.dumps(config))
        
        provider=DockerConfigAuthProvider(config_path)
        creds=provider.get_credentials('test.io')
        assert creds==('user','pass')

def test_chain_auth_provider():
    """Test chaining multiple auth providers."""
    os.environ['REGISTRY_TOKEN']='env_token'
    
    with tempfile.TemporaryDirectory() as tmpdir:
        config_path=Path(tmpdir)/'config.json'
        config={"auths":{"docker.io":{"auth":"ZG9ja2VyOnBhc3M="}}}
        config_path.write_text(json.dumps(config))
        
        chain=ChainAuthProvider([
            EnvironmentAuthProvider(),
            DockerConfigAuthProvider(config_path)
        ])
        
        token=chain.get_token('any.registry')
        assert token=='env_token'
        
        creds=chain.get_credentials('docker.io')
        assert creds==('docker','pass')
    
    del os.environ['REGISTRY_TOKEN']

def test_get_auth_for_registry():
    """Test high-level auth resolution."""
    os.environ['GITHUB_TOKEN']='ghp_mytoken'
    
    token=get_auth_for_registry('ghcr.io')
    assert token=='ghp_mytoken'
    
    token=get_auth_for_registry('ghcr.io', password='override_token')
    assert token=='override_token'
    
    del os.environ['GITHUB_TOKEN']

def test_missing_docker_config():
    """Test handling of missing Docker config."""
    provider=DockerConfigAuthProvider(Path('/nonexistent/config.json'))
    creds=provider.get_credentials('any.registry')
    assert creds is None

def test_azure_cli_provider_non_acr():
    """Test Azure CLI provider skips non-ACR registries."""
    provider=AzureCLIAuthProvider()
    creds=provider.get_credentials('docker.io')
    assert creds is None
    
    creds=provider.get_credentials('ghcr.io')
    assert creds is None

if __name__=="__main__":
    test_environment_auth_provider()
    test_github_token_env()
    test_docker_config_auth_provider()
    test_docker_config_base64_decode()
    test_chain_auth_provider()
    test_get_auth_for_registry()
    test_missing_docker_config()
    test_azure_cli_provider_non_acr()
    print("✅ All authentication tests passed")
