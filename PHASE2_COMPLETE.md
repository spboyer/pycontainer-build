# Phase 2: Base Images & Dependencies - COMPLETE ✅

## Overview

Phase 2 has been successfully implemented, enabling pycontainer-build to layer Python applications on top of base images (e.g., `python:3.11-slim`) and package dependencies efficiently.

## Implemented Features

### 2.1: Base Image Pull & Parsing ✅
- **Registry Client Extensions**: Added `pull_manifest()` and `pull_blob()` methods to `RegistryClient`
- **Multi-arch Support**: Automatically selects `linux/amd64` platform from manifest lists
- **Config Parsing**: Extracts and parses base image config (env, workdir, user, labels)
- **Layer Caching**: Base image layers are cached locally to avoid re-downloading

**Usage:**
```bash
pycontainer build --base-image python:3.11-slim --tag myapp:v1
```

### 2.2: Layer Merging & Multi-Layer Images ✅
- **Preserved Layer Order**: Base layers come first, then dependencies, then app layers
- **Config Merging**: 
  - Environment variables: Base + user (user overrides)
  - Working directory: User value or base value
  - Labels: Merged dictionaries (user overrides)
  - User: Inherited from base unless overridden
  - Entrypoint/Cmd: Smart merging with distroless detection

**Example:**
```python
from pycontainer import BuildConfig, ImageBuilder

config = BuildConfig(
    tag="myapp:v1",
    base_image="python:3.11-slim",
    env={"DEBUG": "true"},
    labels={"version": "1.0"}
)
builder = ImageBuilder(config)
builder.build()
```

### 2.3: Dependency Packaging ✅
- **Virtual Environment Detection**: Automatically finds `venv/`, `.venv/`, or `env/`
- **Site-packages Inclusion**: Packages all files from `site-packages/` into separate layer
- **Requirements.txt Fallback**: If no venv found, includes `requirements.txt`
- **Separate Layer**: Dependencies in their own layer for efficient caching

**Usage:**
```bash
pycontainer build --include-deps --requirements requirements.txt --tag myapp:v1
```

### 2.4: Distroless & Slim Base Image Support ✅
- **Distroless Detection**: Identifies distroless images via labels
- **Shell-less Entrypoint**: Uses `Cmd` instead of shell-based entrypoint for distroless
- **Compatible Bases**:
  - `python:3.11-slim` ✅
  - `python:3.11-alpine` ✅
  - `gcr.io/distroless/python3` ✅
  - `mcr.microsoft.com/python/distroless` ✅

## New Configuration Options

```python
@dataclass
class BuildConfig:
    base_image: Optional[str] = None          # Base image to layer on
    labels: Optional[Dict[str, str]] = None   # Image labels
    user: Optional[str] = None                # Run-as user
    cmd: Optional[List[str]] = None           # Command (alternative to entrypoint)
    include_deps: bool = False                # Include dependency layer
    requirements_file: str = "requirements.txt"
```

## CLI Flags

```bash
--base-image      Base image to layer on (e.g., python:3.11-slim)
--include-deps    Include dependencies from venv or requirements.txt
--requirements    Requirements file for dependency layer (default: requirements.txt)
```

## Architecture Changes

### Modified Files
- `src/pycontainer/registry_client.py`: Added pull methods
- `src/pycontainer/oci.py`: Enhanced config merging with `build_config_json()`
- `src/pycontainer/builder.py`: Added `_pull_base_image()` and `_create_deps_layer()`
- `src/pycontainer/project.py`: Added `find_dependencies()` function
- `src/pycontainer/config.py`: Extended `BuildConfig` with Phase 2 fields
- `src/pycontainer/cli.py`: Added Phase 2 CLI arguments

### New Functions
- `RegistryClient.pull_manifest()`: Fetch manifest from registry
- `RegistryClient.pull_blob()`: Download blob to local path
- `ImageBuilder._pull_base_image()`: Pull and cache base image layers
- `ImageBuilder._create_deps_layer()`: Create dependency layer
- `find_dependencies()`: Discover venv or requirements.txt
- `is_distroless()`: Detect distroless base images

## Testing

### Unit Tests (5/5 passing)
```bash
pytest tests/test_base_image.py -v
```

**Test Coverage:**
- ✅ Base image config merging
- ✅ Distroless detection
- ✅ Layer ordering (base → deps → app)
- ✅ Dependency layer creation
- ✅ Environment variable override

### Integration Test
```bash
pycontainer build --base-image python:3.11-slim --context test_app --tag test:phase2
```

## Example Builds

### Simple App with Base Image
```bash
pycontainer build \
  --base-image python:3.11-slim \
  --tag myapp:v1 \
  --context ./myapp
```

### App with Dependencies
```bash
pycontainer build \
  --base-image python:3.11-slim \
  --include-deps \
  --tag myapp:v1 \
  --context ./myapp
```

### Distroless Build
```bash
pycontainer build \
  --base-image gcr.io/distroless/python3 \
  --tag myapp:v1 \
  --context ./myapp
```

## Performance Improvements

- **Layer Caching**: Base image layers cached locally (no re-download)
- **Dependency Separation**: Deps layer rebuilt only when requirements change
- **App Layer Isolation**: App code changes don't trigger dependency rebuild

## Known Limitations

1. **Live Registry Required**: Base image pulling requires internet access and registry auth
2. **Single Architecture**: Only `linux/amd64` currently supported
3. **No Cross-Platform**: Cannot build ARM images on x86 (and vice versa)

These will be addressed in Phase 4 (Multi-Architecture Support).

## Next Steps

Phase 2 is **COMPLETE** ✅. Ready to move to Phase 3: Toolchain Integrations.

---

**Completed**: November 19, 2025  
**Test Status**: 5/5 passing  
**Integration Status**: Verified with test app
