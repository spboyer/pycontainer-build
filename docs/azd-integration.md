# Azure Developer CLI (azd) Integration

This guide explains how to use pycontainer-build with Azure Developer CLI (azd) to deploy Python applications to Azure without requiring Docker.

## Overview

Azure Developer CLI (azd) is a tool for deploying applications to Azure. By integrating pycontainer-build, you can:

- Deploy Python apps to Azure Container Apps without Docker installed
- Build container images natively using pure Python
- Simplify local development in cloud environments (Codespaces, DevBox)
- Use the same workflow from development to production

## Prerequisites

- [Azure Developer CLI](https://learn.microsoft.com/en-us/azure/developer/azure-developer-cli/install-azd) installed
- Python 3.11+
- Azure subscription
- pycontainer-build installed: `pip install pycontainer-build`

## Quick Start

### 1. Initialize Your Project

If you have an existing Python project:

```bash
# Initialize azd in your project
azd init

# Select "Use code in the current directory"
# Choose "Python (Container App)" as the template
```

### 2. Configure azure.yaml

Create or update your `azure.yaml` file:

```yaml
name: myapp
metadata:
  template: python-container-app

services:
  api:
    project: ./src
    language: python
    host: containerapp
    hooks:
      prebuild:
        shell: sh
        run: pip install pycontainer-build
      build:
        shell: sh
        run: |
          pycontainer build \
            --tag ${SERVICE_IMAGE_NAME}:${SERVICE_IMAGE_TAG} \
            --include-deps \
            --context ${SERVICE_PATH} \
            --push
```

> **Note**: The `--base-image` flag is optional. pycontainer-build will auto-detect the Python version from your `pyproject.toml` (`requires-python` field) and use the appropriate base image (e.g., `python:3.11-slim`). You can override this by explicitly setting `--base-image python:3.12-slim` or any custom base image.

### 3. Deploy

```bash
# Deploy to Azure
azd up

# This will:
# 1. Install pycontainer-build
# 2. Build your container image (without Docker!)
# 3. Push to Azure Container Registry
# 4. Deploy to Azure Container Apps
```

## Configuration Examples

### Basic FastAPI App

```yaml
name: fastapi-app

services:
  api:
    project: ./src
    language: python
    host: containerapp
    hooks:
      prebuild:
        shell: sh
        run: pip install pycontainer-build
      build:
        shell: sh
        run: |
          pycontainer build \
            --tag ${SERVICE_IMAGE_NAME}:${SERVICE_IMAGE_TAG} \
            --base-image python:3.11-slim \
            --include-deps \
            --push \
            --verbose
        env:
          - REGISTRY_URL: ${AZURE_CONTAINER_REGISTRY_ENDPOINT}
```

### Multi-Service Application

```yaml
name: microservices-app

services:
  # API service
  api:
    project: ./services/api
    language: python
    host: containerapp
    hooks:
      prebuild:
        shell: sh
        run: pip install pycontainer-build
      build:
        shell: sh
        run: |
          pycontainer build \
            --tag ${SERVICE_IMAGE_NAME}:${SERVICE_IMAGE_TAG} \
            --base-image python:3.11-slim \
            --context ${SERVICE_PATH} \
            --include-deps \
            --push
  
  # Worker service
  worker:
    project: ./services/worker
    language: python
    host: containerapp
    hooks:
      prebuild:
        shell: sh
        run: pip install pycontainer-build
      build:
        shell: sh
        run: |
          pycontainer build \
            --tag ${SERVICE_IMAGE_NAME}:${SERVICE_IMAGE_TAG} \
            --base-image python:3.11-slim \
            --context ${SERVICE_PATH} \
            --include-deps \
            --push
```

### With SBOM for Security Compliance

```yaml
name: secure-app

services:
  api:
    project: ./src
    language: python
    host: containerapp
    hooks:
      prebuild:
        shell: sh
        run: pip install pycontainer-build
      build:
        shell: sh
        run: |
          pycontainer build \
            --tag ${SERVICE_IMAGE_NAME}:${SERVICE_IMAGE_TAG} \
            --base-image python:3.11-slim \
            --include-deps \
            --sbom spdx \
            --push \
            --verbose
```

## Environment Variables

The following environment variables are available in azd hooks:

- `SERVICE_IMAGE_NAME` - Full image name (e.g., `myregistry.azurecr.io/myapp`)
- `SERVICE_IMAGE_TAG` - Image tag (e.g., `azd-deploy-1234567890`)
- `SERVICE_PATH` - Path to the service directory
- `AZURE_CONTAINER_REGISTRY_ENDPOINT` - ACR endpoint

You can also set custom environment variables:

```yaml
hooks:
  build:
    shell: sh
    run: pycontainer build --tag ${SERVICE_IMAGE_NAME}:${SERVICE_IMAGE_TAG} --push
    env:
      - PYCONTAINER_BASE_IMAGE: python:3.11-slim
      - PYCONTAINER_INCLUDE_DEPS: "true"
```

## Authentication

### Azure Container Registry (ACR)

azd automatically handles ACR authentication. pycontainer-build will use:

1. Azure CLI credentials (`az acr login`)
2. Environment variables set by azd
3. Docker config if available

No additional configuration needed!

### Custom Registry

If using a custom registry:

```yaml
hooks:
  build:
    shell: sh
    run: pycontainer build --tag myregistry.io/myapp:${SERVICE_IMAGE_TAG} --push
    env:
      - REGISTRY_USERNAME: ${REGISTRY_USERNAME}
      - REGISTRY_PASSWORD: ${REGISTRY_PASSWORD}
```

## Project Structure

Recommended project structure for azd integration:

```
myapp/
├── azure.yaml              # azd configuration
├── infra/                  # Azure infrastructure (Bicep)
│   └── main.bicep
├── src/                    # Application code
│   ├── pyproject.toml
│   ├── requirements.txt
│   └── app/
│       ├── __init__.py
│       └── main.py
└── .azure/                 # azd state (auto-generated)
```

## Advanced Configuration

### Using pycontainer.toml

For complex builds, use a configuration file:

```yaml
# azure.yaml
hooks:
  build:
    shell: sh
    run: |
      pycontainer build \
        --config pycontainer.toml \
        --tag ${SERVICE_IMAGE_NAME}:${SERVICE_IMAGE_TAG} \
        --push
```

```toml
# pycontainer.toml
[build]
base_image = "python:3.11-slim"
workdir = "/app"
include_deps = true
reproducible = true

[build.env]
ENV = "production"
PORT = "8080"

[build.labels]
maintainer = "team@example.com"
version = "1.0.0"
```

### Custom Base Images

```yaml
hooks:
  build:
    shell: sh
    run: |
      # Use distroless for smaller images
      pycontainer build \
        --tag ${SERVICE_IMAGE_NAME}:${SERVICE_IMAGE_TAG} \
        --base-image gcr.io/distroless/python3-debian12 \
        --include-deps \
        --push
```

### Development vs Production Builds

```yaml
hooks:
  build:
    shell: sh
    run: |
      if [ "${AZURE_ENV_NAME}" = "production" ]; then
        pycontainer build \
          --tag ${SERVICE_IMAGE_NAME}:${SERVICE_IMAGE_TAG} \
          --base-image python:3.11-slim \
          --include-deps \
          --sbom spdx \
          --push
      else
        pycontainer build \
          --tag ${SERVICE_IMAGE_NAME}:${SERVICE_IMAGE_TAG} \
          --base-image python:3.11 \
          --include-deps \
          --push
      fi
```

## Local Development

Test your azd configuration locally:

```bash
# Install pycontainer-build
pip install pycontainer-build

# Test the build locally
export SERVICE_IMAGE_NAME="localhost:5000/myapp"
export SERVICE_IMAGE_TAG="dev"
export SERVICE_PATH="./src"

# Run the build command
pycontainer build \
  --tag ${SERVICE_IMAGE_NAME}:${SERVICE_IMAGE_TAG} \
  --base-image python:3.11-slim \
  --context ${SERVICE_PATH} \
  --include-deps
```

## Troubleshooting

### Build Fails: "pycontainer not found"

Ensure the prebuild hook installs pycontainer-build:

```yaml
hooks:
  prebuild:
    shell: sh
    run: pip install pycontainer-build
```

### Authentication Fails to ACR

Ensure you're logged in to Azure:

```bash
az login
azd auth login
```

### Image Size Too Large

Optimize your image:
- Use slim base images: `python:3.11-slim`
- Consider distroless: `gcr.io/distroless/python3-debian12`
- Minimize dependencies in `requirements.txt`

### Slow Builds

Enable caching:

```yaml
hooks:
  build:
    shell: sh
    run: |
      pycontainer build \
        --tag ${SERVICE_IMAGE_NAME}:${SERVICE_IMAGE_TAG} \
        --base-image python:3.11-slim \
        --include-deps \
        --push
        # Caching is enabled by default (~/.pycontainer/cache)
```

## Benefits of Using pycontainer-build with azd

1. **No Docker Required** - Works in Codespaces, DevBox, and restricted environments
2. **Simpler Setup** - No Docker installation or configuration
3. **Native Python** - Uses Python's native packaging and tooling
4. **Fast Builds** - Efficient layer caching and reuse
5. **Secure** - No Docker daemon, SBOM generation built-in
6. **Cloud-Native** - Designed for cloud development workflows

## Complete Example

Here's a complete working example:

### Project Structure
```
myapp/
├── azure.yaml
├── infra/
│   └── main.bicep
└── src/
    ├── pyproject.toml
    ├── requirements.txt
    └── app/
        └── main.py
```

### azure.yaml
```yaml
name: myapp
metadata:
  template: python-container-app

services:
  api:
    project: ./src
    language: python
    host: containerapp
    hooks:
      prebuild:
        shell: sh
        run: |
          pip install --upgrade pip
          pip install pycontainer-build
      build:
        shell: sh
        run: |
          pycontainer build \
            --tag ${SERVICE_IMAGE_NAME}:${SERVICE_IMAGE_TAG} \
            --base-image python:3.11-slim \
            --context ${SERVICE_PATH} \
            --include-deps \
            --sbom spdx \
            --push \
            --verbose
```

### src/pyproject.toml
```toml
[project]
name = "myapp"
version = "1.0.0"
dependencies = [
    "fastapi>=0.104.0",
    "uvicorn[standard]>=0.24.0"
]

[project.scripts]
start = "uvicorn app.main:app --host 0.0.0.0 --port 8080"
```

### Deploy
```bash
azd up
```

That's it! Your Python app is now deployed to Azure Container Apps without Docker.

## Next Steps

- [Azure Developer CLI Documentation](https://learn.microsoft.com/en-us/azure/developer/azure-developer-cli/)
- [Azure Container Apps Documentation](https://learn.microsoft.com/en-us/azure/container-apps/)
- [pycontainer-build Configuration](../README.md#configuration)
- [GitHub Actions Integration](./github-actions.md)
