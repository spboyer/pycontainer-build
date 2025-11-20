# pycontainer-build Plugins

This directory contains toolchain integrations for pycontainer-build, enabling seamless container image building across different Python development workflows.

## Available Plugins

### [Poetry Plugin](./poetry-pycontainer/)

Build containers with Poetry projects using the `poetry build-container` command.

```bash
poetry self add poetry-pycontainer
poetry build-container --tag myapp:latest --push
```

**Features:**
- Integrates with Poetry's build system
- Reads configuration from `[tool.pycontainer]` in pyproject.toml
- Automatically includes Poetry dependencies
- Adds Poetry metadata to container labels

### [Hatch Plugin](./hatch-pycontainer/)

Build containers during Hatch builds with automatic integration.

```bash
pip install hatch-pycontainer
hatch build  # Builds both wheel and container
```

**Features:**
- Hooks into Hatch's build process
- Builds container alongside Python packages
- Configurable via `[tool.hatch.build.hooks.pycontainer]`
- Environment-specific builds (dev, prod)

### [VS Code Extension](./vscode-pycontainer/)

Build containers directly from VS Code with UI commands and configuration.

```
Install from VS Code Marketplace or:
code --install-extension vscode-pycontainer-0.1.0.vsix
```

**Features:**
- Command palette integration
- Real-time build output
- Configuration wizard
- Auto-install pycontainer-build
- Context menu actions

## Installation

Each plugin is independently installable. Choose the one(s) that match your workflow:

| Tool | Installation | Usage |
|------|--------------|-------|
| Poetry | `poetry self add poetry-pycontainer` | `poetry build-container` |
| Hatch | `pip install hatch-pycontainer` | `hatch build` |
| VS Code | Install from marketplace | Command palette |

## Configuration

### Poetry (pyproject.toml)

```toml
[tool.pycontainer]
tag = "myapp:latest"
base_image = "python:3.11-slim"
registry = "ghcr.io/user/myapp"
include_deps = true
push = false

[tool.pycontainer.env]
ENV = "production"

[tool.pycontainer.labels]
maintainer = "team@example.com"
```

### Hatch (pyproject.toml)

```toml
[tool.hatch.build.hooks.pycontainer]
tag = "myapp:latest"
base-image = "python:3.11-slim"
include-deps = true
push = false

[tool.hatch.build.hooks.pycontainer.env]
ENV = "production"
```

### VS Code (settings.json)

```json
{
  "pycontainer.pythonPath": "python",
  "pycontainer.defaultTag": "myapp:latest",
  "pycontainer.defaultBaseImage": "python:3.11-slim",
  "pycontainer.autoInstall": true,
  "pycontainer.verbose": false
}
```

## Common Use Cases

### Local Development

```bash
# Poetry
poetry build-container --tag myapp:dev

# Hatch
hatch build

# VS Code
Cmd+Shift+P → "Build Container Image"
```

### CI/CD Pipeline

```yaml
# GitHub Actions with Poetry
- name: Build container
  run: |
    poetry self add poetry-pycontainer
    poetry build-container --tag ghcr.io/${{ github.repository }}:${{ github.sha }} --push
  env:
    GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```

### Multi-Environment Builds

```toml
# Development
[tool.hatch.envs.dev.build.hooks.pycontainer]
tag = "myapp:dev"
base-image = "python:3.11"

# Production
[tool.hatch.envs.prod.build.hooks.pycontainer]
tag = "myapp:latest"
base-image = "python:3.11-slim"
sbom = "spdx"
```

## Plugin Development

Each plugin is a standalone package that integrates with pycontainer-build's Python API:

```python
from pycontainer.config import BuildConfig
from pycontainer.builder import ImageBuilder

config = BuildConfig(
    tag="myapp:latest",
    context_path=".",
    base_image="python:3.11-slim",
    include_deps=True
)

builder = ImageBuilder(config)
builder.build()
```

### Creating a New Plugin

1. Create plugin directory: `plugins/my-tool-pycontainer/`
2. Import pycontainer API: `from pycontainer import BuildConfig, ImageBuilder`
3. Implement tool-specific integration
4. Add tests and documentation
5. Publish to PyPI (optional)

## Testing

Each plugin has its own test suite. To test all plugins:

```bash
# Poetry plugin
cd plugins/poetry-pycontainer
pip install -e .
pytest

# Hatch plugin
cd plugins/hatch-pycontainer
pip install -e .
pytest

# VS Code extension
cd plugins/vscode-pycontainer
npm install
npm test
```

## Publishing

Plugins can be published independently:

```bash
# Poetry plugin
cd plugins/poetry-pycontainer
poetry build
poetry publish

# Hatch plugin
cd plugins/hatch-pycontainer
hatch build
hatch publish

# VS Code extension
cd plugins/vscode-pycontainer
vsce package
vsce publish
```

## Roadmap

### Planned Plugins

- **Azure Developer CLI (azd)** - Native integration for `azd up`
- **PDM Plugin** - Support for PDM package manager
- **Pipenv Plugin** - Integration with Pipenv
- **Flit Plugin** - Build hook for Flit projects

### Future Enhancements

- Interactive configuration wizards
- Template generators (Cookiecutter/Copier)
- IDE integrations (PyCharm, JetBrains)
- Cloud-specific plugins (AWS, GCP, Azure)

## Contributing

We welcome plugin contributions! Please:

1. Follow the existing plugin structure
2. Include comprehensive README and examples
3. Add tests for your integration
4. Update this README with your plugin

See [CONTRIBUTING.md](../CONTRIBUTING.md) for detailed guidelines.

## Support

- [GitHub Issues](https://github.com/spboyer/pycontainer-build/issues)
- [Documentation](https://github.com/spboyer/pycontainer-build/tree/main/docs)
- [Main Repository](https://github.com/spboyer/pycontainer-build)

## License

All plugins are released under the MIT License. See individual plugin directories for license files.
