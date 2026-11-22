# Registry Push Examples

This document shows how to use pycontainer-build to push images to various container registries.

## Prerequisites

- pycontainer-build installed from the repository checkout root:
  `python -m pip install --no-deps -e .`
- Authentication tokens for your target registry

## GitHub Container Registry (GHCR)

### 1. Generate GitHub Personal Access Token

Go to GitHub Settings → Developer settings → Personal access tokens → Tokens (classic)

Generate a token with `write:packages` scope.

### 2. Build and Push

**Option 1: Environment Variable (Automatic)**

```bash
# Set token - automatically detected
export GITHUB_TOKEN="ghp_your_token_here"

# Build and push - no auth flags needed!
pycontainer build \
  --tag ghcr.io/your-username/myapp:v1.0.0 \
  --push
```

**Option 2: Command Line Flags**

```bash
# Pass credentials directly
pycontainer build \
  --tag ghcr.io/your-username/myapp:v1.0.0 \
  --password "ghp_your_token_here" \
  --push
```

**Option 3: Use Docker Login**

```bash
# Login with docker first
echo "ghp_your_token_here" | docker login ghcr.io -u YOUR_USERNAME --password-stdin

# pycontainer will automatically read ~/.docker/config.json
pycontainer build --tag ghcr.io/your-username/myapp:v1.0.0 --push
```

## Docker Hub

### Automatic via Docker Login (Recommended)

```bash
# Login with docker (credentials saved to ~/.docker/config.json)
docker login

# pycontainer automatically reads the credentials
pycontainer build --tag your-username/myapp:latest --push
```

### Environment Variables

```bash
export REGISTRY_USERNAME="your_dockerhub_username"
export REGISTRY_PASSWORD="your_dockerhub_token"

pycontainer build \
  --tag docker.io/your-username/myapp:latest \
  --push
```

### Command Line Flags

```bash
pycontainer build \
  --tag your-username/myapp:latest \
  --username your_dockerhub_username \
  --password your_dockerhub_token \
  --push
```

## Azure Container Registry (ACR)

### Automatic via Azure CLI (Recommended)

```bash
# Login to Azure
az login

# pycontainer automatically gets tokens via 'az acr login --expose-token'
pycontainer build \
  --tag myregistry.azurecr.io/myapp:v1 \
  --push
```

### Using Service Principal

```bash
export REGISTRY_USERNAME="your_service_principal_id"
export REGISTRY_PASSWORD="your_service_principal_password"

pycontainer build \
  --tag myregistry.azurecr.io/myapp:v1 \
  --push
```

### Using Admin Credentials

```bash
# Get admin password from Azure portal or CLI
pycontainer build \
  --tag myregistry.azurecr.io/myapp:v1 \
  --username myregistry \
  --password "admin_password_from_portal" \
  --push
```

## Local Registry (for Testing)

### Start Local Registry

```bash
docker run -d -p 5000:5000 --name registry registry:2
```

### Push to Local Registry

The `registry:2` container serves plain HTTP, so opt that exact host in:

```bash
# No authentication needed for local registry
pycontainer build \
  --tag localhost:5000/myapp:test \
  --insecure-registry localhost:5000 \
  --push
```

Without `--insecure-registry`, pycontainer uses HTTPS and the push fails
with a TLS/network error.

### Verify Push

```bash
# List repositories
curl http://localhost:5000/v2/_catalog

# List tags
curl http://localhost:5000/v2/myapp/tags/list
```

## Insecure (HTTP) Registries and Custom CAs

Registry traffic uses HTTPS with certificate verification by default. Two
explicit settings change how a registry is reached; neither disables
certificate verification, and there is no skip-verify option.

### Plain HTTP for specific hosts

```bash
pycontainer build --tag registry.lan:5000/team/app:dev \
  --insecure-registry registry.lan:5000 --push
```

```toml
[build]
insecure-registries = ["localhost:5000", "registry.lan:5000"]
```

- Entries are exact `host[:port]` values with no scheme, path, or
  credentials (IPv6 literals must be bracketed, e.g. `[::1]:5000`). Matching
  is case-insensitive and must equal the registry in the image reference,
  including the port. Invalid entries are configuration errors.
- Listed hosts use HTTP only; there is no HTTPS probe or fallback. Unlisted
  hosts keep HTTPS and fail with the usual TLS/network error if the server
  only speaks HTTP.
- Each build logs `WARNING: using plain HTTP for insecure registry ...` once
  per insecure host that it contacts (base-image pull or push).
- **Credentials are never sent over HTTP to non-loopback hosts.** Only
  `localhost`, `127.0.0.0/8`, `::1`, and IPv4-mapped loopback literals may
  receive Basic or bearer credentials over HTTP. For any other HTTP registry,
  configured credentials (CLI, environment, Docker config, or Azure CLI) cause
  an explicit `RegistryAuthenticationError`, and a token challenge from that
  registry is refused before any token request is made. Such registries work
  only anonymously.
- HTTP traffic is unencrypted and unauthenticated: anyone on the network path
  can read or alter image content. Use it only on trusted networks.

`--insecure-registry` on the command line replaces, rather than extends, the
`[build]` list.

### Additional CA for private HTTPS registries

```bash
pycontainer build --tag registry.corp:443/team/app:1.0 \
  --registry-ca-file ./corp-root-ca.pem --push
```

```toml
[build]
registry-ca-file = "corp-root-ca.pem"
```

- The PEM bundle is added to the system trust store for all registry HTTPS
  traffic in the build: registry API calls, token realms, and blob storage
  redirects. Hostname and chain verification remain enabled.
- Relative paths resolve from the current working directory. A missing,
  unreadable, or invalid file is a configuration error.
- There is one bundle per build; per-registry CA mapping and client
  certificates (mTLS) are not supported.

### Redirect and transport rules

- HTTPS responses may not redirect to HTTP (manifests, blobs, token realms,
  and upload locations); the downgrade is rejected before the HTTP target is
  contacted.
- An HTTPS registry must advertise an HTTPS token realm.
- `Authorization` is removed when a redirect changes scheme, host, or port;
  blob storage redirects are always fetched without registry credentials.

### Limitations

- Tests cover an in-process loopback Registry v2 stub over HTTP and a
  test-generated CA over HTTPS. Compatibility with specific registry products
  (for example `registry:2`, Harbor, Nexus, or Artifactory) over HTTP or with
  private CAs has not been verified in CI.
- The Poetry, Hatch, and VS Code integrations do not expose these options;
  use the CLI or `pycontainer.toml`.
- Standard proxy environment variables apply to HTTP registries as they do
  to HTTPS ones.

## Base-Image Registry Mirrors

Base-image pulls can go through pull-through mirrors before the upstream
registry. Mirrors never apply to `--push`, which always goes straight to the
registry named in `--tag`.

```bash
pycontainer build --tag ghcr.io/me/app:v1 --base-image python:3.11-slim \
  --registry-mirror docker.io=mirror.example.com \
  --registry-mirror docker.io=backup.example.com:5000
```

```toml
[build.registry_mirrors]
"docker.io" = ["mirror.example.com", "backup.example.com:5000"]
"ghcr.io" = ["ghcr-mirror.example.com"]
```

- Keys and values are `host[:port]` only: no scheme, path, or credentials.
  `docker.io`, `index.docker.io`, and `registry-1.docker.io` are the same
  upstream. A mirror keeps the upstream repository path; Docker Hub
  official images keep the `library/` prefix (`library/python`). Path-prefixed
  mirrors (for example Harbor proxy projects) are not supported.
- Mirrors are tried in order, then the upstream. `--registry-mirror` for an
  upstream replaces that upstream's list from `pycontainer.toml`; lists for
  other upstreams are kept.
- Mirrors use the same transport as any registry: HTTPS with
  `--registry-ca-file` trust, or plain HTTP only when the mirror host is also
  listed in `--insecure-registry`.
- `--verbose` names the host(s) that served the pull (`Served by: ...`).

### Fallback rules

| Mirror response | Behavior |
| --- | --- |
| Connection failure (including TLS verification and an unreachable token realm) | Warn; skip this mirror for the rest of the pull |
| HTTP 5xx after the normal retry budget (registry or token realm) | Warn; skip this mirror for the rest of the pull |
| HTTP 401/403 (including token exchange) | Warn about mirror credentials; skip this mirror for the rest of the pull |
| HTTP 404 | Warn; try the next host for this object only |
| HTTP 429 or quota errors | Fail; no fallback |
| Digest or size mismatch | Fail closed; no fallback, nothing published |
| Refused credentials or token realm (for example credentials over HTTP) | Fail; no fallback |

An error from the upstream, after every mirror is exhausted, is reported as
it would be without mirrors.

### Credentials and integrity

- Each mirror gets credentials only for its own host: its exact Docker
  config entry, Azure CLI for `*.azurecr.io`, or `GITHUB_TOKEN` for
  `ghcr.io`. `REGISTRY_TOKEN`, `REGISTRY_USERNAME`/`REGISTRY_PASSWORD`,
  `--registry-*` flags, and upstream credentials are never sent to a mirror.
  Upstream credentials are resolved only if the upstream is actually
  contacted.
- Manifests requested by digest and every config/layer blob are verified
  against their descriptor digest and size, whichever host serves them.
- A tag is resolved by the mirror that serves it. Pin base images by digest
  (`python:3.11-slim@sha256:...`) if a mirror should not be trusted to
  resolve tags.

Tests use separate in-process loopback registries with synthetic
credentials; no real mirror product (Docker Hub mirror, Harbor, Nexus,
Artifactory, ACR cache rules) has been verified in CI.

## Programmatic Usage

```python
from pycontainer.builder import ImageBuilder
from pycontainer.config import BuildConfig
import os

# Configure build
config = BuildConfig(
    tag="ghcr.io/user/myapp:v1.0.0",
    context_dir="/path/to/app",
    env={"ENV": "production"}
)

# Build image
builder = ImageBuilder(config)
builder.build()

# Push to registry
auth_token = os.getenv('GITHUB_TOKEN')
builder.push(auth_token=auth_token, show_progress=True)
```

## Progress Output

When pushing, pycontainer shows progress:

```
Pushing to ghcr.io/user/myapp:v1
  Pushing layer 1/1 (sha256:787962fa701...)
  Pushing config (sha256:4988c2ac0a9...)
  Pushing manifest (sha256:f1cd9542ad3...)
✓ Pushed ghcr.io/user/myapp:v1
```

Use `--no-progress` to suppress:

```bash
pycontainer build --tag myapp:v1 --push --no-progress
```

## Troubleshooting

### Authentication Errors

If you see `401 Unauthorized`:
- Verify your token has correct permissions
- Check token is set in environment or ~/.docker/config.json
- For GHCR, ensure token has `write:packages` scope

### Network Errors

If you see connection timeouts:
- Check registry URL is correct
- Verify network connectivity
- Try with `--verbose` flag (coming in Phase 1.4)

### Blob Already Exists

This is normal! pycontainer checks if blobs exist before uploading:
```
  Pushing layer 1/1 (sha256:787962fa701...)
    Layer exists, skipped
```

This makes subsequent pushes much faster.

### Registry errors, rate limits and quotas

Registry failures report the operation, registry host, HTTP status and the
OCI distribution error codes from the response body, for example:

```text
Manifest pull failed with HTTP 429 from registry-1.docker.io: TOOMANYREQUESTS:
You have reached your pull rate limit. The registry rate limit was exceeded;
it resets in about 3600 seconds (at 2026-11-20T18:00:00Z). Rate limit: 0 of
100 remaining. Authenticate to the registry ...
```

- **`TOOMANYREQUESTS` / HTTP 429** (`RegistryRateLimitError`): the reset time
  comes from `Retry-After`. Sign in (Docker Hub grants higher pull limits to
  authenticated users), wait for the reset, or use a registry mirror. Only
  safe reads (`GET`/`HEAD`) are retried, at most twice with delays capped at
  5 seconds; when `Retry-After` is longer than that cap the build fails
  immediately instead of retrying into the same limit. Uploads, upload
  initiation and manifest `PUT`s are never retried.
- **HTTP 413, 507, `*QUOTA*` codes or quota messages**
  (`RegistryQuotaError`): the error names the rejected upload size. Free space
  in the repository, raise its quota or reduce the image size.
- **`DENIED` / `UNAUTHORIZED` / HTTP 401 or 403**: check that the credentials
  can pull or push the repository. Some registries also report exhausted
  quotas as `DENIED`.

Run with `--verbose` to log Docker Hub style `RateLimit-Limit` and
`RateLimit-Remaining` headers. A warning is printed once per registry client
when the remaining count drops to 10 (or 10% of a smaller limit) or fewer.

Error bodies are treated as untrusted: at most five errors are shown, each
message is truncated, control characters are removed, and authorization
values, tokens and the configured credentials are replaced with `[REDACTED]`.

## Authentication Priority

pycontainer tries authentication methods in this order:

1. **Command line flags** (`--username`, `--password`)
2. **Environment variables** (`GITHUB_TOKEN`, `REGISTRY_USERNAME`, `REGISTRY_PASSWORD`)
3. **Docker config** (`~/.docker/config.json`)
4. **Azure CLI** (for ACR registries via `az acr login --expose-token`)

This means you can:
- Override any auto-detected credentials with CLI flags
- Set environment variables for CI/CD
- Use `docker login` for local development
- Seamlessly integrate with Azure CLI

## Coming Soon (Phase 1.4)

- Layer caching for faster incremental builds
- Content-addressable storage with cache eviction
- `--no-cache` flag to force full rebuild
