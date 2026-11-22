# pycontainer-build for VS Code

Build OCI container images from Python projects directly in Visual Studio Code — no Docker required.

## Features

- 🚀 **Build Containers** - Build OCI images with a single command
- 🔄 **Push to Registry** - Push images to GHCR, ACR, Docker Hub
- ⚙️ **Configuration** - Generate `pycontainer.toml` configuration
- 📊 **Output Channel** - View build progress in real-time
- 🐍 **Python Native** - No Docker daemon needed

## Requirements

- Visual Studio Code 1.80.0 or higher
- Python 3.11 or higher
- pycontainer-build 0.3.x installed in the selected Python environment

## Installation

### From VS Code Marketplace

1. Open VS Code
2. Press `Ctrl+Shift+X` (Windows/Linux) or `Cmd+Shift+X` (Mac)
3. Search for "pycontainer-build"
4. Click Install

### From VSIX

```bash
code --install-extension vscode-pycontainer-0.1.0.vsix
```

## Usage

### Build Container

1. Open a Python project with `pyproject.toml`
2. Open Command Palette (`Ctrl+Shift+P` or `Cmd+Shift+P`)
3. Type "pycontainer: Build Container Image"
4. Enter image tag (e.g., `myapp:latest`)
5. View build progress in the output panel

### Build and Push

1. Open Command Palette
2. Type "pycontainer: Build and Push Container Image"
3. Enter image tag (e.g., `ghcr.io/user/myapp:v1`)
4. Image will be built and pushed to registry

### Configure Build

1. Open Command Palette
2. Type "pycontainer: Configure Container Build"
3. A `pycontainer.toml` file will be created
4. Edit the configuration as needed

### Context Menu

Right-click on `pyproject.toml` in the Explorer:
- Select "Build Container Image"

## Configuration

### Extension Settings

Configure the extension in VS Code settings:

```json
{
  "pycontainer.pythonPath": "python",
  "pycontainer.defaultTag": "myapp:latest",
  "pycontainer.defaultBaseImage": "python:3.11-slim",
  "pycontainer.autoInstall": false,
  "pycontainer.showOutput": true,
  "pycontainer.verbose": false
}
```

### Project Configuration

Create `pycontainer.toml` in your project:

```toml
[build]
base_image = "python:3.11-slim"
workdir = "/app"
include_deps = true

[build.env]
ENV = "production"
PORT = "8080"
```

The extension invokes `python -m pycontainer.cli`; use the Python interpreter
selected by `pycontainer.pythonPath`. Registry pushes use the image tag entered
in the build prompt. Configure the registry credentials using the core
builder's supported credential providers.

## Development

Use Node.js 22.13 or later in the Node.js 22 line for the lint and packaging tools.

From the repository root, install the core into the selected Python
environment, then run the extension checks:

```bash
python -m pip install -e .
cd plugins/vscode-pycontainer
npm ci
npm run compile
npm run lint
npm test
npm run package
```

`npm test` includes a dry-run smoke test of the exact Python module and
arguments generated for the extension. The core package is not yet available
from PyPI; until its release, install it from this repository as shown above.
The automatic-install prompt uses pip and therefore requires a published
package. Automatic installation defaults off until the core is published.

## Commands

| Command | Description | Keyboard Shortcut |
|---------|-------------|-------------------|
| `pycontainer.build` | Build container image | - |
| `pycontainer.buildAndPush` | Build and push image | - |
| `pycontainer.configure` | Create configuration file | - |

## Examples

### Basic FastAPI App

```python
# main.py
from fastapi import FastAPI

app = FastAPI()

@app.get("/")
def read_root():
    return {"Hello": "World"}
```

```toml
# pyproject.toml
[project]
name = "myapi"
version = "1.0.0"
dependencies = ["fastapi", "uvicorn"]

[project.scripts]
start = "uvicorn main:app --host 0.0.0.0 --port 8000"
```

**Build**: Open Command Palette → "Build Container Image" → Enter `myapi:latest`

### Flask App with Configuration

```python
# app.py
from flask import Flask

app = Flask(__name__)

@app.route("/")
def hello():
    return "Hello World!"
```

```toml
# pycontainer.toml
[build]
base_image = "python:3.11-slim"
include_deps = true

[build.env]
FLASK_APP = "app"
FLASK_ENV = "production"
```

**Build**: Right-click `pyproject.toml` → "Build Container Image"

## Integration with GitHub Actions

Use the extension locally, then deploy with GitHub Actions:

```yaml
# .github/workflows/build.yml
name: Build Container

on:
  push:
    branches: [main]

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      - name: Build and push
        run: |
          python -m pip install 'pycontainer-build @ git+https://github.com/spboyer/pycontainer-build.git@d3ee7e0f5a1f342a42f9a43ad4efd335ec0497b7'
          pycontainer build --tag ghcr.io/${{ github.repository }}:latest --push
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```

## Troubleshooting

### "pycontainer-build not installed"

The extension can prompt to install it, but pip installation requires a
published package. Until then, install the core from a repository checkout
into the interpreter selected by `pycontainer.pythonPath`:

```bash
python -m pip install /path/to/pycontainer-build
```

### "No pyproject.toml found"

Ensure your workspace has a `pyproject.toml` file at the root.

### Build fails with "Permission denied"

For registry push, ensure you're authenticated:

```bash
# GitHub Container Registry
echo $GITHUB_TOKEN | docker login ghcr.io -u USERNAME --password-stdin

# Azure Container Registry
az acr login --name myregistry
```

### Python path not found

Configure the correct Python path in settings:

```json
{
  "pycontainer.pythonPath": "/usr/bin/python3.11"
}
```

## Development

### Building the Extension

```bash
cd plugins/vscode-pycontainer
npm install
npm run compile
```

### Packaging

```bash
npm run package
```

This creates `vscode-pycontainer-0.1.0.vsix`.

### Testing

1. Press F5 in VS Code to open Extension Development Host
2. Test commands in the new window

## Roadmap

- [ ] Interactive configuration wizard
- [ ] Build status in status bar
- [ ] Quick pick for recent images
- [ ] Integration with Docker extension
- [ ] Multi-platform build support
- [ ] Build history view

## Contributing

Contributions welcome! See [CONTRIBUTING.md](../../CONTRIBUTING.md).

## License

MIT License - See LICENSE file for details.

## Related Projects

- [pycontainer-build](https://github.com/spboyer/pycontainer-build) - Core builder
- [poetry-pycontainer](../poetry-pycontainer/) - Poetry plugin
- [hatch-pycontainer](../hatch-pycontainer/) - Hatch plugin

## Support

- [GitHub Issues](https://github.com/spboyer/pycontainer-build/issues)
- [Documentation](https://github.com/spboyer/pycontainer-build/tree/main/docs)
