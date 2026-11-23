# Image inspection

`pycontainer inspect` explains what is inside a local image and where its size
comes from. It reads OCI image layouts and `docker save` archives directly; it
does not contact a registry, read credentials, or need a container daemon.

```text
pycontainer inspect IMAGE [--platform OS/ARCH[/VARIANT]] [--files | --layer N ...]
                          [--diff FROM TO] [--json]
```

## Selecting an image

| `IMAGE` | Meaning |
|---------|---------|
| `dist/image` | A directory containing an `oci-layout` file, such as pycontainer output |
| `oci:<dir>[:<ref>]` | An OCI layout entry by `org.opencontainers.image.ref.name` |
| `oci:<dir>@sha256:<digest>` | An OCI layout entry by manifest or index digest |
| `docker-archive:<file>[:<repo-tag>]` | An uncompressed `docker save` archive entry |

References follow the same rules as local base images (see the README section
"Local base images"). Registry references such as `python:3.11-slim` are
rejected; copy them locally first, for example with
`skopeo copy docker://python:3.11-slim oci:base:3.11-slim`.

`--platform` selects one manifest from a multi-platform index. Without it, an
index that lists more than one manifest is an error that names the available
platforms; inspection never picks one silently. For a single image,
`--platform` is optional and, when given, must match the image config.

## Output

The default text report shows, for every layer:

- the layer digest and its compressed (stored) size;
- the uncompressed tar size and its verified DiffID;
- the number of tar members and how many paths it added, modified, and deleted;
- the `created_by` history step when the config history maps one-to-one onto
  the layers.

It then summarizes the final (merged) filesystem and lists observations.

- `--files` lists every entry of every layer, marked `A` (added), `M`
  (modified), `D` (deleted by a whiteout), or blank (unchanged re-declaration).
- `--layer N` (1-based, repeatable) lists only the selected layers.
- `--diff FROM TO` compares the filesystem after layer `FROM` with the
  filesystem after layer `TO` (`0` is the empty filesystem, so `--diff 0 N`
  lists everything visible after layer `N`). Paths added and later removed
  between the two states are omitted, and each change carries its before and
  after entry in JSON.
- `--json` writes the same report as a JSON document on stdout.

Errors are printed to stderr with exit code 1 and no report.

## Observations

Observations point at content worth reviewing. Their sizes are uncompressed
tar-header sizes: they are **not** measured savings, because compression,
registry deduplication of shared base layers, and rebuild changes all affect
what a fix would actually save.

| Kind | What it means |
|------|---------------|
| `shadowed-files` | File content in a lower layer that a later layer deletes or replaces; it is invisible in the final filesystem but still stored and transferred |
| `duplicate-content` | Identical content in distinct file inodes at more than one visible path (bytes beyond the first copy) |
| `python-bytecode` | `__pycache__` directories and `*.pyc`/`*.pyo` files |
| `pip-cache` | Files under a `.cache/pip/` directory |
| `apt-cache` | `var/lib/apt/lists/` and `var/cache/apt/` |
| `vcs` | Files inside `.git`, `.hg`, or `.svn` directories |
| `largest-files` | The ten largest visible files |

## Layer semantics and safety

Layers are applied in order with OCI semantics. A `.wh.<name>` whiteout deletes
`<name>` and an `.wh..wh..opq` opaque marker hides a directory's existing
contents; both affect lower layers only, never entries added by the same layer.
Replacing a directory with a file or symlink deletes the directory's subtree,
and each removed path counts as deleted. Hardlinks must point at an existing
regular-file or hardlink path; forward links, symlinks, and special-file targets
are rejected. A hardlink shares its target's inode, so removing one name keeps
the content while another link remains, and filesystem byte totals count the
shared inode once.

Inspection fails instead of returning a partial or misleading report when a
layer:

- does not match its config DiffID (the hash of the uncompressed tar);
- declares a media type that does not match its content, is xz or bzip2
  compressed, or uses an unknown media type (`tar+zstd` needs Python 3.14+);
- contains an absolute path, a path with `..`, a NUL byte, or more than 4,096
  bytes;
- places an entry beneath a symlink, contains sparse or unknown entry types,
  an invalid whiteout, or non-zero data after the end of the tar archive;
- is truncated or corrupt.

Text output escapes control, bidirectional-formatting, and undecodable
characters in paths, so file names cannot rewrite the terminal.

## Resource bounds

Layers are streamed: the tar is decompressed, hashed, and read member by member
without extracting files or buffering a layer in memory. The in-memory model
keeps one record per path. Limits include 100,000 members per layer, 1,000,000
members and filesystem nodes per image, 16 MiB of member paths per layer, and
64 MiB each for retained filesystem paths, diff paths, and reported file paths.
Extended tar headers are limited to 100 consecutive headers and 16 MiB total
per layer. Each layer is also limited to 64 GiB uncompressed. The shared
local-image reader verifies each blob into a temporary directory before it is
read: OCI layout layers are copied and removed one at a time, while `docker
save` layers are all staged up front, so inspecting an archive needs free
temporary disk space roughly equal to its layer size.

## What inspection does not do

Inspection reads metadata and file content hashes; it does not run the image.
A clean report is not evidence that the image starts. To check that, use the
opt-in runtime check on Linux/WSL with Skopeo and Docker:

```bash
PYTHONPATH=src timeout 600 python scripts/check_runtime_image.py --base-image python:3.11-slim
```
