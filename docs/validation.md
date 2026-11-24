# Offline image validation

`pycontainer validate image` verifies an existing OCI layout and checks the
selected image configuration without a registry, daemon, cache or file writes.
It does not build an image.

```bash
pycontainer validate image
pycontainer validate image --output dist/image --tag myapp:latest --format json
pycontainer validate image --tag myapp:latest --platform linux/arm64 --fail-on error
pycontainer validate image --config pycontainer.toml --ignore PCB004
pycontainer validate image --max-image-size-mb 500 --max-layer-size-mb 200
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
| PCB006 | warning | The stored image size exceeds `max_image_size_mb`. Reduce the base image or layer contents, or raise the threshold. Evaluated only when the threshold is set. |
| PCB007 | warning | The largest stored layer exceeds `max_layer_size_mb`. Slim that layer or choose a smaller base image, or raise the threshold. Evaluated only when the threshold is set. |

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
max_image_size_mb = 500     # optional; total stored image size
max_layer_size_mb = 200     # optional; largest single stored layer
```

Repeat `--ignore RULE` to add CLI suppressions to the TOML list. Unknown IDs,
invalid types and unknown validation settings are errors. PCB002 is a known,
reserved ID, so ignoring it is currently a no-op. Suppressions apply only to
findings, never to layout or integrity errors. Configuration, ignore and layout
diagnostics redact declared build-argument values, as `validate config` does;
`validate image` redacts declared defaults because it accepts no `--build-arg`.
Both commands also redact declared defaults from key-normalization collisions
(for example `my-key` and `my_key`), which are reported before other checks.

`--max-image-size-mb` and `--max-layer-size-mb` (on `validate image`, and on
`build` together with `--validate`) override the matching TOML key only when
supplied; otherwise the TOML value, if any, applies. Thresholds are positive,
finite decimal numbers of MiB (1 MiB = 1,048,576 bytes), compared exactly
without binary rounding, so `0.1` means 104,857.6 bytes. A size equal to the
threshold passes; only a larger size warns. Zero, negative, non-finite,
boolean or non-numeric values (including TOML strings) are configuration
errors with exit 2. Without a threshold, no size finding is produced. The TOML
keys use underscores only.

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
`provenance.status`, a `findings` array and a `size` object. Each finding has
`rule_id`, `severity`, `message` and `remediation`. Usage and validation errors are
written to stderr with exit 2 and no JSON report on stdout.

## Stored size report

Every successful validation reports sizes for the selected image, whether or
not a threshold is set. They come from descriptors whose digests and sizes
were already verified against the blobs on disk; a descriptor that disagrees
with its blob is an integrity error (exit 2), never a size finding, and
ignoring PCB006 or PCB007 does not suppress it.

- `image_bytes`: the config descriptor size plus the sum of every layer
  descriptor in the manifest. A layer listed twice counts twice; the manifest
  itself is excluded.
- `largest_layer`: the largest layer descriptor (`digest`, `bytes`,
  `media_type`), or `null` for an image without layers.
- `layers`: each layer's `digest`, `bytes`, `media_type`, `compression`
  (`gzip`, `zstd`, `none` for plain tar, or `unknown`, derived from the media
  type) and `role`.
- `contributions`: base versus application/dependency bytes, plus
  `config_bytes` separately.
- `thresholds` and `note`.

These are **stored blob sizes**. Base layers pulled from a registry usually
keep their compressed media types, so they count compressed bytes, while
pycontainer writes dependency and application layers as uncompressed tar, so
they count their full size. Network transfer, registry deduplication and
cache reuse may differ from these numbers.

An OCI layout does not record which layers came from the base image, and media
types or labels are not proof. `validate image` therefore reports the
contribution `status` as `unknown`, each layer's `role` as `unknown`, and the
base and application byte counts as `null`. `build --validate` knows how many
base layers it placed first in the manifest it just wrote; after confirming
the layout still holds that exact manifest digest, it reports `status:
determined`, `base` and `application` roles, and both byte totals. Text output
prints the same summary and note. Multi-platform builds report and check sizes
separately for each platform's manifest.

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
