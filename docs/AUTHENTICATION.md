# Authentication Architecture

This document describes the authentication system in pycontainer-build.

## Overview

pycontainer-build supports multiple authentication methods for pushing images to container registries. The authentication system is designed to be:

- **Flexible**: Multiple auth providers with automatic fallback
- **Secure**: No credentials stored in code, supports environment variables and external tools
- **Convenient**: Auto-discovers credentials from Docker, Azure CLI, environment

## Authentication Flow

```
Push Request
    │
    ├─> Check CLI flags (--username, --password)
    │   └─> If present: Use Basic Auth or Bearer Token
    │
    ├─> Check Environment Variables
    │   ├─> GITHUB_TOKEN (for ghcr.io)
    │   ├─> REGISTRY_TOKEN (generic bearer token)
    │   └─> REGISTRY_USERNAME + REGISTRY_PASSWORD
    │
    ├─> Check Docker Config (~/.docker/config.json)
    │   ├─> Look for registry key
    │   ├─> Decode base64 auth
    │   └─> Extract username/password
    │
    ├─> Check Azure CLI (for *.azurecr.io)
    │   └─> Run: az acr login --expose-token
    │
    └─> Make Request
        ├─> If 401 + Www-Authenticate header
        │   ├─> Parse OAuth2 challenge
        │   ├─> Exchange credentials for bearer token
        │   └─> Retry with bearer token
        └─> Success
```

## Components

### 1. Auth Providers (`src/pycontainer/auth.py`)

#### `AuthProvider` (ABC)
Base interface for authentication providers.

```python
class AuthProvider(ABC):
    def get_credentials(self, registry: str) -> Optional[Tuple[str, str]]:
        """Return (username, password/token) or None"""
        
    def get_token(self, registry: str) -> Optional[str]:
        """Return bearer token or None"""
```

#### `EnvironmentAuthProvider`
Reads credentials from environment variables:
- `REGISTRY_USERNAME` + `REGISTRY_PASSWORD`
- `GITHUB_TOKEN` (for ghcr.io)
- `REGISTRY_TOKEN` (generic)

#### `DockerConfigAuthProvider`
Reads `~/.docker/config.json`:
- Parses `auths` section
- Decodes base64 auth strings
- Handles both encoded and plain credentials
- Tries multiple registry key formats

#### `AzureCLIAuthProvider`
For Azure Container Registry (`*.azurecr.io`):
- Runs `az acr login --expose-token`
- Returns access token from Azure
- Only activates for ACR registries

#### `ChainAuthProvider`
Chains multiple providers, tries each in sequence.

### 2. Registry Client (`src/pycontainer/registry_client.py`)

#### OAuth2 Token Exchange
When the registry returns `401 Unauthorized` with `Www-Authenticate` header:

```
Www-Authenticate: Bearer realm="https://auth.registry.io/token",
                         service="registry.io",
                         scope="repository:user/app:pull,push"
```

The client:
1. Parses the challenge (realm, service, scope)
2. Makes token request to `realm` with query params
3. Includes credentials via `Authorization` header (Basic or Bearer)
4. Extracts token from response
5. Retries original request with bearer token

#### Auth Methods
- **Bearer Token**: `Authorization: Bearer <token>`
- **Basic Auth**: `Authorization: Basic <base64(username:password)>`

Both methods supported, auto-selected based on available credentials.

### 3. Builder Integration (`src/pycontainer/builder.py`)

The `push()` method:
1. Calls `get_auth_for_registry()` if no explicit credentials
2. Passes auth to `RegistryClient`
3. Client handles OAuth2 flow automatically

## Registry-Specific Behavior

### GitHub Container Registry (ghcr.io)
- Prefers `GITHUB_TOKEN` environment variable
- Username can be anything (often just "USERNAME")
- Token must have `write:packages` scope
- Supports OAuth2 token exchange

### Docker Hub (docker.io)
- Uses Docker config by default
- Supports username/password or token
- Token created at https://hub.docker.com/settings/security

### Azure Container Registry (*.azurecr.io)
- Auto-detects via `az acr login`
- Supports service principal credentials
- Supports admin username/password
- Token obtained via `--expose-token` flag

### Private/Generic Registries
- Falls back to Docker config
- Uses environment variables
- Supports Basic Auth and OAuth2

## Usage Examples

### GitHub PAT
```bash
export GITHUB_TOKEN="ghp_xxxxx"
pycontainer build --tag ghcr.io/user/app:v1 --push
```

### Docker Login
```bash
docker login ghcr.io
pycontainer build --tag ghcr.io/user/app:v1 --push
```

### Azure CLI
```bash
az login
pycontainer build --tag myregistry.azurecr.io/app:v1 --push
```

### Explicit Credentials
```bash
pycontainer build \
  --tag registry.example.com/app:v1 \
  --username myuser \
  --password mytoken \
  --push
```

### Environment Variables
```bash
export REGISTRY_USERNAME="myuser"
export REGISTRY_PASSWORD="mypass"
pycontainer build --tag registry.example.com/app:v1 --push
```

## Security Considerations

### Credential Storage
- **Environment variables**: Visible in process list
- **Docker config**: Stored unencrypted (or via credential helper)
- **Azure CLI**: Token cached by Azure CLI
- **CLI flags**: Visible in shell history

**Recommendation**: Use Docker config with credential helpers or Azure CLI for best security.

### Token Scopes
- **GitHub**: Require minimum scope (`write:packages`)
- **Docker Hub**: Use access tokens, not account password
- **ACR**: Use service principals with least privilege

### CI/CD
For CI/CD pipelines:
- Use secrets management (GitHub Secrets, Azure Key Vault)
- Set as environment variables in pipeline
- Avoid logging credentials
- Use short-lived tokens when possible

## Testing

### Unit Tests (`tests/test_auth.py`)
- Mock environment variables
- Create temporary Docker configs
- Test provider chain behavior
- Verify base64 decoding

### Integration Tests
To test live authentication (manual):

```bash
# Test GitHub
export GITHUB_TOKEN="ghp_xxxxx"
pycontainer build --tag ghcr.io/test/myapp:test --push

# Test Docker Hub
docker login
pycontainer build --tag username/myapp:test --push

# Test ACR
az login
pycontainer build --tag myregistry.azurecr.io/myapp:test --push
```

## Troubleshooting

### "401 Unauthorized"
- Check token has correct permissions
- Verify token is for correct registry
- Try `docker login` to verify credentials work

### "No Location header"
- Registry may not support monolithic uploads
- Check registry version (requires v2 API)

### Azure CLI Timeout
- Run `az acr login --name myregistry` manually first
- Check Azure subscription is active
- Verify registry name is correct

### Docker Config Not Found
- Check `~/.docker/config.json` exists
- Run `docker login` to create it
- Verify registry key matches (try variations)

## Future Enhancements

### Phase 1.4+
- Credential refresh for long-running operations
- Support for Docker credential helpers
- Token caching to avoid repeated OAuth2 exchanges
- GitHub App JWT authentication
- Workload identity for cloud environments
