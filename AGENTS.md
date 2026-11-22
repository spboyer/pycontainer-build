# Agent and contributor guidance

## Scope and architecture

pycontainer-build is an experimental, Docker-daemon-free OCI image builder.
The core requires **Python 3.11+** (`tomllib`) and has **zero declared runtime
dependencies**. Do not introduce a runtime dependency or an external build
daemon without discussing the trade-off first.

- `src/pycontainer/cli.py`: argparse interface and configuration overrides.
- `config.py`, `config_loader.py`: `BuildConfig` and `[build]` TOML settings.
- `effective_config.py`: pure resolver and semantic checks shared by `build`
  and the offline `validate config` command.
- `project.py`, `framework.py`, `fs_utils.py`: discovery and file selection.
- `builder.py`: base pull, dependency/app tar layers, layout assembly, push.
- `oci.py`: image configuration, descriptors, manifest and index serialization.
- `cache.py`: optional local application-layer cache.
- `auth.py`, `registry_client.py`: credential providers and Registry v2 I/O.
- `sbom.py`: Python package inventory and SPDX/CycloneDX generation.
- `plugins/`: separate Poetry, Hatch and VS Code integrations.
- External consumer: `spboyer/PyContainer.Aspire.Hosting` runs the CLI with
  the flags and `[build]` keys pinned in `tests/test_aspire_contract.py`.
  Change that contract only together with the adapter.
- `tests/`: actual pytest suite; `docs/TESTING.md` describes an aspirational
  strategy, including directories and tooling that do not yet exist.

The layout is `dist/image/oci-layout`, `index.json`, `blobs/sha256/<digest>`,
and `refs/tags/<tag>`, not a top-level `manifest.json`.
`BuildConfig` accepts `context_dir`, not `context_path`.
The working module invocation is `python -m pycontainer.cli`; the package
currently has no `__main__.py`.

## Cache behavior

Caching is enabled by default under `~/.pycontainer/cache`; `--cache-dir`
selects another location and `--no-cache` disables it. Cache keys include file
content, normalized relative paths, workdir, and reproducibility settings.
Index updates are locked and atomically replaced; cache blobs are verified
before reuse. A malformed index raises an explicit `CacheIndexError`; missing
or corrupt blobs are discarded and rebuilt.

## Setup and verified checks

PowerShell examples, run from the repository root:

```powershell
uv sync --locked --group test --group build
uv run --locked --group test --group build python -m pycontainer.cli --help
uv run --locked --group test --group build python -m pytest tests -q
uv run --locked --group test --group build python scripts/smoke_distributions.py
```

For plugin contract tests, sync the relevant locked group and install the
plugin source without resolving dependencies:

```powershell
uv sync --locked --group test --group build --group poetry-plugin
uv pip install --python .venv\Scripts\python.exe --no-deps --no-build-isolation -e plugins/poetry-pycontainer
uv run --locked --group test --group build --group poetry-plugin python -m pytest plugins/poetry-pycontainer/tests -q

uv sync --locked --group test --group build --group hatch-plugin
uv pip install --python .venv\Scripts\python.exe --no-deps --no-build-isolation -e plugins/hatch-pycontainer
uv run --locked --group test --group build --group hatch-plugin python -m pytest plugins/hatch-pycontainer/tests -q

uv run --locked --group test --group build python -m pycontainer.cli build --dry-run --tag audit:local --context examples\fastapi-app
```

Registry-pulling tests are explicitly marked `registry_integration` and are
excluded by default. Run them deliberately with
`uv run --locked --group test python -m pytest tests -q -o addopts= -m registry_integration`;
they make real base-image pulls and must have a bounded timeout. The lockfile
pins development tools only; the core package continues to declare no runtime
dependencies. The excluded tests are:

- `tests/test_base_image.py::test_dependency_layer_creation`
- `tests/test_build_push.py::test_build_and_push_workflow`
- `tests/test_oci_layout.py::test_oci_layout_structure`
- `tests/test_oci_layout.py::test_tag_extraction`

The pull-request workflow runs hermetic tests on Linux and Windows with Python
3.11 and 3.15. Distribution smoke checks build and install wheel/sdist artifacts
in isolated virtual environments. `python benchmarks/run_build_benchmarks.py`
records repeatable synthetic app-only and local full-build trends; its synthetic
base excludes registry pull/push and container startup. Benchmark timing is not
a pass/fail gate. A passing layout assertion is not evidence that an image
starts in a container runtime. Do not silently remove, mock away, or claim
success for unrun integration tests.

An opt-in real OCI import/startup check is available on Linux/WSL with Skopeo
and a running Docker daemon:
`PYTHONPATH=src timeout 600 python scripts/check_runtime_image.py --base-image python:3.11-slim`.
It pulls the selected base image, imports the generated layout into Docker, and
starts a container. To test only dependency importability in the real target
base image, without Skopeo, add `--dependency-layer-only`. These checks are not
part of the default test suite or CI.

There is no configured root formatter, linter, type checker, or timing gate.
The PR workflow runs the test matrix and records benchmark
trends on main; the release workflow runs hermetic tests on Linux/Python 3.11
and 3.15. The benchmark includes synthetic loopback registry transfers at
multiple blob sizes with per-sample peak RSS and Python allocation metrics; it
does not measure external registry throughput. Do not describe suggested tools
as existing required checks.

## Change discipline

Keep changes surgical and follow the surrounding module's style. Existing core
modules are compact; do not combine fixes with wholesale reformatting.
Use focused pytest regressions that fail before the fix. Patch network/auth
boundaries in unit tests and use synthetic credentials, not local login state.
Surface invalid inputs and operational errors explicitly.

Use host `Path` operations for local files and POSIX paths for Linux OCI
entries. Target metadata selection does not compile native dependencies for
another platform. Never silently copy incompatible host binaries into an image.

For image changes, check ordered manifest layers against `rootfs.diff_ids`,
digest/size/media-type consistency, config inheritance and target platform.
DiffIDs hash **uncompressed** layer tar content, not compressed base blobs.
Use an opt-in real runtime import/startup check for runnable-image claims.

For caching or transfer changes, verify equal-size/equal-time source edits,
layer-affecting configuration, repeated builds, corruption, interruption and
concurrent writers. Hash and transfer large blobs incrementally, publish
verified files atomically, and do not forward registry authorization to
redirected storage hosts. Registry blob downloads verify descriptor digest and
size before publication; only safe reads use bounded retries and explicit
request timeouts.

Report benchmark fixtures, environment, repetitions and cache state. Include
builder construction, traversal, copies and relevant I/O in lifecycle costs.
Distinguish Python-tracked allocations from process RSS and app-only timings
from real end-to-end builds. Do not promise speedups from synthetic results.

## Current limitations and work plan

These are known gaps, **not fixed behavior**:

- CLI defaults can overwrite TOML settings.
- PyPI installation of this project returned HTTP 404 during the 2026-11-19
  audit; publication remains pending in #79. The plugins now declare the core
  package dependency, so source installs must place the core and plugin in the
  same host environment until publication.

Dependency staging installs wheel-only requirements for `linux/amd64` and
`linux/arm64` CPython targets (1 MiB requirements file, 300-second timeout,
512 MiB staged size and 100,000 files); the builder interpreter must provide
`pip`. Existing virtualenv staging accepts
pure-Python packages only and rejects native binaries; its Python minor version
must match the target base image. Custom images provide
`PYTHON_IMPLEMENTATION=CPython`, `PYTHON_VERSION`, and a supported
`PYTHON_LIBC` value. Staged packages are importable under `<workdir>/.python`;
source distributions, lockfiles and cross-compilation are not supported.

Read [the evidence and design](docs/plans/2026-11-19-performance-maintenance-design.md)
and [the dependency-ordered plan](docs/plans/2026-11-19-maintenance-plan.md).
Roadmap: `spboyer/pycontainer-build#66`. Reuse existing issues before opening
another; do not close duplicate candidates without preserving their scope.
Keep fixes separate from the documentation audit. Commit on a feature branch
using conventional commit messages; never put credentials in code or issues.
