# Implementation Plan: pycontainer-build

## Overview

This document outlines the detailed implementation roadmap for transforming pycontainer-build from a foundational OCI image generator into a production-ready, Docker-free container build system for Python.

---

## Phase 0: Foundation ✅ **COMPLETE**

### Achievements

- ✅ Core OCI image structure (manifest, config, layers)
- ✅ Basic CLI (`pycontainer build`)
- ✅ Python project introspection (pyproject.toml parsing)
- ✅ Auto-detection of entry points and include paths
- ✅ File packing into tar layers
- ✅ Local OCI image layout output (`dist/image/`)
- ✅ Programmatic API via `BuildConfig` and `ImageBuilder`

### Architecture

```
src/pycontainer/
├── __init__.py           # Package root
├── cli.py                # Entry point (argparse-based)
├── builder.py            # Orchestration (ImageBuilder class)
├── config.py             # BuildConfig dataclass
├── project.py            # Python project introspection
├── oci.py                # OCI spec structures (manifest, config)
└── fs_utils.py           # File system helpers
```

---

## Phase 1: Registry & Caching 🚧 **NEXT**

**Goal**: Enable pushing images directly to container registries and implement efficient layer caching.

### Milestones

#### 1.1: Complete OCI Image Layout

**Status**: ✅ **COMPLETE**  
**Est. Effort**: 2-3 days  
**Priority**: High

**Tasks**:

- [x] Create `index.json` at image layout root (OCI image-layout spec)
- [x] Implement `refs/` directory structure for tag references
- [x] Add `oci-layout` file with proper version (`{"imageLayoutVersion": "1.0.0"}`)
- [x] Update builder to write complete layout, not just manifest + blobs
- [x] Validate against OCI Image Layout Specification v1

**Technical Details**:

```
dist/image/
├── index.json                  # Points to manifest(s)
├── oci-layout                  # Version marker
├── blobs/sha256/
│   ├── <manifest-digest>       # Manifest blob
│   ├── <config-digest>         # Config blob
│   └── <layer-digest>          # Layer blob(s)
└── refs/
    └── tags/
        └── <tag-name>          # Points to manifest digest
```

**Files to Modify**:

- `builder.py`: Update `build()` to create index.json and oci-layout
- `oci.py`: Add `OCIIndex` dataclass and serialization

**Acceptance Criteria**:

- Image layout passes `oras manifest fetch` validation
- Can load image with `skopeo copy oci:dist/image docker-daemon:myapp:latest`

---

#### 1.2: Docker Registry v2 API Client

**Status**: Not Started  
**Est. Effort**: 5-7 days  
**Priority**: High

**Tasks**:

- [ ] Implement Registry v2 API client class (`registry_client.py`)
- [ ] Blob upload: `POST /v2/<name>/blobs/uploads/` (initiate)
- [ ] Blob upload: `PUT /v2/<name>/blobs/uploads/<uuid>?digest=<digest>` (complete)
- [ ] Manifest upload: `PUT /v2/<name>/manifests/<reference>`
- [ ] Implement chunked blob upload (for large layers)
- [ ] Add progress reporting (bytes uploaded, layer N of M)
- [ ] Handle HTTP redirects (registry CDNs)

**Technical Details**:

- Use `urllib3` or `requests` (pure stdlib alternative if needed)
- Implement proper Content-Type headers:
  - Blobs: `application/octet-stream`
  - Manifests: `application/vnd.oci.image.manifest.v1+json`
- Support both monolithic and chunked uploads

**Files to Create**:

- `src/pycontainer/registry_client.py`

**Files to Modify**:

- `builder.py`: Add `push()` method that uses registry client
- `cli.py`: Add `--push` flag and `--registry` option

**Acceptance Criteria**:

- Successfully push to `ghcr.io/<user>/<repo>:tag`
- Successfully push to Docker Hub (`docker.io/<user>/<repo>:tag`)
- Successfully push to Azure Container Registry

---

#### 1.3: Authentication Support

**Status**: Not Started  
**Est. Effort**: 3-4 days  
**Priority**: High

**Dependencies**: 1.2 (Registry API client)

**Tasks**:

- [ ] Implement OAuth2 token exchange (registry auth flow)
- [ ] Read credentials from Docker config (`~/.docker/config.json`)
- [ ] Support `docker login` credential reuse
- [ ] Environment variable auth (`REGISTRY_USERNAME`, `REGISTRY_PASSWORD`)
- [ ] GitHub token support (`GITHUB_TOKEN` → `ghcr.io`)
- [ ] Azure CLI credential integration (for ACR)
- [ ] Add `--username` / `--password` CLI flags

**Technical Details**:

- Implement token refresh logic (OAuth2 bearer tokens expire)
- Handle `Www-Authenticate` header challenges
- Support both Basic Auth and Bearer Token flows

**Files to Modify**:

- `registry_client.py`: Add `AuthProvider` interface
- `cli.py`: Add auth-related arguments

**Acceptance Criteria**:

- Push to private GHCR repo using GitHub PAT
- Push to ACR using `az acr login` credentials
- Push to Docker Hub using `docker login` credentials

---

#### 1.4: Layer Caching & Content-Addressable Storage

**Status**: Not Started  
**Est. Effort**: 4-5 days  
**Priority**: Medium

**Tasks**:

- [ ] Implement local blob cache (`~/.pycontainer/cache/blobs/`)
- [ ] Check blob existence before upload (`HEAD /v2/<name>/blobs/<digest>`)
- [ ] Skip upload if blob exists in registry
- [ ] Implement cache eviction policy (LRU, max size)
- [ ] Add `--no-cache` flag to force rebuild
- [ ] Cache index by content digest (detect unchanged files)

**Technical Details**:

- Cache structure mirrors OCI layout:
  ```
  ~/.pycontainer/cache/
  └── blobs/sha256/
      └── <digest>
  ```
- Use mtime + file size for fast "is file unchanged?" checks
- Implement layer splitting: base deps layer + app code layer

**Files to Create**:

- `src/pycontainer/cache.py`

**Files to Modify**:

- `builder.py`: Check cache before creating layers
- `registry_client.py`: Query blob existence before upload

**Acceptance Criteria**:

- Second build with unchanged files skips layer creation
- Registry push skips blobs that already exist
- `--no-cache` forces full rebuild

---

### Phase 1 Testing Requirements

#### Unit Tests

- `test_registry_client.py`: Mock HTTP requests, test auth flows
- `test_oci_layout.py`: Validate index.json, refs/ structure
- `test_cache.py`: Cache hit/miss logic, eviction

#### Integration Tests

- Push to local registry (`docker run -d -p 5000:5000 registry:2`)
- Roundtrip: build → push → pull with `skopeo`
- Test against GHCR, ACR, Docker Hub (in CI)

#### End-to-End Tests

- Build sample app, push to registry, deploy to Kubernetes
- Verify image runs correctly after pull

---

## Phase 2: Base Images & Dependencies 📋

**Goal**: Support layering Python applications on top of base images (e.g., `python:3.11-slim`) and package dependencies.

### Milestones

#### 2.1: Base Image Pull & Parsing

**Status**: Not Started  
**Est. Effort**: 5-6 days  
**Priority**: High

**Tasks**:

- [ ] Fetch base image manifest from registry
- [ ] Download and cache base image layers
- [ ] Parse base image config (env, labels, working dir, user)
- [ ] Validate base image platform (linux/amd64)
- [ ] Support manifest lists (multi-arch selection)

**Technical Details**:

- Implement `ImagePuller` class in `registry_client.py`
- Download layers to cache directory
- Parse base image config JSON (extract `Env`, `WorkingDir`, `User`, `Labels`)

**Configuration**:

```python
BuildConfig(
    base_image="python:3.11-slim",  # or "mcr.microsoft.com/python/distroless"
    ...
)
```

**Files to Modify**:

- `config.py`: Activate `base_image` field
- `registry_client.py`: Add `pull_manifest()`, `pull_layer()` methods
- `builder.py`: Merge base layers + app layers

**Acceptance Criteria**:

- Pull `python:3.11-slim` manifest and layers
- Extract base image config successfully
- Cache base image layers locally

---

#### 2.2: Layer Merging & Multi-Layer Images

**Status**: Not Started  
**Est. Effort**: 4-5 days  
**Priority**: High

**Dependencies**: 2.1 (Base image pull)

**Tasks**:

- [ ] Create merged manifest with base + app layers
- [ ] Preserve base image layer order
- [ ] Append application layers on top
- [ ] Merge environment variables (base + user-defined)
- [ ] Respect base image working directory unless overridden
- [ ] Inherit labels from base image

**Technical Details**:

- New manifest structure:
  ```json
  {
    "layers": [
      {"digest": "sha256:...", "mediaType": "...", "size": ...},  // Base layer 1
      {"digest": "sha256:...", "mediaType": "...", "size": ...},  // Base layer 2
      {"digest": "sha256:...", "mediaType": "...", "size": ...}   // App layer
    ]
  }
  ```
- Merge logic for config:
  - `Env`: base env + user env (user overrides)
  - `WorkingDir`: user value or base value
  - `Labels`: merge dictionaries (user overrides)

**Files to Modify**:

- `oci.py`: Update `build_manifest()` to include base layers
- `builder.py`: Implement layer merging logic

**Acceptance Criteria**:

- Built image contains base layers + app layer
- `docker history` shows all layers correctly
- Environment variables merged properly

---

#### 2.3: Dependency Packaging

**Status**: Not Started  
**Est. Effort**: 6-8 days  
**Priority**: High

**Tasks**:

- [ ] Detect virtual environment in project context
- [ ] Copy `site-packages/` or `venv/` into dependency layer
- [ ] Support `requirements.txt` → `pip install` into temp layer
- [ ] Create separate layer for dependencies (cache-friendly)
- [ ] Handle native extensions (`.so` files)
- [ ] Support `--no-deps` flag (skip dependency packaging)

**Technical Details**:

- Layer split strategy:
  ```
  Layer 0-N: Base image (python:3.11-slim)
  Layer N+1: Dependencies (pip packages)
  Layer N+2: Application code
  ```
- Dependency layer only rebuilt when `requirements.txt` changes
- Application layer rebuilt every time (code changes frequently)

**Configuration**:

```python
BuildConfig(
    include_deps=True,  # Package venv or requirements.txt
    requirements_file="requirements.txt",
    ...
)
```

**Files to Modify**:

- `project.py`: Add `find_dependencies()` method
- `builder.py`: Create dependency layer before app layer

**Acceptance Criteria**:

- Image includes `pip install` packages
- Dependency layer cached separately from app layer
- Native extensions (e.g., numpy) work correctly

---

#### 2.4: Distroless & Slim Base Image Support

**Status**: Not Started  
**Est. Effort**: 2-3 days  
**Priority**: Medium

**Dependencies**: 2.1, 2.2

**Tasks**:

- [ ] Test with `python:3.11-slim`, `python:3.11-alpine`
- [ ] Test with Google distroless (`gcr.io/distroless/python3`)
- [ ] Test with Microsoft distroless (`mcr.microsoft.com/python/distroless`)
- [ ] Handle missing shell in distroless (use `Cmd` not `Entrypoint` with `/bin/sh`)
- [ ] Document recommended base images

**Technical Details**:

- Distroless images have no shell → entrypoint must be direct Python invocation
- Update `oci.py` to use `Cmd` instead of `Entrypoint` for distroless

**Files to Modify**:

- `oci.py`: Conditional entrypoint generation based on base image

**Acceptance Criteria**:

- Image builds successfully on slim, alpine, distroless bases
- Runs correctly in Kubernetes without shell dependency

---

### Phase 2 Testing Requirements

#### Unit Tests

- `test_layer_merge.py`: Test base + app layer combination
- `test_dependency_detection.py`: Test venv and requirements.txt parsing

#### Integration Tests

- Build on `python:3.11-slim` and verify layers
- Build with dependencies (Flask app) and test HTTP endpoint
- Build on distroless and verify execution

#### End-to-End Tests

- Full FastAPI app with dependencies on slim base
- Deploy to AKS and verify functionality

---

## Phase 3: Toolchain Integrations 📋

**Goal**: Enable seamless integration with Python tooling, Azure Developer CLI, and CI/CD systems.

### Milestones

#### 3.1: Poetry Plugin

**Status**: Not Started  
**Est. Effort**: 4-5 days  
**Priority**: Medium

**Tasks**:

- [ ] Create `poetry-pycontainer-plugin` package
- [ ] Implement `poetry build --container` command
- [ ] Read config from `[tool.pycontainer]` in `pyproject.toml`
- [ ] Auto-detect Poetry lock file for dependencies
- [ ] Publish to PyPI

**Configuration Example**:

```toml
[tool.pycontainer]
tag = "myapp:latest"
base_image = "python:3.11-slim"
registry = "ghcr.io/user/myapp"
```

**Files to Create**:

- New repo: `poetry-pycontainer-plugin/`

**Acceptance Criteria**:

- `poetry build --container` produces OCI image
- Published as `poetry-pycontainer-plugin` on PyPI

---

#### 3.2: Hatch Build Hook

**Status**: Not Started  
**Est. Effort**: 3-4 days  
**Priority**: Medium

**Tasks**:

- [ ] Create Hatch plugin (`hatch-pycontainer`)
- [ ] Implement build hook for `hatch build --container`
- [ ] Read config from `[tool.hatch.build.targets.container]`
- [ ] Publish to PyPI

**Configuration Example**:

```toml
[tool.hatch.build.targets.container]
tag = "myapp:latest"
base-image = "python:3.11-slim"
```

**Files to Create**:

- New repo: `hatch-pycontainer/`

**Acceptance Criteria**:

- `hatch build --container` works
- Published on PyPI

---

#### 3.3: Azure Developer CLI (azd) Integration

**Status**: Not Started  
**Est. Effort**: 5-6 days  
**Priority**: **High** (Microsoft strategic priority)

**Tasks**:

- [ ] Design `azd` integration strategy (custom build hook)
- [ ] Implement `azd.yaml` schema extension for `python-sdk-container`
- [ ] Create adapter between `azd` and `pycontainer` API
- [ ] Test with `azd up` deployment to Azure Container Apps
- [ ] Document in azd-templates repo

**Configuration Example**:

```yaml
# azure.yaml
services:
  api:
    language: python
    host: containerapp
    build:
      type: python-sdk-container  # Uses pycontainer under the hood
```

**Files to Create**:

- Integration code in azd repo (coordinate with azd team)

**Acceptance Criteria**:

- `azd up` builds container without Docker
- Deploys to Azure Container Apps successfully
- Works in GitHub Codespaces (Dockerless environment)

---

#### 3.4: GitHub Actions Workflow

**Status**: Not Started  
**Est. Effort**: 2-3 days  
**Priority**: Medium

**Tasks**:

- [ ] Create reusable workflow `.github/workflows/pycontainer-build.yml`
- [ ] Support matrix builds (multiple Python versions)
- [ ] Push to GHCR with proper tagging
- [ ] Add example to repo documentation

**Example Workflow**:

```yaml
name: Build Container
on: [push]
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - uses: actions/setup-python@v4
        with:
          python-version: '3.11'
      - run: pip install pycontainer-build
      - run: pycontainer build --tag ghcr.io/${{ github.repository }}:${{ github.sha }} --push
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```

**Files to Create**:

- `.github/workflows/pycontainer-build.yml` (example)

**Acceptance Criteria**:

- Workflow builds and pushes to GHCR on every commit
- Works in forks with secrets configured

---

#### 3.5: VS Code Extension / Copilot Template

**Status**: Not Started  
**Est. Effort**: 3-4 days  
**Priority**: Medium

**Tasks**:

- [ ] Create VS Code extension (`vscode-pycontainer`)
- [ ] Add "Build Container" command to command palette
- [ ] Show build output in terminal
- [ ] Create Copilot template for scaffolding containerized Python apps
- [ ] Publish to VS Code marketplace

**Extension Features**:

- Right-click Python project → "Build Container Image"
- Status bar item showing last build status
- Integrated with VS Code Docker extension

**Files to Create**:

- New repo: `vscode-pycontainer/`

**Acceptance Criteria**:

- Extension installs and runs in VS Code
- Build command executes pycontainer successfully

---

### Phase 3 Testing Requirements

#### Integration Tests

- Test Poetry plugin with real Poetry project
- Test Hatch plugin with real Hatch project
- Test azd integration with sample Python app

#### CI/CD Tests

- Run GitHub Actions workflow in test repo
- Verify images pushed to GHCR work correctly

---

## Phase 4: Polish & Production Readiness 📋

**Goal**: Production-grade features including multi-architecture builds, SBOM generation, and framework auto-detection.

### Milestones

#### 4.1: Framework Auto-Detection

**Status**: Not Started  
**Est. Effort**: 4-5 days  
**Priority**: Medium

**Tasks**:

- [ ] Detect FastAPI apps (look for `from fastapi import FastAPI`)
- [ ] Detect Flask apps (look for `from flask import Flask`)
- [ ] Detect Django apps (look for `manage.py`)
- [ ] Auto-configure entrypoint for frameworks:
  - FastAPI: `uvicorn app.main:app --host 0.0.0.0 --port 8000`
  - Flask: `flask run --host=0.0.0.0`
  - Django: `python manage.py runserver 0.0.0.0:8000`
- [ ] Auto-expose ports (8000 for FastAPI, 5000 for Flask)

**Technical Details**:

- Add `framework.py` module with detection logic
- Update `project.py` to call framework detection
- Set `Cmd`, `ExposedPorts` in OCI config based on framework

**Files to Create**:

- `src/pycontainer/framework.py`

**Files to Modify**:

- `project.py`: Call framework detector
- `oci.py`: Set framework-specific config

**Acceptance Criteria**:

- FastAPI app auto-configures correct entrypoint
- Flask and Django apps detected and configured

---

#### 4.2: Configuration Schema & Validation

**Status**: Not Started  
**Est. Effort**: 3-4 days  
**Priority**: Medium

**Tasks**:

- [ ] Define `pycontainer.toml` schema (TOML format)
- [ ] Implement validation with Pydantic or dataclasses
- [ ] Support loading config from file or CLI args (CLI overrides file)
- [ ] Add `--config` flag to specify custom config file

**Example `pycontainer.toml`**:

```toml
[build]
tag = "myapp:latest"
base_image = "python:3.11-slim"
workdir = "/app"

[build.env]
DEBUG = "false"
LOG_LEVEL = "info"

[build.ports]
expose = [8000, 8080]

[registry]
url = "ghcr.io/user/myapp"
auth = "github-token"
```

**Files to Create**:

- `src/pycontainer/config_schema.py`

**Files to Modify**:

- `config.py`: Load from TOML file
- `cli.py`: Add `--config` flag

**Acceptance Criteria**:

- Config file validated with helpful error messages
- CLI args override config file values

---

#### 4.3: SBOM (Software Bill of Materials) Generation

**Status**: Not Started  
**Est. Effort**: 4-5 days  
**Priority**: Medium (Security/Compliance)

**Tasks**:

- [ ] Generate SPDX or CycloneDX SBOM format
- [ ] Include Python package dependencies (from `pip freeze`)
- [ ] Include system packages (from base image, if available)
- [ ] Attach SBOM as OCI artifact (using `application/vnd.oci.artifact.manifest.v1+json`)
- [ ] Output SBOM to file (`dist/image/sbom.json`)

**Technical Details**:

- Use `cyclonedx-python` library or implement SPDX serialization
- SBOM should list:
  - All pip packages with versions
  - Python version
  - Base image info
  - File checksums (optional)

**Files to Create**:

- `src/pycontainer/sbom.py`

**Files to Modify**:

- `builder.py`: Generate SBOM after build
- `registry_client.py`: Push SBOM as OCI artifact (optional)

**Acceptance Criteria**:

- SBOM generated in SPDX/CycloneDX format
- SBOM includes all dependencies

---

#### 4.4: Reproducible Builds

**Status**: Not Started  
**Est. Effort**: 3-4 days  
**Priority**: Medium

**Tasks**:

- [ ] Use deterministic tar creation (fixed mtime, uid, gid)
- [ ] Sort files before packing (alphabetical order)
- [ ] Set environment variable `SOURCE_DATE_EPOCH` for reproducibility
- [ ] Ensure same input → same digest

**Technical Details**:

- Set all tar member mtimes to `SOURCE_DATE_EPOCH` (or fixed timestamp)
- Set uid=0, gid=0, uname="root", gname="root" for all files
- Sort tar members by name

**Files to Modify**:

- `builder.py`: Update tar creation logic

**Acceptance Criteria**:

- Two builds of same source produce identical layer digests
- Digest matches even after file reordering

---

#### 4.5: Multi-Architecture Support

**Status**: Not Started  
**Est. Effort**: 5-6 days  
**Priority**: Medium

**Tasks**:

- [ ] Add `--platform` flag (e.g., `linux/amd64`, `linux/arm64`)
- [ ] Pull base images for multiple architectures
- [ ] Create manifest list (OCI Index) for multi-arch images
- [ ] Build on ARM64 runners (GitHub Actions, Azure Pipelines)
- [ ] Test on M1/M2 Macs and ARM64 Linux

**Technical Details**:

- Manifest list structure:
  ```json
  {
    "schemaVersion": 2,
    "mediaType": "application/vnd.oci.image.index.v1+json",
    "manifests": [
      {
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "digest": "sha256:...",
        "platform": {"architecture": "amd64", "os": "linux"}
      },
      {
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "digest": "sha256:...",
        "platform": {"architecture": "arm64", "os": "linux"}
      }
    ]
  }
  ```

**Files to Modify**:

- `oci.py`: Add `OCIIndex` dataclass
- `builder.py`: Support multi-platform builds
- `cli.py`: Add `--platform` flag

**Acceptance Criteria**:

- Image runs on both amd64 and arm64
- `docker manifest inspect` shows both platforms

---

#### 4.6: Verbose Logging & Diagnostics

**Status**: Not Started  
**Est. Effort**: 2-3 days  
**Priority**: Low

**Tasks**:

- [ ] Add `--verbose` / `-v` flag
- [ ] Implement structured logging (use `logging` module)
- [ ] Log each step: file discovery, layer creation, upload progress
- [ ] Add `--dry-run` flag (show what would be built without building)

**Files to Modify**:

- All modules: Add logging statements
- `cli.py`: Add `--verbose` and `--dry-run` flags

**Acceptance Criteria**:

- `-v` flag shows detailed progress
- `--dry-run` shows build plan without executing

---

### Phase 4 Testing Requirements

#### Unit Tests

- `test_framework_detection.py`: Test FastAPI/Flask/Django detection
- `test_sbom.py`: Validate SBOM generation
- `test_reproducible.py`: Verify digest consistency

#### Integration Tests

- Multi-arch build and run on ARM64 runner
- SBOM validation with security scanners (Trivy, Grype)

#### End-to-End Tests

- Full production deployment with SBOM attached
- Multi-arch image deployed to Kubernetes cluster

---

## Testing Strategy

### Test Pyramid

```
                    /\
                   /  \
                  / E2E \              5-10 tests (deploy, roundtrip)
                 /______\
                /        \
               / Integr.  \            20-30 tests (build, push, pull)
              /____________\
             /              \
            /   Unit Tests   \        100+ tests (all modules)
           /__________________\
```

### Test Categories

#### Unit Tests (100+ tests)

- **Location**: `tests/unit/`
- **Coverage Target**: >80%
- **Tools**: `pytest`, `unittest.mock`

**Key Test Files**:

- `test_config.py`: BuildConfig validation
- `test_oci.py`: OCI structure serialization
- `test_project.py`: Entry point detection
- `test_fs_utils.py`: File iteration and filtering
- `test_cache.py`: Cache hit/miss logic
- `test_registry_client.py`: Mock HTTP requests

#### Integration Tests (20-30 tests)

- **Location**: `tests/integration/`
- **Requirements**: Local Docker registry, test Python projects
- **Tools**: `pytest`, `docker-py`, `skopeo`

**Key Test Scenarios**:

- Build and push to local registry
- Pull image and verify layers
- Auth flow with GitHub token
- Multi-layer builds with base images
- Dependency packaging with venv
- Framework auto-detection

**Test Setup**:

```bash
# Start local registry
docker run -d -p 5000:5000 --name test-registry registry:2

# Run integration tests
pytest tests/integration/
```

#### End-to-End Tests (5-10 tests)

- **Location**: `tests/e2e/`
- **Requirements**: Cloud resources (AKS, ACA), CI/CD pipeline
- **Tools**: `pytest`, `kubectl`, `az`

**Key Test Scenarios**:

- Build FastAPI app, push to GHCR, deploy to Azure Container Apps
- Build Django app with PostgreSQL, deploy to AKS
- CI/CD pipeline: build → test → push → deploy
- Multi-arch build and deploy to ARM64 cluster

**Test Execution**:

- Run in CI (GitHub Actions) with real cloud resources
- Use test subscriptions / namespaces
- Clean up resources after tests

---

### CI/CD Pipeline

**GitHub Actions Workflow** (`.github/workflows/ci.yml`):

```yaml
name: CI
on: [push, pull_request]

jobs:
  unit-tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - uses: actions/setup-python@v4
      - run: pip install -e .[dev]
      - run: pytest tests/unit/ --cov=pycontainer

  integration-tests:
    runs-on: ubuntu-latest
    services:
      registry:
        image: registry:2
        ports:
          - 5000:5000
    steps:
      - uses: actions/checkout@v3
      - uses: actions/setup-python@v4
      - run: pip install -e .[dev]
      - run: pytest tests/integration/

  e2e-tests:
    runs-on: ubuntu-latest
    if: github.ref == 'refs/heads/main'
    steps:
      - uses: actions/checkout@v3
      - uses: azure/login@v1
      - run: pytest tests/e2e/
```

---

## Dependencies & Prerequisites

### Python Dependencies (Minimal)

- **Python 3.10+** (stdlib only for core, optional deps for extras)
- **Optional**: `requests` (registry client, can use `urllib` instead)
- **Dev Dependencies**: `pytest`, `pytest-cov`, `black`, `mypy`

### Development Tools

- **Code Quality**: `black`, `isort`, `pylint`, `mypy`
- **Testing**: `pytest`, `pytest-mock`, `pytest-cov`
- **CI/CD**: GitHub Actions, Azure Pipelines (optional)

### External Tools (for testing only)

- **skopeo**: Validate OCI layouts
- **docker**: Run local registry for tests
- **kubectl**: E2E tests (deploy to Kubernetes)

---

## Risk Management

### Technical Risks

| Risk | Impact | Mitigation |
|------|--------|------------|
| Registry API compatibility issues | High | Test against all major registries (GHCR, ACR, Docker Hub, Quay) |
| Base image parsing complexity | Medium | Start with well-known images (python:slim), expand gradually |
| Layer caching correctness | Medium | Extensive unit tests, compare digest with Docker builds |
| Multi-arch build complexity | Medium | Start with single arch, add multi-arch in Phase 4 |
| Performance (large projects) | Low | Profile and optimize, add parallel layer creation |

### Schedule Risks

| Risk | Impact | Mitigation |
|------|--------|------------|
| Phase 1 registry push takes longer | High | Break into smaller milestones, ship incremental features |
| Dependency packaging complexity | Medium | Consider using existing tools (pip-tools, poetry export) |
| Toolchain integration delays | Low | Work in parallel with tool maintainers, provide APIs early |

---

## Success Metrics

### Phase 1 Success Criteria

- [ ] Push to GHCR, ACR, Docker Hub without Docker daemon
- [ ] Build time <10s for cached builds
- [ ] Layer reuse reduces rebuild time by >80%

### Phase 2 Success Criteria

- [ ] Build on `python:3.11-slim` with full functionality
- [ ] FastAPI app with dependencies runs correctly
- [ ] Layer caching reduces dependency rebuild time

### Phase 3 Success Criteria

- [ ] `azd up` works without Docker installed
- [ ] Poetry/Hatch plugins published on PyPI
- [ ] GitHub Actions workflow used in 5+ repos

### Phase 4 Success Criteria

- [ ] SBOM included in all builds
- [ ] Reproducible builds (same digest for same input)
- [ ] Multi-arch images run on ARM64 and AMD64

---

## Future Phases (Beyond Phase 4)

### Phase 5: Advanced Features

- Windows container support
- BuildKit backend (alternative to OCI direct generation)
- Plug-in architecture (custom layers, hooks)
- Remote build (build on cloud VM, no local resources)

### Phase 6: Enterprise Features

- Air-gapped environment support (offline builds)
- Corporate proxy support
- FIPS compliance (crypto modules)
- Audit logging and compliance reports

---

## Timeline Estimates

| Phase | Duration | Target Completion |
|-------|----------|-------------------|
| Phase 1 | 3-4 weeks | Q1 2026 |
| Phase 2 | 4-5 weeks | Q2 2026 |
| Phase 3 | 3-4 weeks | Q2 2026 |
| Phase 4 | 4-5 weeks | Q3 2026 |

**Note**: Timeline assumes 1-2 full-time developers working on the project.

---

## Contributing

### How to Contribute

1. Pick a task from this implementation plan
2. Create an issue in the repo referencing the task
3. Submit a PR with tests and documentation
4. Ensure CI passes before requesting review

### Development Workflow

```bash
# Clone and setup
git clone https://github.com/microsoft/pycontainer-build.git
cd pycontainer-build
pip install -e .[dev]

# Make changes
# Write tests
pytest tests/

# Format and lint
black src/ tests/
mypy src/

# Submit PR
```

---

## Contact & Feedback

- **GitHub Issues**: [pycontainer-build/issues](https://github.com/microsoft/pycontainer-build/issues)
- **Discussions**: [pycontainer-build/discussions](https://github.com/microsoft/pycontainer-build/discussions)
- **Email**: pycontainer-team@microsoft.com (placeholder)

---

**Last Updated**: 2025-11-19  
**Document Version**: 1.0  
**Status**: Living document (updated as we progress through phases)
