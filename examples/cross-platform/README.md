# Cross-Platform Build Example

This example demonstrates building container images for different CPU architectures (amd64, arm64) from a single machine using pycontainer-build's cross-platform support.

## Overview

Cross-platform builds allow you to:

- Build `linux/amd64` images from ARM Macs (Apple Silicon)
- Build `linux/arm64` images from x86 machines
- Create multi-architecture images for cloud deployments
- Target specific platforms like AWS Graviton (ARM64) or standard EC2 (AMD64)

## Quick Start

### Build for AMD64 (Most Common)

```bash
# Build AMD64 image (standard x86_64 servers)
pycontainer build \
  --tag myapp:amd64 \
  --platform linux/amd64 \
  --base-image python:3.11-slim

# Verify the platform
jq '.manifests[0].platform' dist/image/index.json
# Output: {"architecture":"amd64","os":"linux"}
```

### Build for ARM64 (AWS Graviton, Apple Silicon)

```bash
# Build ARM64 image (AWS Graviton, Raspberry Pi, etc.)
pycontainer build \
  --tag myapp:arm64 \
  --platform linux/arm64 \
  --base-image python:3.11-slim

# Verify the platform
jq '.manifests[0].platform' dist/image/index.json
# Output: {"architecture":"arm64","os":"linux"}
```

### Build for Multiple Platforms

```bash
#!/bin/bash
# Build and push images for multiple platforms

REGISTRY="ghcr.io/myorg/myapp"

for PLATFORM in linux/amd64 linux/arm64; do
  ARCH=$(echo $PLATFORM | cut -d/ -f2)
  
  echo "Building for $PLATFORM..."
  pycontainer build \
    --tag ${REGISTRY}:${ARCH} \
    --platform $PLATFORM \
    --base-image python:3.11-slim \
    --include-deps \
    --push
  
  echo "✓ Built and pushed ${REGISTRY}:${ARCH}"
done

echo "✅ Multi-platform build complete!"
```

## Simple Example Application

### Create the App

```bash
# Create directory
mkdir cross-platform-demo && cd cross-platform-demo

# Create a simple Python app
cat > app.py << 'EOF'
import platform
import sys

def main():
    print(f"Python {sys.version}")
    print(f"Platform: {platform.machine()}")
    print(f"Architecture: {platform.architecture()}")
    print(f"System: {platform.system()}")
    
if __name__ == "__main__":
    main()
EOF

# Create pyproject.toml
cat > pyproject.toml << 'EOF'
[project]
name = "cross-platform-demo"
version = "1.0.0"
requires-python = ">=3.11"

[project.scripts]
demo = "app:main"
EOF
```

### Build for Different Platforms

```bash
# Build for AMD64
pycontainer build \
  --tag cross-platform-demo:amd64 \
  --platform linux/amd64 \
  --verbose

# Build for ARM64
pycontainer build \
  --tag cross-platform-demo:arm64 \
  --platform linux/arm64 \
  --verbose

# Inspect the differences
echo "=== AMD64 Config ==="
MANIFEST_AMD64=$(jq -r '.manifests[0].digest' dist/image/index.json | cut -d: -f2)
CONFIG_AMD64=$(jq -r '.config.digest' dist/image/blobs/sha256/$MANIFEST_AMD64 | cut -d: -f2)
jq '{arch: .architecture, os: .os}' dist/image/blobs/sha256/$CONFIG_AMD64

# (Rebuild for arm64 to inspect)
pycontainer build --tag cross-platform-demo:arm64 --platform linux/arm64
echo "=== ARM64 Config ==="
MANIFEST_ARM64=$(jq -r '.manifests[0].digest' dist/image/index.json | cut -d: -f2)
CONFIG_ARM64=$(jq -r '.config.digest' dist/image/blobs/sha256/$MANIFEST_ARM64 | cut -d: -f2)
jq '{arch: .architecture, os: .os}' dist/image/blobs/sha256/$CONFIG_ARM64
```

## Real-World Use Cases

### Use Case 1: Build AMD64 from ARM Mac

Scenario: You develop on Apple Silicon but deploy to standard x86 cloud servers.

```bash
# On your ARM Mac
pycontainer build \
  --tag ghcr.io/myorg/myapp:latest \
  --platform linux/amd64 \
  --base-image python:3.11-slim \
  --include-deps \
  --push

# Deploy to standard AMD64 server
# The image will run correctly on x86_64 hosts
```

### Use Case 2: Build ARM64 for AWS Graviton

Scenario: Target AWS Graviton instances for better price/performance.

```bash
# Build for ARM64
pycontainer build \
  --tag myapp:graviton \
  --platform linux/arm64 \
  --base-image python:3.11-slim \
  --include-deps \
  --push

# Deploy to AWS Graviton instance (ARM-based)
# Better performance and cost for ARM workloads
```

### Use Case 3: GitHub Actions Multi-Platform Build

```yaml
name: Multi-Platform Build

on:
  push:
    branches: [main]

jobs:
  build:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        platform: [linux/amd64, linux/arm64]
    
    steps:
      - uses: actions/checkout@v4
      
      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      
      - name: Install pycontainer
        run: pip install pycontainer-build
      
      - name: Extract architecture
        id: arch
        run: echo "arch=$(echo ${{ matrix.platform }} | cut -d/ -f2)" >> $GITHUB_OUTPUT
      
      - name: Build image
        run: |
          pycontainer build \
            --tag ghcr.io/${{ github.repository }}:${{ steps.arch.outputs.arch }} \
            --platform ${{ matrix.platform }} \
            --base-image python:3.11-slim \
            --include-deps \
            --push
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```

### Use Case 4: Create Docker Manifest for Multi-Arch

After building for both platforms, create a manifest list:

```bash
# Build both platforms
pycontainer build --tag ghcr.io/myorg/myapp:amd64 --platform linux/amd64 --push
pycontainer build --tag ghcr.io/myorg/myapp:arm64 --platform linux/arm64 --push

# Create manifest list (requires docker or crane)
docker manifest create ghcr.io/myorg/myapp:latest \
  ghcr.io/myorg/myapp:amd64 \
  ghcr.io/myorg/myapp:arm64

docker manifest push ghcr.io/myorg/myapp:latest

# Now users can pull ghcr.io/myorg/myapp:latest and get the right arch automatically
```

## Configuration File Example

Use `pycontainer.toml` to set default platform:

```toml
[build]
base_image = "python:3.11-slim"
platform = "linux/amd64"  # Default platform
include_deps = true
reproducible = true

[build.env]
PYTHONUNBUFFERED = "1"

[registry]
url = "ghcr.io/myorg/myapp"
```

Then override for specific builds:

```bash
# Uses amd64 from config
pycontainer build --tag myapp:latest --config pycontainer.toml

# Override to arm64
pycontainer build --tag myapp:arm64 --platform linux/arm64 --config pycontainer.toml
```

## How It Works

### Platform Parsing

pycontainer parses the platform string:

```python
# Input: "linux/amd64"
os_name = "linux"
architecture = "amd64"

# Input: "linux/arm64"
os_name = "linux"
architecture = "arm64"
```

### Base Image Selection

When pulling multi-platform base images:

```python
# Python's base image has manifests for multiple platforms
# pycontainer automatically selects the right one:

Base image manifests:
- linux/amd64 → sha256:abc123...
- linux/arm64 → sha256:def456...
- linux/arm/v7 → sha256:ghi789...

# With --platform linux/arm64, pulls sha256:def456...
```

### OCI Metadata

Generated images have correct platform metadata:

```json
{
  "manifests": [{
    "platform": {
      "architecture": "arm64",
      "os": "linux"
    },
    "digest": "sha256:...",
    "size": 1234
  }]
}
```

## Supported Platforms

Currently supported platform strings:

- `linux/amd64` - Standard x86_64 (most common)
- `linux/arm64` - ARM 64-bit (AWS Graviton, Apple Silicon containers)
- `linux/arm/v7` - ARM 32-bit v7
- `linux/arm/v6` - ARM 32-bit v6

**Note**: For Python applications (interpreted language), no actual cross-compilation is needed. The Python interpreter in the base image runs the same bytecode regardless of architecture.

## Limitations

### What Works

✅ Pulling correct architecture variant from multi-platform base images  
✅ Setting proper OCI platform metadata  
✅ Building Python apps (interpreted, no compilation needed)

### What Doesn't Work Yet

❌ True cross-compilation of native extensions (C extensions, Rust, etc.)  
❌ Building platform-specific system packages  
❌ Emulation of target architecture during build

For apps with native dependencies, build on the target architecture or use Docker buildx with QEMU emulation.

## Testing Your Cross-Platform Images

### With Docker

```bash
# Build for ARM64
pycontainer build --tag myapp:arm64 --platform linux/arm64

# Load into Docker
docker load -i <(tar -C dist/image -cf - .)

# Try to run (may fail if wrong architecture)
docker run myapp:arm64  # Only works on ARM64 hosts

# Use QEMU for testing on different architecture
docker run --platform linux/arm64 myapp:arm64
```

### With Crane

```bash
# Copy to Docker daemon
crane copy dist/image docker-daemon:myapp:arm64

# Inspect the image
crane manifest myapp:arm64 | jq .
```

## Troubleshooting

### Error: "No matching manifest for platform"

**Cause**: Base image doesn't support the requested platform.

**Solution**: Check available platforms:

```bash
docker manifest inspect python:3.11-slim
```

Use a base image that supports your target platform.

### Error: "Platform format invalid"

**Cause**: Platform string format is incorrect.

**Solution**: Use format `os/architecture`:

```bash
# ✅ Correct
--platform linux/amd64

# ❌ Wrong
--platform amd64
--platform linux-amd64
```

### Image Runs Slowly

**Cause**: Running wrong architecture through emulation.

**Solution**: Verify you're using the correct platform:

```bash
docker inspect myapp:latest | jq '.[0].Architecture'
uname -m  # Check host architecture
```

## Next Steps

- Deploy to cloud providers (AWS, Azure, GCP)
- Set up CI/CD for multi-platform builds
- Create manifest lists for automatic arch selection
- Explore platform-specific optimizations

## Resources

- [OCI Image Specification](https://github.com/opencontainers/image-spec)
- [Docker Multi-Platform Images](https://docs.docker.com/build/building/multi-platform/)
- [AWS Graviton](https://aws.amazon.com/ec2/graviton/)
- [GitHub Actions Multi-Arch Builds](https://docs.github.com/en/actions/publishing-packages/publishing-docker-images)
