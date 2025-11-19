# Registry Push Examples

This document shows how to use pycontainer-build to push images to various container registries.

## Prerequisites

- pycontainer-build installed: `pip install -e .`
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

```bash
# No authentication needed for local registry
pycontainer build \
  --tag localhost:5000/myapp:test \
  --push
```

### Verify Push

```bash
# List repositories
curl http://localhost:5000/v2/_catalog

# List tags
curl http://localhost:5000/v2/myapp/tags/list
```

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
