# Offline image validation

`pycontainer validate image` verifies an existing OCI layout and checks the
selected image configuration without a registry, daemon, cache or file writes.
It does not build an image.

```bash
pycontainer validate image
pycontainer validate image --output dist/image --tag myapp:latest --format json
pycontainer validate image --tag myapp:latest --platform linux/arm64 --fail-on error
pycontainer validate image --config pycontainer.toml --ignore PCB004
pycontainer build --config pycontainer.toml --validate --push
```

`--output` defaults to `dist/image`. A tag selects an exact
`org.opencontainers.image.ref.name` or `io.containerd.image.name` on a
top-level descriptor; a single-entry layout also supports these annotations on
the index. `--tag @sha256:<digest>` selects top-level descriptors by digest.
`refs/tags` files are not used for selection. Multiple named images require
`--tag` even when their descriptors share a digest; multiple matching
descriptors require a unique `--platform`. No platform is chosen implicitly.
When selecting from an image index, nested indexes are not selectable, though
their reachable contents are still verified.

## Rules

| ID | Severity | Check, rationale and remediation |
|---|---|---|
| PCB001 | warning | An absent or empty `User`, `root`, or a numeric zero user (including `root:group` and `0:group`) runs with root privileges. Set a non-root user to limit privileges. |
| PCB002 | unknown | Unverified metadata cannot establish base-image mutability. This ID is reserved; no finding is emitted until verified provenance is available. |
| PCB003 | error | Neither `Entrypoint` nor `Cmd` is set, leaving no default process. Set at least one. |
| PCB004 | warning | Missing or empty `org.opencontainers.image.source` or `org.opencontainers.image.version` labels hinder traceability. Set both labels. |
| PCB005 | error | A non-empty environment variable whose name contains `PASSWORD`, `TOKEN`, or `SECRET` (case-insensitive) persists a likely secret in the image. Supply secrets at runtime instead. |

Findings contain a rule ID, severity, message and remediation. Environment
names and values are not printed. Image labels and annotations are claims,
not proof that a base image is immutable; even a base-digest label leaves
PCB002's provenance status unknown.

## Suppression and thresholds

Pass a configuration file explicitly to either command; it is not discovered
automatically. Both `[build.validate]` and `[tool.pycontainer.validate]` are
supported:

```toml
[build.validate]
ignore = ["PCB001", "PCB004"]
```

Repeat `--ignore RULE` to add CLI suppressions to the TOML list. Unknown IDs,
invalid types and unknown validation settings are errors. PCB002 is a known,
reserved ID, so ignoring it is currently a no-op. Suppressions apply only to
findings, never to layout or integrity errors.

`--fail-on warning` is the default; `--fail-on error` still reports warnings,
but does not fail solely because of them. `build --validate` always fails on
any unsuppressed warning or error, after writing the layout and before a push.
Its `--ignore` option requires `--validate`, and validation cannot be combined
with `--dry-run`. Without `--validate`, the build output is unchanged.
For multi-platform builds, every built platform must pass before pushing the
image index. When combined with `--scan`, both gates must pass.

| Exit code | Meaning |
|---|---|
| 0 | No unsuppressed finding meets the threshold |
| 1 | At least one finding meets the threshold |
| 2 | Usage, configuration, layout or integrity error |

JSON output contains `image` (selected `digest` and `platform`),
`provenance.status`, and a `findings` array. Each finding has `rule_id`,
`severity`, `message` and `remediation`. Usage and validation errors are
written to stderr with exit 2 and no JSON report on stdout.

## Integrity and limits

Before reporting any finding, validation walks every reachable nested index,
manifest, config and layer, including subjects and images not selected for
linting. It checks each descriptor's SHA-256 digest and size, manifest media-type
consistency, and config platform and `rootfs.diff_ids` structure. Missing,
corrupt, malformed or symlinked layout files and blobs are errors.

Metadata is limited to 16 MiB per file and 64 MiB total for referenced
metadata; indexes and images are limited to 1,000 entries and layers
respectively, with 100,000 descriptors, 32 nested descriptor levels and 64 GiB per
layer. Layer blobs are hashed without decompression, including layers with
unrecognized media types. This does **not** check uncompressed DiffID hashes,
layer contents, registry provenance, runtime import or container startup.
A passing result is not proof that the image is runnable or its base is
verified.
