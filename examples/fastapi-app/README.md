# FastAPI Demo Application

This is a sample FastAPI application demonstrating all pycontainer-build integrations.

## Quick Start

### Build with pycontainer-build (CLI)

```bash
cd examples/fastapi-app
pycontainer build --tag fastapi-demo:latest --include-deps
```

### Build with Poetry Plugin

```bash
cd examples/fastapi-app
poetry install
poetry self add poetry-pycontainer
poetry build-container --tag fastapi-demo:latest
```

### Build with Hatch Plugin

```bash
cd examples/fastapi-app
pip install hatch hatch-pycontainer
hatch build  # Builds both wheel and container
```

### Build with VS Code

1. Open this directory in VS Code
2. Open Command Palette (Ctrl+Shift+P / Cmd+Shift+P)
3. Type "pycontainer: Build Container Image"
4. Enter tag: `fastapi-demo:latest`

### Deploy with Azure Developer CLI

Create `azure.yaml`:

```yaml
name: fastapi-demo

services:
  api:
    project: ./
    language: python
    host: containerapp
    hooks:
      prebuild:
        run: pip install pycontainer-build
      build:
        run: |
          pycontainer build \
            --tag ${SERVICE_IMAGE_NAME}:${SERVICE_IMAGE_TAG} \
            --include-deps \
            --push
```

Then deploy:

```bash
azd up
```

### CI/CD with GitHub Actions

Create `.github/workflows/build.yml`:

```yaml
name: Build Container

on:
  push:
    branches: [main]

jobs:
  build:
    uses: spboyer/pycontainer-build/.github/workflows/pycontainer-build.yml@main
    with:
      tag: ghcr.io/${{ github.repository }}/fastapi-demo:${{ github.sha }}
      context: ./examples/fastapi-app
      include-deps: true
      push: true
    permissions:
      contents: read
      packages: write
```

## Run Locally (Testing)

```bash
# Build the container
pycontainer build --tag fastapi-demo:latest --include-deps

# Run with Docker (for testing only)
docker run -p 8000:8000 fastapi-demo:latest

# Or directly with Python
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Visit http://localhost:8000 for the app and http://localhost:8000/docs for API documentation.

## Configuration

This example includes configuration for all integrations:

- **pycontainer CLI**: Uses `pyproject.toml` [project] section
- **Poetry plugin**: Uses `[tool.pycontainer]` section
- **Hatch plugin**: Uses `[tool.hatch.build.hooks.pycontainer]` section
- **GitHub Actions**: Example workflow provided above
- **Azure Developer CLI**: Example azure.yaml provided above

## Endpoints

- `GET /` - Welcome message
- `GET /health` - Health check
- `GET /info` - Application information
- `GET /docs` - Interactive API documentation (Swagger UI)
- `GET /redoc` - Alternative API documentation (ReDoc)

## Features Demonstrated

- ✅ Framework auto-detection (FastAPI automatically configured)
- ✅ Entry point auto-detection from pyproject.toml
- ✅ Dependency packaging with `--include-deps`
- ✅ Environment variable configuration
- ✅ OCI label metadata
- ✅ Multiple integration methods

## Learn More

- [Poetry Plugin Documentation](../../plugins/poetry-pycontainer/)
- [Hatch Plugin Documentation](../../plugins/hatch-pycontainer/)
- [GitHub Actions Documentation](../../docs/github-actions.md)
- [Azure Developer CLI Documentation](../../docs/azd-integration.md)
- [VS Code Extension Documentation](../../plugins/vscode-pycontainer/)
