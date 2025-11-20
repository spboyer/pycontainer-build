# Local Development Guide

Complete guide for using pycontainer-build locally during development, including installation, building images, testing, and troubleshooting.

---

## 📦 Installation

### From Source (Development)

The recommended way for local development:

```bash
# Clone the repository
git clone https://github.com/spboyer/pycontainer-build.git
cd pycontainer-build

# Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install in editable mode
pip install -e .
```

This installs the `pycontainer` command globally in your virtual environment, and any code changes you make are immediately reflected.

### Verify Installation

```bash
# Check that the command is available
pycontainer --help

# Verify the Python API works
python -c "from pycontainer.builder import ImageBuilder; print('✓ Import successful')"
```

---

## 🏗️ Building Container Images Locally

### Basic Build

```bash
# Build from the current directory
pycontainer build --tag myapp:latest

# Build from a specific directory
pycontainer build --tag myapp:latest --context /path/to/project

# Build with verbose output
pycontainer build --tag myapp:latest --verbose
```

### Output Location

By default, images are created at:

```text
<context-path>/dist/image/
├── index.json                  # OCI index (manifest list)
├── oci-layout                  # Version marker
├── blobs/sha256/
│   ├── <manifest-digest>       # Manifest blob
│   ├── <config-digest>         # Config blob
│   ├── <base-layer-digests>    # Base image layers (if using --base-image)
│   └── <app-layer-digest>      # Application layer (tar)
└── refs/tags/
    └── myapp:latest            # Tag reference
```

### With Base Image & Dependencies

```bash
# Build with auto-detected Python base image
pycontainer build \
  --tag myapp:latest \
  --include-deps

# Or explicitly specify a base image
pycontainer build \
  --tag myapp:latest \
  --base-image python:3.12-slim \
  --include-deps

# This will:
# 1. Pull python:3.11-slim layers from registry
# 2. Package your app files
# 3. Include pip-installed dependencies
# 4. Combine everything into a complete image
```

### Preview Before Building

```bash
# Dry-run mode - see what will be built without creating files
pycontainer build --tag myapp:latest --dry-run --verbose
```

This shows:

- Detected entry point
- Files that will be included
- Base image layers (if applicable)
- Final image configuration

---

## 🧪 Testing Your Builds

### Option 1: Use Docker for Testing

The easiest way to test locally-built images is with Docker:

```bash
# 1. Build the image with pycontainer
pycontainer build --tag myapp:latest --base-image python:3.11-slim --include-deps

# 2. Import into Docker (loads the OCI layout)
docker load -i <(tar -C dist/image -cf - .)

# Alternative: Use crane to copy to local Docker daemon
crane copy dist/image docker-daemon:myapp:latest

# 3. Run the container
docker run -p 8000:8000 myapp:latest
```

### Option 2: Use Podman

```bash
# Build with pycontainer
pycontainer build --tag myapp:latest --base-image python:3.11-slim --include-deps

# Import into Podman
podman load -i <(tar -C dist/image -cf - .)

# Run with Podman
podman run -p 8000:8000 myapp:latest
```

### Option 3: Push to Local Registry

Run a local registry for testing:

```bash
# Start a local registry (Docker required for this step)
docker run -d -p 5000:5000 --name registry registry:2

# Build and push to local registry
pycontainer build \
  --tag localhost:5000/myapp:latest \
  --base-image python:3.11-slim \
  --include-deps \
  --push

# Pull and run from local registry
docker pull localhost:5000/myapp:latest
docker run -p 8000:8000 localhost:5000/myapp:latest

# Clean up when done
docker stop registry && docker rm registry
```

### Option 4: Test Without Docker

For pure Python testing (no container runtime needed):

```bash
# Just run your app directly with Python
cd /path/to/your/app
pip install -r requirements.txt
python -m app  # or uvicorn app.main:app, etc.
```

---

## 🔧 Development Workflow

### Typical Local Development Flow

```bash
# 1. Make code changes to pycontainer-build
vim src/pycontainer/builder.py

# 2. Test with a sample project
cd examples/fastapi-app
pycontainer build --tag test:latest --verbose --dry-run

# 3. Build a real image
pycontainer build --tag test:latest --base-image python:3.11-slim --include-deps

# 4. Test the image
docker load -i <(tar -C dist/image -cf - .)
docker run -p 8000:8000 test:latest
curl http://localhost:8000

# 5. Iterate - changes to pycontainer source are live (editable install)
```

### Quick Iteration with Examples

The `examples/` directory contains ready-to-use test projects:

```bash
# FastAPI example
cd examples/fastapi-app
pycontainer build --tag fastapi-test:latest --include-deps --verbose

# Test the output structure
tree dist/image

# Inspect the manifest
cat dist/image/index.json | jq .
```

---

## 🎯 Common Local Use Cases

### Use Case 1: Testing Config Changes

```bash
# Create a test config file
cat > pycontainer.toml << EOF
[build]
base_image = "python:3.11-slim"
workdir = "/app"
include_deps = true

[build.env]
DEBUG = "true"
PORT = "8000"

[build.labels]
maintainer = "you@example.com"
version = "dev"
EOF

# Build with the config
pycontainer build --tag myapp:dev --config pycontainer.toml --verbose

# Verify the config was applied
jq '.config' dist/image/blobs/sha256/<config-digest>
```

### Use Case 2: Testing Entry Point Detection

```bash
# See what entry point is detected
pycontainer build --tag myapp:latest --dry-run --verbose | grep -i "entry"

# Override with explicit entry point
pycontainer build \
  --tag myapp:latest \
  --entrypoint '["python", "-m", "myapp.cli"]'
```

### Use Case 3: Testing SBOM Generation

```bash
# Generate SPDX SBOM
pycontainer build --tag myapp:latest --sbom spdx --verbose

# Check the SBOM was created
ls -lh dist/image/sbom.spdx.json

# View the SBOM
cat dist/image/sbom.spdx.json | jq '.packages[].name'
```

### Use Case 4: Testing Framework Auto-Detection

```bash
# FastAPI project
cd examples/fastapi-app
pycontainer build --tag test:latest --dry-run --verbose
# Should show: "Detected framework: fastapi"

# Flask project (create test)
mkdir test-flask && cd test-flask
cat > app.py << 'EOF'
from flask import Flask
app = Flask(__name__)

@app.route('/')
def hello():
    return 'Hello from Flask!'
EOF

echo '[project]\nname="flask-test"\nversion="0.1.0"' > pyproject.toml

pycontainer build --tag flask-test:latest --dry-run --verbose
# Should show: "Detected framework: flask"
```

### Use Case 5: Testing Cache Behavior

```bash
# First build (cold cache)
time pycontainer build --tag myapp:v1 --base-image python:3.11-slim --include-deps

# Second build (warm cache - should be faster)
time pycontainer build --tag myapp:v2 --base-image python:3.11-slim --include-deps

# Check cache usage
ls -lh ~/.pycontainer/cache/layers/

# Force rebuild without cache
pycontainer build --tag myapp:v3 --base-image python:3.11-slim --include-deps --no-cache
```

---

## 🔍 Inspecting Built Images

### View Image Manifest

```bash
# Pretty-print the index
jq '.' dist/image/index.json

# Find the manifest digest
MANIFEST_DIGEST=$(jq -r '.manifests[0].digest' dist/image/index.json | cut -d: -f2)

# View the manifest
jq '.' dist/image/blobs/sha256/$MANIFEST_DIGEST
```

### View Image Config

```bash
# Get config digest from manifest
CONFIG_DIGEST=$(jq -r '.config.digest' dist/image/blobs/sha256/$MANIFEST_DIGEST | cut -d: -f2)

# View the config
jq '.' dist/image/blobs/sha256/$CONFIG_DIGEST
```

### Extract a Layer

```bash
# Get layer digest
LAYER_DIGEST=$(jq -r '.layers[0].digest' dist/image/blobs/sha256/$MANIFEST_DIGEST | cut -d: -f2)

# Extract to temporary directory
mkdir -p /tmp/layer
tar -xzf dist/image/blobs/sha256/$LAYER_DIGEST -C /tmp/layer

# View contents
tree /tmp/layer
```

### Compare With Docker Images

```bash
# Build with pycontainer
pycontainer build --tag myapp:pycontainer --base-image python:3.11-slim --include-deps

# Build equivalent with Docker
cat > Dockerfile << EOF
FROM python:3.11-slim
WORKDIR /app
COPY . .
RUN pip install -r requirements.txt
CMD ["python", "-m", "app"]
EOF
docker build -t myapp:docker .

# Compare sizes
du -sh dist/image
docker images myapp:docker --format "{{.Size}}"

# Compare manifests
docker save myapp:docker -o docker-image.tar
tar -xf docker-image.tar
cat manifest.json | jq .
```

---

## 🛠️ Debugging & Troubleshooting

### Enable Verbose Output

```bash
# See detailed build progress
pycontainer build --tag myapp:latest --verbose

# Outputs:
# - Project detection results
# - File paths being included
# - Layer creation progress
# - Registry push details (if --push)
# - Cache hits/misses
```

### Common Issues & Solutions

#### Issue: "No entry point detected"

**Solution**: Explicitly specify entry point or add to `pyproject.toml`:

```bash
# Option 1: CLI flag
pycontainer build --tag myapp:latest --entrypoint '["python", "-m", "myapp"]'

# Option 2: Add to pyproject.toml
[project.scripts]
myapp = "myapp.cli:main"
```

#### Issue: "Base image pull failed"

**Solution**: Check registry authentication:

```bash
# For Docker Hub
docker login

# For GHCR
echo $GITHUB_TOKEN | docker login ghcr.io -u USERNAME --password-stdin

# For ACR
az acr login --name myregistry

# Then build
pycontainer build --tag myapp:latest --base-image python:3.11-slim
```

#### Issue: "Dependencies not found in image"

**Solution**: Ensure `--include-deps` and dependencies are installed:

```bash
# Install dependencies first
pip install -r requirements.txt

# Then build with --include-deps
pycontainer build --tag myapp:latest --base-image python:3.11-slim --include-deps
```

#### Issue: "Permission denied writing to dist/"

**Solution**: Check write permissions:

```bash
# Check permissions
ls -ld dist/

# Fix permissions
chmod -R u+w dist/

# Or use a different output directory
pycontainer build --tag myapp:latest --output /tmp/myimage
```

#### Issue: "Cache is stale or corrupted"

**Solution**: Clear cache and rebuild:

```bash
# Remove cache
rm -rf ~/.pycontainer/cache

# Rebuild
pycontainer build --tag myapp:latest --no-cache
```

### Debug with Python API

For deeper debugging, use the Python API:

```python
from pycontainer.config import BuildConfig
from pycontainer.builder import ImageBuilder
import logging

# Enable debug logging
logging.basicConfig(level=logging.DEBUG)

# Create config
config = BuildConfig(
    tag="myapp:debug",
    context_path=".",
    base_image="python:3.11-slim",
    include_deps=True,
    verbose=True
)

# Build and catch exceptions
try:
    builder = ImageBuilder(config)
    builder.build()
except Exception as e:
    print(f"Build failed: {e}")
    import traceback
    traceback.print_exc()
```

---

## 📊 Performance Tips

### Optimize Build Speed

```bash
# 1. Use cache (enabled by default)
pycontainer build --tag myapp:latest --base-image python:3.11-slim --include-deps

# 2. Use smaller base images
pycontainer build --tag myapp:latest --base-image python:3.11-alpine --include-deps

# 3. Include only necessary files
pycontainer build \
  --tag myapp:latest \
  --include src/ \
  --include pyproject.toml \
  --include requirements.txt
```

### Reduce Image Size

```bash
# Use distroless for smaller images
pycontainer build --tag myapp:latest --base-image gcr.io/distroless/python3-debian11

# Or use Alpine
pycontainer build --tag myapp:latest --base-image python:3.11-alpine

# Don't include unnecessary dependencies
pip install --no-dev -r requirements.txt
pycontainer build --tag myapp:latest --include-deps
```

---

## 🔗 Integration with Local Tools

### Using with VS Code

```bash
# 1. Install the VS Code extension
code --install-extension ms-python.pycontainer

# 2. Open your project in VS Code
code /path/to/project

# 3. Use Command Palette (Cmd+Shift+P):
#    - "pycontainer: Build Container Image"
#    - "pycontainer: Build and Push Container Image"
```

### Using with Poetry

```bash
# Add Poetry plugin
poetry self add poetry-pycontainer

# Build container
cd /path/to/poetry/project
poetry build-container --tag myapp:latest

# Configuration via pyproject.toml
[tool.pycontainer]
base_image = "python:3.11-slim"
include_deps = true
push = false
```

### Using with Hatch

```bash
# Install Hatch plugin
pip install hatch-pycontainer

# Build (creates both wheel and container)
cd /path/to/hatch/project
hatch build

# Configuration via pyproject.toml
[tool.hatch.build.hooks.pycontainer]
base_image = "python:3.11-slim"
include_deps = true
```

---

## 📝 Example Projects

Try these ready-made examples:

### FastAPI Application

```bash
cd examples/fastapi-app

# Quick build
pycontainer build --tag fastapi-demo:latest --include-deps --verbose

# Test locally
docker load -i <(tar -C dist/image -cf - .)
docker run -p 8000:8000 fastapi-demo:latest
curl http://localhost:8000
```

### Minimal Test App

```bash
# Create a minimal test
mkdir minimal-test && cd minimal-test

cat > app.py << 'EOF'
print("Hello from pycontainer!")
EOF

cat > pyproject.toml << 'EOF'
[project]
name = "minimal-test"
version = "0.1.0"
EOF

# Build
pycontainer build --tag minimal:latest --verbose

# Inspect
tree dist/image
```

---

## 🚀 Next Steps

- **Deploy to Azure**: See [Azure Developer CLI Guide](azd-integration.md)
- **Set up CI/CD**: See [GitHub Actions Guide](github-actions.md)
- **Explore Plugins**: Check [plugins/](../plugins/)
- **Read Architecture**: See [ARCHITECTURE.md](../ARCHITECTURE.md)

---

## 💡 Tips & Best Practices

1. **Always use `--verbose`** during development to understand what's happening
2. **Use `--dry-run`** to preview builds before creating artifacts
3. **Test with multiple base images** (slim, alpine, distroless) to find the best fit
4. **Keep `pycontainer.toml`** in your project for consistent builds
5. **Use cache** for faster iterations (don't disable unless debugging cache issues)
6. **Version your images** with meaningful tags (not just `latest`)
7. **Generate SBOMs** for security compliance (`--sbom spdx`)
8. **Use reproducible builds** for consistent outputs (`--reproducible`, enabled by default)

---

## 🆘 Getting Help

- **GitHub Issues**: [spboyer/pycontainer-build](https://github.com/spboyer/pycontainer-build/issues)
- **Documentation**: See other files in [docs/](.)
- **Examples**: Check [examples/](../examples/)
- **Architecture**: Read [ARCHITECTURE.md](../ARCHITECTURE.md)

---

Happy building! 🐍🐳
