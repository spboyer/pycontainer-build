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

```bash
# Set token
export GITHUB_TOKEN="ghp_your_token_here"

# Build and push
pycontainer build \
  --tag ghcr.io/your-username/myapp:v1.0.0 \
  --push

# Or use --registry to override
pycontainer build \
  --tag myapp:v1.0.0 \
  --registry ghcr.io/your-username/myapp:v1.0.0 \
  --push
```

## Docker Hub

### Using Environment Variables

```bash
export REGISTRY_TOKEN="your_dockerhub_token"

pycontainer build \
  --tag docker.io/your-username/myapp:latest \
  --push
```

### Using Docker Login Credentials

```bash
# Login with docker first
docker login

# pycontainer will read ~/.docker/config.json
pycontainer build --tag your-username/myapp:latest --push
```

## Azure Container Registry (ACR)

### Using Azure CLI

```bash
# Login to Azure
az login
az acr login --name myregistry

# Build and push
pycontainer build \
  --tag myregistry.azurecr.io/myapp:v1 \
  --push
```

### Using Service Principal

```bash
export REGISTRY_TOKEN="your_service_principal_password"

pycontainer build \
  --tag myregistry.azurecr.io/myapp:v1 \
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

## Coming Soon (Phase 1.3)

- Automatic Docker config.json credential reading
- Azure CLI integration for ACR
- Multiple authentication provider support
- Token refresh for long-running operations
