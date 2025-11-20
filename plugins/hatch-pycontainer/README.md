# hatch-pycontainer

A Hatch plugin that integrates [pycontainer-build](https://github.com/spboyer/pycontainer-build) to build OCI container images directly from your Hatch projects — no Docker required.

## Features

- 🐍 **Native Hatch Integration** - Build containers during `hatch build`
- 🚫 **No Docker Required** - Pure Python OCI image generation
- ⚡ **Fast Builds** - Efficient layer caching and reuse
- 🔧 **Configuration via pyproject.toml** - Use `[tool.hatch.build.hooks.pycontainer]` section
- 📦 **Automatic Dependency Packaging** - Includes project dependencies
- 🔒 **SBOM Generation** - Built-in security compliance

## Installation

```bash
pip install hatch-pycontainer
```

## Quick Start

### Basic Usage

Add the plugin to your `pyproject.toml`:

```toml
[build-system]
requires = ["hatchling", "hatch-pycontainer"]
build-backend = "hatchling.build"

[tool.hatch.build.hooks.pycontainer]
enable = true
```

Then build your container:

```bash
hatch build
```

This will:
1. Build your Python package (wheel)
2. Create a container image with pycontainer-build
3. Output to `dist/image/`

## Configuration

### Basic Configuration

```toml
[tool.hatch.build.hooks.pycontainer]
# Container image tag
tag = "myapp:latest"

# Base container image
base-image = "python:3.11-slim"

# Include dependencies
include-deps = true
```

### Complete Configuration

```toml
[tool.hatch.build.hooks.pycontainer]
# Enable/disable the hook
enable = true

# Skip container build (useful for local dev)
skip = false

# Container image tag
tag = "myapp:v1.0.0"

# Base container image
base-image = "python:3.11-slim"

# Container registry URL
registry = "ghcr.io/user/myapp"

# Push to registry after build
push = false

# Include project dependencies
include-deps = true

# Generate SBOM (spdx or cyclonedx)
sbom = "spdx"

# Enable verbose output
verbose = true

# Disable layer caching
no-cache = false

# Environment variables
[tool.hatch.build.hooks.pycontainer.env]
ENV = "production"
PORT = "8080"
DEBUG = "false"

# Container labels
[tool.hatch.build.hooks.pycontainer.labels]
maintainer = "team@example.com"
org.opencontainers.image.source = "https://github.com/user/repo"
```

## Usage Examples

### Build Container with Package

```bash
# Build both wheel and container
hatch build

# The container image will be at dist/image/
```

### Build and Push

```toml
[tool.hatch.build.hooks.pycontainer]
tag = "ghcr.io/myorg/myapp:latest"
push = true
```

```bash
# Build and push in one command
GITHUB_TOKEN=$GITHUB_TOKEN hatch build
```

### Development vs Production

Use environment-specific configuration:

```toml
[tool.hatch.envs.default.build.hooks.pycontainer]
tag = "myapp:dev"
base-image = "python:3.11"
push = false

[tool.hatch.envs.production.build.hooks.pycontainer]
tag = "myapp:latest"
base-image = "python:3.11-slim"
push = true
sbom = "spdx"
```

### Skip Container Build

```bash
# Skip container build for local dev
hatch build -e local

# Or set in pyproject.toml
[tool.hatch.envs.local.build.hooks.pycontainer]
skip = true
```

## Integration Examples

### With GitHub Actions

```yaml
name: Build and Release

on:
  push:
    tags:
      - 'v*'

jobs:
  build:
    runs-on: ubuntu-latest
    permissions:
      packages: write
    steps:
      - uses: actions/checkout@v4
      
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      
      - name: Install Hatch
        run: pip install hatch hatch-pycontainer
      
      - name: Build package and container
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: hatch build
```

### With Azure Developer CLI

```yaml
# azure.yaml
name: myapp

services:
  api:
    project: ./
    language: python
    host: containerapp
    hooks:
      prebuild:
        shell: sh
        run: pip install hatch hatch-pycontainer
      build:
        shell: sh
        run: hatch build
```

### With CI/CD Pipeline

```yaml
# .gitlab-ci.yml
build:
  image: python:3.11
  script:
    - pip install hatch hatch-pycontainer
    - hatch build
  artifacts:
    paths:
      - dist/
```

## How It Works

1. **Registers Build Hook** - Hatch calls the pycontainer hook during build
2. **Reads Configuration** - Extracts settings from `pyproject.toml`
3. **Builds Container** - Uses pycontainer-build to create OCI image
4. **Adds Metadata** - Automatically adds version and build info
5. **Pushes (Optional)** - Pushes to registry if configured

## Advanced Usage

### Custom Build Targets

```toml
[tool.hatch.build.targets.wheel.hooks.pycontainer]
tag = "myapp:wheel-build"

[tool.hatch.build.targets.sdist.hooks.pycontainer]
skip = true  # Don't build container for sdist
```

### Multi-Architecture

```toml
[tool.hatch.build.hooks.pycontainer]
platform = "linux/amd64"

# Note: Full multi-arch builds coming in future release
```

### Framework Auto-Detection

pycontainer-build automatically detects:
- **FastAPI** - Configures uvicorn entrypoint
- **Flask** - Sets up Flask server
- **Django** - Configures Django runserver

### Custom Base Images

```toml
[tool.hatch.build.hooks.pycontainer]
# Minimal distroless image
base-image = "gcr.io/distroless/python3-debian12"

# Alpine for small size
base-image = "python:3.11-alpine"

# Microsoft Azure-optimized
base-image = "mcr.microsoft.com/python/distroless"
```

## Environment Variables

Set these during build for authentication:

```bash
# GitHub Container Registry
GITHUB_TOKEN=ghp_... hatch build

# Docker Hub
REGISTRY_USERNAME=myuser REGISTRY_PASSWORD=mypass hatch build

# Azure Container Registry (uses az cli)
az acr login --name myregistry
hatch build
```

## Troubleshooting

### Hook Not Running

Ensure the hook is enabled:

```toml
[tool.hatch.build.hooks.pycontainer]
enable = true
skip = false
```

### pycontainer-build Not Found

Install it explicitly:

```bash
pip install pycontainer-build
```

Or add to build requirements:

```toml
[build-system]
requires = ["hatchling", "hatch-pycontainer", "pycontainer-build"]
```

### Dependencies Not Included

Check configuration:

```toml
[tool.hatch.build.hooks.pycontainer]
include-deps = true
```

### Push Fails

Verify authentication:

```bash
# For GHCR
echo $GITHUB_TOKEN | docker login ghcr.io -u USERNAME --password-stdin

# For ACR
az acr login --name myregistry
```

## Comparison with Other Tools

| Feature | hatch-pycontainer | Dockerfile | Buildpacks |
|---------|------------------|-----------|------------|
| No Docker daemon | ✅ | ❌ | ❌ |
| Pure Python | ✅ | ❌ | ❌ |
| Hatch native | ✅ | ❌ | ❌ |
| Auto-config | ✅ | ❌ | ✅ |
| Layer caching | ✅ | ✅ | ✅ |
| SBOM generation | ✅ | ❌ | ✅ |

## Requirements

- Python 3.11+
- Hatch 1.18.0+
- pycontainer-build (installed automatically)

## License

MIT License - See LICENSE file for details

## Contributing

Contributions welcome! Please see [CONTRIBUTING.md](../../CONTRIBUTING.md) for guidelines.

## Related Projects

- [pycontainer-build](https://github.com/spboyer/pycontainer-build) - Core container builder
- [poetry-pycontainer](../poetry-pycontainer/) - Poetry plugin
- [Hatch](https://hatch.pypa.io/) - Modern Python project manager

## Support

- [GitHub Issues](https://github.com/spboyer/pycontainer-build/issues)
- [Documentation](https://github.com/spboyer/pycontainer-build/tree/main/docs)
