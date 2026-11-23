# Layer analysis and squashing

pycontainer-build normally writes the base image layers unchanged, followed by
the layers it creates itself: an optional dependency layer and the application
layer. Both behaviours below are **opt-in**. Without them, the layout and its
digests are exactly as before.

| Option | CLI | `[build]` key | Effect |
|--------|-----|---------------|--------|
| Layer analysis | `--analyze-layers` | `analyze-layers = true` | Writes `layer-analysis.json` beside the layout and prints a summary |
| Squashing | `--squash` | `squash = true` | Merges the builder-created layers into one layer |

```powershell
pycontainer build --tag myapp:latest --analyze-layers            # report only
pycontainer build --tag myapp:latest --squash --analyze-layers   # squash and report
```

## What is squashed

Only builder-created layers are squashed. **Base image layers are never
squashed, rewritten or decompressed**, so they keep their registry digests and
can still be mounted from the base repository and shared with other images.
With a single builder layer (no dependencies), `--squash` does nothing.

In a multi-platform build each platform's builder layers are squashed
separately. Without squashing, the application layer is often one blob shared
by every platform. With squashing, that sharing goes away whenever the
platforms' dependency layers differ.

## Overlay semantics

The squashed layer is what you would get by applying the source layers in order
on top of the base image, written as one OCI layer:

- **Shadowing:** later entries replace earlier ones. Replaced content is
  removed, and replacing a directory with a file removes everything beneath it.
- **Whiteouts:** `.wh.<name>` markers delete paths in lower layers. A whiteout
  that deletes a path an earlier squashed layer created removes that entry.
  The marker itself stays, because the path may also exist in the base image.
- **Opaque directories:** `.wh..wh..opq` markers are kept. A squashed layer
  hides base-image content when a directory replaces a deleted or non-directory
  path, so pycontainer adds an opaque marker in that case.
- **Metadata:** mode, uid/gid, user and group names, mtime, PAX `atime`/`ctime`
  and extended attributes are kept from the entry that wins. If a directory appears
  in several layers, its metadata comes from the last one.
- **Links:** symlinks are copied verbatim. Hard links must point at a regular
  file that the squashed layers themselves create. A link whose target a later
  layer replaces or deletes cannot be represented exactly, so it is rejected.
- **Ordering and determinism:** entries are written in depth-first order of
  normalized path, so parents always come before children, in PAX format. The
  same source layers always produce byte-identical output and the same digest.

### Explicit rejections

The build fails with `pycontainer.squash.SquashError` (a `ValueError`) and
leaves any existing tags untouched if the builder layers contain something
squashing cannot reproduce exactly:

- `..` traversal paths, entries naming the layer root, or names containing
  control characters or backslashes (leading `/` and `.` components are
  normalized, not rejected)
- device, FIFO, contiguous or sparse entries, or non-regular entries that carry data
- global PAX headers, unknown PAX keys, or `trusted.overlay.*` xattrs
- whiteout markers that are not empty regular files, sit in a parent path
  component, or have reserved names; an opaque marker on the image root
- an opaque marker on a deleted directory that the same layer does not
  explicitly recreate
- a file beneath a path that the result makes a non-directory, or one that
  implicitly recreates a deleted directory
- duplicate entries within a single source layer
- hard links to missing, base-image, non-file or later-replaced targets

pycontainer's own layers do not normally trigger any of these. They exist so
that an invalid or unusual layer fails loudly instead of producing a subtly
different filesystem.

## Recalculated metadata

The squashed layer is written to a temporary file, fsynced, hashed, and then
published under its content address. It is uncompressed, so its DiffID equals
its digest. The manifest layer list and the config `rootfs.diff_ids` are
rebuilt together. Tags and the index (multi-platform) only move after the
whole image has been verified, exactly as in an unsquashed build.

pycontainer never adds history entries for its unsquashed layers. A squashed
build appends one `history` entry (`created_by: pycontainer-build --squash`)
only when the base config's history matches its layers, that is, when the
number of non-`empty_layer` entries equals the base layer count. Otherwise
history is left alone rather than guessing an alignment.

Intermediate, unsquashed builder blobs may remain in `blobs/sha256`. They are
not referenced by any manifest, and there is no layout garbage collection yet.

## Size: measured versus predicted

`layer-analysis.json` reports, per platform:

- every layer's digest, media type, origin (`base` or `builder`), compressed
  size, and uncompressed size (gzip layers are streamed; any other compression
  is reported as `null` with a warning)
- `repeated_content`: identical file contents stored at more than one path in
  the builder layers. **Squashing does not deduplicate these.**
- `squash`: source layer count and bytes, dropped (shadowed or deleted) entries
  and file bytes, `predicted_bytes`, `measured_bytes` and
  `measured_savings_bytes`, or `unsquashable` with the reason squashing would fail
- `shared_blobs` (top level): layer blobs referenced by more than one platform

The prediction is computed from the regenerated headers and padded file sizes
of the entries that will actually be written, plus the end-of-archive marker,
rounded up to the 10 KiB tar record size, without copying file data. It matches
the measured size in tests, including superseded directory headers and
synthesized opaque markers; `measured_bytes` remains authoritative. In practice
pycontainer's own layers rarely
shadow each other, so the measured saving is usually only the record padding
and end-of-archive overhead of the removed layer (a few KiB). Real savings need
an application layer that overwrites or deletes dependency files.

Builder layers are stored uncompressed. Registry and runtime size differences
after compression are not measured or predicted.

## Lost cache reuse

Squashing trades layer count for reuse:

- Without squashing, a code-only change rebuilds only the application layer.
  The dependency layer keeps its digest, so registries skip uploading it and
  runtimes skip pulling it.
- With squashing, any change to application code **or** dependencies produces
  a new squashed layer, which is uploaded and pulled in full every time.
- Across platforms, a squashed layer is shared only when the inputs are
  identical.

Use `--analyze-layers` first. Squash only when the report shows meaningful
`dropped_file_bytes`, or when a layer-count limit outweighs repeated transfer
of the dependencies.

## Real-runtime equivalence check

`scripts/check_squash_equivalence.py` builds the same project with and without
squashing, imports both layouts into Docker with Skopeo, and compares two things:

- the container output
- a recursive listing of `/app` (type, mode, uid/gid, size, SHA-256, link
  target)

It also asserts that squashing removes exactly one layer. It needs a running
Docker daemon, Skopeo, and pip, and it pulls the base image. On a Linux host:

```bash
PYTHONPATH=src timeout 900 python scripts/check_squash_equivalence.py --base-image python:3.11-slim
```

Use `--skopeo-image quay.io/skopeo/stable:latest` to run Skopeo from a
container when no `skopeo` binary is installed. It is not part of the default
test suite or CI.

Historically, Windows staging produced CRLF console-script shebangs that failed
in Linux containers, with or without squashing
([#225](https://github.com/spboyer/pycontainer-build/issues/225)). The fix
from [#228](https://github.com/spboyer/pycontainer-build/pull/228) is included.
Before the final main integration, the Windows-built fixture passed the hosted Linux comparison on commit
`a4fc1d5a41ff80b2b85f7441ab9c5a62c85294f0` in
[run 45203932420](https://github.com/spboyer/pycontainer-build/actions/runs/45203932420);
that result applies to this fixture and commit. The former Linux-builder
workaround is no longer required for the fixed Windows-generated entrypoints.
The verifier still requires Linux runtime tools, so use a Linux host or the
isolated hosted workflow below. Current integration coverage and exact-head
artifact provenance are recorded in
[#195's testing section](https://github.com/spboyer/pycontainer-build/pull/195).

For an isolated cross-host check, dispatch the existing runtime workflow from
the branch to test:

```powershell
gh workflow run runtime-check.yml --ref <branch> -f windows-squash-equivalence=true
```

The opt-in jobs build both layouts on Windows, upload them as a run- and
attempt-specific artifact, and import and compare those exact layouts on a
Linux runner. The verifier checks the recorded commit, run, artifact identity,
OCI digests and imported image identities before comparing runtime output,
filesystem contents and layer counts. The separate evidence artifact records
the artifact ID and digest, runner details and result. A queued, skipped or
failed job is not evidence of equivalence. Rerun all jobs, rather than only
failed jobs, so the build and comparison share the same attempt.

## Related work

- #33 splits dependencies into multiple layers to improve cache reuse; it goes
  the opposite way from squashing. Squashing does not split or re-split
  dependencies. It merges whichever builder layers exist.
- #26 tracks distroless and minimal base images. Squashing never touches base
  layers, so it does not shrink them.
- #141 proposes `pycontainer inspect` for deep inspection of existing images.
  `--analyze-layers` is a build-time report focused on squash decisions.
