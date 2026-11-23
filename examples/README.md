# pycontainer-build Examples

This directory contains example applications demonstrating pycontainer-build integrations.

## Available Examples

### [Editor and Task Runners](./editor-task-runners/)

Project-scoped PyCharm, Vim, and Emacs commands, plus reusable GNU Make and
Go Task templates for builds and dry-runs.

### [FastAPI Application](./fastapi-app/)

A complete FastAPI web application showcasing all integration methods:

- ✅ Direct CLI usage
- ✅ Poetry plugin integration
- ✅ Hatch plugin integration  
- ✅ VS Code extension
- ✅ GitHub Actions workflow
- ✅ Azure Developer CLI deployment

**Key Features**:
- Framework auto-detection
- Dependency packaging
- Configuration examples for all tools
- Production-ready structure

[View Example →](./fastapi-app/)

### [Cross-Platform Builds](./cross-platform/)

Demonstrates building container images for different CPU architectures:

- ✅ Build AMD64 from ARM Macs
- ✅ Build ARM64 for AWS Graviton
- ✅ Multi-platform GitHub Actions
- ✅ Docker manifest lists

**Key Features**:
- Platform-specific base image selection
- Multi-architecture deployment patterns
- Real-world use cases and workflows
- Testing cross-platform images

[View Example →](./cross-platform/)

### [Skaffold Custom Builder](./skaffold/) and [Tilt custom_build](./tilt/)

Kubernetes inner-loop builds without a Docker daemon. pycontainer reads each
tool's environment contract and pushes to a registry the cluster can pull from.
See [Skaffold and Tilt](../docs/skaffold-tilt.md).

## Quick Start

Dependency builds of codename-less official Python tags use digest-checked
base-layer libc package metadata, not a guessed wheel floor. If evidence is
unavailable, pin a mapped release or provide supported base `PYTHON_LIBC`.

### Try the FastAPI Example

```bash
# Clone and navigate
cd examples/fastapi-app

# Build with pycontainer (CLI)
pycontainer build --tag fastapi-demo:latest --include-deps

# Or with Poetry
poetry self add poetry-pycontainer
poetry build-container --tag fastapi-demo:latest

# Or with Hatch
pip install hatch hatch-pycontainer
hatch build

# Or with VS Code
# Open in VS Code → Cmd+Shift+P → "Build Container Image"
```

## Example Structure

Each example includes:

1. **Application Code** - Working Python application
2. **Configuration** - pyproject.toml with all integration configs
3. **README** - Step-by-step usage instructions
4. **Requirements** - dependencies.txt or pyproject.toml
5. **Examples** - Workflow files and deployment configs

## Integration Comparison

| Method | Command | Configuration | Best For |
|--------|---------|--------------|----------|
| **CLI** | `pycontainer build` | Command-line flags | Quick builds, CI/CD |
| **Poetry** | `poetry build-container` | `[tool.pycontainer]` | Poetry projects |
| **Hatch** | `hatch build` | `[tool.hatch.build.hooks]` | Hatch projects |
| **VS Code** | Command Palette | Settings UI | IDE users |
| **GitHub Actions** | Workflow file | YAML inputs | Automation |
| **azd** | `azd up` | azure.yaml hooks | Azure deployment |
| **Skaffold** | `skaffold dev` | `skaffold.yaml` custom builder | Kubernetes inner loop |
| **Tilt** | `tilt up` | `Tiltfile` `custom_build` | Kubernetes inner loop |

## Contributing Examples

Want to add an example? Follow this template:

```
examples/my-example/
├── README.md              # Usage instructions
├── pyproject.toml         # Project config with all integrations
├── requirements.txt       # Python dependencies
├── app/                   # Application code
│   ├── __init__.py
│   └── main.py
└── .github/               # Optional: workflow examples
    └── workflows/
        └── build.yml
```

Include:
- Clear documentation
- All integration configs
- Working application code
- Deployment examples (optional)

## Quick Comparison

| Example | Complexity | Demonstrates | Best For |
|---------|-----------|--------------|----------|
| **FastAPI App** | Medium | Full integration suite | Learning all features |
| **Cross-Platform** | Simple | Multi-arch builds | ARM/AMD64 deployment |

## More Examples Coming Soon

- **Flask Web App** - Traditional web framework
- **Django Application** - Full-featured framework
- **CLI Tool** - Command-line application
- **Data Processing** - Batch job / worker
- **Microservices** - Multi-service architecture

## Support

- [Main Documentation](../README.md)
- [GitHub Actions Guide](../docs/github-actions.md)
- [Azure Developer CLI Guide](../docs/azd-integration.md)
- [Plugin Documentation](../plugins/)
