# poetry-pycontainer

A Poetry plugin that integrates [pycontainer-build](https://github.com/spboyer/pycontainer-build) to build OCI container images directly from your Poetry projects — no Docker required.

## Features

- 🐍 **Native Poetry Integration** - Build containers with `poetry build-container`
- 🚫 **No Docker Required** - Pure Python OCI image generation
- ⚡ **Fast Builds** - Efficient layer caching and reuse
- 🔧 **Configuration via pyproject.toml** - Use `[tool.pycontainer]` section
- 📦 **Automatic Dependency Packaging** - Includes Poetry dependencies
- 🔒 **SBOM Generation** - Built-in security compliance

## Installation

```bash
poetry self add poetry-pycontainer
```

Or install pycontainer-build and the plugin:

```bash
pip install poetry-pycontainer pycontainer-build
```

## Quick Start

### Basic Usage

```bash
# Build a container image from your Poetry project
poetry build-container

# Build with a specific tag
poetry build-container --tag myapp:v1.0.0

# Build and push to registry
poetry build-container --tag ghcr.io/user/myapp:latest --push
```

### Configuration

Add configuration to your `pyproject.toml`:

```toml
[tool.poetry]
name = "myapp"
version = "1.0.0"
description = "My Python application"

[tool.pycontainer]
tag = "myapp:latest"
base_image = "python:3.11-slim"
registry = "ghcr.io/user/myapp"
include_deps = true
push = false

[tool.pycontainer.env]
ENV = "production"
PORT = "8080"

[tool.pycontainer.labels]
maintainer = "team@example.com"
```

## Commands

### `poetry build-container`

Build an OCI container image from your Poetry project.

**Options:**

- `--tag, -t` - Container image tag (default: `{name}:{version}`)
- `--base-image, -b` - Base container image (default: `python:3.11-slim`)
- `--registry, -r` - Container registry URL
- `--push, -p` - Push image to registry
- `--include-deps, -d` - Include Poetry dependencies (default: true)
- `--sbom, -s` - Generate SBOM (`spdx` or `cyclonedx`)
- `--verbose, -v` - Verbose output
- `--dry-run` - Show what would be built without building
- `--no-cache` - Disable layer caching

**Examples:**

```bash
# Basic build
poetry build-container

# Build with custom tag
poetry build-container --tag myapp:v2.0.0

# Build on different base image
poetry build-container --base-image python:3.12-alpine

# Build with SBOM
poetry build-container --sbom spdx

# Build and push to GHCR
poetry build-container \
  --tag ghcr.io/myorg/myapp:latest \
  --push \
  --verbose

# Dry run to preview
poetry build-container --dry-run --verbose
```

## Configuration Reference

All configuration is under `[tool.pycontainer]` in `pyproject.toml`:

```toml
[tool.pycontainer]
# Container image tag (optional, defaults to {name}:{version})
tag = "myapp:latest"

# Base container image
base_image = "python:3.11-slim"

# Container registry URL
registry = "ghcr.io/user/myapp"

# Push image to registry after build
push = false

# Include Poetry dependencies in the image
include_deps = true

# Generate SBOM (spdx or cyclonedx)
sbom = "spdx"

# Enable verbose output
verbose = false

# Dry run mode
dry_run = false

# Disable layer caching
no_cache = false

# Environment variables to set in the container
[tool.pycontainer.env]
ENV = "production"
PORT = "8080"
DEBUG = "false"

# Container labels
[tool.pycontainer.labels]
maintainer = "team@example.com"
version = "1.0.0"
org.opencontainers.image.source = "https://github.com/user/repo"
```

## How It Works

1. **Reads Poetry Configuration** - Extracts project name, version, dependencies from `pyproject.toml`
2. **Applies Tool Configuration** - Merges `[tool.pycontainer]` settings
3. **Builds Container** - Uses pycontainer-build to create OCI image
4. **Adds Metadata** - Automatically adds Poetry metadata as OCI labels
5. **Pushes (Optional)** - Pushes to registry if `--push` is specified

## Integration Examples

### With GitHub Actions

```yaml
name: Build and Push Container

on:
  push:
    branches: [main]

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
      
      - name: Install Poetry
        run: pipx install poetry
      
      - name: Install plugin
        run: poetry self add poetry-pycontainer
      
      - name: Install dependencies
        run: poetry install
      
      - name: Build and push container
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: |
          poetry build-container \
            --tag ghcr.io/${{ github.repository }}:${{ github.sha }} \
            --push \
            --verbose
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
        run: |
          pip install poetry
          poetry self add poetry-pycontainer
      build:
        shell: sh
        run: |
          poetry build-container \
            --tag ${SERVICE_IMAGE_NAME}:${SERVICE_IMAGE_TAG} \
            --push
```

### Local Development

```bash
# Install dependencies
poetry install

# Build container locally
poetry build-container --tag myapp:dev

# Test with Docker (for validation only)
docker run myapp:dev
```

## Advanced Usage

### Custom Base Images

```bash
# Use distroless for smaller images
poetry build-container --base-image gcr.io/distroless/python3-debian12

# Use Alpine for minimal size
poetry build-container --base-image python:3.11-alpine
```

### Multi-Stage Configuration

```toml
[tool.pycontainer]
base_image = "python:3.11-slim"

[tool.pycontainer.env]
# Development
ENV = "development"

# Override in CI with --env flag or environment variables
```

### Framework Auto-Detection

pycontainer-build automatically detects and configures:
- **FastAPI** - Sets up uvicorn with correct host/port
- **Flask** - Configures Flask development server
- **Django** - Sets up Django runserver

No manual configuration needed!

## Troubleshooting

### Plugin Not Found

```bash
# Ensure plugin is installed
poetry self show plugins

# Reinstall if needed
poetry self remove poetry-pycontainer
poetry self add poetry-pycontainer
```

### Build Fails: "pycontainer-build not found"

```bash
# Install pycontainer-build
pip install pycontainer-build

# Or add as a dev dependency
poetry add --group dev pycontainer-build
```

### Dependencies Not Included

Ensure `include_deps = true` in your config:

```toml
[tool.pycontainer]
include_deps = true
```

## Requirements

- Python 3.11+
- Poetry 1.2.0+
- pycontainer-build (installed automatically as dependency)

## License

MIT License - See LICENSE file for details

## Contributing

Contributions welcome! Please see [CONTRIBUTING.md](../../CONTRIBUTING.md) for guidelines.

## Related Projects

- [pycontainer-build](https://github.com/spboyer/pycontainer-build) - The core container builder
- [hatch-pycontainer](../hatch-pycontainer/) - Hatch plugin for pycontainer-build
- [Poetry](https://python-poetry.org/) - Python dependency management

## Support

- [GitHub Issues](https://github.com/spboyer/pycontainer-build/issues)
- [Documentation](https://github.com/spboyer/pycontainer-build/tree/main/docs)
