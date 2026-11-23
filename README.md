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

Requires Python 3.11 or newer. Until a package release is available, install
from a repository checkout:

```bash
# From a repository checkout
python -m pip install --no-deps -e .

# Using uv (faster)
uv pip install --no-deps -e .

# Or run directly from a verified commit with uvx
uvx --from git+https://github.com/spboyer/pycontainer-build.git@d3ee7e0f5a1f342a42f9a43ad4efd335ec0497b7 pycontainer build --tag myapp:latest
```

### Build Your First Image

```bash
# Simple build (auto-detects Python version and base image)
pycontainer build --tag myapp:latest

# Build with custom base image and dependencies
pycontainer build \
  --tag myapp:v1 \
  --base-image python:3.12-slim-bookworm \
  --include-deps

# Build FastAPI app (auto-detected, entrypoint configured)
pycontainer build --tag api:latest --context ./my-fastapi-app

# Build with SBOM for security compliance
pycontainer build \
  --tag myapp:v1 \
  --sbom spdx \
  --config pycontainer.toml

# Build and push to registry
pycontainer build --tag ghcr.io/user/myapp:v1 --push

# Build for different platform (e.g., amd64 from ARM Mac)
pycontainer build --tag myapp:latest --platform linux/amd64

# Dry-run to preview (verbose mode)
pycontainer build --tag test:latest --dry-run --verbose
```

### Output

Creates a complete OCI image layout at `dist/image/`:
```
dist/image/
  ├── index.json                  # OCI index (manifest list)
  ├── oci-layout                  # Version marker
  ├── blobs/sha256/
  │   ├── <manifest-digest>       # Manifest blob
  │   ├── <config-digest>         # Config blob
  │   └── <layer-digest>          # Application layer (tar)
  └── refs/tags/
      └── <tag-name>              # Tag reference
```

### Generate Deployment Artifacts

After publishing an image, generate Kubernetes resources and a matching Helm
chart, or a Compose service that references that image:

```bash
pycontainer generate kubernetes --image ghcr.io/example/myapp:v1 --port 8000 --name myapp --service-port 80 -o deploy
pycontainer generate compose --image ghcr.io/example/myapp:v1 --port 8000 --name myapp --host-port 8080 -o compose.yaml
```

Generation does not run a daemon or apply resources, and refuses existing
output. See the [deployment artifact guide](docs/deployment-artifacts.md) for
supported fields, output safety, versions, and optional validation.

### Testing Locally

**Important**: `pycontainer` creates OCI-compliant image layouts, not Docker-specific images. To test locally:

#### Option 1: Use Skopeo (Recommended)

```bash
# Install skopeo
brew install skopeo  # macOS
# OR: sudo apt-get install skopeo  (Ubuntu)

# Copy OCI layout to Docker
skopeo copy oci:dist/image docker-daemon:myapp:latest
docker run -p 8000:8000 myapp:latest
```

With several tags, select one explicitly, for example
`skopeo copy oci:dist/image:myapp:stable docker-daemon:myapp:stable`; see
[Multiple tags](docs/multiple-tags.md#output).

#### Option 2: Use Podman (Native OCI Support)

```bash
# Install Podman
brew install podman  # macOS

# Run directly from OCI layout
podman run --rm -p 8000:8000 oci:dist/image:myapp
```

#### Option 3: Push to Registry (Production)

```bash
# Build and push (works with any container runtime)
pycontainer build --tag ghcr.io/user/myapp:latest --push

# Pull and run with Docker or Podman
docker pull ghcr.io/user/myapp:latest
docker run -p 8000:8000 ghcr.io/user/myapp:latest
```

**See [Local Development Guide](docs/local-development.md) for detailed testing instructions.**

---

## ✨ Features

### Current Capabilities (Phases 0-2, 4 ✅)

**Foundation & Registry** (Phases 0-1):
- ✅ **Zero Docker dependencies** — Pure Python implementation
- ✅ **Auto-detects Python project structure** — Finds `src/`, `app/`, entry points
- ✅ **Infers entrypoints** — Reads `pyproject.toml` scripts, falls back to `python -m`
- ✅ **Creates OCI-compliant images** — Complete OCI image layout v1
- ✅ **Command-line interface** — Simple `pycontainer build` workflow
- ✅ **Programmatic API** — Use as a library in your tools
- ✅ **Registry push support** — Push to GHCR, ACR, Docker Hub via Registry v2 API
- ✅ **Blob existence checks** — Skip uploading layers that already exist
- ✅ **Progress reporting** — Visual feedback during push operations
- ✅ **Multi-provider authentication** — GitHub tokens, Docker config, Azure CLI, env vars
- ✅ **OAuth2 token exchange** — Automatic bearer token flow with Www-Authenticate
- ✅ **Credential auto-discovery** — Tries multiple auth sources automatically
- ✅ **Layer caching** — Content-addressable storage with LRU eviction
- ✅ **Cache invalidation** — Keys file contents, relative paths, hardlink
  relationships, workdir, and reproducibility settings
- ✅ **Fast incremental builds** — Reuses unchanged layers from cache

**Base Images & Dependencies** (Phase 2):
- ✅ **Smart base image detection** — Auto-selects Python base image from `requires-python` in pyproject.toml
- ✅ **Base image support** — Build on top of `python:3.11-slim`, `python:3.12-slim`, distroless, etc.
- ✅ **Layer merging** — Combines base image layers with application layers
- ✅ **Config inheritance** — Merges env vars, labels, working dir from base images
- ✅ **Dependency packaging** — Include pip packages from venv or requirements.txt
- ✅ **Distroless detection** — Auto-handles shell-less base images

**Production Features** (Phase 4):
- ✅ **Framework auto-detection** — FastAPI, Flask, Django automatically configured
- ✅ **Configuration files** — Load settings from `pycontainer.toml`
- ✅ **SBOM generation** — Create SPDX 2.3 or CycloneDX 1.4 security manifests
- ✅ **Runtime security fragments** — Opt-in Kubernetes `securityContext` and
  OCI runtime-spec fragments for capabilities, seccomp, AppArmor, SELinux and
  read-only root; applied by your runtime, not enforced by the image
  ([guide](docs/security-fragments.md))
- ✅ **Reproducible builds** — Deterministic layer creation with fixed timestamps
- ✅ **Cross-platform builds** — Build linux/amd64 from ARM, linux/arm64 from x86, etc.
- ✅ **Platform auto-selection** — Pulls correct architecture variant from multi-arch base images
- ✅ **Verbose logging** — Detailed build progress with `--verbose`
- ✅ **Dry-run mode** — Preview builds with `--dry-run`
- ✅ **Opt-in local history** — Inspect private build outcomes and phase timings
- ✅ **Deployment artifacts** — Generate Kubernetes, Helm, and Compose
  configuration for an existing image without applying it

### Coming Soon

- 🔜 **Toolchain integrations** — Poetry, Hatch, Azure Developer CLI (Phase 3)
- 🔜 **Full multi-arch builds** — Actual cross-compilation for ARM64, AMD64 (Phase 4+)

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
- **Azure Developer CLI (azd)** — Custom build strategies ([docs](docs/azd-integration.md))
- **GitHub Actions** — Automated CI/CD workflows ([docs](docs/github-actions.md))
- **GitLab CI, Jenkins, CircleCI** — Standalone pipeline examples ([GitLab](docs/ci/gitlab.md), [Jenkins](docs/ci/jenkins.md), [CircleCI](docs/ci/circleci.md))
- **Poetry/Hatch** — Build plugins ([plugins](plugins/))
- **VS Code** — Extension for container builds ([plugin](plugins/vscode-pycontainer/))
- **AI agents** — Copilot, MCP servers, automated scaffolding

## 🔌 Integrations

pycontainer-build integrates seamlessly with popular Python tools:

The core package is not yet available from PyPI (release work is tracked in
[#79](https://github.com/spboyer/pycontainer-build/issues/79)). Until then,
follow each plugin's README for installing the core and plugin from source.

### Poetry Plugin

After the packages are published:

```bash
poetry self add poetry-pycontainer
poetry build-container --tag myapp:latest --push
```

[See full documentation →](plugins/poetry-pycontainer/)

### Hatch Plugin

After the packages are published:

```bash
pip install hatch hatch-pycontainer
hatch build  # Builds both wheel and container
```

[See full documentation →](plugins/hatch-pycontainer/)

### GitHub Actions

```yaml
jobs:
  build:
    uses: spboyer/pycontainer-build/.github/workflows/pycontainer-build.yml@main
    with:
      tag: ghcr.io/${{ github.repository }}:latest
      push: true
```

[See full documentation →](docs/github-actions.md)

### Azure Developer CLI

```yaml
# azure.yaml
hooks:
  build:
    run: pycontainer build --tag ${SERVICE_IMAGE_NAME} --push
```

[See full documentation →](docs/azd-integration.md)

### Skaffold and Tilt

Use pycontainer as a custom builder for the Kubernetes inner loop. It builds
without a Docker daemon and pushes to a registry the cluster pulls from:

```yaml
# skaffold.yaml (build.local.push: true is required)
custom:
  buildCommand: python -m pycontainer.cli skaffold-build --no-progress
```

```python
# Tiltfile
custom_build('app', ['python', '-m', 'pycontainer.cli', 'tilt-build'],
             deps=['app'], skips_local_docker=True)
```

Local-daemon handoff (`PUSH_IMAGE=false`, `custom_build(tag=...)`) and
multi-platform requests are rejected. Plain HTTP requires per-host opt-in.
[See full documentation →](docs/skaffold-tilt.md)

### VS Code Extension

Install from VS Code Marketplace or command palette:
- "Build Container Image"
- "Build and Push Container Image"

[See full documentation →](plugins/vscode-pycontainer/)

### Other editors and task runners

Project-scoped PyCharm, Vim, Emacs, GNU Make, and Go Task examples are
available in the [integration examples](examples/editor-task-runners/).

---

## 🎓 Configuration

### Auto-Detection (Zero Config)

By default, `pycontainer` auto-detects:

- **Base image**: Python version from `requires-python` in `pyproject.toml` (e.g., `>=3.11` → `python:3.11-slim`)
- **Entry point**: First `[project.scripts]` entry in `pyproject.toml`
- **Include paths**: `src/`, `app/`, or `<package>/` dirs + `pyproject.toml`, `requirements.txt`
- **Working directory**: `/app/`
- **Architecture**: `amd64/linux`

### Explicit Configuration

```bash
# Full configuration with all options
pycontainer build \
  --tag myapp:v1.2.3 \
  --context /my/project \
  --base-image python:3.11-slim-bookworm \
  --include-deps \
  --workdir /app \
  --env KEY=value \
  --env ANOTHER=value \
  --platform linux/amd64 \
  --sbom cyclonedx \
  --config pycontainer.toml \
  --verbose \
  --push \
  --no-cache

# Build for ARM64 (e.g., for AWS Graviton, Apple Silicon containers)
pycontainer build \
  --tag myapp:arm64 \
  --platform linux/arm64 \
  --push
```

**Base Image & Dependencies**:
- `--base-image IMAGE` — Base image to build on (auto-detected from `requires-python` if not specified, e.g., `python:3.11-slim`). Local, daemon-free sources are also accepted; see [Local base images](#local-base-images).
- `--include-deps` — Stage importable dependencies from a venv or install a
  requirements file as target-compatible binary wheels. Requirements installs
  are limited to 1 MiB, 300 seconds, 512 MiB of staged files, and 100,000 files;
  supported targets are `linux/amd64` and `linux/arm64` with CPython. A venv
  must match the target Python minor version and contain only pure-Python
  packages. Dependencies are placed under `<workdir>/.python`, added to
  `PYTHONPATH`, and their console entrypoints are added to `PATH`; the Python
  interpreter running pycontainer must have `pip` available. Source
  distributions, cross-compilation, and lockfile resolution are not supported.
  Generated Linux console scripts use UTF-8 and LF line endings on every host,
  including Windows, and retain executable mode in the dependency layer.
  Copied application files are not normalized.
  Custom base images must set `PYTHON_IMPLEMENTATION=CPython`,
  `PYTHON_VERSION`, and `PYTHON_LIBC=glibc-2.N` (N 17-99) or
  `PYTHON_LIBC=musl-1.N` (N 1-99) in their image config so wheel tags can be
  validated. pip receives tags through the selected libc bound, newest first:
  `manylinux_2_N` down to `manylinux_2_17` plus the `manylinux2014` alias for
  glibc, or `musllinux_1_N` down to `musllinux_1_1` for musl. Official
  `python` tag-only registry pulls derive libc from the resolved tag (`alpine`
  tags use musl, others glibc). A codename token in such a tag raises the
  libc version to a conservative bound for that release:

  | Tag codename | Target libc |
  | --- | --- |
  | `jessie` / `stretch` / `buster` | glibc 2.17 / 2.19 / 2.24 |
  | `bullseye` / `bookworm` / `trixie` | glibc 2.28 / 2.31 / 2.36 |
  | `alpine3.0`-`alpine3.12` | musl 1.1 |
  | `alpine3.13`-`alpine3.24` | musl 1.2 |

  Each row is pinned to immutable sources (content-addressed Debian snapshots,
  official image declarations and Alpine `aports` commits) in
  [docs/libc-codename-mappings.md](docs/libc-codename-mappings.md).
  These bounds may reject wheels that a newer image could actually run.

  Codename-less tags such as `python:3.11-slim` or `python:3.11-alpine`,
  and unmapped releases, cannot establish a libc floor from their names: official
  `python:3.8.0-alpine` uses musl 1.1 and `python:3.4-wheezy` uses glibc 2.13,
  so the previous `musllinux_1_2` / `manylinux2014` fallbacks were unsafe.
  For verified official tag-only pulls, the builder instead reads the effective
  `libc6`/`musl` package record from digest-checked base layers. It applies
  whiteouts and rejects missing, linked, ambiguous, unsupported or wrong-architecture
  records. A foreign-database root link such as Debian's `lib -> usr/lib` is
  allowed only when its redirected database has no conflicting evidence;
  other foreign links and links on the active database path fail closed.
  Inspection is bounded (8 MiB metadata, 2 GiB decoded bytes and 100,000
  members per layer, 300 seconds overall) and never extracts base files.
  Thus the CLI's automatic selection, `BuildConfig`'s `python:3.11-slim`
  default, and Aspire's `python:3.12-slim --include-deps` invocation remain
  supported when the pulled image supplies valid package evidence.
  If evidence is unavailable, explicitly select a mapped release, for example
  `--base-image python:3.11-slim-bookworm`, or provide supported libc metadata
  in the base image config. Application `--env` values are not base metadata.
  Inferred `PYTHON_LIBC` is confined to a staging config copy, not added to
  the final image environment. No distribution is silently substituted.
  When both a codename and `PYTHON_LIBC` are present they must
  name the same family, and the lower version wins. All digest references, including
  `python:3.11-slim@sha256:...`, require supported `PYTHON_LIBC` metadata:
  the digest is fetched without verifying that it matches any supplied tag.
  Local image names/annotations also cannot establish libc. Adding a tag to a
  digest does not make it safe; use an image config with supported metadata
  or a tag-only registry pull. Unsupported metadata always fails, as does
  metadata conflicting with a verified tag-only pull.
  Digest and unverified local sources also require explicit
  `PYTHON_IMPLEMENTATION=CPython` and `PYTHON_VERSION`; claimed names do not
  establish the Python ABI. These metadata checks apply to requirements and
  virtualenv staging alike.

  Environment markers in the requirements file and in transitive
  `Requires-Dist` metadata are evaluated for the target (`linux`, the target
  machine, CPython and the base image's Python version), not the build host.
  Markers on `platform_release` or `platform_version`, and on
  `python_full_version` when the base image does not declare a full
  `PYTHON_VERSION`, cannot be known in advance and fail the build. A virtualenv
  cannot be re-resolved, so its closure is verified for each target instead.
  Requirements-file and pyproject roots and installed `Requires-Dist` metadata
  are checked for missing packages, version mismatches, and requested extras;
  `Requires-Python` and requirements constraint files are also checked. Every
  installed `.dist-info` needs valid `METADATA` with a supported
  `Metadata-Version`. Direct URLs, egg distributions, unknown markers, and
  patch-dependent `Requires-Python` without a full `PYTHON_VERSION` fail.
  Virtualenvs do not record selected extras or groups. An extra or group
  counts as not requested only when it is missing a requirement that any
  Windows, Linux or macOS CPython host of the venv's Python version would have
  installed; a `lib/pythonX.Y/site-packages` layout whose `pyvenv.cfg` `home`
  is a POSIX path limits the hosts to Linux and macOS (MSYS2 CPython is
  Windows with that layout), and `Lib/site-packages` to Windows. Otherwise, if
  its target requirements are not satisfied, verification fails rather than
  guessing.
  Direct URL installation metadata is rejected because it can contain
  credentials. Host-only packages remain in the image, and loose modules
  without package metadata are copied unverified. `setup.py`, `setup.cfg`,
  `Pipfile`, file-based dynamic dependencies, and Poetry, PDM, Hatch,
  setuptools or Flit tables mentioning host markers fail, as do unreadable
  dynamic dependencies. Unmarked declarations in those unparsed sources are
  not verified. Use a requirements file when a virtualenv cannot be verified.

  Private indexes can authenticate through `PIP_INDEX_URL`, a pip config file,
  `.netrc`, keyring (`PIP_KEYRING_PROVIDER=import` or `subprocess`) or `${VAR}`
  placeholders in the requirements file, including in URL paths. Literal
  credentials in requirements files (URL userinfo, secret-like query values
  and recognized URL path tokens) are rejected, and host pip settings that
  could change target selection (for example `PIP_PLATFORM` or
  `PIP_NO_BINARY`) are ignored; see
  [Private package indexes](docs/AUTHENTICATION.md#private-package-indexes).

### Local base images

`--base-image` (and `base_image` in `[build]`) can read a base image from disk
without a registry, credentials, or a Docker daemon:

```bash
# OCI image layout (for example `skopeo copy docker://python:3.11-slim oci:base:3.11-slim`)
pycontainer build --tag app:v1 --base-image oci:./base:3.11-slim
pycontainer build --tag app:v1 --base-image oci:./base@sha256:<manifest-digest>

# A previous pycontainer output is itself an OCI layout
pycontainer build --tag app:v2 --base-image oci:./dist/image:app:v1 --output ./dist/v2

# Uncompressed `docker save` archive (legacy and Docker 25+ formats)
pycontainer build --tag app:v1 --base-image docker-archive:./python.tar:python:3.11-slim
```

- `oci:<path>[:<ref>]` selects an index entry whose
  `org.opencontainers.image.ref.name` or `io.containerd.image.name` annotation
  exactly matches `<ref>` (or `@sha256:<digest>`). For a single-entry index,
  either annotation on the index itself can also identify the entry. The
  reference may be omitted when the layout holds one image, or a set of images
  that all carry platform metadata. Nested image indexes use the same exact
  OS/architecture/variant selection as registry pulls.
- `docker-archive:<path>[:<repo-tag>]` selects a `manifest.json` entry by
  `RepoTags`, normalized like registry references (`python:3.11-slim` matches
  `docker.io/library/python:3.11-slim`). The tag may be omitted when the archive
  holds one image.
- The path ends at the first `:` after an optional Windows drive prefix, so
  paths containing `:` are not supported. Relative paths resolve against the
  current directory, and the source must not overlap `--output`.
- Every blob is checked against its digest and size, and every base layer is
  checked against its config `rootfs.diff_ids` (`tar`, `tar+gzip`; `tar+zstd`
  needs Python 3.14+). Unknown layer media types, layers labeled as plain
  `tar` whose bytes are compressed or not a tar stream, unsafe or duplicate
  archive members, sparse members, symlinked OCI layout blobs, archive links
  that escape the archive, and
  more than 100,000 members, 1,000 layers or 16 MiB of metadata are rejected.
  Each OCI index, including nested indexes, is limited to 1,000 entries.
  Layers are limited to 64 GiB (including decompressed content); Docker archive
  member sizes are checked before any selected image blobs are staged.
- Compressed outer archives (`docker save | gzip`) and `oci-archive:` tarballs
  are not supported; extract or decompress them first.
- OCI annotations and Docker archive tags do not establish an image's Python
  ABI or libc. For `--include-deps`, local images must set
  `PYTHON_IMPLEMENTATION=CPython`, `PYTHON_VERSION`, and either
  `PYTHON_LIBC=glibc-2.17` or `PYTHON_LIBC=musl-1.2` in their image config,
  even if their name claims to be an official Python image. These settings are
  not required when dependency staging is disabled.

**Context Selection**:
- `--include PATH` — Include a context-root-relative file or directory; repeat to select multiple paths instead of automatic discovery
- `--exclude PATH` — Exclude a context-root-relative path or glob; repeat to add exclusions
- Default exclusions prune VCS metadata, virtual environments, generated output/cache directories, and test/fixture files and directories before scanning
- `--no-default-excludes` disables those defaults; use it only when those files are intentionally part of the image

The same options are available in `[build]` as `include-paths`, `exclude-paths`,
and `exclude-defaults`. Include paths are relative to the build context and
overlapping paths are deduplicated. For example:

```toml
[build]
include-paths = ["src", "pyproject.toml"]
exclude-paths = ["src/generated", "fixtures/**"]
exclude-defaults = true
```

**Caching Options**:
- `--no-cache` — Disable layer caching, force full rebuild
- `--cache-dir PATH` — Custom cache directory (default: `~/.pycontainer/cache`)

**Local Build History**:

```bash
pycontainer build --record-history
pycontainer history list
pycontainer history clear
```

History is disabled by default. It records only a random run ID, UTC start
time, outcome, and timings, never build configuration or error text. Use
`--history-dir PATH` to select a directory outside the build context and OCI
output; specifying a directory alone does not enable recording. For a build
configured with `record-history = true`, `--no-record-history` disables it.
See the [build history guide](docs/build-history.md) for retention, privacy,
error handling, and custom-directory inspection.

**Production Features**:
- `--config FILE` — Load settings from `pycontainer.toml`
- `--sbom [FORMAT]` — Generate an SBOM (defaults to `spdx`; accepts `spdx` or `cyclonedx`)
- `--platform PLATFORM` — Target platform (`os/architecture[/variant]`, e.g.,
  `linux/arm64` or `linux/arm/v7`)
- `--verbose` / `-v` — Detailed build progress
- `--dry-run` — Preview build without creating artifacts
- `--no-reproducible` — Disable deterministic builds

**Registry Transport** (see [docs/REGISTRY_PUSH.md](docs/REGISTRY_PUSH.md#insecure-http-registries-and-custom-cas)):
- `--insecure-registry HOST[:PORT]` — Use plain HTTP for exactly this registry
  (repeatable; `[build] insecure-registries = [...]`). Each build that uses
  one logs a warning. Credentials are sent over HTTP only to loopback hosts
  (`localhost`, `127.0.0.0/8`, `::1`); any other HTTP registry must be
  anonymous.
- `--registry-ca-file PATH` — Trust an additional PEM CA bundle for registry
  HTTPS (`[build] registry-ca-file`). Certificate verification always stays on;
  there is no option to skip it.
Registry connections are direct: environment/system proxy settings are ignored.
URLs containing embedded credentials are rejected; use the credential providers
or authentication flags instead.

SBOMs are sidecar files: their UTC timestamps and UUID document identifiers are
new for each generation, even when the OCI image build is reproducible. They do
not affect the image manifest digest. Inventory contains Python distributions
staged by pycontainer, not host packages, uninstalled requirements, or packages
inside the base image.

The cache automatically:
- Reuses unchanged layers across builds (content-addressable by SHA256)
- Invalidates when file contents, relative paths, permission bits, the
  workdir or reproducibility settings change (mtime is ignored for
  reproducible builds)
- Invalidates when hardlinks between selected files are added or removed;
  the key records context-relative link targets, not inode numbers
- Evicts old entries using LRU when size limit reached (default: 5GB)

**Saving and restoring the cache (offline export/import):** the `--cache-dir`
directory (`index.json` plus `blobs/sha256/`) is self-contained and stores only
relative source paths, so it can be archived or saved by CI (for example with
`actions/cache`) and restored to a different absolute path and checkout
location. A restored cache produces cache hits and an identical OCI manifest,
config and layers. Partial restores are handled as follows:

- Blobs without `index.json`: a clean miss; the layer is rebuilt and indexed.
- `index.json` with missing or corrupt blobs: those entries are discarded and
  rebuilt.
- Malformed `index.json`: the build fails with `CacheIndexError`; delete the
  reported `index.json` (cached layers are rebuilt) or rerun with `--no-cache`.
- Concurrent builds may share a restored directory; index updates are locked.

Caches are portable between machines of the same OS, not across operating
systems: Windows reports source files as mode `0o666`, Linux checkouts as
`0o644`/`0o755`, and the mode is recorded in the layer, so a cache saved on
Windows misses on Linux (and vice versa) by design. Windows checkouts with
`core.autocrlf` also change file content. Key saved caches on the OS. Blobs
left without an index entry are not counted or evicted; delete the directory to
reclaim them. See [GitHub Actions](docs/github-actions.md#caching-application-layers).

### Python API

```python
from pycontainer.config import BuildConfig
from pycontainer.builder import ImageBuilder

config = BuildConfig(
    tag="myapp:latest",
    context_dir=".",
    base_image="python:3.11-slim",  # Optional: auto-detected if omitted
    include_deps=True,
    workdir="/app",
    env={"DEBUG": "false", "ENV": "production"},
    labels={"version": "1.0", "maintainer": "team@example.com"},
    include_paths=["src/", "lib/", "pyproject.toml"],
    exclude_paths=["src/generated", "fixtures/**"],
    entrypoint=["python", "-m", "myapp"],
    generate_sbom="spdx",  # or "cyclonedx"; True defaults to SPDX
    reproducible=True,
    verbose=True
)

builder = ImageBuilder(config)
builder.build()
```

### Configuration File (`pycontainer.toml`)

```toml
[build]
base_image = "python:3.11-slim"
workdir = "/app"
include_deps = true
reproducible = true
generate-sbom = "cyclonedx" # false disables; true or "spdx" selects SPDX

[build.labels]
maintainer = "team@example.com"
version = "1.0.0"

[build.env]
PORT = "8080"
ENV = "production"
DEBUG = "false"

[registry]
url = "ghcr.io/myorg/myapp"
```

Explicit CLI options override `[build]` values; omitted options leave the file
values intact, and settings absent from both use `BuildConfig` defaults. Keys
in `[build]` may use hyphens or underscores. After overrides are applied,
unknown settings and values with the wrong TOML type are reported as
configuration errors.

`--config` also accepts a `pyproject.toml` whose settings live under
`[tool.pycontainer]` (with `[tool.pycontainer.env]` and
`[tool.pycontainer.labels]`), which is what `pycontainer import` generates.
The same keys, validation and CLI precedence apply. A file that defines both
`[build]` and `[tool.pycontainer]` is rejected rather than merged; keep exactly
one. Poetry-only keys such as `push` are reported as unknown by
`pycontainer build`; use `--push` instead. See
[Dockerfile import](docs/dockerfile-import.md).

Optional `[build.security.kubernetes]` and `[build.security.oci]` tables
(`[tool.pycontainer.security.*]` in `pyproject.toml`) write deployment
fragments to `<output_dir>/security/` without changing the image. They are
not enforced by the image; see [runtime security
fragments](docs/security-fragments.md) for the schema, supported versions and
how to apply them.

Concurrent builds targeting the same output directory are rejected; use a
separate output directory or retry after the current build finishes.

---

## 🗺️ Roadmap

### ✅ **Phase 0: Foundation** (COMPLETE)

- Core OCI image generation
- Basic CLI and Python API
- Project introspection and auto-detection
- File packing and layer creation

### ✅ **Phase 1: Registry & Caching** (COMPLETE)

- [x] Implement complete OCI image layout (index.json, refs/)
- [x] Push images to registries via Docker Registry v2 API
- [x] Support authentication (GHCR, ACR, Docker Hub, private registries)
- [x] Add layer caching and reuse logic
- [x] Digest verification and content-addressable storage

### ✅ **Phase 2: Base Images & Dependencies** (COMPLETE)

- [x] Pull and parse base image manifests
- [x] Layer Python app files on top of base images
- [x] Support slim, distroless, and custom base images
- [x] Package pip-installed dependencies into layers
- [x] Respect base image configuration (env, labels, user)

### ✅ **Phase 3: Toolchain Integrations** (COMPLETE)

- [x] Poetry plugin (`poetry build-container`)
- [x] Hatch build hook
- [x] Azure Developer CLI (azd) integration
- [x] GitHub Actions reusable workflow
- [x] VS Code extension

### ✅ **Phase 4: Polish & Production Readiness** (COMPLETE)

- [x] Framework auto-detection (FastAPI, Flask, Django)
- [x] `pycontainer.toml` configuration schema
- [x] SBOM (Software Bill of Materials) generation
- [x] Reproducible builds (deterministic layer creation)
- [x] Platform configuration (metadata for multi-arch)
- [x] Verbose logging and diagnostics

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

## 🔬 Current Limitations

Known limitations and future enhancements:

- **Framework detection** — Supports FastAPI, Flask, Django only (easy to extend)
- **SBOM scope** — Python distributions staged by pycontainer; does not scan host or base-image packages
- **System packages** — Verification is advisory; installing apt/apk packages
  is not available. See [system packages](docs/system-packages.md).
- **Registry access** — HTTPS by default; plain HTTP requires per-host opt-in. Docker `credsStore`/`credHelpers` are not read

Multi-platform base images select an exact OS/architecture/variant match.
If the platform omits a variant, the image must have one unambiguous match;
otherwise specify it explicitly. Missing targets and inconsistent image
metadata fail before base layers are downloaded. Base-image references may
include a registry port or an immutable `@sha256:...` digest. Platform
selection writes target metadata; it does not cross-compile dependencies.

---

## 🛠️ Development

### Prerequisites

- Python 3.11+ (uses `tomllib` for TOML parsing)
- No other dependencies — pure stdlib

### Install for Development

```bash
git clone https://github.com/spboyer/pycontainer-build.git
cd pycontainer-build
python -m pip install -e .
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
pycontainer build --tag test:latest --verbose

# Test with skopeo + Docker
skopeo copy oci:dist/image docker-daemon:test:latest
docker run test:latest

# Or test with Podman (native OCI support)
podman run oci:dist/image:test
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

### Documentation

- **[Local Development Guide](docs/local-development.md)** — Complete guide for using pycontainer-build locally
- **[Build History](docs/build-history.md)** — Opt-in timing, privacy, retention, and deletion
- **[Azure Developer CLI Integration](docs/azd-integration.md)** — Deploy to Azure with azd
- **[GitHub Actions Guide](docs/github-actions.md)** — Automate builds in CI/CD
- **[Skaffold and Tilt](docs/skaffold-tilt.md)** — Custom-builder contracts for the Kubernetes inner loop
- **[Registry Push Guide](docs/REGISTRY_PUSH.md)** — Pushing images, including [troubleshooting registry errors, rate limits and quotas](docs/REGISTRY_PUSH.md#registry-errors-rate-limits-and-quotas)

### External References

- **OCI Image Spec**: [opencontainers/image-spec](https://github.com/opencontainers/image-spec)
- **Docker Registry v2 API**: [Docker Registry HTTP API V2](https://docs.docker.com/registry/spec/api/)
- **.NET Native Containers**: [Announcing built-in container support](https://devblogs.microsoft.com/dotnet/announcing-builtin-container-support-for-the-dotnet-sdk/)
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

Built with ❤️ by the Microsoft Python & Azure tooling teams
