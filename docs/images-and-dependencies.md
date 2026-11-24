# Images and dependencies

## A build is a set of layers

pycontainer selects a base image, stages optional Python dependencies, adds your
application, then writes an OCI manifest and image configuration. Existing base
layers remain intact. Registries and container runtimes consume the resulting
image; no build daemon is involved.

Image builds may pull from registries and download packages. "Daemon-free" does
not mean "offline". Configuration templates and offline validators need no
registry access.

## Choose a target

Supported image targets are `linux/amd64`, `linux/arm64` and `linux/arm/v7`.
Select one or build an index by supplying a comma-separated subset:

```bash
pycontainer build --tag myapp:latest --platform linux/amd64,linux/arm64 --include-deps
```

Each target gets its own base-image selection and dependency layer. The builder
can run on Windows, macOS or Linux, but target selection does not compile native
dependencies. Always verify startup in a native runtime for the target you ship.

## Stage Python dependencies

Use `--include-deps`. Dependency selection follows this order:

1. An explicit `--lockfile`.
2. An explicit `--requirements`.
3. One auto-detected root lockfile.
4. An existing virtualenv.
5. `requirements.txt`.

Explicit lockfile and requirements-file options are mutually exclusive.
Poetry, Pipenv, PDM and uv locks are read without running their respective tools.
Stale locks, unverifiable package sources or incompatible target selections fail
closed. PDM static-URL locks and package/file URL rows are not supported.

Requirements and lockfile installs stage wheels only. A compatible wheel must
exist for the target Python version, architecture and libc. Source distributions
and cross-compilation are not supported. Virtualenv staging accepts pure-Python
packages only and requires the same Python minor version as the target.

See [configuration](../README.md#explicit-configuration) for supported selectors
and [private package indexes](AUTHENTICATION.md#private-package-indexes) for
credential handling.

## Base-image metadata matters

Verified official Python tag-only pulls can derive Python/libc compatibility
from mapped releases or verified base-layer metadata. Custom, local or digest
references require supported explicit metadata. Application `--env` overrides
do not establish facts about the base image.

Use a mapped release such as `python:3.11-slim-bookworm` for a predictable first
build. Refer to [libc mappings](libc-codename-mappings.md) and
[local base images](../README.md#local-base-images) for details.

## Cache what can be verified

Application-layer caching is enabled by default under `~/.pycontainer/cache`.
Use `--cache-dir` to choose a location or `--no-cache` to disable it.
Keys include content and layer-affecting configuration, not just modification
times. Blobs are verified before reuse.

Dependency layers are reused only when a hash-pinned closure and safe inputs can
be established. Unprovable settings produce an explicit cache miss and an
uncached install, not speculative reuse. Caches are portable between checkouts
on the same OS; Windows and Linux caches are not interchangeable.

## Inspect before publishing

[Validate configuration](validate-config.md) before a build and
[validate the OCI layout](validation.md) after it. Generate a Python package SBOM
with `--sbom spdx` or `--sbom cyclonedx`; an SBOM is an inventory, not a
vulnerability scan.

The optional [Grype gate](vulnerability-scanning.md) scans a selected image.
Multi-platform index scanning is rejected: build and scan targets separately.
System-package declarations currently report requirements and base-image
verification; they do not execute a package manager.
