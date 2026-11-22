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
    │   ├─> Username + password: Use Basic Auth
    │   └─> Password only: Use it as a bearer token
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
        │   │   ├─> Exchange Basic credentials or bearer token for a registry token
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
- `REGISTRY_USERNAME` + `REGISTRY_PASSWORD` (Basic Auth)
- `GITHUB_TOKEN` (for ghcr.io)
- `REGISTRY_TOKEN` (generic)

#### `DockerConfigAuthProvider`
Reads `~/.docker/config.json`:
- Parses `auths` section
- Decodes base64 auth strings
- Handles both encoded and plain credentials
- Tries multiple registry key formats
- Returns decoded username/password as Basic credentials, never as a bearer token

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
1. Resolves explicit CLI arguments or the environment, Docker config and Azure CLI providers
2. Preserves bearer tokens and username/password as distinct credential types
3. Passes credentials to `RegistryClient`, which handles the OAuth2 challenge flow

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
  --password mypassword \
  --push
```

The username and password are sent using Basic authentication. A password/token
without `--username` is treated as a bearer token. Supplying `--username`
without either `--password` or `REGISTRY_PASSWORD` is an error.

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
- Credentials are not written to application logs. Avoid passing secrets on
  command lines in shared or recorded shells.

**Recommendation**: Use Docker config with credential helpers or Azure CLI for best security.

### Transport Security
- Registry traffic is HTTPS with certificate verification by default.
  `--registry-ca-file` adds a trusted CA; it never disables verification.
- `--insecure-registry HOST[:PORT]` allows plain HTTP for that exact host.
  Credentials are sent over HTTP only to loopback hosts; credentials for any
  other HTTP registry, or a token challenge from one, fail with
  `RegistryAuthenticationError` before secrets leave the process.
- HTTPS-to-HTTP redirects and HTTP token realms from HTTPS registries are
  rejected. `Authorization`, `Proxy-Authorization` and `Cookie` are dropped on
  cross-origin redirects; blob storage redirects are credential-free.
- Embedded URL credentials are rejected and error URLs omit userinfo/query
  secrets. Registry connections ignore environment/system proxies.
- Concurrent initial bearer challenges share token acquisition and replay once;
  rejection of the current token is reported without indefinite refresh. See
  [REGISTRY_PUSH.md](REGISTRY_PUSH.md#insecure-http-registries-and-custom-cas).
- Base-image mirrors (`--registry-mirror`) resolve credentials with
  `get_mirror_auth`, which uses only host-scoped sources (the mirror's own
  Docker config entry, Azure CLI for `*.azurecr.io`, `GITHUB_TOKEN` for
  `ghcr.io`). Generic `REGISTRY_*` variables, explicit flags, and upstream
  credentials are never sent to a mirror. See
  [REGISTRY_PUSH.md](REGISTRY_PUSH.md#base-image-registry-mirrors).

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
