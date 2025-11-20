# pycontainer-build AI Coding Agent Instructions

## Project Overview

This is an **experimental Docker-free OCI image builder** for Python projects, inspired by .NET's native container tooling. It creates OCI-compliant container images without requiring Docker daemon or Dockerfiles—just pure Python code that generates the OCI layout directly.

**Core Philosophy**: Build container images programmatically using native Python APIs, not shell commands or external daemons.

## Architecture

### Component Hierarchy
```
cli.py (entry point)
  └─> builder.py (orchestrates build)
       ├─> config.py (build configuration)
       ├─> project.py (Python project introspection)
       ├─> oci.py (OCI spec structs)
       └─> fs_utils.py (file system helpers)
```

### Key Architectural Decisions

1. **Smart Base Image Selection**: Auto-detects Python version from `requires-python` in `pyproject.toml` and constructs base image name (e.g., `>=3.11` → `python:3.11-slim`). Users can override with `--base-image` flag. Base images are always required—every build pulls and layers on a Python runtime.

2. **Multi-Layer Output**: Creates base image layers + optional dependency layer + application layer. All layers are written to `dist/image/blobs/sha256/<digest>`. The OCI manifest + config are written to `dist/image/`.

3. **Entrypoint Auto-Detection**: `project.py` reads `pyproject.toml` → looks for `[project.scripts]` → converts first script to Python module invocation. Falls back to `python -m app` if nothing found.

4. **Smart Include Paths**: `default_include_paths()` auto-discovers `src/`, `app/`, or `package/` dirs + essential files (`pyproject.toml`, `requirements.txt`). Include entire directories, not individual files.

## Code Style

**Ultra-minimalist, single-line Python**: The codebase uses aggressive compaction (semicolons, compact imports). This is intentional for the experimental nature.

```python
# This style is used throughout:
def ensure_dir(path):
    p=Path(path); p.mkdir(parents=True, exist_ok=True); return p
```

**When contributing**:
- Match this compact style—use semicolons for simple multi-statement lines
- No docstrings on trivial functions (code is self-documenting)
- Prefer dataclasses over dicts for structured data (see `BuildConfig`, `OCILayer`)

## Critical Workflows

### Building an Image
```bash
# Install in editable mode first
pip install -e .

# Build with defaults (creates dist/image/)
pycontainer build

# Build with custom tag
pycontainer build --tag myapp:v1 --context /path/to/app
```

**Output Structure**:
```
dist/image/
  ├── manifest.json          # OCI manifest
  └── blobs/sha256/
      ├── <layer-digest>     # Tar file of app files
      └── <config-digest>    # JSON config blob
```

### Testing Changes
Since this is a builder tool, test by running it against sample Python projects:
```bash
# Create a test project
mkdir test_app && cd test_app
echo 'print("Hello")' > app.py
echo '[project]\nname="test"\nversion="0.1.0"' > pyproject.toml

# Build container for it
pycontainer build --context . --tag test:latest
```

## Key Integration Points

### OCI Spec Compliance
- `oci.py`: Implements OCI Image Manifest v1 (`application/vnd.oci.image.manifest.v1+json`)
- Manifests use `schemaVersion: 2` with proper media types
- Layer digests are SHA256, format: `sha256:<hexdigest>`

### File System Operations
- `builder.py` creates tar archives using Python's `tarfile` module directly
- Archive paths are prefixed with `workdir` (default `/app/`)—e.g., `src/foo.py` → `/app/src/foo.py` in tar
- Temporary tar written as `app-layer.tar`, then renamed to digest-based name

### Environment Variables
Set via `BuildConfig.env` dict—serialized to OCI config as `"Env": ["KEY=value"]` list.

## Common Patterns

### Extending BuildConfig
To add new fields (e.g., labels, volumes):
1. Add field to `BuildConfig` dataclass in `config.py`
2. Pass to `build_config_json()` in `oci.py`
3. Update OCI config JSON structure (see OCI spec)

### Adding CLI Flags
Pattern in `cli.py`:
```python
b.add_argument("--new-flag", default="value")
# Then pass args.new_flag to BuildConfig constructor
```

### Custom File Filters
To exclude patterns (e.g., `__pycache__`), modify `iter_files()` in `fs_utils.py`:
```python
if child.is_file() and not any(child.match(p) for p in exclude):
    yield child, child.relative_to(base)
```

## Known Limitations (Do Not "Fix" Without Discussion)

1. **Single architecture support**: Metadata supports multi-arch but no actual cross-compilation yet.
2. **Limited framework detection**: Supports FastAPI, Flask, Django only (easy to extend).
3. **SBOM scope**: Python packages only; doesn't parse OS packages from base images.

These are intentional scope limitations for the current phase.

## Dependencies

- **Zero runtime dependencies**: Pure stdlib (pathlib, tarfile, hashlib, json)
- Python 3.10+ required (uses `tomllib` for TOML parsing, added in 3.11 but backported to 3.10)
- No Docker, no external CLI tools

## File Layout Conventions

- All source in `src/pycontainer/` (importable as `pycontainer` package)
- Entry point: `pyproject.toml` declares `pycontainer = "pycontainer.cli:main"`
- No tests yet (experimental stage)
- No docs/ directory—README is minimal by design
