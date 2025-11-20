# Dev.to Blog Post

## Metadata

```yaml
title: "Building Container Images Without Docker: Introducing pycontainer-build"
published: false
description: A Docker-free OCI image builder for Python projects, inspired by .NET's native container tooling
tags: python, docker, devops, opensource
cover_image: # Add URL to cover image (recommended: 1000x420px)
canonical_url: # Add canonical URL if cross-posting
series: # Optional series name
```

---

## Building Container Images Without Docker: Introducing pycontainer-build

What if you could build production-ready container images for your Python projects without installing Docker, writing Dockerfiles, or dealing with daemon dependencies? That's the vision behind **pycontainer-build** — a native, Docker-free container image builder for Python.

### The Problem with Traditional Container Builds

Today, containerizing Python applications typically requires:

1. **Installing Docker Desktop or Docker Engine** — Not always possible in locked-down corporate environments, cloud IDEs like GitHub Codespaces, or CI/CD runners
2. **Writing and maintaining Dockerfiles** — Boilerplate, multi-stage builds, and keeping base images updated
3. **Understanding Docker-specific concepts** — Layers, build contexts, caching strategies
4. **Docker-in-Docker in CI** — Complex and fragile setups with privileged containers

These friction points slow down developer onboarding and create unnecessary complexity for a straightforward task: packaging Python code into a container.

### Enter pycontainer-build

Inspired by [.NET's native container support](https://devblogs.microsoft.com/dotnet/announcing-builtin-container-support-for-the-dotnet-sdk/) and tools like [Jib](https://github.com/GoogleContainerTools/jib) (Java) and [ko](https://github.com/ko-build/ko) (Go), **pycontainer-build** provides a Python-native way to create OCI-compliant container images.

Here's what it looks like:

```bash
# Install
pip install -e .

# Build a container image
pycontainer build --tag myapp:latest

# That's it!
```

No Dockerfile. No Docker daemon. Just pure Python creating OCI images.

### How It Works

pycontainer-build introspects your Python project and automatically:

1. **Detects your Python version** from `requires-python` in `pyproject.toml`
2. **Selects the right base image** (e.g., `python:3.11-slim`)
3. **Finds your entry points** from `[project.scripts]` in `pyproject.toml`
4. **Packages your application files** into layers
5. **Creates an OCI-compliant image layout** ready for registries

Under the hood, it:
- Uses only Python stdlib (no external dependencies!)
- Creates proper OCI image manifests and configs
- Generates content-addressable layers (SHA256 digests)
- Supports pushing to any OCI registry (GHCR, ACR, Docker Hub, private registries)

### Real-World Example

Let's say you have a FastAPI application:

```python
# app.py
from fastapi import FastAPI

app = FastAPI()

@app.get("/")
def read_root():
    return {"message": "Hello from pycontainer!"}
```

```toml
# pyproject.toml
[project]
name = "myapi"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["fastapi", "uvicorn[standard]"]

[project.scripts]
myapi = "uvicorn app:app --host 0.0.0.0 --port 8080"
```

Build and push with one command:

```bash
pycontainer build \
  --tag ghcr.io/myorg/myapi:latest \
  --include-deps \
  --push
```

pycontainer-build will:
- Pull `python:3.11-slim` as the base image
- Package your `app.py` and dependencies
- Configure the entrypoint to run uvicorn
- Push the image to GitHub Container Registry

**Compare this to a typical Dockerfile approach:**

```dockerfile
# Dockerfile
FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Expose port
EXPOSE 8080

# Run the application
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
```

```bash
docker build -t ghcr.io/myorg/myapi:latest .
docker push ghcr.io/myorg/myapi:latest
```

The Dockerfile approach requires Docker installed, manual maintenance of the Dockerfile, and explicit knowledge of best practices (multi-stage builds, layer optimization, etc.). pycontainer-build handles all of this automatically.

### Key Features

#### 🎯 Zero Docker Dependencies
Pure Python implementation using only stdlib. Works in environments where Docker isn't available or allowed.

#### 🧠 Smart Auto-Detection
- Python version from `pyproject.toml`
- Entry points from `[project.scripts]`
- Project structure (`src/`, `app/`, or package directories)
- Web frameworks (FastAPI, Flask, Django)

#### 📦 Base Image Support
Build on top of official Python images, distroless, or custom base images. Properly merges layers and configuration.

#### 🚀 Registry Integration
Push to any OCI-compatible registry with automatic authentication:
- GitHub Container Registry (GHCR)
- Azure Container Registry (ACR)
- Docker Hub
- Private registries

#### ⚡ Layer Caching
Content-addressable caching with LRU eviction. Only rebuild layers that changed.

#### 🔒 Security & Compliance
- SBOM generation (SPDX 2.3 or CycloneDX 1.4)
- Reproducible builds with deterministic timestamps
- No secrets in images (proper credential handling)

#### 🔌 Toolchain Integrations
- **Poetry plugin**: `poetry build-container`
- **Hatch plugin**: `hatch build`
- **GitHub Actions**: Reusable workflow
- **Azure Developer CLI**: Custom build hooks
- **VS Code extension**: Build from command palette

### Use Cases

**1. CI/CD Without Docker**
```yaml
# GitHub Actions
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
      - run: pip install pycontainer-build
      - run: pycontainer build --tag ghcr.io/${{ github.repository }}:latest --push
```

No need for Docker daemon or privileged containers.

**2. Local Development**
```bash
# Quick iteration
pycontainer build --tag myapp:dev
podman run -p 8080:8080 myapp:dev
```

**3. Multi-Service Applications**
```bash
# Build multiple services
pycontainer build --context ./api --tag myapp-api:latest
pycontainer build --context ./worker --tag myapp-worker:latest
pycontainer build --context ./frontend --tag myapp-frontend:latest
```

**4. Restricted Environments**
- GitHub Codespaces (no Docker daemon)
- Azure Dev Box
- Corporate locked-down workstations
- Air-gapped environments (offline builds)

### Configuration Options

#### Command-Line Interface
```bash
pycontainer build \
  --tag myapp:v1.2.3 \
  --base-image python:3.11-slim \
  --include-deps \
  --workdir /app \
  --env KEY=value \
  --platform linux/amd64 \
  --sbom spdx \
  --config pycontainer.toml \
  --verbose \
  --push
```

#### Configuration File (`pycontainer.toml`)
```toml
[build]
base_image = "python:3.11-slim"
workdir = "/app"
include_deps = true
reproducible = true

[build.labels]
maintainer = "team@example.com"
version = "1.0.0"

[build.env]
PORT = "8080"
ENV = "production"

[registry]
url = "ghcr.io/myorg/myapp"
```

#### Python API
```python
from pycontainer.config import BuildConfig
from pycontainer.builder import ImageBuilder

config = BuildConfig(
    tag="myapp:latest",
    context_path=".",
    base_image="python:3.11-slim",
    include_deps=True,
    env={"ENV": "production"},
    generate_sbom="spdx",
    reproducible=True
)

builder = ImageBuilder(config)
builder.build()
```

Perfect for integration with build tools, AI agents, and automation scripts.

### Under the Hood: OCI Image Spec

pycontainer-build creates compliant OCI images following the [OCI Image Specification](https://github.com/opencontainers/image-spec):

```
dist/image/
  ├── index.json              # OCI index (manifest list)
  ├── oci-layout              # Version marker
  ├── blobs/sha256/
  │   ├── <manifest-digest>   # Manifest blob
  │   ├── <config-digest>     # Config blob
  │   └── <layer-digest>      # Application layer (tar)
  └── refs/tags/
      └── <tag-name>          # Tag reference
```

Each layer is:
1. Created as a tar archive with proper paths (`/app/src/`, `/app/pyproject.toml`)
2. Compressed (optional)
3. Hashed with SHA256 for content-addressable storage
4. Referenced in the manifest by digest

This means images are:
- **Portable** — Work with any OCI-compatible runtime (Docker, Podman, containerd, CRI-O)
- **Cacheable** — Layers shared across images save space
- **Verifiable** — SHA256 digests ensure integrity

### Comparison with Other Tools

| Feature | pycontainer | Docker/Dockerfile | Jib (Java) | ko (Go) |
|---------|-------------|-------------------|------------|---------|
| No Docker daemon | ✅ | ❌ | ✅ | ✅ |
| Language-native | ✅ Python | ❌ Generic | ✅ Java | ✅ Go |
| Zero config | ✅ | ❌ Requires Dockerfile | ✅ | ✅ |
| Auto-detects dependencies | ✅ | ❌ | ✅ | ✅ |
| Pure stdlib | ✅ | N/A | ❌ | ✅ |
| Python-specific optimizations | ✅ | ❌ | N/A | N/A |

### Current Limitations

This is an **experimental project**. Known limitations:

- **Multi-arch builds**: Platform flag sets metadata only; no actual cross-compilation yet
- **Framework detection**: Supports FastAPI, Flask, Django (easy to extend)
- **SBOM scope**: Python packages only (doesn't parse OS packages from base images)

These are planned for future phases. Contributions welcome!

### Getting Started

#### Installation

```bash
# Install from source (PyPI package coming soon)
git clone https://github.com/spboyer/pycontainer-build.git
cd pycontainer-build
pip install -e .
```

#### Quick Example

```bash
# Create a sample app
mkdir myapp && cd myapp
echo 'print("Hello from container!")' > app.py
cat > pyproject.toml << EOF
[project]
name = "myapp"
version = "0.1.0"
requires-python = ">=3.11"
EOF

# Build container
pycontainer build --tag myapp:latest

# Output location
ls dist/image/
```

#### Push to GitHub Container Registry

```bash
# Authenticate (uses GitHub CLI or GITHUB_TOKEN env var)
gh auth login

# Build and push
pycontainer build \
  --tag ghcr.io/yourusername/myapp:latest \
  --push
```

### Why This Matters

#### For Python Developers
- **Simpler workflow** than writing Dockerfiles
- **Faster onboarding** to containerization
- **No Docker Desktop licensing concerns**
- **Native Python experience** (feels like `pip` or `poetry`)

#### For DevOps Teams
- **Eliminate Docker-in-Docker** complexity in CI/CD
- **Faster builds** with intelligent caching
- **Security compliance** with SBOM generation
- **Reproducible builds** with deterministic layers

#### For the Python Ecosystem
- **Modern, standards-based** approach to containers
- **Foundation for tool integrations** (Poetry, Hatch, Azure DevOps)
- **Opens possibilities** for Python in cloud-native environments
- **Aligns with .NET/Java** container strategies (unified multi-language story)

### Roadmap

**Completed** (Phases 0-4):
- ✅ Core OCI image generation
- ✅ Registry push support
- ✅ Base image support
- ✅ Layer caching
- ✅ SBOM generation
- ✅ Toolchain integrations (Poetry, Hatch, GitHub Actions, VS Code)
- ✅ Framework auto-detection

**Coming Soon**:
- 🔜 Full multi-arch builds (ARM64, AMD64)
- 🔜 PyPI package distribution
- 🔜 More framework support (Streamlit, Gradio, etc.)
- 🔜 Build optimization (parallel layer creation)
- 🔜 Integration with more CI/CD platforms

### Try It Out and Share Feedback!

This project is experimental and evolving based on community feedback. I'd love to hear:

- **What use cases resonate with you?**
- **What blockers do you face with current Docker workflows?**
- **What features would make this production-ready for your team?**

**Links:**
- 📦 **Repository**: [github.com/spboyer/pycontainer-build](https://github.com/spboyer/pycontainer-build)
- 💬 **Feedback & Issues**: [github.com/spboyer/pycontainer-build/issues](https://github.com/spboyer/pycontainer-build/issues)
- 📖 **Documentation**: See [README](https://github.com/spboyer/pycontainer-build#readme) for more details

If you find this useful, give it a star ⭐ on GitHub and share your experience!

---

### Acknowledgments

Inspired by:
- [.NET SDK's native container support](https://github.com/dotnet/sdk-container-builds)
- [Jib (Java)](https://github.com/GoogleContainerTools/jib) — Daemonless container builds
- [ko (Go)](https://github.com/ko-build/ko) — Simple container images for Go

Built with ❤️ for the Python community by the Microsoft Python & Azure tooling teams.

---

## Discussion Questions

*Add these as prompts at the end to encourage engagement:*

1. How do you currently containerize your Python applications? What pain points do you experience?
2. Would a Docker-free approach fit into your workflow? What concerns would you have?
3. What Python frameworks or tools would you want to see auto-detected?
4. Are there specific CI/CD platforms you'd like to see integrated?

---

## Publishing Tips

**Before Publishing:**
- [ ] Add a cover image (1000x420px recommended)
- [ ] Set `published: true` in metadata
- [ ] Choose 3-4 relevant tags (max 4 on dev.to)
- [ ] Consider adding code screenshots or diagrams
- [ ] Optionally create a canonical URL if cross-posting

**Engagement Strategy:**
- Reply to all comments within 24 hours
- Share article link on Twitter/LinkedIn referencing the post
- Add to relevant dev.to series if you have one
- Cross-post to Medium, Hashnode, or other platforms
- Consider recording a video walkthrough and embedding it

**SEO Tips:**
- Use "python container" and "docker alternative" in title/description
- Include "OCI" and "cloud native" for discoverability
- Link to related articles you've written
- Update with "Edit:" sections as project evolves

**Timing:**
- Best posting times: Monday-Wednesday mornings (EST)
- Avoid posting during major tech news events
- Consider scheduling for Python community events (PyCon, local meetups)
