# GitHub Actions Integration

This guide shows how to use pycontainer-build in GitHub Actions workflows to build and push container images without Docker.

## Reusable Workflow

pycontainer-build provides a reusable GitHub Actions workflow that makes it easy to integrate container builds into your CI/CD pipeline.

### Basic Usage

```yaml
name: Build Container

on:
  push:
    branches: [main]

jobs:
  build:
    uses: spboyer/pycontainer-build/.github/workflows/pycontainer-build.yml@main
    with:
      tag: ghcr.io/${{ github.repository }}:${{ github.sha }}
      push: true
    permissions:
      contents: read
      packages: write
```

This will:
1. Check out your code
2. Set up Python 3.11
3. Install pycontainer-build
4. Build your container image
5. Push to GitHub Container Registry (ghcr.io)

### Complete Configuration

```yaml
jobs:
  build:
    uses: spboyer/pycontainer-build/.github/workflows/pycontainer-build.yml@main
    with:
      # Python version for the build environment
      python-version: '3.11'
      
      # Container image tag (required)
      tag: 'ghcr.io/myorg/myapp:v1.0.0'
      
      # Base image for the container
      base-image: 'python:3.11-slim'
      
      # Build context directory
      context: '.'
      
      # Push to registry (true/false)
      push: true
      
      # Include dependencies from venv/requirements.txt
      include-deps: true
      
      # Generate SBOM (spdx or cyclonedx)
      sbom: 'spdx'
      
      # Container registry
      registry: 'ghcr.io'
    
    secrets:
      # Optional: Custom registry credentials
      # If not provided, uses GITHUB_TOKEN for ghcr.io
      REGISTRY_USERNAME: ${{ secrets.REGISTRY_USERNAME }}
      REGISTRY_PASSWORD: ${{ secrets.REGISTRY_PASSWORD }}
```

## Use Cases

### 1. Build on Every Commit

```yaml
name: Build Container

on:
  push:
    branches: [main, develop]

jobs:
  build:
    uses: spboyer/pycontainer-build/.github/workflows/pycontainer-build.yml@main
    with:
      tag: ghcr.io/${{ github.repository }}:${{ github.sha }}
      push: true
    permissions:
      contents: read
      packages: write
```

### 2. Build and Tag Releases

```yaml
name: Release Container

on:
  release:
    types: [published]

jobs:
  build:
    uses: spboyer/pycontainer-build/.github/workflows/pycontainer-build.yml@main
    with:
      tag: ghcr.io/${{ github.repository }}:${{ github.event.release.tag_name }}
      base-image: 'python:3.11-slim'
      include-deps: true
      sbom: 'spdx'
      push: true
    permissions:
      contents: read
      packages: write
```

### 3. Matrix Builds (Multiple Python Versions)

```yaml
name: Multi-Version Build

on:
  push:
    branches: [main]

jobs:
  build:
    strategy:
      matrix:
        python-version: ['3.10', '3.11', '3.12']
    uses: spboyer/pycontainer-build/.github/workflows/pycontainer-build.yml@main
    with:
      python-version: ${{ matrix.python-version }}
      tag: ghcr.io/${{ github.repository }}:py${{ matrix.python-version }}
      base-image: python:${{ matrix.python-version }}-slim
      push: true
    permissions:
      contents: read
      packages: write
```

### 4. PR Preview Images (Don't Push)

```yaml
name: PR Build

on:
  pull_request:

jobs:
  build:
    uses: spboyer/pycontainer-build/.github/workflows/pycontainer-build.yml@main
    with:
      tag: ghcr.io/${{ github.repository }}:pr-${{ github.event.pull_request.number }}
      push: false  # Just build, don't push
    permissions:
      contents: read
```

## Custom Workflow (Direct Usage)

If you need more control, you can use pycontainer-build directly:

```yaml
name: Custom Build

on:
  push:
    branches: [main]

jobs:
  build:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      packages: write
    
    steps:
      - uses: actions/checkout@v4
      
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      
      - name: Install pycontainer-build
        run: pip install pycontainer-build
      
      - name: Build and push container
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: |
          pycontainer build \
            --tag ghcr.io/${{ github.repository }}:${{ github.sha }} \
            --base-image python:3.11-slim \
            --include-deps \
            --sbom spdx \
            --push \
            --verbose
      
      - name: Run tests on container (optional)
        run: |
          # Example: pull and test the image
          # Note: Requires Docker for testing, but not for building
          docker pull ghcr.io/${{ github.repository }}:${{ github.sha }}
          docker run ghcr.io/${{ github.repository }}:${{ github.sha }} python -c "import myapp; print('OK')"
```

## Authentication

### GitHub Container Registry (ghcr.io)

The workflow automatically uses `GITHUB_TOKEN` for authentication to ghcr.io. No additional configuration needed.

### Other Registries

For Docker Hub, Azure Container Registry, or private registries:

```yaml
jobs:
  build:
    uses: spboyer/pycontainer-build/.github/workflows/pycontainer-build.yml@main
    with:
      tag: 'myregistry.azurecr.io/myapp:latest'
      registry: 'myregistry.azurecr.io'
      push: true
    secrets:
      REGISTRY_USERNAME: ${{ secrets.ACR_USERNAME }}
      REGISTRY_PASSWORD: ${{ secrets.ACR_PASSWORD }}
```

## Environment Variables

The workflow sets these environment variables automatically:

- `GITHUB_TOKEN` - For ghcr.io authentication
- `REGISTRY_USERNAME` - Custom registry username (if provided)
- `REGISTRY_PASSWORD` - Custom registry password (if provided)

## Build Summary

The workflow generates a build summary in the GitHub Actions UI showing:
- Image tag
- Base image used
- Python version
- Build context
- Whether the image was pushed
- SBOM generation status

## Troubleshooting

### Build Fails: "Module not found"

Ensure your dependencies are properly specified:
- Include a `requirements.txt` or `pyproject.toml`
- Use `--include-deps` flag
- Or pre-install dependencies in your workflow

### Push Fails: "Authentication required"

Check:
- Workflow has `packages: write` permission
- For custom registries, `REGISTRY_USERNAME` and `REGISTRY_PASSWORD` secrets are set
- Registry URL is correct

### Image Size Too Large

Optimize your base image:
- Use slim images: `python:3.11-slim` instead of `python:3.11`
- Consider distroless: `gcr.io/distroless/python3-debian12`
- Exclude unnecessary files in your project

## Benefits Over Docker-Based Builds

1. **No Docker daemon required** - Works in any CI environment
2. **Faster builds** - No Docker layer caching overhead
3. **Simpler workflows** - No Docker setup or Docker-in-Docker
4. **Better security** - No privileged containers needed
5. **Cloud-native** - Works in GitHub Codespaces, Azure DevOps, etc.

## Next Steps

- See [example-build.yml.example](../../.github/workflows/example-build.yml.example) for complete examples
- Read [Configuration](../README.md#configuration) for all build options
- Explore [IMPLEMENTATION_PLAN.md](../IMPLEMENTATION_PLAN.md) for roadmap
