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
    │   ├─> GITHUB_TOKEN (only for ghcr.io)
    │   ├─> REGISTRY_TOKEN (generic bearer token)
    │   └─> REGISTRY_USERNAME + REGISTRY_PASSWORD
    │
    ├─> Check Docker Config (~/.docker/config.json)
    │   ├─> Look for registry key
    │   ├─> Decode base64 auth
    │   └─> Extract username/password
    │
    ├─> Check Azure CLI (only for an ACR login server)
    │   └─> Run: az acr login --expose-token
    │
    └─> Make Request
        ├─> If 401 + Www-Authenticate header
        │   ├─> Parse OAuth2 challenge
        │   │   ├─> Exchange Basic credentials or bearer token for a registry token
        │   └─> Retry with bearer token
        └─> Success
```

### Remote cache requests

Remote-cache import and export resolve credentials for the cache registry,
independently of image push. They use `get_registry_auth` with
`include_generic_environment=False`: `REGISTRY_TOKEN`, `REGISTRY_USERNAME`
and `REGISTRY_PASSWORD` are ignored because they carry no registry identity,
including when the cache and image registry are the same host. CLI image-push
credentials are not passed to the cache.

Host-scoped Docker configuration, GHCR's `GITHUB_TOKEN` and ACR's Azure CLI
provider remain available. For other private cache registries, configure an
exact registry entry in `~/.docker/config.json`. Without usable cache
credentials, an unauthorized import warns and rebuilds locally; an
unauthorized export fails explicitly. Normal image authentication continues
to accept generic environment credentials. A registry's authentication
challenge can direct credential exchange to a token service on another origin;
trust both the cache registry and its advertised token service.
Cache connections honor `--registry-ca-file` and the exact-host
`--insecure-registry` opt-in. Credentials are sent over HTTP only to loopback
hosts. Blob redirects never carry registry credentials and every hop must use
HTTPS, except that an exact-host `--insecure-registry` may redirect over HTTP
to its own origin (same scheme, host and port). Cross-origin HTTP hops,
HTTPS-to-HTTP downgrades and URL credentials are rejected on every hop.

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
- `GITHUB_TOKEN` (only for the `ghcr.io` registry)
- `REGISTRY_TOKEN` (generic)

The generic `REGISTRY_*` variables are not scoped to a registry. Pass
`include_generic_environment=False` to `EnvironmentAuthProvider`,
`get_default_auth_provider` or `get_registry_auth` to ignore them while keeping
`GITHUB_TOKEN` for ghcr.io; base-image pulls do this.

#### Registry host scoping
Provider-specific credentials are selected only when the registry is a bare
`host` authority, optionally with a numeric HTTPS port of 443 (including
zero-padded forms such as `:0443`). Hostnames are compared case-insensitively
and must consist of valid DNS labels; schemes, paths, user information, trailing
dots, other ports and other malformed authorities never match.

- `GITHUB_TOKEN` is used only for exactly `ghcr.io`. Hosts such as
  `ghcr.io.example.com` or `evilghcr.io` do not receive it.
- Azure CLI runs only for an ACR login server named `<name>.azurecr.io` or
  `<name>-<suffix>.azurecr.io`. The registry resource name is 5-50 ASCII
  letters or digits, and the optional suffix accommodates domain-name-label
  protected registries. The provider passes the resource name to
  `az acr login --name` and accepts the token only if the returned
  `loginServer` matches the requested host.

Explicit CLI credentials and `REGISTRY_USERNAME` + `REGISTRY_PASSWORD` remain
host-agnostic, subject to provider precedence. `REGISTRY_TOKEN` is available
for non-GHCR registries; for GHCR, the environment provider uses `GITHUB_TOKEN`
instead, even when it is unset. Docker config credentials use registry keys.

#### `DockerConfigAuthProvider`
Reads `~/.docker/config.json`:
- Parses `auths` section
- Decodes base64 auth strings
- Handles both encoded and plain credentials
- Tries multiple registry key formats
- Returns decoded username/password as Basic credentials, never as a bearer token

#### `AzureCLIAuthProvider`
For Azure Container Registry login servers (optionally with HTTPS port 443):
- Runs `az acr login --expose-token`
- Returns the exposed token only when the login server matches
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

Bearer-token exchanges, authenticated request replays and blob redirects run
after leaving the original `HTTPError` handler, so subsequent failures do not
retain its potentially credential-bearing reason in their exception chain.
Token responses that are not valid UTF-8 JSON raise an authentication error.
Upload replays restore the body's original stream offset before retrying;
rewind failures propagate outside that handler too.

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

### Azure Container Registry (`<name>.azurecr.io`)
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

### Private Base Images
Base-image pulls use only registry-scoped credentials. Generic
`REGISTRY_TOKEN`, `REGISTRY_USERNAME` and `REGISTRY_PASSWORD` values, and the
`--username`/`--password` flags, are reserved for the destination push and are
never sent to the base registry or its token endpoint. This applies to every
target in a multi-platform image-index build. Authenticate a private
base independently of the destination with an inline `auths` entry for the base
registry host in `~/.docker/config.json` (credential helpers and `credsStore`
are not read). The host-scoped `GITHUB_TOKEN` (ghcr.io) and Azure CLI
(`*.azurecr.io`) providers remain enabled for base pulls, with the same behavior
as for pushes.

```bash
# ~/.docker/config.json contains an inline auths entry for base.example.com
export REGISTRY_USERNAME="push-user"
export REGISTRY_PASSWORD="push-password"
pycontainer build \
  --base-image base.example.com/team/python:3.11 \
  --tag registry.example.com/app:v1 \
  --push
```

## Private Package Indexes

With `--include-deps` and a requirements file, the builder runs the host
interpreter's `pip` to download target wheels. Index credentials are read by
pip on the build host only; they are not passed through pycontainer
configuration. This section covers that path; registry authentication above is
separate.

### Supported credential sources

| Source | How to use it |
|--------|---------------|
| Environment | `PIP_INDEX_URL` / `PIP_EXTRA_INDEX_URL` containing `https://user:token@host/simple` |
| pip config file | `index-url` / `extra-index-url` in a pip config file outside the build context, or selected with `PIP_CONFIG_FILE` |
| netrc | `~/.netrc` (or the file named by `NETRC`) with a `machine <host>` entry; the index URL carries no credentials |
| keyring | `PIP_KEYRING_PROVIDER=import` (keyring importable by the builder's Python) or `subprocess` (a `keyring` executable on `PATH`; put the username in the URL, e.g. `https://user@host/simple`). pip runs with `--no-input`, so the default `auto` provider does not query keyring |
| Placeholders | `--index-url https://${INDEX_USER}:${INDEX_TOKEN}@host/simple` in the requirements file; pip expands `${NAME}` (uppercase letters, digits, `_`) from the builder's environment |

### What the builder enforces

- **Literal credentials are rejected.** Every file in the requirements graph
  (`-r` and `-c` includes, comments included) is scanned before pip runs. URLs
  with a password, a bare username or token (other than `git@`), or a query
  parameter named like `token`, `key`, `secret`, `password`, `auth`, `sig` or
  `credential` fail the build unless the value is only `${VAR}`
  placeholders. URL path segments are also checked: known token
  formats (GitHub `ghp_`/`github_pat_`, GitLab `glpat-`, PyPI `pypi-AgE…`,
  Hugging Face `hf_`), the first path segment on `dl.cloudsmith.io` other than
  `public` or `basic`, and, except on public source hosts such as `github.com`
  and `pypi.org`, a whole path segment of 20+ alphanumerics that mixes upper
  case, lower case and digits with frequent changes between them. Userinfo or
  path segments made only of `${VAR}`
  placeholders, or a literal user with a `${VAR}` password, are allowed; pip
  expands placeholders anywhere on a requirements line, so
  `https://dl.cloudsmith.io/${CLOUDSMITH_TOKEN}/org/repo/python/simple/` works.
  The error names the file, line and URL scheme and host only.
- **Shipped pip inputs are checked even without `--include-deps`.** Before the
  application layer is created (and before any cache lookup), included
  `requirements*.txt|.in`, `constraints*.txt|.in`, `pip.conf`, `pip.ini` and
  requirements-graph files are scanned the same way, and `.netrc` / `_netrc`
  files are rejected. A host `PIP_CONFIG_FILE` or `NETRC` that points inside
  the context is treated the same way, whatever its file name. Files over
  1 MiB are rejected rather than partially scanned. Exclude them
  from the context or use placeholders.
- **Host pip settings cannot change target selection.** Only these `PIP_*`
  variables, and the matching pip config-file keys, reach pip:
  `PIP_CERT`, `PIP_CLIENT_CERT`, `PIP_CONFIG_FILE`, `PIP_DEFAULT_TIMEOUT`,
  `PIP_EXTRA_INDEX_URL`, `PIP_FIND_LINKS`, `PIP_INDEX_URL`,
  `PIP_KEYRING_PROVIDER`, `PIP_NO_INDEX`, `PIP_PROXY`, `PIP_REQUIRE_HASHES`,
  `PIP_RETRIES`, `PIP_TIMEOUT` and `PIP_TRUSTED_HOST`. Others, such as
  `PIP_PLATFORM`, `PIP_ONLY_BINARY`, `PIP_NO_BINARY`, `PIP_TARGET`,
  `PIP_PREFIX` or `PIP_USER`, are ignored; their names (never values) are
  printed. pip `append` options combine config and command-line values, so
  command-line flags alone would not pin the target. A self-test refuses to
  install if pip's configuration hook changes. Standard `HTTP(S)_PROXY`,
  `NO_PROXY`, `REQUESTS_CA_BUNDLE` and `SSL_CERT_FILE` variables still apply.
  pip runs with `python -E`, so `PYTHONPATH` and `PYTHONHOME` cannot replace
  the pinned pip.
- **Only installed packages ship.** The dependency layer contains the staged
  `site-packages` and generated console scripts. pip's stderr and debug log are
  kept in host temporary files outside the staged tree. Installed
  `direct_url.json` metadata with URL credentials fails pip-staged builds;
  virtualenv staging rejects all `direct_url.json` metadata.
- **Errors are classified and redacted.** HTTP 401/403 failures name the
  supported credential sources, HTTP 407 points at proxy credentials, and
  connection or TLS failures say so. Excerpts are limited to a few pip lines
  with URL userinfo, secret-like query values, detected URL path tokens, known
  environment URL credentials and expanded placeholder values replaced by
  `****`.

### Limits

- Detection is pattern-based and best effort. The random-looking-segment check
  misses a share of genuinely random tokens (in a synthetic sample, roughly
  16% of 20-character and 7% of 40-character alphanumeric tokens), and it
  ignores short, word-like, single-case or all-hex values. Path tokens in
  positions without a host rule (for example Gemfury's
  `pypi.fury.io/<token>/<user>/`) and credentials in other source files (for
  example application config) may not be detected. Use `${VAR}` placeholders
  for any URL path secret and keep secrets out of the build context.
- Redaction covers values the builder can see: URL userinfo, secret-like query
  parameters, detected URL path tokens, `PIP_*`/proxy URL credentials and
  placeholder values. Values pip reads from config files, netrc or keyring are
  redacted only where they appear in those URL positions; pip itself also
  masks passwords in URLs it logs.
- pip, keyring backends and the index are trusted. A malicious index or
  package is out of scope.
- The tests use a loopback basic-auth index, a loopback index authenticated by
  a URL path token, and synthetic secrets (`tests/test_private_index.py`);
  they verify that those secrets do not
  appear in the OCI layout blobs (decompressed), SBOM, layer cache, standard
  output or errors. A second loopback origin on a different hostname verifies
  that index credentials (environment userinfo, netrc and placeholders) are not
  sent to an artifact host that the index links or redirects to. Credentials
  configured for that artifact host itself would still be sent. The tests do
  not cover every index product or authentication scheme.

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
  cross-origin redirects; blob storage redirects are credential-free on every
  hop and use HTTPS unless they stay on an explicitly insecure registry's own
  origin.
- Embedded URL credentials are rejected and error URLs omit userinfo/query
  secrets. Registry connections ignore environment/system proxies.
- Concurrent initial bearer challenges share token acquisition and replay once;
  rejection of the current token is reported without indefinite refresh. See
  [REGISTRY_PUSH.md](REGISTRY_PUSH.md#insecure-http-registries-and-custom-cas).

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
