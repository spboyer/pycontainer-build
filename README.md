# 🐍 pycontainer-build

> **Build OCI container images from Python projects — no Docker required**

A native, Docker-free container image builder for Python, inspired by .NET's `PublishContainer`. Create production-ready OCI images using pure Python, without Dockerfiles or Docker daemon.

---

## 🎯 Why This Exists

Today, containerizing Python applications requires:
- Writing and maintaining Dockerfiles
- Installing Docker Desktop or Docker Engine
- Understanding Docker-specific concepts and commands
- Managing multi-stage builds for dependencies

**pycontainer-build** provides a simpler path:

```bash
pycontainer build
```

That's it. No Dockerfile. No Docker daemon. Just pure Python creating OCI-compliant container images.

This mirrors the elegant developer experience that .NET provides with its SDK's native container publishing — now available for Python.

---

## 🚀 Quick Start

### Installation

```bash
# Install from source (PyPI package coming soon)
pip install -e .
```

### Build Your First Image

```bash
# From your Python project root
pycontainer build --tag myapp:latest

# With custom context
pycontainer build --tag myapp:v1 --context /path/to/app
```

### Output

Creates an OCI image layout at `dist/image/`:
```
dist/image/
  ├── manifest.json              # OCI manifest
  └── blobs/sha256/
      ├── <layer-digest>         # Application layer (tar)
      └── <config-digest>        # Image config (JSON)
```

---

## ✨ Features

### Current Capabilities (Phase 0 ✅)

- ✅ **Zero Docker dependencies** — Pure Python implementation
- ✅ **Auto-detects Python project structure** — Finds `src/`, `app/`, entry points
- ✅ **Infers entrypoints** — Reads `pyproject.toml` scripts, falls back to `python -m`
- ✅ **Creates OCI-compliant images** — Proper manifests, configs, and layers
- ✅ **Command-line interface** — Simple `pycontainer build` workflow
- ✅ **Programmatic API** — Use as a library in your tools

### Coming Soon

- 🔜 **Registry push support** — Direct push to GHCR, ACR, Docker Hub (Phase 1)
- 🔜 **Base image layering** — Build on top of `python:3.11-slim`, distroless, etc. (Phase 2)
- 🔜 **Dependency packaging** — Include pip-installed packages (Phase 2)
- 🔜 **Multi-architecture builds** — ARM64, AMD64 support (Phase 3)
- 🔜 **Caching & layer reuse** — Fast incremental builds (Phase 1)

---

## 📖 How It Works

### Architecture Overview

```
cli.py (entry point)
  └─> builder.py (orchestrates build)
       ├─> config.py (build configuration)
       ├─> project.py (Python project introspection)
       ├─> oci.py (OCI spec structs)
       └─> fs_utils.py (file system helpers)
```

### Build Process

1. **Project Discovery** — Reads `pyproject.toml`, detects entry points and structure
2. **File Collection** — Gathers source files based on auto-detected or configured paths
3. **Layer Creation** — Packs files into tar archive with correct `/app/` prefixes
4. **OCI Generation** — Creates manifest and config JSON per OCI Image Spec v1
5. **Output** — Writes image layout to disk (registry push coming in Phase 1)

---

## 🧩 Programmatic Usage

Use as a library in your Python tools:

```python
from pycontainer.config import BuildConfig
from pycontainer.builder import ImageBuilder

config = BuildConfig(
    tag="myapp:latest",
    context_path="/path/to/app",
    env={"ENV": "production"},
    include_paths=["src/", "pyproject.toml"]
)

builder = ImageBuilder(config)
builder.build()  # Creates dist/image/
```

Perfect for integration with:
- **Azure Developer CLI (azd)** — Custom build strategies
- **GitHub Actions** — Automated CI/CD workflows
- **Poetry/Hatch** — Build plugins
- **AI agents** — Copilot, MCP servers, automated scaffolding

---

## 🎓 Configuration

### Auto-Detection (Zero Config)

By default, `pycontainer` auto-detects:

- **Entry point**: First `[project.scripts]` entry in `pyproject.toml`
- **Include paths**: `src/`, `app/`, or `<package>/` dirs + `pyproject.toml`, `requirements.txt`
- **Working directory**: `/app/`
- **Architecture**: `amd64/linux`

### Explicit Configuration

```bash
pycontainer build \
  --tag myapp:v1.2.3 \
  --context /my/project \
  --workdir /app \
  --env KEY=value \
  --env ANOTHER=value
```

### Python API

```python
BuildConfig(
    tag="myapp:latest",
    context_path=".",
    workdir="/app",
    env={"DEBUG": "false"},
    include_paths=["src/", "lib/", "pyproject.toml"],
    entrypoint=["python", "-m", "myapp"]
)
```

---

## 🗺️ Roadmap

### ✅ **Phase 0: Foundation** (COMPLETE)

- Core OCI image generation
- Basic CLI and Python API
- Project introspection and auto-detection
- File packing and layer creation

### 🚧 **Phase 1: Registry & Caching** (In Progress)

- [ ] Implement complete OCI image layout (index.json, refs/)
- [ ] Push images to registries via Docker Registry v2 API
- [ ] Support authentication (GHCR, ACR, Docker Hub, private registries)
- [ ] Add layer caching and reuse logic
- [ ] Digest verification and content-addressable storage

### 📋 **Phase 2: Base Images & Dependencies**

- [ ] Pull and parse base image manifests
- [ ] Layer Python app files on top of base images
- [ ] Support slim, distroless, and custom base images
- [ ] Package pip-installed dependencies into layers
- [ ] Respect base image configuration (env, labels, user)

### 📋 **Phase 3: Toolchain Integrations**

- [ ] Poetry plugin (`poetry build --container`)
- [ ] Hatch build hook
- [ ] Azure Developer CLI (azd) integration
- [ ] GitHub Actions reusable workflow
- [ ] VS Code extension / Copilot templates

### 📋 **Phase 4: Polish & Production Readiness**

- [ ] Framework auto-detection (FastAPI, Flask, Django)
- [ ] `pycontainer.toml` configuration schema
- [ ] SBOM (Software Bill of Materials) generation
- [ ] Reproducible builds (deterministic layer creation)
- [ ] Multi-architecture support (ARM64)
- [ ] Verbose logging and diagnostics

---

## 🎯 Design Goals

### **1. Native Python Experience**
Container building should feel like a native Python operation, not a Docker side quest.

### **2. Zero External Dependencies**
No Docker daemon, CLI tools, or system packages required. Pure Python stdlib + OCI specs.

### **3. Language-Integrated**
Understand Python projects natively — entry points, modules, dependencies, project structure.

### **4. AI-Friendly API**
Simple, programmable interface for agentic workflows and Copilot-generated scaffolding.

### **5. Cross-Platform & Daemonless**
Works in GitHub Codespaces, Dev Box, locked-down environments — anywhere Python runs.

---

## 🤝 Why This Matters

### For Python Developers
- Simpler workflow than Dockerfiles
- No Docker Desktop licensing concerns
- Faster onboarding for containerization

### For Microsoft & Azure
- Unified multi-language container story (aligns with .NET, Java/Jib)
- Enables Dockerless Azure Developer CLI workflows
- First-class integration with Copilot and agentic systems
- Better dev experience in Codespaces and cloud dev environments

### For the Python Ecosystem
- A modern, standards-based approach to container builds
- Foundation for Poetry, Hatch, and other tool integrations
- Opens new possibilities for Python in cloud-native environments

---

## 🔬 Current Limitations (By Design)

These are intentional scope limitations for the experimental phase:

- **No base image support yet** — Only creates application layers (Phase 2)
- **No registry push** — Local OCI layout only (Phase 1)
- **No dependency packaging** — Expects dependencies in context (Phase 2)
- **Single architecture** — `amd64/linux` only (Phase 4)

---

## 🛠️ Development

### Prerequisites

- Python 3.10+ (uses `tomllib` for TOML parsing)
- No other dependencies — pure stdlib

### Install for Development

```bash
git clone https://github.com/microsoft/pycontainer-build.git
cd pycontainer-build
pip install -e .
```

### Test a Build

```bash
# Create a test project
mkdir test_app && cd test_app
echo 'print("Hello from container!")' > app.py
cat > pyproject.toml << EOF
[project]
name = "test-app"
version = "0.1.0"
EOF

# Build it
pycontainer build --tag test:latest
```

### Code Style

This codebase uses **ultra-minimalist, compact Python**:
- Semicolons for simple multi-statement lines
- No docstrings on trivial functions
- Aggressive use of pathlib and comprehensions
- Dataclasses over dicts for structured data

This style is intentional for the experimental phase.

---

## 📚 Resources

- **OCI Image Spec**: https://github.com/opencontainers/image-spec
- **Docker Registry v2 API**: https://docs.docker.com/registry/spec/api/
- **.NET Native Containers**: https://devblogs.microsoft.com/dotnet/announcing-builtin-container-support-for-the-dotnet-sdk/
- **Project Tracking**: See `IMPLEMENTATION_PLAN.md` for detailed roadmap

---

## 📄 License

MIT License - See [LICENSE](LICENSE) for details

---

## 🙏 Acknowledgments

Inspired by:
- [.NET SDK's native container support](https://github.com/dotnet/sdk-container-builds)
- [Jib (Java)](https://github.com/GoogleContainerTools/jib) — Daemonless container builds
- [ko (Go)](https://github.com/ko-build/ko) — Simple container images for Go

---

**Built with ❤️ by the Microsoft Python & Azure tooling teams**
