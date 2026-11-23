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

# Experimental opt-in vulnerability gate (requires Grype 0.119.0)
pycontainer build --tag ghcr.io/user/myapp:v1 --scan --push

# Build for different platform (e.g., amd64 from ARM Mac)
pycontainer build --tag myapp:latest --platform linux/amd64

# Dry-run to preview (verbose mode)
pycontainer build --tag test:latest --dry-run --verbose

# Preview pruning old versions (dry run by default; --execute to delete)
pycontainer registry prune ghcr.io/user/myapp --keep 5
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

### Optional vulnerability gate

`--scan` uses an external **Grype 0.119.0** executable against the complete
selected OCI image, not the Python-only SBOM. It defaults to failing on
high/critical findings and unknown severity, and blocks registry publication
before authentication or uploads. Multi-platform indexes cannot be scanned;
build and scan each platform separately. Core runtime dependencies remain empty.

```bash
pycontainer build --scan --scan-report-format sarif --scan-report scan.sarif
pycontainer build --scan --scan-threshold critical --scan-unknown ignore
```

JSON/SARIF reports include image digest, scanner version, database content
digest/build time, policy and coverage limits, and are retained on findings
failures. Database freshness is enforced even offline. **The adapter is
experimental: a real Grype 0.119.0 acceptance run passed on linux/amd64 with
a pinned database (OS and staged Python findings, whiteouts, JSON and SARIF);
native Windows is rejected because of an upstream `oci-dir:` cache-path
failure, and macOS/arm64 scanner hosts are unvalidated.**
See [Vulnerability scanning](docs/vulnerability-scanning.md) for prerequisites,
offline preparation, error behavior and the opt-in acceptance test.

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
- ✅ **Registry cleanup** — `registry delete` by digest with confirmation and
  dry-run-by-default semver `registry prune` that never deletes digests shared
  with retained tags ([guide](docs/REGISTRY_CLEANUP.md))
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
  (skipped when `cmd` is configured without `entrypoint`: the image then keeps
  the base image's `Entrypoint` and runs `cmd`, as Docker does; an explicit
  `entrypoint = []` / `cmd = []` clear the base image's value)
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
- `--include-deps` — Stage importable dependencies from a lockfile or
  requirements file as target-compatible binary wheels, or copy a verified
  pure-Python venv. Requirements files, including generated lock selections,
  are limited to 1 MiB; staging is limited to 300 seconds, 512 MiB, and 100,000 files;
  supported targets are `linux/amd64`, `linux/arm64` and `linux/arm/v7` with
  CPython (`linux/arm/v7` uses `armv7l` wheel tags; `linux/arm` without the
  `v7` variant is rejected). A venv
  must match the target Python minor version and contain only pure-Python
  packages. Dependencies are placed under `<workdir>/.python`, added to
  `PYTHONPATH`, and their console entrypoints are added to `PATH`; the Python
  interpreter running pycontainer must have `pip` available. Source
  distributions and cross-compilation are not supported.
  Pip runs without the host's `PYTHONPATH`, and pip configuration cannot disable
  target `Requires-Python` checks.
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
  establish the Python ABI. These metadata checks apply to lockfile,
  requirements and virtualenv staging alike.

  Environment markers in the requirements file and in transitive
  `Requires-Dist` metadata are evaluated for the target (`linux`, the target
  machine, CPython and the base image's Python version), not the build host.
  Markers on `platform_release` or `platform_version`, and on
  `python_full_version` or `implementation_version` when the base image does not declare a full
  `PYTHON_VERSION`, cannot be known in advance and fail the build. A virtualenv
  cannot be re-resolved, so its closure is verified for each target instead.
  Requirements-file and pyproject roots and installed `Requires-Dist` metadata
  are checked for missing packages, version mismatches, and requested extras;
  `Requires-Python` and requirements constraint files are also checked. Every
  installed `.dist-info` needs valid `METADATA` with a supported
  `Metadata-Version`. Direct URLs, egg distributions, unknown markers, and
  patch-dependent `Requires-Python` without a full `PYTHON_VERSION` fail.
  Virtualenvs do not record selected extras or groups. An extra or group
  counts as not requested only when it is missing a requirement that the
  venv's creator host provably would have installed. Only facts the venv
  establishes are used: CPython, the venv's Python minor version when known,
  and the OS family from the layout (`lib/pythonX.Y/site-packages` with a
  POSIX `pyvenv.cfg` `home` is non-Windows; MSYS2 CPython is Windows with that
  layout; `Lib/site-packages` is Windows). Markers on any other host value,
  such as `platform_machine` or a specific non-Windows `sys_platform`, are
  unknown rather than assumed, so a requirement like
  `fast; extra == "speed" and platform_machine != "armv7l"` cannot rule the
  extra out. Otherwise, if its target requirements are not satisfied,
  verification fails rather than guessing; install the extra into the venv or
  use a requirements file instead.
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
- `--requirements FILE` — Install this requirements file (`[build]
  requirements-file`). A requirements file that is not fully pinned with
  `==` versions and `--hash` values logs a warning that the dependency layer
  is not reproducible; generate one with `pip-compile --generate-hashes` or use
  a lockfile.
- `--lockfile FILE` — Stage dependencies from `poetry.lock`, `Pipfile.lock`,
  `pdm.lock`, or `uv.lock` (`[build] lockfile`). `--no-lockfile`
  (`use-lockfile = false`) ignores auto-detected lockfiles.
- `--strip-type-stubs` — Opt in (`[build] strip-type-stubs = true`) to drop
  typing-only files from the dependency layer. Off by default; the default
  layer is unchanged. It deletes every staged `*.pyi` file and removes
  stub-only distributions (canonical names `types-*` or `*-stubs`) with their
  `.dist-info`, rewrites each affected `RECORD`, and leaves them out of the
  SBOM. A stub-named distribution is removed only if its `RECORD` lists
  nothing but `.pyi`, `py.typed`, `*-stubs/METADATA.toml` and its own
  metadata; one that also ships runtime code (for example `django-stubs`
  with its mypy plugin) is kept, minus its `.pyi` files. Files also listed by
  a retained distribution are preserved. A malformed `RECORD`, a stub
  distribution without a `RECORD`, or any `RECORD` path outside
  site-packages fails the build before anything is changed. The only outside
  entries accepted are virtualenv launchers in retained distributions,
  including stub-named packages with runtime files
  (`../../Scripts/<name>` or `../../../bin/<name>`, not `.pyi`); they are never
  deleted and stay byte-identical in the rewritten `RECORD`.
  Identical inputs still give
  identical layer digests. Leave it off if a dependency reads `.pyi` files
  at runtime; there are no per-package exceptions.
- **Conda**: name-only detection recognizes `environment.yml`, `environment.yaml`,
  `conda-lock.yml`, and `conda-meta/` directories in the context. With
  `--include-deps`, Conda-only inputs fail explicitly (also in dry-run); a
  selected lockfile or pip requirements file is used with a warning that
  Conda inputs and Conda-only packages are ignored. Without `--include-deps`,
  ordinary Conda projects still build application-only images. Host Conda
  prefixes are never copied, even with explicit includes or
  `--no-default-excludes`; a context inside a Conda prefix requires a source-only
  context instead. See the [export workaround](docs/local-development.md#conda-projects).
  Full Conda staging is deferred to [#187](https://github.com/spboyer/pycontainer-build/issues/187).

**Lockfiles**: With `--include-deps`, the dependency source is chosen in this
order: `--lockfile`, then `--requirements`, then a single lockfile at the
context root, then a venv, then `requirements.txt`. Explicit `--lockfile` and
`--requirements` are mutually exclusive. More than one root lockfile is an
error until you choose one. No locking tool is needed at build time, and none
is added as a dependency:

1. The lock is checked against its manifest (`pyproject.toml` or `Pipfile`):
   Poetry and PDM content hashes, the Pipfile hash, or uv's recorded
   requirements. A stale lock fails; run `poetry lock`, `pipenv lock`,
   `pdm lock`, or `uv lock` and rebuild.
2. Root and recorded transitive markers are evaluated for each target image,
   not the build host, so platform and Python forks select the container's
   dependencies. The base image must expose a full `PYTHON_VERSION=X.Y.Z`.
3. The exact locked versions and their recorded hashes are installed with
   `pip --no-deps --require-hashes` as target wheels and included in the SBOM.
   Pip does not resolve transitive dependencies in this mode: the lock must
   record the complete target graph and a matching wheel hash for each target.

The build context must be writable: temporary `.pycontainer-lockfile-*`
directories hold generated requirements, are excluded from images, and are
removed after staging.

The lockfile's project manifest must resolve inside the build context. Symlinks
to files within the context are supported; dangling or external links are
rejected. Path checks do not protect against concurrent filesystem changes.

`--dry-run` checks lockfile selection and locally verifiable freshness
without pulling the base image. Target markers (including uv marker
freshness), Python compatibility and wheel availability are checked during
the build, not the dry run.

Only the default/main dependencies are staged; dev groups and project extras
are not. Supported formats: Poetry lock-version 2.1+, Pipenv pipfile-spec 6,
PDM lock_version 4 with `inherit_metadata` (Python-only targets), and uv lock
version 1 for a single project. Git, path, URL, and editable packages, uv
workspaces, sources, overrides and constraints, and PDM `find_links` fail with
an explicit error rather than falling back to an unconstrained install.
PDM and uv projects with dynamic dependencies are unsupported. Poetry dynamic
dependencies must be declared in `tool.poetry.dependencies`.
Poetry's Python and dependency ranges support PEP 440 constraints, exact
versions, wildcards, and single caret or tilde constraints; other
Poetry-specific combinations are rejected. Applicable Poetry root and
recorded transitive dependencies, including requested extras, must be present
at compatible locked versions. uv extras without recorded dependencies are
treated as empty, consistent with uv.
Pipenv roots and PDM roots and recorded edges must be present at compatible
locked versions. For all four formats, staged wheel dependency metadata is
checked for target-compatible transitive versions, markers and requested
extras before creating a layer. Each wheel needs valid metadata in its matching
`.dist-info` directory, limited to 1 MiB. This cannot detect dependencies
omitted from the wheels' own metadata.
uv registry URLs must match the current `[[tool.uv.index]]` declarations
or the implicit PyPI index; settings from `uv.toml` and legacy index options
are not supported. Limited-resolution uv locks are accepted only when their
recorded supported markers match the configured environments after supported
normalization and include the target; unverifiable marker equivalence and
unknown target marker values fail explicitly. Normalization supports
`python_version >= 'X.Y'` and `python_version < 'X.Y'` bounds; other
unrecognized equivalences are rejected even if they are logically equal.
Non-PyPI indexes in the lock are passed to pip as `--extra-index-url`;
PyPI itself is left to your pip configuration so mirrors keep working, and
pip can also use an offline wheelhouse (`PIP_NO_INDEX` and `PIP_FIND_LINKS`).
The same pip bootstrap used for ordinary requirements ignores inherited settings
that can override the wheel target or add requirements.
Hashes verify the bytes, not which server supplied them. Index credentials
must use `${VARIABLE}` placeholders and HTTPS, never literal secrets.

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
  `PYTHON_LIBC=glibc-2.N` (N 17-99) or `PYTHON_LIBC=musl-1.N` (N 1-99)
  in their image config,
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

Context traversal never follows directory symlinks or Windows junctions. A
linked include root (including auto-detected `src`, `app` or a virtualenv)
fails the build; nested directory links are skipped. A selected file symlink
must use a relative target that resolves to an existing file inside the
context and that is itself selected (exclusions win); it is stored as a link
with `/` separators. A target may climb with leading `..` components, but not
above the context root, and may not use `..` after descending into a directory.
Absolute, dangling, cyclic, out-of-context and unselected-target file links
fail the build. A context directory that is itself a symlink is still
supported. These checks are not a defense against the context changing during
a build, and they do not cover explicitly named inputs such as requirements or
manifest files.

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
- `--label KEY=VALUE` — Set an image-config label; repeat to add labels
- `--manifest-annotation KEY=VALUE` — Set an image-manifest annotation; repeat to add annotations
- `--index-annotation KEY=VALUE` — Set a local layout-index annotation; repeat to add annotations
- `--release-notes FILE` — Embed a UTF-8 release notes file (at most 8 KiB after
  line endings become LF) in the `io.github.spboyer.pycontainer.release-notes`
  label; `--release-notes-label KEY` selects another key
- `--sbom [FORMAT]` — Generate an SBOM (defaults to `spdx`; accepts `spdx` or `cyclonedx`)
- `--platform PLATFORM` — Target platform (`os/architecture[/variant]`, e.g.,
  `linux/arm64` or `linux/arm/v7`), or a comma-separated subset of
  `linux/amd64,linux/arm64,linux/arm/v7` to build an OCI image index
- `--stop-signal SIGNAL` — Image `StopSignal`, as a canonical Linux name such
  as `SIGTERM`, `SIGINT` or `SIGRTMIN+3`
- `--volume PATH` — Declare an image volume (repeatable; absolute Linux path)
- `--verbose` / `-v` — Detailed build progress
- `--dry-run` — Preview build without creating artifacts
- `--no-reproducible` — Disable deterministic builds

Labels inherit from the base image; their precedence is CLI, configuration
file, detected framework, then base image. Manifest and index annotations are
separate from labels and are not inherited from the base. For a single-image
build, index annotations are in the local `index.json`; registry pushes publish
the image manifest, not that layout index. Metadata is visible to image
consumers and registries: **do not put secrets in labels or annotations**.
See [metadata configuration and integration details](docs/metadata.md).

**Registry Transport** (see [docs/REGISTRY_PUSH.md](docs/REGISTRY_PUSH.md#insecure-http-registries-and-custom-cas)):
- `--insecure-registry HOST[:PORT]` — Use plain HTTP for exactly this registry
  (repeatable; `[build] insecure-registries = [...]`). Each build that uses
  one logs a warning. Credentials are sent over HTTP only to loopback hosts
  (`localhost`, `127.0.0.0/8`, `::1`); any other HTTP registry must be
  anonymous.
- `--registry-ca-file PATH` — Trust an additional PEM CA bundle for registry
  HTTPS (`[build] registry-ca-file`). Certificate verification always stays on;
  there is no option to skip it.
- `--registry-mirror UPSTREAM=MIRROR` — Pull base images through `MIRROR`
  before `UPSTREAM` (repeatable, tried in order; `[build.registry_mirrors]`).
  Pushes never use mirrors, and mirrors never receive upstream credentials.
  See [base-image registry mirrors](docs/REGISTRY_PUSH.md#base-image-registry-mirrors).

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
    manifest_annotations={"org.opencontainers.image.title": "myapp"},
    index_annotations={"org.opencontainers.image.description": "myapp layout"},
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
release-notes-file = "CHANGELOG-1.0.0.md" # optional; relative to the context

[build.labels]
maintainer = "team@example.com"
version = "1.0.0"

[build.manifest_annotations]
"org.opencontainers.image.title" = "myapp"

[build.index_annotations]
"org.opencontainers.image.description" = "myapp layout"

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

#### Stop signal and volumes

`stop_signal` (`--stop-signal`) and `volumes` (`--volume`, repeatable) set the
OCI image config `StopSignal` and `Volumes` fields:

```toml
[build]
stop_signal = "SIGINT"
volumes = ["/data", "/var/cache/app"]
```

- When unset, the base image's `StopSignal` and `Volumes` are inherited
  unchanged. A configured `stop_signal` replaces the base value.
- Configured volumes are merged with the base volumes as a sorted,
  de-duplicated set, so the config digest is deterministic. Base volumes
  cannot be removed. `--volume` values replace (not extend) the file's list.
- Signals must be canonical Linux names (`SIGTERM`, not `term`, `15` or
  `sigterm`). Volume paths must be absolute, normalized Linux paths; `/`,
  relative paths, `.`/`..` segments, backslashes, trailing slashes and control
  characters are rejected.
- Volumes are image metadata only: pycontainer does not create mounts, and the
  container runtime decides how to back them. The stop signal is honored only by
  runtimes that read it, such as `docker stop`.
- These are config fields, not OCI labels; `labels` are unaffected.

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

Multi-platform image indexes (`--platform linux/amd64,linux/arm64,linux/arm/v7`
or TOML `platform = "linux/amd64,linux/arm64"`) accept only `linux/amd64`,
`linux/arm64` and `linux/arm/v7`; other targets, including `linux/arm` without
a variant, are rejected. `linux/arm/v7` selects the exact `arm`/`v7` base
manifest (never v5 or v6) and records `variant: v7` in the image config and
index descriptor. Its dependencies install only as `armv7l` wheels
(`manylinux2014_armv7l` or `musllinux_1_2_armv7l`, the same libc floor as the
other targets). Many packages do not publish `armv7l` wheels; such builds fail
explicitly, and source builds or host binaries are never used as a fallback. Each
platform is built in isolation: it selects its own base manifest, stages its own
wheel-only dependency layer, and shares the byte-identical application layer
(its cache key needs no platform input; dependency layers are never cached).
Virtualenv closure is verified separately against each target base. Explicit
dependencies and detected lockfiles take precedence over virtualenvs; otherwise,
exclude a virtualenv (for example, `--exclude .venv`) to select `requirements.txt`.
The output `index.json` and tag refs point at the image index only after
every platform manifest, config, platform field and DiffID has been verified;
a failed platform leaves earlier tags untouched. A local `oci:` base must
keep the same layout `index.json` for every platform and until publication;
a change fails the build instead of mixing base versions. A registry base tag
is read once: every platform selects from that first verified base index or
manifest snapshot and fetches its platform manifest by descriptor digest, so a
tag that moves mid-build cannot mix snapshots. The snapshot must match a
`sha256:` or `sha512:` `Docker-Content-Digest` header; a mismatch fails. An
unrecognized header algorithm cannot be verified: it is kept as an opaque
value, the first snapshot is still reused for every platform, and the tag is
not treated as verified provenance for libc or Python version inference. If a pinned
platform manifest can no longer be fetched, the build fails before the layout
`index.json` or refs are written (a reused output directory may still contain
an earlier build's index). Push uploads blobs, then
platform manifests and the index by digest, and moves tags last. Registry v2
has no atomic multi-tag update: tags are pushed one at a time, so if a later
tag fails, earlier tags in the same push may already point at the new index.
On a tag push error, the exception lists confirmed pushes, the failed or
unknown-outcome reference, and references not attempted, even with
`--no-progress`. `builder.pushed_refs` contains only confirmed successes from
the current index push; no rollback is attempted.
SBOM generation is not yet supported for index builds. A local single-platform
`docker-archive:` base cannot satisfy an index build. Lockfile, requirements and
transitive dependency markers are evaluated separately for each target, and
unknown target marker values fail the build. Hermetic layout and protocol
checks do not prove that images start. The optional
`scripts/check_runtime_image.py --multi-platform-index` check builds an index,
imports its native target with Skopeo, and starts it; see
[runtime testing](docs/TESTING.md).

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
- **[Registry Cleanup Guide](docs/REGISTRY_CLEANUP.md)** — Deleting images and pruning old versions, shared-digest hazards and registry garbage collection

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
