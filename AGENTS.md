# Agent and contributor guidance

## Scope and architecture

pycontainer-build is an experimental, Docker-daemon-free OCI image builder.
The core requires **Python 3.11+** (`tomllib`) and has **zero declared runtime
dependencies**. Do not introduce a runtime dependency or an external build
daemon without discussing the trade-off first.

- `src/pycontainer/cli.py`: argparse interface and configuration overrides.
- `devloop.py`: Skaffold (`skaffold-build`) and Tilt (`tilt-build`) custom-builder
  environment contracts; registry push only, single platform, see
  `docs/skaffold-tilt.md` and `examples/skaffold`, `examples/tilt`.
- `config.py`, `config_loader.py`: `BuildConfig` and `[build]` TOML settings.
- `project.py`, `framework.py`, `fs_utils.py`: discovery and file selection.
- `builder.py`: base pull, dependency/app tar layers, layout assembly, push.
- `oci.py`: image configuration, descriptors, manifest and index serialization.
- `cache.py`: optional local application- and dependency-layer cache.
- `dependency_cache.py`: dependency-layer cache key, eligibility and inventory.
- `auth.py`, `registry_client.py`: credential providers and Registry v2 I/O.
- `sbom.py`: Python package inventory and SPDX/CycloneDX generation.
- `scanning.py`: opt-in, fail-closed Grype gate and JSON/SARIF reports.
  Multi-platform index scanning is rejected; build and scan each target separately.
- `system_packages.py`: system package declaration, inference and base-image
  verification (warning only). `system_install.py`: fail-closed groundwork for
  #31; it executes no package manager and has no callers until the #22
  isolated executor exists.
- `security.py`: opt-in Kubernetes/OCI runtime security fragments (not image
  config); see `docs/security-fragments.md`.
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
content, normalized relative paths, workdir, and reproducibility settings, plus
the context-relative hardlink targets `tarfile` emits (never inode numbers or
absolute paths); key version 3 invalidated earlier keys that lacked them.
Index updates are locked and atomically replaced; cache blobs are verified
before reuse. A malformed index raises an explicit `CacheIndexError` with a
remediation message; missing or corrupt blobs are discarded and rebuilt. A
saved `--cache-dir` can be restored to another absolute path and checkout on
the same OS (`tests/test_cache_portability.py`); source modes are part of the
key, so Windows and Linux caches are not interchangeable. The non-gating
`cache-portability-*` CI jobs run `scripts/check_cache_portability.py` to record
cross-OS restore outcomes.

A staged dependency layer is reused only for a provably hash-pinned closure
(lock selection, or fully `==`/`--hash` requirements including nested inputs);
everything unprovable is an explicit `DependencyCacheMiss` with a secret-free
reason. Keys hold target tags, Python, workdir, pip version, sanitized index
identity (URLs with userinfo, credential-bearing paths or query values miss),
the installer's exact target marker environment, locked manifest roots/extras
and verifier backend, allowlisted ambient pip settings for plain requirements,
and `INSTALLER_BACKEND`. Nonempty Python startup overrides (such as
`PYTHONPATH`) are explicit misses even for locked installs; pip and the config
probe ignore `PYTHONPATH`, but the builder process may not. A relative
`PIP_CONFIG_FILE` is a miss for plain requirements. Plain requirements read
pip's local configuration through the installed pip parser before lookup. On a
miss, the installer uses private snapshots of keyed requirements, verified
local find-links files, pip configuration and environment; snapshots are
bounded by the staged size/file limits and removed after use. Unsupported or
unsafe snapshots are explicit misses followed by uncached installs. Do not
include their credentials in cache metadata or image layers. The snapshot
protects cache identity, not all other image layers against concurrent context
edits; see `README.md` for eligibility limits.

HTTP(S), all-protocol and system proxies are explicit misses unless all hosts
are bypassed; a configured pip proxy also makes plain requirements miss.
Eligible locked installs use the key's captured no-proxy environment so a
later proxy change cannot alter the installed source under that key.
Netrc credentials, client certificates and explicit keyring providers are
also misses; eligible installs disable netrc to prevent later authentication
changes from altering the source selected under the key.

A backend change that alters staged output or verification must bump
`INSTALLER_BACKEND`; marker changes must also key the marker environment.
Derive locked manifest roots and extras from the same manifest snapshot used
for package selection; use those full-selection roots for cache keys and
verification, including uncached installs. Recheck manifest containment
before verification and after a cache hit. Preserve scanned requirement paths
on hits for app-layer credential checks. The inventory entry is stored after
the layer and must name its digest; a hit runs no pip install and makes no
network requests.

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

Real scanner checks are separately marked `scanner_integration` and excluded
by default. They require an explicitly selected Grype executable and controlled
database digest; see [vulnerability scanning](docs/vulnerability-scanning.md)
for the opt-in command and coverage limits. Fake-executable tests do not prove
real Grype compatibility or OS/Python whiteout handling. A real Grype 0.119.0
`linux/amd64` acceptance run passed on 2026-11-22; native Windows is rejected
and macOS/arm64 scanner hosts remain unvalidated.

The pull-request workflow runs hermetic tests on Linux and Windows with Python
3.11 and 3.15. Distribution smoke checks build and install wheel/sdist artifacts
in isolated virtual environments. `python benchmarks/run_build_benchmarks.py`
records repeatable synthetic app-only and local full-build trends; its synthetic
base excludes registry pull/push and container startup. Benchmark timing is not
a pass/fail gate. A passing layout assertion is not evidence that an image
starts in a container runtime. Do not silently remove, mock away, or claim
success for unrun integration tests.

A real OCI import/startup check runs on Linux/WSL with Skopeo and a running
Docker daemon:
`PYTHONPATH=src timeout 600 python scripts/check_runtime_image.py --base-image python:3.11-slim --platform linux/amd64 --evidence runtime-evidence/linux-amd64.json`.
It pulls the selected base image, imports the generated layout into Docker, and
starts a container. `--platform` must match the daemon; emulated runs fail
rather than count as evidence. `--evidence` writes a passed or failed JSON
record. To test only dependency importability in the real target base image,
without Skopeo, add `--dependency-layer-only`; that mode is not release evidence.
Add `--multi-platform-index` to build both targets, select the native image
from the index, verify its imported config digest and start it. Index evidence
is separate from single-platform release evidence.
To run an imported quoted shell-form `ENTRYPOINT`, use `--shell-command`
(optionally `--shell '["/bin/bash", "-c"]'`); this mode is not release evidence.
These modes are not part of default pytest.

`.github/workflows/runtime-check.yml` runs the full check natively on
`linux/amd64` (`ubuntu-24.04`) and `linux/arm64` (`ubuntu-24.04-arm`) for
same-repository PRs, pushes to main and release tags. Each target first runs
`tests/test_skopeo_tag_refs.py`, which requires Skopeo to select every
configured tag through its `index.json` descriptor reference.
`scripts/verify_runtime_evidence.py` then requires exactly one passing full
record per target for the current commit, workflow run and attempt; missing,
skipped, failed, emulated, partial, stale or duplicate evidence fails the gate
and is listed separately as not validated. Re-run all jobs, not just failed
jobs, to produce fresh evidence for every target. The release job depends on
this gate, re-verifies evidence for its own attempt and appends the summary to
the release notes. Only targets listed as validated have runtime evidence.
An opt-in `workflow_dispatch` choice `dependency_fixture=lockfile` (or
`--dependency-fixture lockfile` on the script) instead stages the offline
`tests/fixtures/runtime-lock` uv lock. It verifies wheel archive hashes against
the lock before staging and checks imported distribution names, versions and
installed module/METADATA hashes in the container. The platform-tagged wheels
contain only Python; this is not native-binary evidence. Lock evidence records
the fixture path, lock digest and assertion-script digest and is gated against
the committed fixture and the exact commit/run/attempt for both native targets.
Workflow calls and release gates continue to use the default requirements fixture.

The tests workflow also runs the multi-platform index check on both native
targets and uploads separate `index-runtime-evidence-*` artifacts; the release
workflow continues to use only the single-platform release evidence.

Runtime security fragments (`security.py`) are deployment artifacts written
beside the layout, never image config. Verify them with the opt-in Linux
harness `PYTHONPATH=src timeout 900 python scripts/check_security_fragments.py`
(runc or crun; add `--kubernetes` for a cluster). It reports unavailable
AppArmor, SELinux, non-root and Kubernetes checks as SKIP, which is not
evidence of enforcement.

`.github/workflows/execution-backend-probe.yml` records isolated-execution host
prerequisites (user namespaces, subuids, cgroup delegation, binfmt, OCI
runtimes) on GitHub-hosted x64 and arm64 runners. It is observational, not a
gate; see `docs/plans/2026-11-22-execution-backend-host-probe.md`.

`tests/test_devloop.py` runs the Skaffold and Tilt example commands through
fake tool launchers against a loopback registry. Real interoperability is
opt-in: `python scripts/check_devloop_tools.py --registry <https-registry>/ns`
needs `skaffold`, `tilt`, `kubectl`, a current cluster context and an HTTPS
registry the cluster can pull from, plus permission to create and delete a
test namespace. It is not part of CI.

There is no configured root formatter, linter, type checker, or timing gate.
The PR workflow runs the test matrix and records benchmark
trends on main; the release workflow runs hermetic tests on Linux/Python 3.11
and 3.15 plus the runtime gate. The benchmark includes synthetic loopback registry transfers at
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
request timeouts. Registry HTTP(S) requests go through the client's openers:
keep HTTPS verification on (custom CAs only add trust), plain HTTP only for
explicit `insecure_registries` hosts, no HTTPS-to-HTTP redirects, and no
credentials over HTTP to non-loopback hosts.

Report benchmark fixtures, environment, repetitions and cache state. Include
builder construction, traversal, copies and relevant I/O in lifecycle costs.
Distinguish Python-tracked allocations from process RSS and app-only timings
from real end-to-end builds. Do not promise speedups from synthetic results.

## Current limitations and work plan

These are known gaps, **not fixed behavior**:

- PyPI installation of this project returned HTTP 404 during the 2026-11-19
  audit; publication remains pending in #79. The plugins now declare the core
  package dependency, so source installs must place the core and plugin in the
  same host environment until publication.
- The registry client does not read Docker `credsStore`, `credHelpers` or
  `DOCKER_CONFIG`. Skaffold/Tilt local-daemon handoff (`PUSH_IMAGE=false`,
  `custom_build(tag=...)`) and multi-platform dev-loop requests are rejected,
  not emulated.

Dependency staging installs wheel-only requirements for `linux/amd64`,
`linux/arm64` and `linux/arm/v7` CPython targets (1 MiB requirements file, 300-second timeout,
512 MiB staged size and 100,000 files); the builder interpreter must provide
`pip`. Existing virtualenv staging accepts
pure-Python packages only and rejects native binaries; its Python minor version
must match the target base image. Custom images provide
`PYTHON_IMPLEMENTATION=CPython`, `PYTHON_VERSION`, and a supported
`PYTHON_LIBC` value (`glibc-2.N`, N 17-99, or `musl-1.N`, N 1-99); pip
receives tags through the selected libc bound, newest first (`manylinux_2_N`..`manylinux_2_17`
plus `manylinux2014`, or `musllinux_1_N`..`musllinux_1_1`). Official `python`
tag-only registry pulls derive the libc family from the resolved tag (`alpine`
means musl, otherwise glibc) and the version from mapped Debian/Alpine
codename tokens (`docs/libc-codename-mappings.md` pins each row to immutable
snapshot/commit sources; update it with the code table). Codename-less or unmapped
official tag-only pulls instead use effective `libc6`/`musl` package metadata
from digest-checked ordered layers. `base_libc.py` applies whiteouts and fails
closed on active database links, unknown foreign links, absent/ambiguous
records, unsupported versions or architecture mismatches. It recognizes the
foreign root aliases `lib -> usr/lib` and `var -> usr/var` without following
links and checks their redirected databases for conflicts. Historical links
may cause conservative rejection. Limits are 8 MiB metadata, 2 GiB decoded
bytes and 100,000 members per layer, and 300 seconds overall. It never extracts
base files.
Core defaults, auto-detection and the Aspire unversioned-tag invocation remain
supported with this evidence. Missing evidence requires a mapped release or
explicit base config `PYTHON_LIBC`. Inference uses a staging-only config copy.
The system-package reporting scanner is not libc proof. Application `--env`
overrides do not establish base metadata. The shared
`_PIP_PLATFORMS` table keeps `manylinux2014_<machine>` legacy alias values that
`target_wheel_platforms` expands. All digest references, including tag-plus-digest, need supported
`PYTHON_LIBC` metadata: the supplied tag is not verified against the digest.
Local image names/annotations are not provenance either. The shared
`target_wheel_platform` helper defaults to requiring explicit metadata;
`base_image_tag_verified=True` permits tag inference only for tag-only registry
pulls. Local sources must leave it false. Unsupported metadata and metadata
conflicting with a verified tag-only pull fail before pip runs.
`target_python_version` uses the same default-false keyword; digest and
unverified local sources require explicit `PYTHON_IMPLEMENTATION=CPython` and
`PYTHON_VERSION`. Both helpers validate before lockfile, requirements or venv
staging. Staged packages are importable under `<workdir>/.python`; source
distributions and cross-compilation are not supported.

`dependencies.install_requirements` runs pip through `_pip_target.py`, which
forces every `pip._vendor.packaging` marker evaluation (requirement-file roots,
extras, transitive `Requires-Dist`) onto the target environment and fails
closed on `platform_release`, `platform_version`, unknown variables, or
`python_full_version` or `implementation_version` without a full base-image
`PYTHON_VERSION`. It relies on pip's vendored `packaging`; a self-test refuses
to install if that changes. Lock selections resolve recorded transitive markers
before pip runs because locked installs use `--no-deps`.
Virtualenv staging copies the host's resolution, then `_venv_closure.py` (run
with `python -P -c`, reading stdlib-parsed `METADATA` as JSON on stdin) verifies
target-active roots, transitive `Requires-Dist`, versions, extras, constraint
files, and `Requires-Python` using pip's vendored `packaging`; `venv_markers.py`
collects project roots, plausible venv hosts and checks unparsed sources. The
verifier never imports staged packages and fails closed on unknown markers, egg
distributions, missing or unsupported `Metadata-Version`, invalid metadata,
direct URLs, and patch-dependent `Requires-Python` without a full
`PYTHON_VERSION`. Direct URL installation metadata is also rejected because it
can contain credentials. An unrecorded extra or optional group is ruled out
only if every plausible Windows/Linux/macOS CPython host (narrowed to posix by a
`lib/pythonX.Y` layout with a POSIX `pyvenv.cfg` `home`, or to Windows by a
`Lib` layout; `python_version` shared only when
the venv minor is established) would have installed one of its missing
requirements; otherwise unsatisfied target-active requirements fail closed.
Extra markers compare canonical names on any vendored `packaging` version;
`in`/`not in` keep PEP 508 substring semantics (not list membership), never
name candidate extras or rule one out, and fail closed without
`Provides-Extra`. Other `extra` operators and evaluation errors fail closed.
Loose modules without metadata remain unverified; host-only packages remain
staged. Unparsed root sources with host markers and unreadable dynamic
dependencies fail closed. Unmarked declarations in those unparsed sources are
not verified.
Comma-separated `platform` values build an OCI image index for a subset of
`linux/amd64`, `linux/arm64` and `linux/arm/v7`. arm/v7 requires the exact
`v7` variant, uses `armv7l` wheel tags from the shared `_PIP_PLATFORMS` table
in `dependencies.py` (keep #143 libc changes in that table), and never falls
back to source builds; each platform gets its own
base selection and dependency layer, including its own virtualenv closure check.
Tags move only after the whole index verifies. A local `oci:` base must keep the
same layout `index.json` across platforms. Explicit dependency sources and
detected lockfiles take precedence over virtualenvs; otherwise exclude a
virtualenv to select `requirements.txt`. Multi-tag registry pushes are not
atomic: a later tag failure can leave earlier tags moved. Hermetic index tests do
not prove per-architecture runtime startup.

The `_pip_target.py` bootstrap also filters host pip environment variables and config-file keys
to index, authentication, TLS, proxy and timeout settings (see
`dependencies._PIP_ENVIRONMENT_ALLOWED`), with a self-test that fails closed if
pip's config hook changes. Requirements graphs and shipped pip inputs with
literal URL credentials (userinfo, secret-like query values, or recognized or
random-looking URL path tokens; detection is pattern-based) are rejected; pip
errors are redacted. See
`docs/AUTHENTICATION.md#private-package-indexes` and
`tests/test_private_index.py`, which uses a loopback index and synthetic
secrets only.

Lockfile staging (`lockfiles.py`) reads Poetry, Pipenv, PDM and uv locks
without their tools, rejects stale locks against the manifest, evaluates
markers for the target (never the host) and installs the selection through
`install_locked_requirements`: one isolated pip run per recorded package
index (`--isolated --no-deps --require-hashes`, `PIP_*` and pip config
ignored), merged only when files are byte-identical. A format that cannot
establish a package's index fails closed (PDM allows one effective index).
For `linux/arm/v7`, lock selection and staged-wheel closure use the
`platform_machine=armv7l` marker and the same variant-specific wheel tags as
ordinary requirements.
Generated requirements live temporarily inside the writable build context;
`.pycontainer-lockfile-*` staging directories are always excluded from images.
Manifest paths are rechecked before lock reads and installation verification;
in-context links are allowed, while external or dangling links fail. Resolution
does not protect against concurrent filesystem changes.
Poetry checks locked and legacy Python ranges and recorded applicable root,
transitive and extra dependencies. uv limited environments must match the
recorded supported markers and include the target; unverifiable marker
normalization and unknown target values fail closed. Pipenv roots and PDM
roots and recorded edges must match the selection. After installation, all
four formats' wheel metadata is checked for target-compatible transitive
versions, markers and requested extras. Metadata must match its `.dist-info`
directory and is limited to 1 MiB; this cannot recover dependencies missing
from the wheels' own metadata. Plain requirements keep ambient pip configuration.
Precedence: `lockfile`, `requirements_file`, one auto-detected root lock,
venv, `requirements.txt`; explicit `lockfile` and `requirements_file` are
mutually exclusive. Non-registry sources fail; never add an
unconstrained fallback. Real lock fixtures live in `tests/fixtures/lockfiles`
and are validated with `poetry check --lock`, `pipenv verify`,
`pdm lock --check` and `uv lock --check`.

Read [the evidence and design](docs/plans/2026-11-19-performance-maintenance-design.md)
and [the dependency-ordered plan](docs/plans/2026-11-19-maintenance-plan.md).
Roadmap: `spboyer/pycontainer-build#66`. Reuse existing issues before opening
another; do not close duplicate candidates without preserving their scope.
Keep fixes separate from the documentation audit. Commit on a feature branch
using conventional commit messages; never put credentials in code or issues.


<!-- github-knowledge-base-start -->
## Knowledge Base

### Purpose

This repository uses the Knowledge Base at [https://github.com/spboyer/pycontainer-build](https://github.com/spboyer/pycontainer-build) on branch `main`.

### Required behavior

1. Before changing code, read `docs/index.md` from that branch.
2. Use the index to open only the knowledge files relevant to the task.
3. If the index is unavailable, stop and report that the Knowledge Base could not be loaded.

### Source of truth

Generated knowledge tracks the code. When the knowledge and code disagree, trust the code.
<!-- github-knowledge-base-end -->
