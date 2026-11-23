"""Tests for authentication providers."""
import json, tempfile, os
from pathlib import Path
import pycontainer.auth as auth_module
from pycontainer.auth import (
    EnvironmentAuthProvider,
    DockerConfigAuthProvider,
    AzureCLIAuthProvider,
    ChainAuthProvider,
    get_auth_for_registry,
    get_registry_auth,
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

def test_docker_config_password_is_not_a_bearer_token(tmp_path, monkeypatch):
    config_path=tmp_path/'config.json'
    config_path.write_text(json.dumps({
        "auths": {
            "registry.example": {
                "auth": "dXNlcjpwYXNzd29yZA=="
            }
        }
    }))
    provider=DockerConfigAuthProvider(config_path)
    monkeypatch.setattr(auth_module, "get_default_auth_provider", lambda **kwargs: provider)

    auth=get_registry_auth("registry.example")

    assert auth.bearer_token is None
    assert auth.username=="user"
    assert auth.password=="password"
    assert provider.get_token("registry.example") is None

def test_environment_username_password_resolve_as_basic_auth(monkeypatch):
    monkeypatch.setenv("REGISTRY_USERNAME", "env-user")
    monkeypatch.setenv("REGISTRY_PASSWORD", "env-password")
    monkeypatch.delenv("REGISTRY_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(
        auth_module,
        "get_default_auth_provider",
        lambda **kwargs: EnvironmentAuthProvider(**kwargs),
    )

    auth=get_registry_auth("registry.example")

    assert auth.bearer_token is None
    assert auth.username=="env-user"
    assert auth.password=="env-password"

def test_explicit_credentials_keep_bearer_and_basic_types_distinct():
    basic=get_registry_auth(
        "registry.example", username="cli-user", password="cli-password"
    )
    token=get_registry_auth("registry.example", password="cli-token")

    assert basic.bearer_token is None
    assert (basic.username, basic.password)==("cli-user", "cli-password")
    assert token.bearer_token=="cli-token"
    assert token.username is None
    assert token.password is None

def test_username_without_password_is_rejected():
    import pytest

    with pytest.raises(ValueError, match="must be paired with a password"):
        get_registry_auth("registry.example", username="cli-user")

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

import pytest

UNSCOPED_REGISTRIES=[
    'ghcr.io.example.com',
    'evilghcr.io',
    'ghcr.io-evil.example',
    'example.com/ghcr.io',
    'ghcr.io@evil.example',
    'user@ghcr.io',
    'ghcr.io:444',
    'ghcr.io:80',
    'ghcr.io:00080',
    'ghcr.io:+443',
    'ghcr.io:4430',
    'ghcr.io:',
    'ghcr.io.',
    'ghcr..io',
    'https://ghcr.io',
    'ghcr.io/v2',
    'ghcr.io:443:443',
    ' ghcr.io',
    'ghcr.io\n',
    '',
]

@pytest.mark.parametrize('registry', UNSCOPED_REGISTRIES)
def test_github_token_is_not_sent_to_non_ghcr_hosts(registry, monkeypatch):
    monkeypatch.setenv('GITHUB_TOKEN', 'synthetic-github-token')
    for name in ('REGISTRY_TOKEN', 'REGISTRY_USERNAME', 'REGISTRY_PASSWORD'):
        monkeypatch.delenv(name, raising=False)
    provider=EnvironmentAuthProvider()

    assert provider.get_token(registry) is None
    assert provider.get_credentials(registry) is None

@pytest.mark.parametrize('registry', [
    'ghcr.io', 'GHCR.IO', 'Ghcr.Io:443', 'ghcr.io:0443', 'GHCR.IO:000443'
])
def test_github_token_is_used_for_ghcr_origin(registry, monkeypatch):
    monkeypatch.setenv('GITHUB_TOKEN', 'synthetic-github-token')
    for name in ('REGISTRY_TOKEN', 'REGISTRY_USERNAME', 'REGISTRY_PASSWORD'):
        monkeypatch.delenv(name, raising=False)
    provider=EnvironmentAuthProvider()

    assert provider.get_token(registry)=='synthetic-github-token'
    assert provider.get_credentials(registry)==('USERNAME', 'synthetic-github-token')

def test_ghcr_default_port_keeps_token_precedence(monkeypatch):
    monkeypatch.delenv('GITHUB_TOKEN', raising=False)
    monkeypatch.setenv('REGISTRY_TOKEN', 'synthetic-registry-token')

    assert EnvironmentAuthProvider().get_token('ghcr.io:0443') is None

@pytest.mark.parametrize('registry', ['ghcr.io.example.com', 'registry.example:5000'])
def test_generic_environment_credentials_still_apply_to_other_hosts(registry, monkeypatch):
    monkeypatch.setenv('GITHUB_TOKEN', 'synthetic-github-token')
    monkeypatch.setenv('REGISTRY_TOKEN', 'synthetic-registry-token')
    monkeypatch.setenv('REGISTRY_USERNAME', 'synthetic-user')
    monkeypatch.setenv('REGISTRY_PASSWORD', 'synthetic-password')
    provider=EnvironmentAuthProvider()

    assert provider.get_token(registry)=='synthetic-registry-token'
    assert provider.get_credentials(registry)==('synthetic-user', 'synthetic-password')

@pytest.mark.parametrize('registry', [
    'myregistry.azurecr.io.example.com',
    'myregistry.azurecr.io-evil.example',
    'evil.example/myregistry.azurecr.io',
    'user@myregistry.azurecr.io',
    'myregistry.azurecr.io@evil.example',
    'myregistry.azurecr.io:444',
    'myregistry.azurecr.io:+443',
    'myregistry.azurecr.io.',
    'my-registry.azurecr.io',
    'abcd-hash.azurecr.io',
    'myregistry--hash.azurecr.io',
    'myregistry-.azurecr.io',
    'myregistry-hash.azurecr.io.example.com',
    'abcd.azurecr.io',
    'a'*51+'.azurecr.io',
    'myregistry.eastus.data.azurecr.io',
    'azurecr.io',
    'notazurecr.io',
    'https://myregistry.azurecr.io',
])
def test_azure_cli_is_not_used_for_non_acr_hosts(registry, monkeypatch):
    calls=[]
    monkeypatch.setattr(
        auth_module.subprocess, 'run', lambda *args, **kwargs: calls.append(args)
    )
    provider=AzureCLIAuthProvider()

    assert provider.get_credentials(registry) is None
    assert provider.get_token(registry) is None
    assert calls==[]

@pytest.mark.parametrize('registry, login_server', [
    ('myregistry.azurecr.io', 'myregistry.azurecr.io'),
    ('MyRegistry.AzureCR.io', 'myregistry.azurecr.io'),
    ('myregistry.azurecr.io:443', 'MYREGISTRY.AZURECR.IO'),
    ('myregistry.azurecr.io:0443', 'myregistry.azurecr.io'),
    ('myregistry-abc123.azurecr.io', 'myregistry-abc123.azurecr.io'),
    ('MyRegistry-ABC123.AzureCR.io:000443', 'myregistry-abc123.azurecr.io'),
])
def test_azure_cli_is_used_for_acr_login_server(registry, login_server, monkeypatch):
    calls=[]

    def fake_run(args, **kwargs):
        calls.append(args)
        return auth_module.subprocess.CompletedProcess(
            args, 0, stdout=json.dumps({
                'accessToken': 'synthetic-acr-token',
                'loginServer': login_server,
            }), stderr=''
        )

    monkeypatch.setattr(auth_module.subprocess, 'run', fake_run)
    provider=AzureCLIAuthProvider()

    assert provider.get_credentials(registry)==(
        '00000000-0000-0000-0000-000000000000', 'synthetic-acr-token'
    )
    assert provider.get_token(registry)=='synthetic-acr-token'
    assert len(calls)==2
    assert all(call[3:5]==['--name', 'myregistry'] for call in calls)

@pytest.mark.parametrize('registry, login_server', [
    ('myregistry.azurecr.io', 'otherregistry.azurecr.io'),
    ('myregistry.azurecr.io', 'myregistry-abc123.azurecr.io'),
    ('myregistry.azurecr.io', 'myregistry.azurecr.io:444'),
    ('myregistry.azurecr.io', None),
    ('myregistry-abc123.azurecr.io', 'myregistry-def456.azurecr.io'),
    ('myregistry-abc123.azurecr.io', 'myregistry.azurecr.io'),
])
def test_azure_cli_rejects_credentials_for_another_login_server(
    registry, login_server, monkeypatch, caplog
):
    def fake_run(args, **kwargs):
        return auth_module.subprocess.CompletedProcess(
            args, 0, stdout=json.dumps({
                'accessToken': 'synthetic-acr-token',
                'loginServer': login_server,
            }), stderr=''
        )

    monkeypatch.setattr(auth_module.subprocess, 'run', fake_run)
    provider=AzureCLIAuthProvider()

    assert provider.get_credentials(registry) is None
    assert provider.get_token(registry) is None
    assert 'login server' in caplog.text

@pytest.mark.parametrize('stdout', [
    '{}',
    '{"loginServer": "myregistry.azurecr.io", "accessToken": null}',
    '{"loginServer": "myregistry.azurecr.io", "accessToken": 123}',
    '[]',
    'not-json',
])
def test_azure_cli_rejects_malformed_credentials(stdout, monkeypatch, caplog):
    def fake_run(args, **kwargs):
        return auth_module.subprocess.CompletedProcess(args, 0, stdout=stdout, stderr='')

    monkeypatch.setattr(auth_module.subprocess, 'run', fake_run)

    assert AzureCLIAuthProvider().get_credentials('myregistry.azurecr.io') is None
    assert 'Azure CLI returned' in caplog.text

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
